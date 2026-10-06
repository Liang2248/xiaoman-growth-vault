"""天气与地理：open-meteo 天气、nominatim/高德逆地理、open-meteo 地理编码。

全部尽力而为：5 秒超时，任何异常都吞掉返回 None，绝不阻塞主流程。
"""
from __future__ import annotations

from datetime import date, datetime

import httpx

from . import db

TIMEOUT = 5.0
UA = {"User-Agent": "growth-vault/1.0"}

# WMO weather_code -> 中文
WMO_TEXT = {
    0: "晴", 1: "多云", 2: "多云", 3: "阴",
    45: "雾", 48: "雾",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨", 56: "冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪",
    80: "阵雨", 81: "阵雨", 82: "阵雨", 85: "阵雪", 86: "阵雪",
    95: "雷雨", 96: "雷雨伴冰雹", 99: "雷雨伴冰雹",
}


def _code_text(code) -> str:
    try:
        return WMO_TEXT.get(int(code), "未知")
    except (TypeError, ValueError):
        return "未知"


def fetch_weather(lat: float, lon: float, day: str) -> dict | None:
    """取某天的天气。今日/未来用 forecast 当前值，过去日期用 archive 日均温。"""
    try:
        day_d = date.fromisoformat(day)
    except ValueError:
        day_d = datetime.now().astimezone().date()
    today = datetime.now().astimezone().date()

    if day_d >= today:
        return _forecast_current(lat, lon)
    w = _archive_day(lat, lon, day_d.isoformat())
    if w is None and (today - day_d).days <= 7:
        # archive 有近 5 天延迟，最近几天拿不到就用当前天气兜底
        w = _forecast_current(lat, lon)
    return w


def _forecast_current(lat: float, lon: float) -> dict | None:
    try:
        with httpx.Client(timeout=TIMEOUT, headers=UA) as c:
            r = c.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,weather_code",
                    "timezone": "auto",
                },
            )
            r.raise_for_status()
            cur = r.json().get("current") or {}
            if "temperature_2m" not in cur:
                return None
            return {
                "text": _code_text(cur.get("weather_code")),
                "temperature_c": cur.get("temperature_2m"),
                "humidity": cur.get("relative_humidity_2m"),
                "provider": "open-meteo",
            }
    except Exception:
        return None


def _archive_day(lat: float, lon: float, day: str) -> dict | None:
    try:
        with httpx.Client(timeout=TIMEOUT, headers=UA) as c:
            r = c.get(
                "https://archive-api.open-meteo.com/v1/archive",
                params={
                    "latitude": lat,
                    "longitude": lon,
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
                "humidity": None,
                "provider": "open-meteo",
            }
    except Exception:
        return None


def reverse_geocode(lat: float, lon: float, amap_key: str = "") -> str | None:
    """坐标 -> 地名。配置了 amap_key 用高德，否则 nominatim。"""
    if amap_key:
        try:
            with httpx.Client(timeout=TIMEOUT, headers=UA) as c:
                r = c.get(
                    "https://restapi.amap.com/v3/geocode/regeo",
                    params={"key": amap_key, "location": f"{lon},{lat}"},
                )
                r.raise_for_status()
                data = r.json()
                if data.get("status") == "1":
                    addr = (data.get("regeocode") or {}).get("formatted_address")
                    if addr:
                        return str(addr)
        except Exception:
            pass
    try:
        with httpx.Client(timeout=TIMEOUT, headers=UA) as c:
            r = c.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "json", "accept-language": "zh-CN"},
            )
            r.raise_for_status()
            data = r.json()
            name = data.get("display_name")
            if name:
                # display_name 往往很长，截取前三段更有可读性
                parts = [p.strip() for p in str(name).split(",") if p.strip()]
                return "，".join(parts[:3]) if parts else None
    except Exception:
        return None
    return None


def geocode_city(name: str) -> tuple[float, float, str] | None:
    """城市名 -> (lat, lon, 显示名)。"""
    try:
        with httpx.Client(timeout=TIMEOUT, headers=UA) as c:
            r = c.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": name, "count": 1, "language": "zh"},
            )
            r.raise_for_status()
            results = r.json().get("results") or []
            if not results:
                return None
            item = results[0]
            display = item.get("name") or name
            admin = item.get("admin1")
            if admin and admin != display:
                display = f"{admin}{display}"
            return float(item["latitude"]), float(item["longitude"]), str(display)
    except Exception:
        return None


def enrich(lat, lon, location_name: str, day: str) -> tuple:
    """创建/更新记录时的尽力补全：地名 + 天气。

    返回 (location_name, latitude, longitude, weather|None)。
    设置 weather_city_only=true 时忽略坐标（代理/VPN 会把 IP 定位带歪），只用默认城市。
    """
    amap_key = db.get_setting("amap_key")
    city_only = db.get_setting("weather_city_only") == "true"
    if city_only:
        city = db.get_setting("default_city")
        geo = geocode_city(city) if city else None
        if geo:
            lat, lon, city_name = geo
            location_name = location_name or city_name
        else:
            # 城市没解析出来（离线/接口异常）：宁可不填，也不用可能被代理带歪的坐标
            lat = lon = None
    elif (lat is None or lon is None) and not location_name:
        city = db.get_setting("default_city")
        if city:
            geo = geocode_city(city)
            if geo:
                lat, lon, city_name = geo
                location_name = location_name or city_name
    if lat is not None and lon is not None:
        if not location_name:
            location_name = reverse_geocode(lat, lon, amap_key) or ""
        weather = fetch_weather(lat, lon, day)
    else:
        weather = None
    return location_name, lat, lon, weather
