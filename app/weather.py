"""天气与地理：open-meteo 天气、nominatim/高德逆地理、open-meteo 地理编码。

全部尽力而为：任何异常都吞掉返回 None，绝不阻塞主流程。
- 超时区分连接（2s）与读取（4s）；`enrich` 另有总预算，超预算就带着已有结果返回
- 外发天气坐标前按约 1km 粗化（_coarsen），逆地理需要街区精度，保持原值
- 进程内复用 httpx 连接池 + 带 TTL 的简易缓存；nominatim 作为低流量兜底，限速 1 次/秒
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime
from typing import NamedTuple

import httpx

from . import db, logx

# read 4 秒 / connect 2 秒：httpx 的单个数字是"每阶段各 N 秒"，不是整次请求上限。
TIMEOUT = httpx.Timeout(4.0, connect=2.0)
ENRICH_BUDGET = 8.0          # enrich 一次调用的总预算（秒），超预算不再发起新的外部请求

# Nominatim（OSM）使用政策要求 UA 可标识应用并留下联系方式。
# 「+」后面的内容请按需替换成你自己的仓库/邮箱；留空则不加，不影响功能。
_UA_CONTACT = ""
UA = {"User-Agent": "growth-vault/1.0" + (f" ({_UA_CONTACT})" if _UA_CONTACT else "")}

# WMO weather_code -> 中文
WMO_TEXT = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "雾",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨", 56: "冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪",
    80: "阵雨", 81: "阵雨", 82: "阵雨", 85: "阵雪", 86: "阵雪",
    95: "雷雨", 96: "雷雨伴冰雹", 99: "雷雨伴冰雹",
}

_CLIENT: httpx.Client | None = None
_CLIENT_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()
_GEO_CACHE: dict[str, tuple[float, float, str]] = {}      # 城市名 -> (lat, lon, 显示名)
_REVERSE_CACHE: dict[tuple, str] = {}                     # (坐标) -> 地名
_WEATHER_CACHE: dict[tuple, dict] = {}                    # (坐标, 日期) -> 天气
_GEO_TTL = 3600.0          # 城市解析 1 小时
_REVERSE_TTL = 24 * 3600.0  # 坐标反查 1 天（地名基本不变，也少打 nominatim）
_WEATHER_TTL = 12 * 3600.0  # 历史日期天气半天（当天数据不会再变）
_NOW_TTL = 600.0            # 当前观测值 10 分钟（连续保存不必每次重打接口）
_RATE_LOCK = threading.Lock()
_LAST_NOMINATIM = 0.0
_NOMINATIM_INTERVAL = 1.05    # 政策上限 1 请求/秒，留一点余量


class Enriched(NamedTuple):
    """enrich 的具名结果。

    仍是 4 元 tuple（NamedTuple 是 tuple 子类），老的 `a, b, c, d = enrich(...)`
    解包写法完全兼容；新代码建议按字段名取值，避免位置错位。
    latitude/longitude 为 None 时表示"本次没有坐标可用"，调用方不应据此清空已存坐标。
    """
    location_name: str
    latitude: float | None
    longitude: float | None
    weather: dict | None


def _coarsen(v: float, step: float = 0.01) -> float:
    """天气只需要城市级精度（约 1km）：粗化后再外发，避免把住址级坐标交出去。"""
    try:
        return round(float(v) / step) * step
    except (TypeError, ValueError):
        return v


def _client() -> httpx.Client:
    """进程内复用同一个 Client（连接池/TLS 会话复用）；httpx.Client 本身线程安全。"""
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                _CLIENT = httpx.Client(timeout=TIMEOUT, headers=UA)
    return _CLIENT


def _cache_get(cache: dict, key):
    """只缓存成功结果；返回 None 表示未命中或已过期。"""
    if key is None:
        return None
    with _CACHE_LOCK:
        hit = cache.get(key)
    if hit is None:
        return None
    value, expires = hit
    if expires > time.monotonic():
        return value
    with _CACHE_LOCK:
        cache.pop(key, None)
    return None


def _cache_put(cache: dict, key, value, ttl: float) -> None:
    if value is None or key is None:
        return
    with _CACHE_LOCK:
        cache[key] = (value, time.monotonic() + ttl)
        if len(cache) > 256:      # 简易上界，避免长期运行无限增长
            for k in list(cache)[:64]:
                cache.pop(k, None)


def _cache_key(lat, lon, day: str = "") -> tuple | None:
    """缓存键：坐标粗化到 4 位小数（同一天同一片区域取一次就够）。"""
    try:
        return (round(_coarsen(lat), 4), round(_coarsen(lon), 4), day)
    except (TypeError, ValueError):
        return None


def _code_text(code) -> str:
    try:
        return WMO_TEXT.get(int(code), "未知")
    except (TypeError, ValueError):
        return "未知"


def _today() -> date:
    return datetime.now().astimezone().date()


def fetch_weather(lat: float, lon: float, day: str) -> dict | None:
    """取某天的天气。今天用 forecast 当前值，过去日期用 archive 日均温，未来日期没有当天天气。"""
    if lat is None or lon is None:
        return None
    try:
        day_d = date.fromisoformat(day)
    except (TypeError, ValueError):
        day_d = _today()
    today = _today()

    if day_d > today:
        # 未来日期没有"当天天气"：不能用此刻的观测值冒充那一天的天气落库。
        return None
    if day_d < today:
        # 过去日期：同一天同一片区域的天气不会再变，按天缓存，避免 scheduler 每天重复算。
        key = _cache_key(lat, lon, day_d.isoformat())
        cached = _cache_get(_WEATHER_CACHE, key)
        if cached is not None:
            return cached
        w = _archive_day(lat, lon, day_d.isoformat())
        if w is None and (today - day_d).days <= 7:
            # archive 有近 5 天延迟，最近几天拿不到就用当前天气兜底
            w = _forecast_current(lat, lon)
        _cache_put(_WEATHER_CACHE, key, w, _WEATHER_TTL)
        return w
    return _forecast_current(lat, lon)


def _forecast_current(lat: float, lon: float) -> dict | None:
    # 当前观测值 10 分钟内视为有效：连续保存/预览不必每次重打接口。
    key = _cache_key(lat, lon, "current")
    cached = _cache_get(_WEATHER_CACHE, key)
    if cached is not None:
        return cached
    try:
        r = _client().get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": _coarsen(lat),
                "longitude": _coarsen(lon),
                "current": "temperature_2m,relative_humidity_2m,weather_code",
                "timezone": "auto",
            },
        )
        r.raise_for_status()
        cur = r.json().get("current") or {}
        if "temperature_2m" not in cur:
            return None
        out = {
            "text": _code_text(cur.get("weather_code")),
            "temperature_c": cur.get("temperature_2m"),
            "humidity": cur.get("relative_humidity_2m"),
            "provider": "open-meteo",
        }
        _cache_put(_WEATHER_CACHE, key, out, _NOW_TTL)
        return out
    except Exception:
        return None


def _archive_day(lat: float, lon: float, day: str) -> dict | None:
    try:
        r = _client().get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": _coarsen(lat),
                "longitude": _coarsen(lon),
                "start_date": day,
                "end_date": day,
                "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                "timezone": "auto",
            },
        )
        r.raise_for_status()
        daily = r.json().get("daily") or {}
        codes = daily.get("weather_code") or []
        tmax = (daily.get("temperature_2m_max") or [None])[0]
        tmin = (daily.get("temperature_2m_min") or [None])[0]
        if not codes or codes[0] is None:
            return None
        temp = None
        if tmax is not None and tmin is not None:
            temp = round((tmax + tmin) / 2, 1)
        elif tmax is not None:
            temp = tmax
        return {
            "text": _code_text(codes[0]),
            "temperature_c": temp,
            "humidity": None,   # 存档接口没有当日相对湿度：None 表示"无此字段"，不是 0
            "provider": "open-meteo",
        }
    except Exception:
        return None


def _nominatim_get(path: str, params: dict) -> httpx.Response:
    """按 Nominatim 使用政策限速（≥1 秒/次）后请求。"""
    global _LAST_NOMINATIM
    with _RATE_LOCK:
        wait = _NOMINATIM_INTERVAL - (time.monotonic() - _LAST_NOMINATIM)
        if wait > 0:
            time.sleep(wait)
        _LAST_NOMINATIM = time.monotonic()
    return _client().get(f"https://nominatim.openstreetmap.org{path}", params=params)


def reverse_geocode(lat: float, lon: float, amap_key: str = "") -> str | None:
    """坐标 -> 地名。配置了 amap_key 用高德，否则用 nominatim（低流量兜底）。"""
    if lat is None or lon is None:
        return None
    cache_key = _cache_key(lat, lon)
    cached = _cache_get(_REVERSE_CACHE, cache_key)
    if cached is not None:
        return cached
    if amap_key:
        try:
            r = _client().get(
                "https://restapi.amap.com/v3/geocode/regeo",
                params={"key": amap_key, "location": f"{lon},{lat}"},   # 高德约定：经度在前
            )
            r.raise_for_status()
            data = r.json()
            if data.get("status") == "1":
                addr = (data.get("regeocode") or {}).get("formatted_address")
                if addr:
                    _cache_put(_REVERSE_CACHE, cache_key, str(addr), _REVERSE_TTL)
                    return str(addr)
                logx.log("高德逆地理无地址", "返回空 formatted_address（改用 nominatim）")
            else:
                # 高德 key 失效/配额用尽是 HTTP 200 + status=0，raise_for_status 抓不到，必须自己出声
                logx.log("高德逆地理失败", f"{data.get('info')}（改用 nominatim）")
        except Exception as e:
            logx.log("高德逆地理异常", f"{type(e).__name__}: {str(e)[:80]}（改用 nominatim）")
    try:
        r = _nominatim_get("/reverse", {
            "lat": lat, "lon": lon, "format": "json", "accept-language": "zh-CN",
        })
        r.raise_for_status()
        name = (r.json()).get("display_name")
        if name:
            # display_name 往往很长，截取前三段更有可读性
            parts = [p.strip() for p in str(name).split(",") if p.strip()]
            if parts:
                out = "，".join(parts[:3])
                _cache_put(_REVERSE_CACHE, cache_key, out, _REVERSE_TTL)
                return out
    except Exception:
        return None
    return None


def geocode_city(name: str) -> tuple[float, float, str] | None:
    """城市名 -> (lat, lon, 显示名)。结果按城市名缓存，重名时优先国别匹配的结果。"""
    if not name:
        return None
    cached = _cache_get(_GEO_CACHE, name)
    if cached is not None:
        return cached
    try:
        r = _client().get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": name, "count": 5, "language": "zh"},
        )
        r.raise_for_status()
        results = r.json().get("results") or []
        if not results:
            return None
        # 地名重名很常见（长安/新城/朝阳…），取多条结果并按国别选优，别盲信第一条。
        wanted = (db.get_setting("country_code") or "CN").strip().upper()
        item = next((x for x in results
                     if str(x.get("country_code") or "").strip().upper() == wanted), results[0])
        display = item.get("name") or name
        admin = item.get("admin1")
        if admin and admin != display:
            display = f"{admin}{display}"
        out = (float(item["latitude"]), float(item["longitude"]), str(display))
        _cache_put(_GEO_CACHE, name, out, _GEO_TTL)
        return out
    except Exception:
        return None


def enrich(lat, lon, location_name: str, day: str) -> Enriched:
    """创建/更新记录时的尽力补全：地名 + 天气。

    返回 Enriched(location_name, latitude, longitude, weather|None)（仍是 4 元 tuple）。
    - 解析失败时**原样返回入参坐标**：None 表示"本次没有坐标可用"，不是"请清空坐标"，
      调用方（main.py）无需配套改动也不会误删已存经纬度。
    - 设置 weather_city_only=true 时忽略入参坐标（代理/VPN 会把 IP 定位带歪），只用默认城市。
    - 总预算 ENRICH_BUDGET：超预算就带着已有结果返回，不把用户请求拖到几十秒。
    """
    deadline = time.monotonic() + ENRICH_BUDGET
    amap_key = db.get_setting("amap_key")
    city_only = db.get_setting("weather_city_only") == "true"
    use_coords = True
    if city_only:
        city = db.get_setting("default_city")
        geo = geocode_city(city) if city and time.monotonic() < deadline else None
        if geo:
            lat, lon, city_name = geo
            location_name = location_name or city_name
        else:
            # 城市没解析出来（离线/接口异常）：本次不用坐标（宁可不填，也不用可能被代理
            # 带歪的坐标）。但返回值必须原样带回入参坐标 —— None 的语义是"未知"，
            # 调用方会把它当"清空坐标"落库（A0-10）。
            use_coords = False
    elif (lat is None or lon is None) and not location_name:
        city = db.get_setting("default_city")
        if city and time.monotonic() < deadline:
            geo = geocode_city(city)
            if geo:
                lat, lon, city_name = geo
                location_name = location_name or city_name
    if use_coords and lat is not None and lon is not None:
        if not location_name and time.monotonic() < deadline:
            location_name = reverse_geocode(lat, lon, amap_key) or ""
        weather = fetch_weather(lat, lon, day) if time.monotonic() < deadline else None
    else:
        weather = None
    return Enriched(location_name, lat, lon, weather)
