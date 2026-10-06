"""成长证据库后端：FastAPI 应用、全部 /api 路由、静态文件服务、启动初始化。

运行：python -m app.main   （默认监听 0.0.0.0:52122，手机经局域网访问）
"""
from __future__ import annotations

import io
import ipaddress
import json
import mimetypes
import re
import secrets
import socket
import sys
import threading
import time
import hashlib
import hmac
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile as StarletteUploadFile

from . import ai as ai_mod
from . import circle as circle_mod
from . import db, exporter, linkfetch, logx, mediafiles, scheduler, stats as stats_mod, temporal, weather as weather_mod

STATIC_DIR = db.BASE_DIR / "static"

VERSION = "1.5.0"
_STARTED_AT = time.time()

CATEGORIES = {"work", "life", "mixed"}
NODE_STATUS = {"suggested", "accepted", "rejected", "done"}
REPORT_TYPES = {"daily", "weekly", "monthly"}
EXPERIMENT_CONCLUSIONS = {"supported", "refuted", "insufficient", "undecided"}


# ---------------- 启动 / 生命周期 ----------------

def purge_old_deleted(days: int = 30) -> None:
    """硬删除 30 天前软删除的记录，并清理不再被引用的附件文件。"""
    cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = db.q("SELECT id FROM entries WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,))
    for r in rows:
        for a in db.q("SELECT id FROM attachments WHERE entry_id=?", (r["id"],)):
            mediafiles.delete_attachment(a["id"])
        db.execute("DELETE FROM entry_embeddings WHERE entry_id=?", (r["id"],))
        db.execute("DELETE FROM entries WHERE id=?", (r["id"],))
    if rows:
        logx.log("回收站自动清理", f"永久删除 {len(rows)} 条 30 天前的记录")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    logx.init(db.LOGS_DIR)
    # 存量明文口令迁移为哈希（一次性）
    ak = db.get_setting("access_key")
    if ak and not (ak.startswith("h$") or ak.startswith("p$")):
        db.set_setting("access_key", _hash_access_key(ak))
    purge_old_deleted()
    scheduler.start()
    logx.log("小满已启动", f"数据目录 {db.DATA_DIR}")
    # 就绪提示：告诉用户现在能用了、从哪访问（端口从启动参数里猜，猜不到就只给路径）
    port = 52122
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except (ValueError, IndexError):
            pass
    ips = _lan_ips()
    phone = f"http://{ips[0]}:{port}" if ips else "（未检测到局域网）"
    logx.log("小满就绪，可以开始记录啦", f"电脑 http://localhost:{port} ｜ 手机 {phone}")
    # 启动完成，解除启动锁（供 start.bat 判断"正在启动中"）
    try:
        (db.DATA_DIR / ".launching").unlink(missing_ok=True)
    except Exception:
        pass
    yield
    logx.log("小满已停止")


app = FastAPI(title="成长证据库", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """页面与前端资源每次回源校验，避免浏览器缓存住旧版 app.js/style.css。
    （媒体原件/缩略图是内容寻址的，不受影响，照常缓存。）"""
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def _access_token(key: str) -> str:
    return hashlib.sha256((key + "|xm").encode("utf-8")).hexdigest()


def _hash_access_key(plain: str) -> str:
    """口令哈希存储：随机盐 + PBKDF2-HMAC-SHA256，数据库/备份里看不到明文。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, 200_000)
    return f"p${salt.hex()}${dk.hex()}"


def _verify_access_key(plain: str, stored: str) -> bool:
    """校验口令是否匹配已存储的哈希；兼容旧版无盐格式（h$）。"""
    if not stored:
        return False
    if stored.startswith("p$"):
        try:
            _, salt_hex, dk_hex = stored.split("$", 2)
            salt = bytes.fromhex(salt_hex)
        except ValueError:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, 200_000).hex()
        return hmac.compare_digest(dk, dk_hex)
    legacy = "h$" + hashlib.sha256(("h$" + plain).encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy, stored)


def _client_is_local(request: Request) -> bool:
    """请求是否来自本机（回环地址）。"""
    client = request.client.host if request.client else ""
    if client == "localhost":
        return True
    try:
        return ipaddress.ip_address(client).is_loopback
    except ValueError:
        return False


def _setup_required_response(request: Request) -> Response:
    """未设访问口令时的默认姿态：只认本机，局域网设备得到引导页。"""
    msg = ("请先在运行小满的这台电脑上打开页面，"
           "在“设置”里设置访问口令，然后手机等设备才能连接。")
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=403, content={"detail": msg})
    return HTMLResponse(
        "<meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<div style=\"font-family:system-ui,-apple-system,'Microsoft YaHei',sans-serif;"
        "max-width:520px;margin:16vh auto;padding:0 26px;color:#4a3b2a;line-height:1.9\">"
        "<h2 style=\"color:#b5651d\">还差一步</h2>"
        f"<p>{msg}</p></div>",
        status_code=403,
    )


@app.middleware("http")
async def access_lock(request: Request, call_next):
    """访问口令与默认安全姿态。

    - 未设访问口令：默认只允许本机（回环）访问，局域网设备得到 403 引导先设置口令；
      /api/agent/*（自带 Bearer 鉴权）与 /api/healthz 不受此限。
    - 已设访问口令：除 /api/agent/*、/api/auth、/api/healthz 外，
      页面/静态/API/媒体都需要合法 cookie xm_auth，否则 401 {"detail":"locked"}。
    """
    path = request.url.path
    key = db.get_setting("access_key")
    if not key:
        if path.startswith("/api/agent/") or path == "/api/healthz":
            return await call_next(request)
        if not _client_is_local(request):
            return _setup_required_response(request)
        return await call_next(request)
    if path.startswith("/api/agent/") or path.startswith("/api/auth") or path == "/api/healthz":
        return await call_next(request)
    if (path == "/" or path.startswith(("/static", "/api", "/media", "/thumbs"))
            or path in ("/docs", "/redoc", "/openapi.json")):
        if not hmac.compare_digest(request.cookies.get("xm_auth", "") or "", _access_token(key)):
            return JSONResponse(status_code=401, content={"detail": "locked"})
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def validation_handler(_request, _exc):
    return JSONResponse(status_code=422, content={"detail": "请求参数格式不正确，请检查输入"})


@app.exception_handler(Exception)
async def unhandled_handler(_request, exc):
    # 固定文案回客户端（sqlite 错误/路径/内部细节绝不外抛，I-1）；原文只进本地日志
    logx.log("服务器内部错误", f"{type(exc).__name__}: {str(exc)[:300]}")
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})


# ---------------- 通用工具 ----------------

def _parse_date(s: str, name: str = "日期") -> str:
    try:
        return date.fromisoformat(s).isoformat()
    except (ValueError, TypeError):
        raise HTTPException(400, f"{name}格式应为 YYYY-MM-DD")


def _parse_dt(s: str) -> str:
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        raise HTTPException(400, "时间格式不正确，应为 ISO8601")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    else:
        # 统一存成本地时区：日历/统计按 substr(occurred_at,1,10) 聚合，必须是本地日
        dt = dt.astimezone()
    return dt.isoformat(timespec="seconds")


def _clean_tags(tags) -> list[str]:
    if tags is None:
        return []
    if not isinstance(tags, list):
        raise HTTPException(400, "tags 应为字符串数组")
    seen, out = set(), []
    for t in tags:
        t = str(t).strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _clean_blocks(value) -> list[dict]:
    """校验日志模块数组：[{title, text}]，title≤50 字、text≤5000 字、最多 30 个。"""
    if not isinstance(value, list):
        raise HTTPException(400, "blocks 应为模块数组")
    if len(value) > 30:
        raise HTTPException(400, "模块最多 30 个")
    out = []
    for item in value:
        if not isinstance(item, dict):
            raise HTTPException(400, "每个模块应为 {title, text} 对象")
        title, text = item.get("title"), item.get("text")
        if not isinstance(title, str) or not isinstance(text, str):
            raise HTTPException(400, "模块的 title 和 text 都应为字符串")
        title, text = title.strip()[:50], text.strip()[:5000]
        if title or text:
            out.append({"title": title, "text": text})
    return out


def _flatten_blocks(blocks: list[dict]) -> str:
    """把模块拍平为 markdown 写入 content，FTS/AI/导出因此零改动。"""
    return "\n\n".join(f"## {b['title']}\n{b['text']}" for b in blocks)


def _attachment_dict(a) -> dict:
    return {
        "id": a["id"],
        "kind": a["kind"],
        "mime": a["mime"],
        "filename": a["filename"],
        "size": a["size"],
        "duration_ms": a["duration_ms"],
        "width": a["width"],
        "height": a["height"],
        "url": f"/media/{a['path']}",
        "thumb_url": f"/thumbs/{a['thumb']}" if a["thumb"] else None,
    }


def entry_to_dict(row) -> dict:
    atts = db.q("SELECT * FROM attachments WHERE entry_id=? ORDER BY created_at", (row["id"],))
    links = db.q("SELECT * FROM links WHERE entry_id=? ORDER BY created_at", (row["id"],))
    try:
        tags = json.loads(row["tags"] or "[]")
    except ValueError:
        tags = []
    try:
        weather = json.loads(row["weather_json"]) if row["weather_json"] else None
    except ValueError:
        weather = None
    try:
        blocks = json.loads(row["blocks_json"]) if row["blocks_json"] else None
    except ValueError:
        blocks = None
    cats = db.q(
        "SELECT c.slug, c.name_zh FROM entry_categories ec "
        "JOIN categories c ON c.id = ec.category_id "
        "WHERE ec.entry_id=? AND c.status='active' ORDER BY c.id",
        (row["id"],),
    )
    return {
        "id": row["id"],
        "occurred_at": row["occurred_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "title": row["title"],
        "summary": row["summary"],
        "content": row["content"],
        "blocks": blocks,
        "category": row["category"],
        "is_work": None if row["is_work"] is None else bool(row["is_work"]),
        "is_work_manual": bool(row["is_work_manual"]),
        "work_related": bool(row["work_related"]),
        "categories": [{"slug": c["slug"], "name_zh": c["name_zh"]} for c in cats],
        "tags": tags,
        "location_name": row["location_name"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "weather": weather,
        "attachments": [_attachment_dict(a) for a in atts],
        "links": [
            {"id": l["id"], "url": l["url"], "title": l["title"], "description": l["description"]}
            for l in links
        ],
        "mood_score": row["mood_score"],
        "mood_label": row["mood_label"],
        "starred": bool(row["starred"]),
        "exclude_from_ai": bool(row["exclude_from_ai"]),
        # M11：软删标记——按 id 取详情也能分清活记录与回收站记录（主站/agent 同获益）
        "deleted": bool(row["deleted_at"]),
    }


def _get_entry_or_404(entry_id: str):
    row = db.q1("SELECT * FROM entries WHERE id=?", (entry_id,))
    if row is None:
        raise HTTPException(404, "记录不存在")
    return row


# ---------------- 记录：实现 ----------------

class EntryCreate(BaseModel):
    occurred_at: str | None = None
    title: str = ""
    summary: str = ""
    content: str = ""
    category: str = "life"
    tags: list[str] | None = None
    location_name: str = ""
    latitude: float | None = None
    longitude: float | None = None
    links: list[str] | None = None
    blocks: Any = None  # 由 _clean_blocks 校验，保证非法输入返回 400 而非 422
    auto_category: bool = False  # 自动补全时是否允许 AI 覆盖分类
    exclude_from_ai: bool = False  # 不让 AI 分析这条（不补全、不向量化、不进报告）
    is_work: bool | None = None  # 用户显式点亮/熄灭「工作」；None=交给小满


def _any_ai_configured() -> bool:
    return ai_mod.strong_configured() or ai_mod.fast_configured()


def _auto_enrich_worker(entry_id: str, auto_category: bool, rejudge: bool = False) -> None:
    """后台线程：轻量补全（只填空字段；rejudge=正文大改重判 is_work/类目）+ 顺手生成记录向量，
    任何失败静默。"""
    try:
        if _any_ai_configured():
            row = db.q1("SELECT * FROM entries WHERE id=? AND deleted_at IS NULL AND exclude_from_ai=0", (entry_id,))
            if row is not None:
                try:
                    tags = json.loads(row["tags"] or "[]")
                except ValueError:
                    tags = []
                has_gap = not (row["title"].strip() and row["summary"].strip() and tags)
                if has_gap or auto_category or rejudge:
                    try:
                        changed = _enrich_fill(row, use_category=auto_category, rejudge=rejudge)
                        if changed:
                            new = db.q1("SELECT title FROM entries WHERE id=?", (entry_id,))
                            logx.log("自动补全完成", f"标题《{(new['title'] or '')[:20]}》")
                    except ai_mod.AIError as e:
                        logx.log("自动补全失败", str(e))
    except Exception:
        pass  # 后台线程：任何失败都静默，不影响记录本身
    try:
        if _any_ai_configured():
            _mood_step(entry_id)
    except Exception:
        pass
    try:
        ai_mod.embed_entry(entry_id)
    except Exception:
        pass
    try:
        if circle_mod.enabled() and _any_ai_configured():
            circle_mod.run_extraction_for_entry(entry_id)
    except Exception:
        pass
    # 隐性关联发现（link_discovery 开关，B10）
    try:
        from . import links as links_mod
        if db.get_setting("link_discovery", "1") not in ("0", "false"):
            links_mod.discover_for_entry(entry_id)
    except Exception:
        pass


def _mood_step(entry_id: str) -> None:
    """fast 推断心情，只填 mood_score 为空的记录（不覆盖人工或已有推断）。"""
    row = db.q1("SELECT * FROM entries WHERE id=? AND deleted_at IS NULL", (entry_id,))
    if row is None or row["exclude_from_ai"] or row["mood_score"] is not None:
        return
    if not (row["content"] or "").strip():
        return
    score, label = ai_mod.infer_mood(row["title"], row["content"])
    if score is not None:
        db.execute("UPDATE entries SET mood_score=?, mood_label=? WHERE id=?",
                   (score, label, entry_id))


# 保存/编辑后的异步链（enrich→心情→embed→圈子→关联）做 per-entry 合并（I4）：
# 同一条记录快速连改不会堆并发线程——在跑则置 dirty，跑完补一轮即可。
_enrich_gate = threading.Lock()
_enrich_running: set[str] = set()
_enrich_dirty: set[str] = set()


def _maybe_auto_enrich(entry_id: str, auto_category: bool, rejudge: bool = False) -> None:
    if not (_any_ai_configured() or ai_mod.embed_configured()):
        return
    with _enrich_gate:
        if entry_id in _enrich_running:
            _enrich_dirty.add(entry_id)
            if rejudge:
                _enrich_dirty_rejudge.add(entry_id)  # dirty 轮也要带 rejudge 语义
            return
        _enrich_running.add(entry_id)
    threading.Thread(target=_enrich_loop, args=(entry_id, auto_category, rejudge),
                     daemon=True).start()


_enrich_dirty_rejudge: set[str] = set()


def _enrich_loop(entry_id: str, auto_category: bool, rejudge: bool) -> None:
    try:
        while True:
            _auto_enrich_worker(entry_id, auto_category, rejudge)
            with _enrich_gate:
                if entry_id in _enrich_dirty:
                    _enrich_dirty.discard(entry_id)
                    rejudge = entry_id in _enrich_dirty_rejudge
                    _enrich_dirty_rejudge.discard(entry_id)
                    continue  # 在跑期间这条又被改了：补跑一轮拿到最新内容
                _enrich_running.discard(entry_id)
                return
    except Exception:
        with _enrich_gate:
            _enrich_running.discard(entry_id)
            _enrich_dirty.discard(entry_id)
            _enrich_dirty_rejudge.discard(entry_id)


def _embed_worker(entry_id: str) -> None:
    try:
        ai_mod.embed_entry(entry_id)
    except Exception:
        pass


def _maybe_embed_async(entry_id: str) -> None:
    if ai_mod.embed_configured():
        threading.Thread(target=_embed_worker, args=(entry_id,), daemon=True).start()


def _write_entry_categories(entry_id: str, slugs: list, source: str = "ai") -> int:
    """把 AI/用户给的类目 slug 落到 entry_categories（只认 active 类目，幂等）。返回写入数。"""
    n = 0
    for slug in (slugs or [])[:3]:
        cat = db.q1("SELECT id FROM categories WHERE slug=? AND status='active'", (str(slug).strip(),))
        if cat is None:
            continue
        db.execute(
            "INSERT OR IGNORE INTO entry_categories(entry_id, category_id, confidence, source) "
            "VALUES(?,?,0,?)",
            (entry_id, cat["id"], source),
        )
        n += 1
    return n


def _category_ai_on() -> bool:
    """AI 分类开关（默认开）。关了就不自动打类目、不自动判 is_work。"""
    return db.get_setting("category_ai", "1") not in ("0", "false")


def _enrich_fill(row, use_category: bool, force: bool = False, rejudge: bool = False) -> bool:
    """调用轻量模型补全字段并 UPDATE（FTS 触发器自动同步），返回是否有更新。
    默认只补空字段；force=True 时用当前正文重新生成标题/摘要/标签（覆盖旧值）。
    rejudge=True（正文大改）时：is_work 重判（is_work_manual=1 以用户为准不动），
    AI 类目先清掉 source='ai' 的旧类目再按新正文重打（source='user' 的手动类目保留）。
    exclude_from_ai 的记录绝不发给外部 AI。"""
    if row["exclude_from_ai"]:
        return False
    try:
        tags = json.loads(row["tags"] or "[]")
    except ValueError:
        tags = []
    # 附件名也作为线索，正文单薄时也能起出像样的标题
    content = row["content"] or ""
    atts = db.q("SELECT filename FROM attachments WHERE entry_id=? ORDER BY created_at", (row["id"],))
    if atts:
        names = "、".join(a["filename"] for a in atts[:10])
        content = (content + f"\n（附件：{names}）").strip()
    result = ai_mod.enrich_entry_fields(row["title"], row["summary"], content,
                                        tags, row["category"])
    sets, params = [], []
    if (force or not row["title"].strip()) and result["title"]:
        sets.append("title=?")
        params.append(result["title"])
    if (force or not row["summary"].strip()) and result["summary"]:
        sets.append("summary=?")
        params.append(result["summary"])
    if (force or not tags) and result["tags"]:
        sets.append("tags=?")
        params.append(json.dumps(result["tags"], ensure_ascii=False))
    if _category_ai_on() and not row["is_work_manual"] and result.get("is_work") is not None:
        sets.append("is_work=?")
        params.append(1 if result["is_work"] else 0)
    if sets:
        sets.append("updated_at=?")
        params.append(db.now_iso())
        params.append(row["id"])
        db.execute(f"UPDATE entries SET {', '.join(sets)} WHERE id=?", tuple(params))
    if rejudge and _category_ai_on():
        db.execute("DELETE FROM entry_categories WHERE entry_id=? AND source='ai'", (row["id"],))
    if _category_ai_on() and result.get("categories"):
        _write_entry_categories(row["id"], result["categories"])
    return bool(sets)


def create_entry_impl(body: EntryCreate) -> dict:
    if body.category not in CATEGORIES:
        raise HTTPException(400, "category 只能是 work / life / mixed")
    blocks = _clean_blocks(body.blocks) if body.blocks is not None else None
    if not (body.title.strip() or body.summary.strip() or body.content.strip()
            or body.links or blocks):
        raise HTTPException(400, "记录不能为空：请至少填写标题、正文、模块或链接")

    occurred_at = _parse_dt(body.occurred_at) if body.occurred_at else db.now_iso()
    tags = _clean_tags(body.tags)
    content = _flatten_blocks(blocks) if blocks else body.content
    if _weather_enabled():
        location_name, lat, lon, weather = weather_mod.enrich(
            body.latitude, body.longitude, body.location_name.strip(), occurred_at[:10]
        )
        if weather is None and body.latitude is not None and body.longitude is not None:
            logx.log("天气补全未成功", "离线或供应商无响应，已正常保存")
    else:
        # 天气/定位总开关关闭：不调任何外部接口
        location_name = body.location_name.strip() or "公司"
        lat, lon = body.latitude, body.longitude
        weather = None

    entry_id = db.new_id()
    now = db.now_iso()
    is_work_val = None if body.is_work is None else (1 if body.is_work else 0)
    db.execute(
        "INSERT INTO entries(id, occurred_at, created_at, updated_at, title, summary, content, "
        "category, tags, location_name, latitude, longitude, weather_json, blocks_json, "
        "exclude_from_ai, is_work, is_work_manual) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (entry_id, occurred_at, now, now, body.title.strip(), body.summary.strip(),
         content, body.category, json.dumps(tags, ensure_ascii=False),
         location_name, lat, lon,
         json.dumps(weather, ensure_ascii=False) if weather else None,
         json.dumps(blocks, ensure_ascii=False) if blocks else None,
         1 if body.exclude_from_ai else 0,
         is_work_val, 1 if body.is_work is not None else 0),
    )
    for url in body.links or []:
        _add_link(entry_id, url)
    _maybe_auto_enrich(entry_id, body.auto_category)
    parts = []
    if body.is_work is not None:
        parts.append("工作" if body.is_work else "非工作")
    else:
        cat_label = {"work": "工作", "life": "生活", "mixed": "混合"}.get(body.category, body.category)
        parts.append(f"分类 {cat_label}")
    if body.title.strip():
        parts.append(f"标题《{body.title.strip()[:20]}》")
    if body.links:
        parts.append(f"{len(body.links)} 个链接")
    if weather:
        parts.append(f"天气 {weather.get('text', '')}")
    logx.log("新记录已保存", "，".join(parts))
    return entry_to_dict(_get_entry_or_404(entry_id))


def _add_link(entry_id: str, url: str) -> None:
    url = (url or "").strip()
    if not url:
        raise HTTPException(400, "链接不能为空")
    try:
        meta = linkfetch.fetch_link_meta(url)
    except linkfetch.LinkRejected as e:
        raise HTTPException(400, str(e))
    db.execute(
        "INSERT INTO links(id, entry_id, url, title, description, created_at) VALUES(?,?,?,?,?,?)",
        (db.new_id(), entry_id, meta["url"], meta["title"], meta["description"], db.now_iso()),
    )
    db.execute("UPDATE entries SET updated_at=? WHERE id=?", (db.now_iso(), entry_id))
    if meta["title"] and meta["title"] != meta["url"]:
        logx.log("链接已添加", f"抓到的标题《{meta['title'][:30]}》")
    else:
        logx.log("链接已添加", "未能抓取标题，按网址保存")


def list_entries_impl(date_=None, from_=None, to=None, q=None, category=None,
                      tag=None, limit=50, offset=0, deleted=0, starred=0, work=None) -> dict:
    clauses = ["deleted_at IS NOT NULL" if deleted else "deleted_at IS NULL"]
    params: list = []
    if starred:
        clauses.append("starred=1")
    if work:
        clauses.append("is_work=1")
    if date_:
        clauses.append("substr(occurred_at,1,10)=?")
        params.append(_parse_date(date_))
    if from_:
        clauses.append("substr(occurred_at,1,10)>=?")
        params.append(_parse_date(from_))
    if to:
        clauses.append("substr(occurred_at,1,10)<=?")
        params.append(_parse_date(to))
    if category:
        # 新体系：category 参数是类目 slug（旧 work/life/mixed 值静默不匹配任何记录）
        clauses.append(
            "id IN (SELECT ec.entry_id FROM entry_categories ec "
            "JOIN categories c ON c.id=ec.category_id WHERE c.slug=?)")
        params.append(str(category).strip())
    if tag:
        clauses.append("tags LIKE ?")
        params.append(f'%"{tag}"%')
    if q and q.strip():
        rowids = db.search_rowids(q, limit=500)
        if not rowids:
            return {"items": [], "total": 0}
        placeholders = ",".join("?" for _ in rowids)
        clauses.append(f"rowid IN ({placeholders})")
        params.extend(rowids)

    where = " AND ".join(clauses)
    total = db.q1(f"SELECT COUNT(*) AS n FROM entries WHERE {where}", tuple(params))["n"]
    rows = db.q(
        f"SELECT * FROM entries WHERE {where} ORDER BY occurred_at DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    return {"items": [entry_to_dict(r) for r in rows], "total": total}


def search_impl(q: str, limit: int = 50) -> dict:
    if not q or not q.strip():
        return {"items": []}
    rowids = db.search_rowids(q, limit=max(1, min(limit, 200)))
    if not rowids:
        return {"items": []}
    placeholders = ",".join("?" for _ in rowids)
    rows = db.q(
        f"SELECT * FROM entries WHERE deleted_at IS NULL AND rowid IN ({placeholders}) "
        "ORDER BY occurred_at DESC",
        tuple(rowids),
    )
    return {"items": [entry_to_dict(r) for r in rows]}


# ---------------- 记录：路由 ----------------

@app.get("/api/entries")
def list_entries(
    date: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    q: str | None = Query(None),
    category: str | None = Query(None),
    tag: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    deleted: int = Query(0, ge=0, le=1),
    starred: int = Query(0, ge=0, le=1),
    work: int = Query(0, ge=0, le=1),
):
    return list_entries_impl(date, from_, to, q, category, tag, limit, offset, deleted, starred,
                             work=work)


@app.post("/api/entries", status_code=201)
def create_entry(body: EntryCreate):
    return create_entry_impl(body)


@app.get("/api/entries/random")
def random_entry():
    """随便翻翻：随机一条未删除记录（本机展示，不排除 exclude_from_ai）。"""
    row = db.q1("SELECT * FROM entries WHERE deleted_at IS NULL ORDER BY RANDOM() LIMIT 1")
    if row is None:
        raise HTTPException(404, "还没有任何记录")
    return entry_to_dict(row)


@app.get("/api/entries/{entry_id}")
def get_entry(entry_id: str):
    return entry_to_dict(_get_entry_or_404(entry_id))


@app.patch("/api/entries/{entry_id}")
def patch_entry(entry_id: str, payload: dict = Body(...)):
    row = _get_entry_or_404(entry_id)
    # M3：回收站里的记录不可编辑（先恢复再改）
    if row["deleted_at"]:
        raise HTTPException(400, "该记录在回收站，请先恢复再编辑")
    sets, params = [], []

    # blocks：非空数组→存 blocks_json 并拍平覆盖 content；null/空数组→清除模块（content 不动）
    flattened_content = None
    if "blocks" in payload:
        bv = payload["blocks"]
        if bv is None:
            sets.append("blocks_json=?")
            params.append(None)
        else:
            blocks = _clean_blocks(bv)
            sets.append("blocks_json=?")
            if blocks:
                params.append(json.dumps(blocks, ensure_ascii=False))
                flattened_content = _flatten_blocks(blocks)
            else:
                params.append(None)

    if "occurred_at" in payload:
        sets.append("occurred_at=?")
        params.append(_parse_dt(str(payload["occurred_at"])))
    for field in ("title", "summary", "content", "location_name"):
        if field in payload:
            if field == "content" and flattened_content is not None:
                continue  # blocks 拍平结果优先于显式 content
            sets.append(f"{field}=?")
            params.append(str(payload[field] or ""))
    if flattened_content is not None:
        sets.append("content=?")
        params.append(flattened_content)
    if "category" in payload:
        if payload["category"] not in CATEGORIES:
            raise HTTPException(400, "category 只能是 work / life / mixed")
        sets.append("category=?")
        params.append(payload["category"])
    if "is_work" in payload:
        v = payload["is_work"]
        if v is not None and not isinstance(v, bool):
            raise HTTPException(400, "is_work 应为 true / false / null")
        # 用户亲手改的：以后这条以用户为准，AI 不再覆盖
        sets.append("is_work=?")
        params.append(None if v is None else (1 if v else 0))
        sets.append("is_work_manual=?")
        params.append(1)
    if "tags" in payload:
        sets.append("tags=?")
        params.append(json.dumps(_clean_tags(payload["tags"]), ensure_ascii=False))
    for field in ("latitude", "longitude"):
        if field in payload:
            v = payload[field]
            if v is not None and not isinstance(v, (int, float)):
                raise HTTPException(400, f"{field} 应为数字或 null")
            sets.append(f"{field}=?")
            params.append(v)
    if "exclude_from_ai" in payload:
        sets.append("exclude_from_ai=?")
        params.append(1 if payload["exclude_from_ai"] else 0)
    if "starred" in payload:
        sets.append("starred=?")
        params.append(1 if payload["starred"] else 0)

    weather_touched = "weather" in payload
    if weather_touched:
        w = payload["weather"]
        if w is not None and not isinstance(w, dict):
            raise HTTPException(400, "weather 应为对象或 null")
        sets.append("weather_json=?")
        params.append(json.dumps(w, ensure_ascii=False) if w else None)

    if not sets:
        return entry_to_dict(row)

    sets.append("updated_at=?")
    params.append(db.now_iso())
    params.append(entry_id)
    db.execute(f"UPDATE entries SET {', '.join(sets)} WHERE id=?", tuple(params))

    # 位置/时间变了且未显式给 weather：尽力重新补全天气与地名（总开关关闭时跳过，保留原值）
    context_changed = ({"occurred_at", "latitude", "longitude", "location_name"}
                       & payload.keys())
    if context_changed and not weather_touched and _weather_enabled():
        new = _get_entry_or_404(entry_id)
        lat = new["latitude"] if new["latitude"] is not None else row["latitude"]
        lon = new["longitude"] if new["longitude"] is not None else row["longitude"]
        loc = new["location_name"] or row["location_name"]
        loc2, lat2, lon2, w = weather_mod.enrich(lat, lon, loc, new["occurred_at"][:10])
        db.execute(
            "UPDATE entries SET location_name=?, latitude=?, longitude=?, weather_json=? WHERE id=?",
            (loc2, lat2, lon2, json.dumps(w, ensure_ascii=False) if w else None, entry_id),
        )
    # 正文/标题/摘要/模块变了：异步链统一处理（内含 embed，无需再单起 _maybe_embed_async——R3）；
    # 正文/模块变化的轮次带 rejudge：is_work 与 AI 类目按新正文重判（用户手动定过的不动）
    if {"content", "title", "summary", "blocks"} & payload.keys():
        _maybe_auto_enrich(entry_id, False,
                           rejudge=bool({"content", "blocks"} & payload.keys()))
    # 标记"不让 AI 分析"：清掉已有向量，此后任何自动任务都不再碰这条
    if payload.get("exclude_from_ai"):
        db.execute("DELETE FROM entry_embeddings WHERE entry_id=?", (entry_id,))
    return entry_to_dict(_get_entry_or_404(entry_id))


@app.delete("/api/entries/{entry_id}")
def delete_entry(entry_id: str):
    row = _get_entry_or_404(entry_id)
    # M3：重复删除幂等 200，但不刷新 deleted_at——不重置 30 天自动清除计时
    if row["deleted_at"]:
        return {"ok": True, "already_deleted": True}
    db.execute("UPDATE entries SET deleted_at=?, updated_at=? WHERE id=?",
               (db.now_iso(), db.now_iso(), entry_id))
    db.execute("DELETE FROM entry_embeddings WHERE entry_id=?", (entry_id,))
    logx.log("记录已移入回收站", "30 天内可在设置页恢复")
    return {"ok": True}


@app.post("/api/entries/{entry_id}/restore")
def restore_entry(entry_id: str):
    _get_entry_or_404(entry_id)
    db.execute("UPDATE entries SET deleted_at=NULL, updated_at=? WHERE id=?",
               (db.now_iso(), entry_id))
    _maybe_embed_async(entry_id)  # 恢复后重建向量
    logx.log("记录已从回收站恢复")
    return entry_to_dict(_get_entry_or_404(entry_id))


# ---------------- 附件 / 链接 ----------------

def _sanitize_filename(raw, max_len: int = 80) -> str:
    """去路径分隔符/控制字符、去首尾空白、限长。"""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "", str(raw or "")).strip()
    return name[:max_len].strip()


def _after_upload_worker(entry_id: str, att_ids: list) -> None:
    """上传附件的后台线程链：补全 → 嵌入 → 自动命名。"""
    try:
        _auto_enrich_worker(entry_id, False)
    except Exception:
        pass
    try:
        _auto_name_worker(entry_id, att_ids)
    except Exception:
        pass


def _auto_name_worker(entry_id: str, att_ids: list) -> None:
    """fast 模型为新附件生成显示名；任何失败静默保留原名。老附件不动。"""
    if not att_ids or not _any_ai_configured():
        return
    row = db.q1("SELECT content, exclude_from_ai FROM entries WHERE id=?", (entry_id,))
    if row and row["exclude_from_ai"]:
        return  # 标记了不让 AI 分析：附件也不发去命名
    content = (row["content"] if row else "") or ""
    vision = db.get_setting("ai_vision") == "true"
    for att_id in att_ids:
        try:
            a = db.q1("SELECT * FROM attachments WHERE id=?", (att_id,))
            if a is None:
                continue
            image_path = db.MEDIA_DIR / a["path"] if a["kind"] == "image" else None
            named = ai_mod.name_attachment(content, a["filename"], image_path, vision)
            if not named:
                continue
            ext = Path(a["filename"]).suffix
            clean = _sanitize_filename(named["name"], 20)
            if ext and clean.lower().endswith(ext.lower()):
                clean = clean[: -len(ext)].rstrip(".")
            if not clean:
                continue
            db.execute("UPDATE attachments SET filename=?, caption=? WHERE id=?",
                       (clean + ext, named.get("caption") or None, att_id))
            logx.log("附件自动命名", f"{a['filename']} → {clean + ext}")
        except Exception:
            pass


@app.post("/api/entries/{entry_id}/attachments")
def upload_attachments(entry_id: str, files: list[UploadFile] = File(...)):
    _get_entry_or_404(entry_id)
    if not files:
        raise HTTPException(400, "未接收到文件")
    att_ids = []
    try:
        for f in files:
            att_ids.append(mediafiles.save_upload(entry_id, f))
    except mediafiles.UploadError as e:
        raise HTTPException(e.status, str(e))
    db.execute("UPDATE entries SET updated_at=? WHERE id=?", (db.now_iso(), entry_id))
    names = [f.filename for f in files[:5] if f.filename]
    logx.log(f"收到 {len(files)} 个附件", "、".join(names) + (" 等" if len(files) > 5 else ""))
    if _any_ai_configured() or ai_mod.embed_configured():
        threading.Thread(target=_after_upload_worker, args=(entry_id, att_ids),
                         daemon=True).start()
    return entry_to_dict(_get_entry_or_404(entry_id))


@app.patch("/api/attachments/{att_id}")
def patch_attachment(att_id: str, payload: dict = Body(...)):
    row = db.q1("SELECT * FROM attachments WHERE id=?", (att_id,))
    if row is None:
        raise HTTPException(404, "附件不存在")
    if "filename" not in payload:
        return _attachment_dict(row)
    name = _sanitize_filename(payload["filename"], 80)
    if not name:
        raise HTTPException(400, "文件名不能为空")
    orig_ext = Path(row["filename"]).suffix
    if not Path(name).suffix and orig_ext:
        if len(name) + len(orig_ext) > 80:
            name = name[: 80 - len(orig_ext)]
        name = name + orig_ext
    db.execute("UPDATE attachments SET filename=? WHERE id=?", (name, att_id))
    if name != row["filename"]:
        logx.log("附件已改名", f"{row['filename']} → {name}")
    return _attachment_dict(db.q1("SELECT * FROM attachments WHERE id=?", (att_id,)))


@app.delete("/api/attachments/{att_id}")
def delete_attachment(att_id: str):
    row = db.q1("SELECT filename FROM attachments WHERE id=?", (att_id,))
    if row is None:
        raise HTTPException(404, "附件不存在")
    mediafiles.delete_attachment(att_id)
    logx.log("附件已删除", row["filename"])
    return {"ok": True}


class LinkIn(BaseModel):
    url: str


@app.post("/api/entries/{entry_id}/links")
def add_link(entry_id: str, body: LinkIn):
    _get_entry_or_404(entry_id)
    _add_link(entry_id, body.url)
    return entry_to_dict(_get_entry_or_404(entry_id))


def _assets_payload(kind: str | None, limit: int, offset: int) -> dict:
    clauses = ["e.deleted_at IS NULL"]
    params: list = []
    if kind:
        if kind not in ("image", "video", "audio", "file"):
            raise HTTPException(400, "kind 只能是 image / video / audio / file")
        clauses.append("a.kind=?")
        params.append(kind)
    where = " AND ".join(clauses)
    rows = db.q(
        f"SELECT a.* FROM attachments a JOIN entries e ON a.entry_id=e.id "
        f"WHERE {where} ORDER BY a.created_at DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    return {
        "items": [
            {
                "id": a["id"],
                "entry_id": a["entry_id"],
                "kind": a["kind"],
                "mime": a["mime"],
                "filename": a["filename"],
                "size": a["size"],
                "url": f"/media/{a['path']}",
                "thumb_url": f"/thumbs/{a['thumb']}" if a["thumb"] else None,
                "created_at": a["created_at"],
            }
            for a in rows
        ]
    }


@app.get("/api/assets")
def list_assets(
    kind: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return _assets_payload(kind, limit, offset)


# ---------------- 手写笔记拍照识别（多页） ----------------

MAX_SCAN_BYTES = 20 * 1024 * 1024   # 单张扫描图片上限 20MB
MAX_SCAN_TOTAL = 80 * 1024 * 1024   # 一次扫描总大小上限 80MB
MAX_SCAN_PAGES = 9


def _crop_figures(images: list[bytes], figures: list) -> list[tuple[int, int, str, bytes]]:
    """按 figure.page 从对应页裁剪手绘图区域。

    规则：box clamp 到 0~1、x2>x1 且面积占比≥0.03、总数≤6。
    返回 [(page, 页内序号, label, jpeg_bytes), ...]。
    """
    from PIL import Image
    opened: dict[int, Image.Image | None] = {}
    counters: dict[int, int] = {}
    out = []
    for fig in figures:
        if len(out) >= 6:
            break
        page = fig.get("page")
        if not isinstance(page, int) or not 1 <= page <= len(images):
            continue
        try:
            x1, y1, x2, y2 = fig["box"]
        except (KeyError, TypeError, ValueError):
            continue
        x1, x2 = min(max(x1, 0.0), 1.0), min(max(x2, 0.0), 1.0)
        y1, y2 = min(max(y1, 0.0), 1.0), min(max(y2, 0.0), 1.0)
        if x2 <= x1 or y2 <= y1:
            continue
        if (x2 - x1) * (y2 - y1) < 0.03:
            continue
        if page not in opened:
            try:
                opened[page] = Image.open(io.BytesIO(images[page - 1]))
            except Exception:
                opened[page] = None
        im = opened[page]
        if im is None:
            continue
        W, H = im.size
        try:
            crop = im.crop((int(x1 * W), int(y1 * H), int(x2 * W), int(y2 * H)))
            buf = io.BytesIO()
            crop.convert("RGB").save(buf, "JPEG", quality=85)
        except Exception:
            continue
        counters[page] = counters.get(page, 0) + 1
        out.append((page, counters[page], fig.get("label") or "", buf.getvalue()))
    return out


def _safe_filename_part(label: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|\s]+', "-", label).strip("-")[:20]
    return safe or "插图"


@app.post("/api/scan-note", status_code=201)
async def scan_note(request: Request):
    # 收集全部文件 part（兼容旧字段名 file 与新字段名 files）
    form = await request.form()
    uploads = [v for _, v in form.multi_items() if isinstance(v, StarletteUploadFile)]
    if not uploads:
        raise HTTPException(400, "请上传图片文件")
    if len(uploads) > MAX_SCAN_PAGES:
        raise HTTPException(400, "一次最多识别 9 页")

    # 先验 MIME（只需头部，保证非图片错误优先于其他错误）
    mimes = []
    for up in uploads:
        mime = up.content_type or mimetypes.guess_type(up.filename or "")[0] or ""
        if not mime.startswith("image/"):
            raise HTTPException(400, "请上传图片文件")
        mimes.append(mime)

    if not (db.get_setting("ai_base_url").strip() and db.get_setting("ai_key").strip()
            and db.get_setting("ai_model").strip()):
        raise HTTPException(400, "请先在设置页配置 AI 接口")

    from PIL import Image
    images, total = [], 0
    for up in uploads:
        data = await up.read(MAX_SCAN_BYTES + 1)
        if len(data) > MAX_SCAN_BYTES:
            raise HTTPException(413, "单张图片不能超过 20MB")
        total += len(data)
        if total > MAX_SCAN_TOTAL:
            raise HTTPException(413, "图片总大小不能超过 80MB")
        try:
            with Image.open(io.BytesIO(data)) as im:
                im.verify()
        except Exception:
            raise HTTPException(400, "图片文件无法识别或已损坏")
        images.append(data)

    try:
        logx.log("开始识别手写笔记", f"共 {len(images)} 页")
        result = ai_mod.scan_note_image(images)
    except ai_mod.AIError as e:
        logx.log("识别手写笔记失败", str(e))
        raise HTTPException(502, str(e))

    if result["occurred_date"]:
        d = date.fromisoformat(result["occurred_date"])
        # 笔记常不写年份，AI 可能猜错年：未来日期收回到今天；
        # 年份异常且换成今年后仍是过去时，优先用今年（识别后用户仍可改）
        today = datetime.now().astimezone().date()
        if d > today:
            d = today
        elif d.year != today.year:
            try:
                candidate = d.replace(year=today.year)
                if candidate <= today:
                    d = candidate
            except ValueError:
                pass
        local_tz = datetime.now().astimezone().tzinfo
        occurred_at = datetime(d.year, d.month, d.day, 12, 0, tzinfo=local_tz).isoformat(timespec="seconds")
    else:
        occurred_at = db.now_iso()

    location_name, lat, lon = "", None, None
    if result["weather_text"]:
        weather = {"text": result["weather_text"], "temperature_c": None,
                   "humidity": None, "provider": "笔记转写"}
    elif _weather_enabled():
        location_name, lat, lon, weather = weather_mod.enrich(None, None, "", occurred_at[:10])
    else:
        weather = None  # 总开关关闭：不补天气也不填地点

    title = result["title"] or "扫描笔记"
    is_work_val = None if result["is_work"] is None else (1 if result["is_work"] else 0)

    entry_id = db.new_id()
    now = db.now_iso()
    db.execute(
        "INSERT INTO entries(id, occurred_at, created_at, updated_at, title, summary, content, "
        "category, tags, location_name, latitude, longitude, weather_json, is_work) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (entry_id, occurred_at, now, now, title, "", result["content"], "life",
         "[]", location_name, lat, lon,
         json.dumps(weather, ensure_ascii=False) if weather else None, is_work_val),
    )
    # 附件 = 全部原图（保留原文件名）+ 全部裁图（复用 sha256 去重存储）
    for i, (up, mime) in enumerate(zip(uploads, mimes), 1):
        mediafiles.store_bytes(entry_id, up.filename or f"扫描原件-{i}.jpg", mime,
                               images[i - 1])
    crops = _crop_figures(images, result["figures"])
    for page, m, label, crop_bytes in crops:
        mediafiles.store_bytes(entry_id,
                               f"第{page}页-区域{m}-{_safe_filename_part(label)}.jpg",
                               "image/jpeg", crop_bytes)
    logx.log("识别完成", f"标题《{title}》，裁出 {len(crops)} 张附图")
    return entry_to_dict(_get_entry_or_404(entry_id))


# ---------------- 轻量 AI 补全 ----------------

@app.post("/api/ai/enrich-entry/{entry_id}")
def enrich_entry(entry_id: str, payload: dict | None = Body(None)):
    """手动触发轻量补全：默认只填空的 title/summary/tags，不动分类；
    force=true 时用当前正文重新生成（覆盖旧的标题/摘要/标签）。"""
    force = bool((payload or {}).get("force"))
    row = _get_entry_or_404(entry_id)
    if not _any_ai_configured():
        raise HTTPException(400, "请先在设置页配置 AI 接口")
    try:
        _enrich_fill(row, use_category=False, force=force)
    except ai_mod.AIError as e:
        logx.log("手动补全失败", str(e))
        raise HTTPException(502, str(e))
    logx.log("手动补全", "成功")
    return entry_to_dict(_get_entry_or_404(entry_id))


@app.post("/api/ai/enrich-batch")
def enrich_batch(payload: dict = Body(default={})):
    """批量补全「未删除、有正文、无标题」的记录，同步执行，单条失败跳过计数。"""
    raw_limit = (payload or {}).get("limit", 20)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        raise HTTPException(400, "limit 应为整数")
    limit = max(1, min(limit, 50))
    if not _any_ai_configured():
        raise HTTPException(400, "请先在设置页配置 AI 接口")

    rows = db.q(
        "SELECT * FROM entries WHERE deleted_at IS NULL AND trim(content) != '' "
        "AND trim(title) = '' ORDER BY occurred_at DESC LIMIT ?",
        (limit,),
    )
    checked, updated, failed = len(rows), 0, 0
    for row in rows:
        try:
            if _enrich_fill(row, use_category=False):
                updated += 1
        except Exception:
            failed += 1
    logx.log("批量补全完成", f"检查 {checked} 条，补全 {updated} 条，失败 {failed} 条")
    return {"checked": checked, "updated": updated, "failed": failed}


@app.post("/api/ai/embed-batch")
def embed_batch(payload: dict = Body(default={})):
    """为还没有向量的历史记录建立语义索引（问小满/相关回忆/知识去重用）。"""
    if not ai_mod.embed_configured():
        raise HTTPException(400, "请先在设置页配置嵌入模型")
    raw_limit = (payload or {}).get("limit", 100)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        raise HTTPException(400, "limit 应为整数")
    limit = max(1, min(limit, 500))
    rows = db.q(
        "SELECT e.id FROM entries e LEFT JOIN entry_embeddings v ON v.entry_id=e.id "
        "WHERE e.deleted_at IS NULL AND v.entry_id IS NULL ORDER BY e.occurred_at DESC LIMIT ?",
        (limit,),
    )
    checked, updated = len(rows), 0
    for r in rows:
        if ai_mod.embed_entry(r["id"]):
            updated += 1
    # 顺带补知识条目的向量（语义判重的前提）
    krows = db.q(
        "SELECT id FROM knowledge WHERE (vector_json IS NULL OR vector_json = '') "
        "AND status IN ('pending','accepted','rejected') LIMIT 200"
    )
    k_updated = 0
    for r in krows:
        if ai_mod.embed_knowledge_item(r["id"]):
            k_updated += 1
    remaining = db.q1(
        "SELECT COUNT(*) AS n FROM entries e LEFT JOIN entry_embeddings v ON v.entry_id=e.id "
        "WHERE e.deleted_at IS NULL AND v.entry_id IS NULL")["n"]
    logx.log("语义索引批量建立", f"记录 {updated} 条、知识 {k_updated} 条")
    return {"checked": checked, "updated": updated, "failed": checked - updated,
            "remaining": remaining, "knowledge_updated": k_updated}


# ---------------- 问小满 / 相关记录 / 每日一句 ----------------

@app.post("/api/checkin")
def checkin(payload: dict = Body(...)):
    """对话式记录：小满陪你把今天聊出来。"""
    history = (payload or {}).get("history") or []
    if not isinstance(history, list):
        raise HTTPException(400, "history 应为对话数组")
    if not _any_ai_configured():
        raise HTTPException(400, "请先在设置页配置 AI 接口")
    try:
        result = ai_mod.checkin_reply(history)
    except ai_mod.AIError as e:
        raise HTTPException(502, str(e))
    if result.get("done") and result.get("entry_content"):
        logx.log("对话记录完成", "已整理成一条记录")
    return result


@app.get("/api/next-day-intents")
def next_day_intents():
    """明日接力：从昨天记录里提取打算做的事。按天缓存。"""
    today = date.today().isoformat()
    cached = db.get_setting("intents_cache")
    if cached:
        try:
            c = json.loads(cached)
            # v2：排除"暂缓/推迟"类表述、措辞更自然；旧缓存直接作废
            if c.get("date") == today and c.get("v") == 2 and isinstance(c.get("items"), list):
                return {"items": c["items"]}
        except ValueError:
            pass
    items: list = []
    if _any_ai_configured():
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        rows = db.q(
            "SELECT title, content FROM entries WHERE deleted_at IS NULL "
            "AND exclude_from_ai=0 AND substr(occurred_at,1,10)=? AND trim(content) != ''",
            (yesterday,),
        )
        if rows:
            digest = "\n".join(
                f"- {r['title']}：{(r['content'] or '')[:300]}" for r in rows
            )
            try:
                items = ai_mod.extract_intents(digest)
            except ai_mod.AIError:
                items = []
    db.set_setting("intents_cache",
                   json.dumps({"date": today, "items": items, "v": 2}, ensure_ascii=False))
    return {"items": items}


def _ids_from_rowids(rowids: list[int]) -> list[str]:
    if not rowids:
        return []
    placeholders = ",".join("?" for _ in rowids)
    rows = db.q(
        f"SELECT id FROM entries WHERE deleted_at IS NULL AND exclude_from_ai=0 "
        f"AND rowid IN ({placeholders})",
        tuple(rowids),
    )
    return [r["id"] for r in rows]


def _ask_fts_ids(question: str, limit: int = 8) -> list[str]:
    """FTS 检索：整句 phrase 优先；无命中时拆 2~3 字滑窗词，按命中词数排序。

    问句（"我最近在缓存方面做了什么？"）整句不可能作为子串命中记录，
    必须做关键词级召回。
    """
    ids = _ids_from_rowids(db.search_rowids(question, limit=limit))
    if ids:
        return ids
    runs = re.findall(r"[A-Za-z0-9_]+|[一-鿿]+", question)
    terms: set[str] = set()
    for run in runs:
        if len(run) <= 3:
            terms.add(run)
        else:
            for n in (2, 3):
                for i in range(len(run) - n + 1):
                    terms.add(run[i:i + n])
    scores: dict[str, int] = {}
    for t in terms:
        if len(t) < 2:
            continue
        for eid in _ids_from_rowids(db.search_rowids(t, limit=50)):
            scores[eid] = scores.get(eid, 0) + 1
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [eid for eid, _ in ranked[:limit]]


def _retrieve_for_ask(question: str, limit: int = 15,
                      scope: tuple[str, str] | None = None) -> list[dict]:
    """FTS top8 + 向量余弦 top8（嵌入已配置时），合并去重后按
    score = 0.7·cos + 0.3·0.5^(age_days/14) 重排（两周半衰期）；时间窗内命中加权 2。
    返回 [{row, score, cos, is_fts}] 按 score 倒序，最多 limit 条。"""
    fts_ids = set(_ask_fts_ids(question, limit=8))
    ids: list[str] = list(fts_ids)

    vec = None
    if ai_mod.embed_configured():
        vec = ai_mod.embed_text(question)
        if vec:
            for entry_id, _score in ai_mod.semantic_topk(vec, k=8):
                if entry_id not in fts_ids:
                    ids.append(entry_id)
    if scope:
        # 时间问题不能先从全库取 Top-K 再过滤：目标月份的记录可能根本没进候选。
        # 直接补入窗口内最近的记录，保证“上个月做了什么”在没有关键词命中时也能回答。
        scoped_rows = db.q(
            "SELECT id FROM entries WHERE deleted_at IS NULL AND exclude_from_ai=0 "
            "AND substr(occurred_at,1,10)>=? AND substr(occurred_at,1,10)<=? "
            "ORDER BY occurred_at DESC LIMIT 80",
            scope,
        )
        known = set(ids)
        for row in scoped_rows:
            if row["id"] not in known:
                ids.append(row["id"])
                known.add(row["id"])
    if not ids:
        return []

    placeholders = ",".join("?" for _ in ids)
    rows = db.q(
        f"SELECT e.*, v.vector_json FROM entries e "
        f"LEFT JOIN entry_embeddings v ON v.entry_id=e.id "
        f"WHERE e.deleted_at IS NULL AND e.exclude_from_ai=0 AND e.id IN ({placeholders})",
        tuple(ids),
    )
    today = date.today()
    ranked = []
    for r in rows:
        cos = 0.0
        if vec is not None and r["vector_json"]:
            try:
                cos = ai_mod.cosine(vec, json.loads(r["vector_json"]))
            except ValueError:
                cos = 0.0
        elif r["id"] in fts_ids:
            cos = 0.5  # 无向量时的 FTS 命中给个中性分，参与重排
        try:
            age_days = max(0, (today - date.fromisoformat(r["occurred_at"][:10])).days)
        except ValueError:
            age_days = 0
        score = 0.7 * cos + 0.3 * (0.5 ** (age_days / 14))
        day = r["occurred_at"][:10]
        if scope and scope[0] <= day <= scope[1]:
            score *= 2.0  # 用户显式给了时间范围：窗内命中强加权（盖得过新旧衰减）
        ranked.append({"row": r, "score": score, "cos": cos, "is_fts": r["id"] in fts_ids})
    ranked.sort(key=lambda x: x["score"], reverse=True)
    if scope:
        # 时间范围是用户问题的硬约束，不让范围外的旧记录混进答案。
        scoped = [item for item in ranked
                  if scope[0] <= item["row"]["occurred_at"][:10] <= scope[1]]
        return scoped[:limit]
    return ranked[:limit]


def _ask_payload(question: str) -> dict:
    question = (question or "").strip()
    if not question:
        raise HTTPException(400, "问题不能为空")
    if not ai_mod.strong_configured():
        raise HTTPException(400, "请先在设置页配置 AI 接口")
    # 先做不依赖模型的明确日期解析，再让 fast 解析“上周/最近 N 天”等自然表达。
    # 这样“2026年8月15日/上个月”不会因 fast 暂时不可用而丢失硬时间约束。
    ask_today = datetime.now().astimezone().date()
    scope = ai_mod.parse_explicit_time_scope(question, ask_today)
    if scope is None and _any_ai_configured():
        scope = ai_mod.parse_time_scope(question, ask_today)
    ranked = _retrieve_for_ask(question, scope=scope)
    if not ranked:
        return {"answer": "没有找到相关记录，换个问法试试？", "refs": []}
    # CRAG 拒答：能算余弦时 top <0.35 且无 FTS 命中 → 不编，温柔拒答
    if ai_mod.embed_configured() and ranked[0]["cos"] < 0.35 and not any(r["is_fts"] for r in ranked):
        logx.log("问小满", f"问题「{question[:20]}」，相关度太低，拒答")
        return {"answer": "记录里没找到相关内容，换个问法试试？", "refs": []}
    hits = [r["row"] for r in ranked]
    logx.log("问小满", f"问题「{question[:20]}」，命中 {len(hits)} 条记录"
                     + (f"（时间窗 {scope[0]}~{scope[1]}）" if scope else ""))
    temporal_context = ""
    if scope:
        # 时间范围问题也需要按窗口末日重建当时状态，避免把今天的人物/关系倒推回过去。
        try:
            temporal_context = temporal.prompt_context(scope[1])
        except ValueError:
            temporal_context = ""
    try:
        answer, entry_ids = ai_mod.ask(question, hits, temporal_context=temporal_context)
    except ai_mod.AIError as e:
        raise HTTPException(502, str(e))
    by_id = {r["id"]: r for r in hits}
    refs = [
        {"id": i, "occurred_at": by_id[i]["occurred_at"], "title": by_id[i]["title"]}
        for i in entry_ids if i in by_id
    ]
    return {"answer": answer, "refs": refs}


@app.post("/api/ask")
def ask(payload: dict = Body(...)):
    return _ask_payload(str((payload or {}).get("question") or ""))


@app.get("/api/entries/{entry_id}/links")
def entry_links(entry_id: str):
    """「可能相关」：该记录的隐性关联提案（pending+confirmed，含目标标题/类型/reason/score）。"""
    _get_entry_or_404(entry_id)
    from . import links as links_mod
    return {"items": links_mod.links_for_entry(entry_id)}


@app.post("/api/links/{lid}/confirm")
def link_confirm(lid: str):
    from . import links as links_mod
    try:
        return links_mod.confirm_link(lid)
    except ValueError as e:
        if str(e) == "not found":
            raise HTTPException(404, "关联提案不存在")
        raise HTTPException(400, "这条提案已经处理过了")


@app.post("/api/links/{lid}/dismiss")
def link_dismiss(lid: str):
    from . import links as links_mod
    try:
        return links_mod.dismiss_link(lid)
    except ValueError as e:
        if str(e) == "not found":
            raise HTTPException(404, "关联提案不存在")
        raise HTTPException(400, "这条提案已经处理过了")


@app.get("/api/entries/{entry_id}/related")
def related_entries(entry_id: str, limit: int = Query(3, ge=1, le=20)):
    _get_entry_or_404(entry_id)
    vec_row = db.q1("SELECT vector_json FROM entry_embeddings WHERE entry_id=?", (entry_id,))
    if vec_row is None:
        return {"items": []}
    try:
        vec = json.loads(vec_row["vector_json"])
    except ValueError:
        return {"items": []}
    top = ai_mod.semantic_topk(vec, k=limit, exclude_id=entry_id)
    if not top:
        return {"items": []}
    ids = [eid for eid, _ in top]
    placeholders = ",".join("?" for _ in ids)
    rows = db.q(
        f"SELECT * FROM entries WHERE id IN ({placeholders}) "
        "AND deleted_at IS NULL AND exclude_from_ai=0",
        tuple(ids),
    )
    order = {eid: i for i, eid in enumerate(ids)}
    rows.sort(key=lambda r: order.get(r["id"], 999))
    items = []
    for r in rows:
        try:
            tags = json.loads(r["tags"] or "[]")
        except ValueError:
            tags = []
        items.append({
            "id": r["id"],
            "occurred_at": r["occurred_at"],
            "title": r["title"],
            "summary": r["summary"],
            "category": r["category"],
            "tags": tags,
        })
    return {"items": items}


MUSE_STATIC = [
    "今天有什么值得记下的小事？",
    "今天遇到的最大挑战是什么？",
    "今天学到了什么新东西？",
    "有没有一个瞬间觉得自己有进步？",
    "今天解决了什么问题？怎么解决的？",
    "今天和谁聊过？有什么启发？",
    "今天最费劲的事是什么？记下来会轻松点",
    "如果给今天贴一个标签，会是什么？",
    "今天有什么想做却没做成的事？",
    "睡前花两分钟，记一笔今天的收获吧",
]


@app.get("/api/muse")
def muse():
    today = date.today().isoformat()
    cached = db.get_setting("muse_cache")
    if cached:
        try:
            c = json.loads(cached)
            if c.get("date") == today and c.get("text"):
                return {"text": c["text"], "personalized": bool(c.get("personalized"))}
        except ValueError:
            pass

    text, personalized = None, False
    if _any_ai_configured():
        rows = db.q(
            "SELECT title, summary, content FROM entries WHERE deleted_at IS NULL "
            "AND exclude_from_ai=0 AND substr(occurred_at,1,10) >= ? "
            "ORDER BY occurred_at DESC LIMIT 10",
            ((date.today() - timedelta(days=3)).isoformat(),),
        )
        if rows:
            digest = "\n".join(
                "- " + (r["summary"] or r["title"] or (r["content"] or "")[:40]).strip()
                for r in rows
            )
            text = ai_mod.generate_muse(digest)
            personalized = bool(text)
    if not text:
        text = MUSE_STATIC[date.today().toordinal() % len(MUSE_STATIC)]
        personalized = False
    db.set_setting("muse_cache", json.dumps(
        {"date": today, "text": text, "personalized": personalized}, ensure_ascii=False))
    return {"text": text, "personalized": personalized}


# ---------------- 上下文 / 日历 / 统计 / 搜索 ----------------

@app.get("/api/context/preview")
def context_preview(
    lat: float | None = Query(None),
    lon: float | None = Query(None),
    date: str | None = Query(None),
):
    if not _weather_enabled():
        return {"location_name": "公司", "weather": None}
    day = _parse_date(date) if date else datetime.now().astimezone().date().isoformat()
    location_name = ""
    if lat is None or lon is None:
        city = db.get_setting("default_city")
        if city:
            geo = weather_mod.geocode_city(city)
            if geo:
                lat, lon, location_name = geo
    if lat is None or lon is None:
        return {"location_name": "", "weather": None}
    if not location_name:
        location_name = weather_mod.reverse_geocode(lat, lon, db.get_setting("amap_key")) or ""
    return {"location_name": location_name, "weather": weather_mod.fetch_weather(lat, lon, day)}


def _calendar_payload(month: str) -> dict:
    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        raise HTTPException(400, "month 格式应为 YYYY-MM")
    year, mon = int(month[:4]), int(month[5:])
    if not 1 <= mon <= 12:
        raise HTTPException(400, "month 格式应为 YYYY-MM")
    start = date(year, mon, 1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    rows = db.q(
        "SELECT substr(occurred_at,1,10) AS d, COUNT(*) AS c FROM entries "
        "WHERE deleted_at IS NULL AND substr(occurred_at,1,10) BETWEEN ? AND ? GROUP BY d",
        (start.isoformat(), end.isoformat()),
    )
    return {"days": {r["d"]: r["c"] for r in rows}}


@app.get("/api/calendar")
def calendar(month: str = Query(...)):
    return _calendar_payload(month)


@app.get("/api/stats/overview")
def stats_overview():
    return stats_mod.overview()


class SearchIn(BaseModel):
    q: str
    limit: int = 50


@app.post("/api/search")
def search(body: SearchIn):
    return search_impl(body.q, body.limit)


# ---------------- 设置 ----------------

def _weather_enabled() -> bool:
    return db.get_setting("weather_enabled", "true") != "false"


LOG_PRESETS_DEFAULT = ["工作内容", "挑战", "解决办法", "提升", "手记", "见识", "学习"]


def _log_presets() -> list:
    raw = db.get_setting("log_presets")
    if raw:
        try:
            v = json.loads(raw)
            if isinstance(v, list) and v:
                return [str(x) for x in v]
        except ValueError:
            pass
    return list(LOG_PRESETS_DEFAULT)


def _bool_setting(v) -> str:
    enabled = (str(v).strip().lower() in ("true", "1", "yes", "on")) if isinstance(v, str) else bool(v)
    return "true" if enabled else "false"


AI_PROVIDER_SLOTS = {"strong", "fast", "embed"}
AI_PROVIDER_EFFORTS = {"", "auto", "none", "low", "medium", "high", "xhigh", "max"}


def _ai_provider_public(row) -> dict:
    return {"id": row["id"], "name": row["name"], "slot": row["slot"],
            "base_url": row["base_url"], "model": row["model"],
            "vision": bool(row["vision"]), "effort": row["effort"] or "auto",
            "enabled": bool(row["enabled"]),
            "is_active": db.get_setting(f"ai_active_{row['slot']}") == row["id"],
            "has_key": bool(row["api_key"]), "created_at": row["created_at"],
            "updated_at": row["updated_at"]}


def ai_providers_payload() -> dict:
    return {"items": [_ai_provider_public(r) for r in db.ai_provider_rows()],
            "active": {s: db.get_setting(f"ai_active_{s}") for s in sorted(AI_PROVIDER_SLOTS)}}


def _provider_values(p: dict, old=None) -> dict:
    slot = str(p.get("slot") or (old["slot"] if old else "strong")).strip().lower()
    if slot not in AI_PROVIDER_SLOTS: raise HTTPException(400, "slot 只能是 strong / fast / embed")
    base = str(p.get("base_url") or (old["base_url"] if old else "")).strip().rstrip("/")
    model = str(p.get("model") or (old["model"] if old else "")).strip()
    name = str(p.get("name") or (old["name"] if old else "供应商")).strip()[:80]
    if not base or not model or not name: raise HTTPException(400, "请填写供应商名称、Base URL 和模型名")
    key = str(p.get("key", p.get("api_key", "")) or "").strip()
    if old is not None and not key and not p.get("clear_key"): key = old["api_key"]
    effort = str(p.get("effort") or (old["effort"] if old else "auto")).lower()
    if effort not in AI_PROVIDER_EFFORTS: raise HTTPException(400, "思考深度不合法")
    vision_value = p.get("vision", bool(old["vision"]) if old else False)
    enabled_value = p.get("enabled", bool(old["enabled"]) if old else True)
    return {"name": name, "slot": slot, "base_url": base, "api_key": key,
            "model": model, "vision": _bool_setting(vision_value) == "true",
            "effort": effort, "enabled": _bool_setting(enabled_value) == "true"}


def _sync_provider(row, *, make_active: bool = False) -> None:
    slot = row["slot"]
    pref = {"strong": ("ai_base_url", "ai_key", "ai_model", "ai_effort"),
            "fast": ("ai_fast_base_url", "ai_fast_key", "ai_fast_model", "ai_fast_effort"),
            "embed": ("embed_base_url", "embed_key", "embed_model", None)}[slot]
    values = dict(zip(pref[:3], (row["base_url"], row["api_key"], row["model"])))
    if pref[3]: values[pref[3]] = row["effort"] or "auto"
    if slot == "strong": values["ai_vision"] = "true" if row["vision"] else "false"
    if make_active: values[f"ai_active_{slot}"] = row["id"]
    db.set_settings(values)


def _provider_configured(row) -> bool:
    return bool(row and row["enabled"] and row["base_url"] and row["api_key"] and row["model"])


def _clear_provider_slot(slot: str) -> None:
    keys = {"strong": ("ai_base_url", "ai_key", "ai_model"),
            "fast": ("ai_fast_base_url", "ai_fast_key", "ai_fast_model"),
            "embed": ("embed_base_url", "embed_key", "embed_model")}[slot]
    values = {key: "" for key in keys}
    if slot == "strong":
        values.update({"ai_effort": "auto", "ai_vision": "false"})
    elif slot == "fast":
        values["ai_fast_effort"] = "auto"
    values[f"ai_active_{slot}"] = ""
    db.set_settings(values)


def _use_provider(row) -> None:
    """切换当前供应商；嵌入向量空间变化时同步作废旧向量。"""
    embed_changed = row["slot"] == "embed" and (
        db.get_setting("embed_base_url") != row["base_url"]
        or db.get_setting("embed_model") != row["model"])
    _sync_provider(row, make_active=True)
    if embed_changed:
        db.execute("DELETE FROM entry_embeddings")
        db.execute("UPDATE knowledge SET vector_json=NULL WHERE vector_json IS NOT NULL")


def _use_fallback_provider(slot: str, exclude_id: str = "") -> None:
    row = db.q1(
        "SELECT * FROM ai_providers WHERE slot=? AND enabled=1 AND id<>? "
        "AND trim(base_url)<>'' AND trim(api_key)<>'' AND trim(model)<>'' "
        "ORDER BY updated_at DESC, created_at DESC LIMIT 1",
        (slot, exclude_id),
    )
    if row:
        _use_provider(row)
    else:
        embed_had_model = slot == "embed" and bool(db.get_setting("embed_model"))
        _clear_provider_slot(slot)
        if embed_had_model:
            db.execute("DELETE FROM entry_embeddings")
            db.execute("UPDATE knowledge SET vector_json=NULL WHERE vector_json IS NOT NULL")


def _sync_legacy_ai_edits(payload: dict) -> None:
    """兼容旧设置表单：旧 KV 被修改时，同步更新当前供应商记录。"""
    groups = {
        "strong": {"ai_base_url", "ai_key", "ai_model", "ai_vision", "ai_effort"},
        "fast": {"ai_fast_base_url", "ai_fast_key", "ai_fast_model", "ai_fast_effort"},
        "embed": {"embed_base_url", "embed_key", "embed_model"},
    }
    names = {"strong": "当前大模型", "fast": "当前小模型", "embed": "当前嵌入模型"}
    for slot, keys in groups.items():
        if not keys.intersection(payload):
            continue
        provider_id = db.get_setting(f"ai_active_{slot}")
        row = db.ai_provider_row(provider_id) if provider_id else None
        if slot == "strong":
            values = {"base_url": db.get_setting("ai_base_url"), "api_key": db.get_setting("ai_key"),
                      "model": db.get_setting("ai_model"), "vision": db.get_setting("ai_vision") == "true",
                      "effort": db.get_setting("ai_effort") or "auto"}
        elif slot == "fast":
            values = {"base_url": db.get_setting("ai_fast_base_url"), "api_key": db.get_setting("ai_fast_key"),
                      "model": db.get_setting("ai_fast_model"), "vision": False,
                      "effort": db.get_setting("ai_fast_effort") or "auto"}
        else:
            values = {"base_url": db.get_setting("embed_base_url"), "api_key": db.get_setting("embed_key"),
                      "model": db.get_setting("embed_model"), "vision": False, "effort": "auto"}
        if row:
            db.ai_provider_update(provider_id, **values)
        elif values["base_url"] and values["api_key"] and values["model"]:
            provider_id = db.new_id()
            db.ai_provider_insert(provider_id=provider_id, name=names[slot], slot=slot, **values)
            db.set_setting(f"ai_active_{slot}", provider_id)


def settings_payload() -> dict:
    ai_key = db.get_setting("ai_key")
    amap_key = db.get_setting("amap_key")
    agent_token = db.get_setting("agent_token")
    return {
        "ai_base_url": db.get_setting("ai_base_url"),
        "ai_model": db.get_setting("ai_model"),
        "has_ai_key": bool(ai_key),
        "ai_vision": db.get_setting("ai_vision") == "true",
        "ai_effort": db.get_setting("ai_effort"),
        "ai_fast_effort": db.get_setting("ai_fast_effort"),
        "ai_fast_base_url": db.get_setting("ai_fast_base_url"),
        "ai_fast_model": db.get_setting("ai_fast_model"),
        "has_ai_fast_key": bool(db.get_setting("ai_fast_key")),
        "embed_base_url": db.get_setting("embed_base_url"),
        "embed_model": db.get_setting("embed_model"),
        "has_embed_key": bool(db.get_setting("embed_key")),
        "weather_enabled": _weather_enabled(),
        "weather_city_only": db.get_setting("weather_city_only") == "true",
        "log_presets": _log_presets(),
        "has_push_key": bool(db.get_setting("push_key")),
        "push_enabled": db.get_setting("push_enabled", "true") != "false",
        "has_access_key": bool(db.get_setting("access_key")),
        "ai_rewrite_model": db.get_setting("ai_rewrite_model"),
        "auto_report_enabled": db.get_setting("auto_report_enabled", "true") != "false",
        "circle_enabled": circle_mod.enabled(),
        "circle_auto_confirm": db.get_setting("circle_auto_confirm", "1") not in ("0", "false"),
        "link_discovery": db.get_setting("link_discovery", "1") not in ("0", "false"),
        "category_ai": db.get_setting("category_ai", "1") not in ("0", "false"),
        "backup_dir": db.get_setting("backup_dir"),
        "last_backup_at": db.get_setting("last_backup_at"),
        "default_city": db.get_setting("default_city"),
        "has_amap_key": bool(amap_key),
        "has_agent_token": bool(agent_token),
        "agent_token": agent_token,
        "ai_providers": ai_providers_payload(),
    }


SETTINGS_STR_KEYS = ("ai_base_url", "ai_key", "ai_model", "default_city", "amap_key",
                     "agent_token", "ai_fast_base_url", "ai_fast_key", "ai_fast_model",
                     "embed_base_url", "embed_key", "embed_model", "backup_dir", "push_key",
                     "access_key", "ai_rewrite_model", "ai_effort", "ai_fast_effort")
SETTINGS_BOOL_KEYS = ("ai_vision", "weather_enabled", "push_enabled", "auto_report_enabled",
                      "weather_city_only", "circle_enabled",
                      "circle_auto_confirm", "link_discovery", "category_ai")


@app.get("/api/settings")
def get_settings():
    return settings_payload()


@app.get("/api/settings/ai-providers")
def get_ai_providers():
    return ai_providers_payload()


@app.post("/api/settings/ai-providers")
def create_ai_provider(payload: dict = Body(...)):
    values = _provider_values(payload)
    provider_id = db.new_id()
    db.ai_provider_insert(provider_id=provider_id, **values)
    row = db.ai_provider_row(provider_id)
    active_key = f"ai_active_{row['slot']}"
    if not db.get_setting(active_key) and _provider_configured(row):
        _use_provider(row)
    logx.log("新增 AI 供应商", f"{row['name']}（{row['slot']}）")
    return _ai_provider_public(db.ai_provider_row(provider_id))


@app.patch("/api/settings/ai-providers/{provider_id}")
def update_ai_provider(provider_id: str, payload: dict = Body(...)):
    old = db.ai_provider_row(provider_id)
    if not old:
        raise HTTPException(404, "供应商不存在")
    was_active = db.get_setting(f"ai_active_{old['slot']}") == provider_id
    values = _provider_values(payload, old)
    db.ai_provider_update(provider_id, **values)
    row = db.ai_provider_row(provider_id)
    if was_active and row["slot"] != old["slot"]:
        _use_fallback_provider(old["slot"], provider_id)
        was_active = False
    if was_active and _provider_configured(row):
        embed_changed = row["slot"] == "embed" and (
            row["base_url"] != old["base_url"] or row["model"] != old["model"])
        _sync_provider(row)
        if embed_changed:
            db.execute("DELETE FROM entry_embeddings")
            db.execute("UPDATE knowledge SET vector_json=NULL WHERE vector_json IS NOT NULL")
    elif was_active:
        _use_fallback_provider(row["slot"], provider_id)
    elif not db.get_setting(f"ai_active_{row['slot']}") and _provider_configured(row):
        _use_provider(row)
    logx.log("修改 AI 供应商", row["name"])
    return _ai_provider_public(row)


@app.delete("/api/settings/ai-providers/{provider_id}")
def delete_ai_provider(provider_id: str):
    row = db.ai_provider_row(provider_id)
    if not row:
        raise HTTPException(404, "供应商不存在")
    slot = row["slot"]
    was_active = db.get_setting(f"ai_active_{slot}") == provider_id
    db.ai_provider_delete(provider_id)
    if was_active:
        _use_fallback_provider(slot, provider_id)
    logx.log("删除 AI 供应商", row["name"])
    return ai_providers_payload()


@app.post("/api/settings/ai-providers/test")
def test_unsaved_ai_provider(payload: dict = Body(...)):
    # 编辑时允许 Key 留空：沿用该供应商已保存的 Key，但始终使用当前表单里的
    # Base URL / 模型名，避免用户改了地址却误测旧配置。
    old = None
    provider_id = str((payload or {}).get("provider_id") or "").strip()
    if provider_id:
        old = db.ai_provider_row(provider_id)
        if old is None:
            raise HTTPException(404, "供应商不存在")
    values = _provider_values(payload, old)
    if not values["api_key"]:
        return {"ok": False, "message": "请填写 API Key"}
    if values["slot"] == "embed":
        ok, message = ai_mod.test_embed_with(values["base_url"], values["api_key"], values["model"])
    else:
        ok, message = ai_mod.test_chat_with(values["base_url"], values["api_key"], values["model"])
    return {"ok": ok, "message": message}


@app.post("/api/settings/ai-providers/{provider_id}/test")
def test_saved_ai_provider(provider_id: str):
    row = db.ai_provider_row(provider_id)
    if not row:
        raise HTTPException(404, "供应商不存在")
    if not row["api_key"]:
        return {"ok": False, "message": "该供应商尚未填写 API Key", "id": provider_id}
    if row["slot"] == "embed":
        ok, message = ai_mod.test_embed_with(row["base_url"], row["api_key"], row["model"])
    else:
        ok, message = ai_mod.test_chat_with(row["base_url"], row["api_key"], row["model"])
    logx.log("测试 AI 供应商", f"{row['name']}：{'成功' if ok else message}")
    return {"ok": ok, "message": message, "id": provider_id}


@app.post("/api/settings/ai-providers/{provider_id}/activate")
def activate_ai_provider(provider_id: str):
    row = db.ai_provider_row(provider_id)
    if not row:
        raise HTTPException(404, "供应商不存在")
    if not row["enabled"]:
        raise HTTPException(400, "请先启用该供应商")
    if not _provider_configured(row):
        raise HTTPException(400, "该供应商配置不完整")
    slot = row["slot"]
    _use_provider(row)
    logx.log("切换 AI 供应商", f"{row['name']}（{slot}）")
    return ai_providers_payload()


@app.put("/api/settings")
def put_settings(payload: dict = Body(...)):
    # 嵌入模型/接口更换：向量空间不兼容，需清空重建（后台滴灌会自动用新模型重算）
    embed_changed = False
    for key in ("embed_model", "embed_base_url"):
        if key in payload:
            if db.get_setting(key) != str(payload[key] or "").strip():
                embed_changed = True

    for key in SETTINGS_STR_KEYS:
        if key in ("ai_effort", "ai_fast_effort"):
            continue  # 下面单独校验
        if key in payload:
            db.set_setting(key, str(payload[key] or "").strip())
    if "ai_vision" in payload:
        db.set_setting("ai_vision", _bool_setting(payload["ai_vision"]))
    if "weather_enabled" in payload:
        db.set_setting("weather_enabled", _bool_setting(payload["weather_enabled"]))
    if "weather_city_only" in payload:
        db.set_setting("weather_city_only", _bool_setting(payload["weather_city_only"]))
    if "push_enabled" in payload:
        db.set_setting("push_enabled", _bool_setting(payload["push_enabled"]))
    if "log_presets" in payload:
        v = payload["log_presets"]
        if not isinstance(v, list):
            raise HTTPException(400, "log_presets 应为字符串数组")
        cleaned = []
        for x in v:
            x = str(x).strip()[:8]
            if x and x not in cleaned:
                cleaned.append(x)
        db.set_setting("log_presets", json.dumps(cleaned[:12] or LOG_PRESETS_DEFAULT,
                                                 ensure_ascii=False))
    if "auto_report_enabled" in payload:
        db.set_setting("auto_report_enabled", _bool_setting(payload["auto_report_enabled"]))
    if "circle_enabled" in payload:
        db.set_setting("circle_enabled", _bool_setting(payload["circle_enabled"]))
    for key in ("circle_auto_confirm", "link_discovery", "category_ai"):
        if key in payload:
            db.set_setting(key, _bool_setting(payload[key]))
    for key in ("ai_effort", "ai_fast_effort"):
        if key in payload:
            v = str(payload[key] or "").strip()
            if v not in ("", "auto", "none", "low", "medium", "high", "xhigh", "max"):
                raise HTTPException(400, "思考深度只能是 auto / none / low / medium / high / xhigh / max")
            db.set_setting(key, v)
    if payload.get("regenerate_agent_token"):
        db.set_setting("agent_token", secrets.token_urlsafe(24))
    if "access_key" in payload:
        # 口令哈希存储：数据库与备份里不留明文；空串 = 关闭
        v = str(payload["access_key"] or "").strip()
        db.set_setting("access_key", _hash_access_key(v) if v else "")

    _sync_legacy_ai_edits(payload)

    result = settings_payload()
    if embed_changed:
        db.execute("DELETE FROM entry_embeddings")
        db.execute("UPDATE knowledge SET vector_json=NULL WHERE vector_json IS NOT NULL")
        result["embed_rebuilt"] = True

    # 只记改了哪些键名，绝不记录任何值（Key/Token 保密）
    changed = [k for k in payload
               if k in SETTINGS_STR_KEYS or k in SETTINGS_BOOL_KEYS
               or k in ("log_presets", "regenerate_agent_token")]
    if changed:
        logx.log("设置已更新", "改了 " + "、".join(changed))
    if "weather_enabled" in payload:
        logx.log("天气与定位已开启" if _weather_enabled() else "天气与定位已关闭")
    if "ai_vision" in payload:
        logx.log("视觉识别已开启" if db.get_setting("ai_vision") == "true" else "视觉识别已关闭")
    if "access_key" in payload:
        logx.log("访问口令已开启" if db.get_setting("access_key") else "访问口令已关闭")
    return result


@app.post("/api/auth")
def auth_login(payload: dict = Body(...)):
    key = db.get_setting("access_key")
    if not key:
        return {"ok": True}  # 未设锁，无需口令
    password = str((payload or {}).get("password") or "")
    if not _verify_access_key(password, key):
        logx.log("有人输错了口令")
        raise HTTPException(401, "口令不对")
    if not key.startswith("p$"):
        # 旧格式口令校验通过，顺手升级为带盐哈希
        db.set_setting("access_key", _hash_access_key(password))
    logx.log("已用口令解锁")
    resp = JSONResponse({"ok": True})
    resp.set_cookie("xm_auth", _access_token(key), max_age=30 * 24 * 3600,
                    httponly=True, samesite="lax")
    return resp


@app.post("/api/auth/logout")
def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("xm_auth")
    return resp


@app.post("/api/settings/test-push")
def test_push():
    if not db.get_setting("push_key").strip():
        raise HTTPException(400, "请先配置推送 Key")
    ok, message = scheduler.send_push("小满推送测试", "这是一条测试推送，收到说明配置成功。")
    return {"ok": ok, "message": message}


@app.post("/api/settings/test-ai")
def test_ai(payload: dict | None = Body(None)):
    result = _test_ai_impl(payload)
    slot = ((payload or {}).get("slot", "strong"))
    label = {"strong": "大模型", "fast": "小模型", "embed": "嵌入模型"}.get(slot, slot)
    logx.log(f"测试连接（{label}）", "成功" if result.get("ok") else result.get("message", "失败"))
    return result


def _test_ai_impl(payload: dict | None):
    p = payload or {}
    slot = p.get("slot", "strong")
    if slot not in ("strong", "fast", "embed"):
        raise HTTPException(400, "slot 只能是 strong / fast / embed")
    # 表单里有临时配置：不用先保存，直接测填着的内容
    base_url = str(p.get("base_url") or "").strip()
    model = str(p.get("model") or "").strip()
    key = str(p.get("key") or "").strip()
    if base_url or model:
        if not (base_url and model):
            return {"ok": False, "message": "请把 Base URL 和模型名都填上再测试"}
        if not key:
            fallbacks = {"strong": ["ai_key"], "fast": ["ai_fast_key", "ai_key"], "embed": ["embed_key", "ai_key"]}[slot]
            for name in fallbacks:
                key = db.get_setting(name).strip()
                if key:
                    break
        if not key:
            return {"ok": False, "message": "还没填 Key"}
        if slot == "embed":
            ok, msg = ai_mod.test_embed_with(base_url, key, model)
        else:
            ok, msg = ai_mod.test_chat_with(base_url, key, model)
        return {"ok": ok, "message": msg}
    if slot == "embed":
        if not ai_mod.embed_configured():
            return {"ok": False, "message": "嵌入模型未配置"}
        vec = ai_mod.embed_text("测试")
        if vec:
            return {"ok": True, "message": "连接成功，嵌入向量返回正常"}
        return {"ok": False, "message": "嵌入接口调用失败，请检查配置"}
    ok, message = ai_mod.test_ai(slot)
    return {"ok": ok, "message": message}


def _lan_ips() -> list[str]:
    """尽力列出本机局域网 IPv4，私网段优先（192.168 > 10 > 172.16-31）。"""
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass

    def rank(ip: str) -> int:
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if re.match(r"^172\.(1[6-9]|2\d|3[01])\.", ip):
            return 2
        return 3

    ips.sort(key=rank)
    return ips


@app.get("/api/server-info")
def server_info(request: Request):
    """给前端展示手机连接地址用：本机局域网 IP 列表与推荐访问 URL。"""
    port = request.url.port or 52122
    ips = _lan_ips()
    return {"port": port, "ips": ips, "url": f"http://{ips[0]}:{port}" if ips else ""}


@app.get("/api/logs/tail")
def logs_tail(n: int = Query(50, ge=1, le=200)):
    """设置页日志查看器：返回 app.log 最后 n 行（倒序）。文件不存在返回空。"""
    path = db.LOGS_DIR / "app.log"
    if not path.exists():
        return {"lines": []}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        return {"lines": [l for l in lines if l.strip()][-n:][::-1]}
    except Exception:
        return {"lines": []}


# ---------------- 报告 ----------------

class ReportIn(BaseModel):
    type: str
    date: str


@app.post("/api/reports/generate", status_code=201)
def generate_report(body: ReportIn):
    if body.type not in REPORT_TYPES:
        raise HTTPException(400, "type 只能是 daily / weekly / monthly")
    _parse_date(body.date)
    label = {"daily": "日报", "weekly": "周报", "monthly": "月报"}[body.type]
    logx.log(f"开始生成{label}", f"基准日期 {body.date}")
    t0 = time.time()
    try:
        rep = ai_mod.generate_report(body.type, body.date)
    except ai_mod.NoDataError:
        logx.log(f"{label}未生成", "该时间段没有记录")
        raise HTTPException(422, "该时间段没有记录")
    except ai_mod.AIError as e:
        logx.log(f"{label}生成失败", str(e))
        raise HTTPException(502, str(e))
    hl = len(rep.get("new_highlights") or [])
    extra = f"，新圈高光 {hl} 颗" if hl else ""
    logx.log(f"{label}已生成",
             f"覆盖 {rep['entry_count']} 条记录，提取知识 {rep.get('knowledge_extracted', 0)} 条{extra}，用时 {int(time.time() - t0)} 秒")
    return rep


def _reports_list_payload(type: str | None, limit: int) -> dict:
    clauses, params = [], []
    if type:
        if type not in REPORT_TYPES:
            raise HTTPException(400, "type 只能是 daily / weekly / monthly")
        clauses.append("type=?")
        params.append(type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.q(
        f"SELECT * FROM reports {where} ORDER BY created_at DESC LIMIT ?",
        tuple(params) + (limit,),
    )
    return {
        "items": [
            {
                "id": r["id"],
                "type": r["type"],
                "period_start": r["period_start"],
                "period_end": r["period_end"],
                "created_at": r["created_at"],
                "model": r["model"],
                "entry_count": r["entry_count"],
            }
            for r in rows
        ]
    }


@app.get("/api/reports")
def list_reports(type: str | None = Query(None), limit: int = Query(20, ge=1, le=200)):
    return _reports_list_payload(type, limit)


@app.get("/api/reports/{report_id}")
def get_report(report_id: str):
    row = db.q1("SELECT * FROM reports WHERE id=?", (report_id,))
    if row is None:
        raise HTTPException(404, "报告不存在")
    return ai_mod.report_detail(row)


@app.delete("/api/reports/{report_id}")
def delete_report(report_id: str):
    row = db.q1("SELECT id FROM reports WHERE id=?", (report_id,))
    if row is None:
        raise HTTPException(404, "报告不存在")
    db.execute("DELETE FROM reports WHERE id=?", (report_id,))
    return {"ok": True}


# ---------------- 知识库 ----------------

def _knowledge_list_payload(status: str | None, type: str | None) -> dict:
    clauses, params = [], []
    if status:
        clauses.append("status=?")
        params.append(status)
    if type:
        clauses.append("type=?")
        params.append(type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.q(f"SELECT * FROM knowledge {where} ORDER BY created_at DESC", tuple(params))
    return {"items": [_knowledge_item(r) for r in rows]}


@app.get("/api/knowledge")
def list_knowledge(status: str | None = Query(None), type: str | None = Query(None)):
    return _knowledge_list_payload(status, type)


def _knowledge_item(row) -> dict:
    try:
        evidence = json.loads(row["evidence_json"] or "[]") if "evidence_json" in row.keys() else []
    except (TypeError, ValueError):
        evidence = []
    if not isinstance(evidence, list):
        evidence = []
    # 旧版只有单个 entry_id，新版 evidence_json 可记录多条来源。统一校验来源
    # 是否真实存在，再交给前端生成链接，绝不把 NULL/失效 ID 拼成 #/entry/null。
    candidate_ids = []
    primary_id = str(row["entry_id"] or "").strip()
    if primary_id:
        candidate_ids.append(primary_id)
    quote_by_id = {}
    for item in evidence:
        if not isinstance(item, dict):
            continue
        eid = str(item.get("entry_id") or "").strip()
        if eid and eid not in candidate_ids:
            candidate_ids.append(eid)
        if eid and item.get("quote"):
            quote_by_id.setdefault(eid, str(item.get("quote") or "").strip()[:240])
    sources = []
    if candidate_ids:
        marks = ",".join("?" for _ in candidate_ids)
        source_rows = db.q(
            f"SELECT id, occurred_at, title, summary FROM entries WHERE id IN ({marks})",
            tuple(candidate_ids),
        )
        by_id = {r["id"]: r for r in source_rows}
        for eid in candidate_ids:
            source = by_id.get(eid)
            if source is None:
                continue
            sources.append({
                "id": source["id"],
                "occurred_at": source["occurred_at"],
                "title": source["title"],
                "summary": source["summary"],
                "quote": quote_by_id.get(eid, ""),
            })
    entry_id = sources[0]["id"] if sources else None
    entry_exists = bool(sources)
    # evidence 仅保留真实存在的来源，避免 API 把历史脏数据继续传给前端。
    evidence = [
        {"entry_id": source["id"], "quote": source["quote"]}
        for source in sources if source["quote"]
    ]
    report_id = row["source_report_id"] if "source_report_id" in row.keys() else None
    report_exists = bool(report_id and db.q1("SELECT id FROM reports WHERE id=?", (report_id,)))
    return {
        "id": row["id"],
        "entry_id": entry_id if entry_exists else None,
        "source_entry_exists": entry_exists,
        "sources": sources,
        "source_report_id": report_id if report_exists else None,
        "source_report_exists": report_exists,
        "evidence": evidence,
        "type": row["type"],
        "title": row["title"],
        "content": row["content"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


def _set_knowledge_status(kid: str, status: str):
    row = db.q1("SELECT id, title FROM knowledge WHERE id=?", (kid,))
    if row is None:
        raise HTTPException(404, "知识条目不存在")
    db.execute("UPDATE knowledge SET status=? WHERE id=?", (status, kid))
    if status == "accepted":
        logx.log("知识已入库", f"《{row['title']}》")
    elif status == "rejected":
        logx.log("知识已忽略", f"《{row['title']}》")
    return {"ok": True}


@app.patch("/api/knowledge/{kid}")
def patch_knowledge(kid: str, payload: dict = Body(...)):
    row = db.q1("SELECT * FROM knowledge WHERE id=?", (kid,))
    if row is None:
        raise HTTPException(404, "知识条目不存在")
    sets, params = [], []
    if "title" in payload:
        if not isinstance(payload["title"], str):
            raise HTTPException(400, "title 应为字符串")
        sets.append("title=?")
        params.append(payload["title"].strip()[:100])
    if "content" in payload:
        if not isinstance(payload["content"], str):
            raise HTTPException(400, "content 应为字符串")
        sets.append("content=?")
        params.append(payload["content"].strip()[:2000])
    if "type" in payload:
        if payload["type"] not in ai_mod.KNOWLEDGE_TYPES:
            raise HTTPException(400, "type 只能是 experience / pitfall / case / sop / skill")
        sets.append("type=?")
        params.append(payload["type"])
    if sets:
        params.append(kid)
        db.execute(f"UPDATE knowledge SET {', '.join(sets)} WHERE id=?", tuple(params))
    updated = db.q1("SELECT * FROM knowledge WHERE id=?", (kid,))
    logx.log("知识已编辑", f"《{updated['title']}》")
    return _knowledge_item(updated)


@app.post("/api/knowledge/{kid}/accept")
def accept_knowledge(kid: str):
    return _set_knowledge_status(kid, "accepted")


@app.post("/api/knowledge/{kid}/reject")
def reject_knowledge(kid: str):
    return _set_knowledge_status(kid, "rejected")


@app.delete("/api/knowledge/{kid}")
def delete_knowledge(kid: str):
    row = db.q1("SELECT id, title FROM knowledge WHERE id=?", (kid,))
    if row is None:
        raise HTTPException(404, "知识条目不存在")
    db.execute("DELETE FROM knowledge WHERE id=?", (kid,))
    logx.log("知识已删除", f"《{row['title']}》")
    return {"ok": True}


# ---------------- 成长：目标 / 路线 / 推演 ----------------

def goals_payload() -> dict:
    row = db.q1("SELECT * FROM goals WHERE id=1")
    if row is None:
        return {"current_role": "", "target_role": "", "goal_6m": "", "goal_12m": ""}
    return {
        "current_role": row["current_role"],
        "target_role": row["target_role"],
        "goal_6m": row["goal_6m"],
        "goal_12m": row["goal_12m"],
    }


@app.get("/api/growth/goals")
def get_goals():
    return goals_payload()


@app.put("/api/growth/goals")
def put_goals(payload: dict = Body(...)):
    current = goals_payload()
    for key in ("current_role", "target_role", "goal_6m", "goal_12m"):
        if key in payload:
            current[key] = str(payload[key] or "")
    db.execute(
        "INSERT INTO goals(id, current_role, target_role, goal_6m, goal_12m, updated_at) "
        "VALUES(1,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET current_role=excluded.current_role, "
        "target_role=excluded.target_role, goal_6m=excluded.goal_6m, "
        "goal_12m=excluded.goal_12m, updated_at=excluded.updated_at",
        (current["current_role"], current["target_role"], current["goal_6m"],
         current["goal_12m"], db.now_iso()),
    )
    return goals_payload()


def roadmap_payload() -> dict:
    rows = db.q("SELECT * FROM roadmap_nodes ORDER BY horizon_days, sort_order, created_at")
    def evidence(row):
        try:
            return json.loads(row["evidence_json"] or "[]")
        except (TypeError, ValueError, KeyError):
            return []
    evidence_ids = []
    for row in rows:
        for entry_id in evidence(row):
            entry_id = str(entry_id).strip()
            if entry_id and entry_id not in evidence_ids:
                evidence_ids.append(entry_id)
    evidence_by_id = {}
    if evidence_ids:
        marks = ",".join("?" for _ in evidence_ids)
        evidence_rows = db.q(
            f"SELECT id, occurred_at, title, summary, content FROM entries "
            f"WHERE id IN ({marks}) AND deleted_at IS NULL",
            tuple(evidence_ids),
        )
        evidence_by_id = {
            r["id"]: {
                "id": r["id"],
                "occurred_at": r["occurred_at"],
                "title": r["title"],
                "summary": r["summary"],
                "excerpt": (r["summary"] or r["title"] or (r["content"] or "")[:120]).strip(),
            }
            for r in evidence_rows
        }
    def progress(row):
        try:
            return max(0, min(100, int(row["progress"] or 0))) if "progress" in row.keys() else 0
        except (TypeError, ValueError, KeyError):
            return 0
    checkins_by_node = {}
    checkin_counts = {}
    if rows:
        placeholders = ",".join("?" for _ in rows)
        checkins = db.q(
            f"SELECT * FROM roadmap_checkins WHERE node_id IN ({placeholders}) "
            "ORDER BY created_at DESC",
            tuple(r["id"] for r in rows),
        )
        for item in checkins:
            checkin_counts[item["node_id"]] = checkin_counts.get(item["node_id"], 0) + 1
            bucket = checkins_by_node.setdefault(item["node_id"], [])
            if len(bucket) < 5:
                bucket.append({
                    "id": item["id"], "progress": item["progress"],
                    "note": item["note"], "created_at": item["created_at"],
                })
    return {
        "goals": goals_payload(),
        "direction": db.get_setting("growth_direction"),
        "nodes": [
            {
                "id": r["id"],
                "title": r["title"],
                "description": r["description"],
                "horizon_days": r["horizon_days"],
                "status": r["status"],
                "sort_order": r["sort_order"],
                "evidence_entry_ids": evidence(r),
                "evidence": [evidence_by_id[str(eid)] for eid in evidence(r)
                             if str(eid) in evidence_by_id],
                "first_step": r["first_step"] if "first_step" in r.keys() else "",
                "done_when": r["done_when"] if "done_when" in r.keys() else "",
                "progress": progress(r),
                "last_checkin_at": r["last_checkin_at"] if "last_checkin_at" in r.keys() else None,
                "checkin_note": r["checkin_note"] if "checkin_note" in r.keys() else "",
                "completed_at": r["completed_at"] if "completed_at" in r.keys() else None,
                "checkins": checkins_by_node.get(r["id"], []),
                "checkin_count": checkin_counts.get(r["id"], 0),
            }
            for r in rows
        ],
    }


@app.post("/api/growth/directions")
def growth_directions():
    try:
        result = ai_mod.generate_directions()
    except ai_mod.NoDataError as e:
        raise HTTPException(422, str(e) or "记录还太少，先写几天日志再来吧")
    except ai_mod.AIError as e:
        logx.log("成长方向发现失败", str(e))
        raise HTTPException(502, str(e))
    logx.log(f"已发现 {len(result)} 个成长方向")
    return {"directions": result}


@app.get("/api/growth/roadmap")
def get_roadmap():
    return roadmap_payload()


@app.post("/api/growth/roadmap/generate")
def generate_roadmap(payload: dict | None = Body(None)):
    direction = str((payload or {}).get("direction") or "").strip()
    if not direction:
        direction = db.get_setting("growth_direction").strip()
    if not direction:
        raise HTTPException(400, "先让 AI 为你发现方向")
    try:
        ai_mod.generate_roadmap(direction)
    except ai_mod.AIError as e:
        logx.log("路线生成失败", str(e))
        raise HTTPException(502, str(e))
    db.set_setting("growth_direction", direction)
    result = roadmap_payload()
    logx.log("路线已生成", f"方向「{direction[:20]}」，{len(result['nodes'])} 个节点")
    return result


# ---------------- 成长轨迹总结（现状为主） ----------------

@app.get("/api/growth/summary")
def get_growth_summary():
    row = db.q1("SELECT * FROM growth_summaries ORDER BY created_at DESC LIMIT 1")
    return {"summary": ai_mod.summary_detail(row) if row else None}


@app.post("/api/growth/summary/generate", status_code=201)
def generate_growth_summary():
    try:
        return ai_mod.generate_growth_summary()
    except ai_mod.NoDataError as e:
        raise HTTPException(422, str(e) or "记录还太少，先写几天日志再来吧")
    except ai_mod.AIError as e:
        raise HTTPException(502, str(e))


@app.patch("/api/growth/roadmap/nodes/{node_id}")
def patch_roadmap_node(node_id: str, payload: dict = Body(...)):
    row = db.q1("SELECT * FROM roadmap_nodes WHERE id=?", (node_id,))
    if row is None:
        raise HTTPException(404, "路线节点不存在")
    sets, params = [], []
    next_status = payload.get("status") if "status" in payload else row["status"]
    if "status" in payload:
        if payload["status"] not in NODE_STATUS:
            raise HTTPException(400, "status 只能是 suggested / accepted / rejected / done")
        sets.append("status=?")
        params.append(payload["status"])
        if payload["status"] == "accepted" and "progress" not in payload and row["status"] == "done":
            sets.append("progress=?")
            params.append(0)
    for field in ("title", "description", "first_step", "done_when"):
        if field in payload:
            sets.append(f"{field}=?")
            params.append(str(payload[field] or ""))
    if "evidence_entry_ids" in payload:
        vals = payload["evidence_entry_ids"] if isinstance(payload["evidence_entry_ids"], list) else []
        sets.append("evidence_json=?")
        params.append(json.dumps([str(x) for x in vals[:5]], ensure_ascii=False))
    if "progress" in payload:
        try:
            value = int(payload["progress"])
        except (TypeError, ValueError):
            raise HTTPException(400, "progress 应为 0 到 100 的整数")
        if value < 0 or value > 100:
            raise HTTPException(400, "progress 应为 0 到 100 的整数")
        if payload.get("status") == "done":
            value = 100
        elif "status" in payload and value >= 100:
            raise HTTPException(400, "进度达到 100 时，status 应为 done")
        sets.append("progress=?")
        params.append(value)
        if value >= 100 and "status" not in payload:
            next_status = "done"
            sets.append("status=?")
            params.append("done")
        elif value < 100 and row["status"] == "done" and "status" not in payload:
            next_status = "accepted"
            sets.append("status=?")
            params.append("accepted")
    if "checkin_note" in payload:
        note = str(payload["checkin_note"] or "").strip()[:1000]
        sets.extend(["checkin_note=?", "last_checkin_at=?"])
        params.extend([note, db.now_iso()])
    # 完成/重开节点时同步完成时间和进度，避免状态与进度互相打架。
    if next_status == "done":
        if "status" not in payload and not any(s == "status=?" for s in sets):
            sets.append("status=?")
            params.append("done")
        if "progress=?" not in sets:
            sets.append("progress=?")
            params.append(100)
        if row["status"] != "done" or not row["completed_at"]:
            sets.extend(["completed_at=?"])
            params.append(db.now_iso())
    elif ("status" in payload and payload["status"] in ("accepted", "suggested", "rejected")) or (
            "status" not in payload and "progress" in payload and next_status != "done"):
        sets.append("completed_at=NULL")
    if sets:
        params.append(node_id)
        db.execute(f"UPDATE roadmap_nodes SET {', '.join(sets)} WHERE id=?", tuple(params))
    if ("progress" in payload or "checkin_note" in payload or
            ("status" in payload and payload["status"] == "done")):
        current = db.q1("SELECT progress, checkin_note FROM roadmap_nodes WHERE id=?", (node_id,))
        db.execute(
            "INSERT INTO roadmap_checkins(id, node_id, progress, note, created_at) VALUES(?,?,?,?,?)",
            (db.new_id(), node_id, current["progress"], current["checkin_note"], db.now_iso()),
        )
    return roadmap_payload()


@app.post("/api/growth/forecast/generate", status_code=201)
def generate_forecast():
    try:
        result = ai_mod.generate_forecast()
    except ai_mod.InsufficientDataError as e:
        raise HTTPException(422, f"数据不足：需要至少 8 周记录，目前已有 {e.weeks} 周")
    except ai_mod.AIError as e:
        logx.log("情景推演生成失败", str(e))
        raise HTTPException(502, str(e))
    logx.log("情景推演已生成", f"{len(result['scenarios'])} 个 90 天情景")
    return result


@app.get("/api/growth/forecast/latest")
def latest_forecast():
    row = db.q1("SELECT * FROM forecasts ORDER BY generated_at DESC LIMIT 1")
    return {"forecast": ai_mod.forecast_detail(row) if row else None}


# ---------------- 我的假设：不用 AI 的自我实验 ----------------

def _experiment_measure(metric: str, start_date: str, end_date: str) -> dict:
    """按用户给出的观察词做字面匹配，只描述记录分布，不推断因果。"""
    rows = db.q(
        "SELECT id, occurred_at, title, summary, content, tags FROM entries "
        "WHERE deleted_at IS NULL AND substr(occurred_at,1,10)>=? "
        "AND substr(occurred_at,1,10)<=? ORDER BY occurred_at DESC",
        (start_date, end_date),
    )
    needle = metric.casefold()
    days = set()
    matching_days = set()
    evidence = []
    match_count = 0
    for row in rows:
        day = str(row["occurred_at"] or "")[:10]
        if day:
            days.add(day)
        haystack = "\n".join(str(row[key] or "") for key in ("title", "summary", "content", "tags"))
        if needle not in haystack.casefold():
            continue
        match_count += 1
        if day:
            matching_days.add(day)
        if len(evidence) >= 12:
            continue
        title = str(row["title"] or "").strip()
        excerpt = re.sub(r"\s+", " ", str(row["summary"] or row["content"] or "")).strip()
        if not title:
            title = excerpt[:40] or "（无标题记录）"
        evidence.append({
            "entry_id": row["id"],
            "occurred_at": row["occurred_at"],
            "date": day,
            "title": title[:100],
            "excerpt": excerpt[:160],
        })
    total = len(rows)
    active_days = len(days)
    return {
        "total_entries": total,
        "active_days": active_days,
        "matching_entries": match_count,
        "matching_days": len(matching_days),
        "evidence": evidence,
        "computed_at": db.now_iso(),
        "match_method": "literal",
        "assessment_basis": (
            "窗口内少于 3 条记录或少于 2 个有记录日，证据不足"
            if total < 3 or active_days < 2 else
            "只统计观察词出现情况，由你结合上下文判断"
        ),
    }


def _experiment_payload(row, live: bool = True) -> dict:
    stored = {}
    try:
        stored = json.loads(row["result_json"] or "{}")
    except (TypeError, ValueError):
        stored = {}
    if row["status"] == "active" and live:
        result = _experiment_measure(row["metric"], row["start_date"], row["end_date"])
    elif stored:
        result = stored
    else:
        result = _experiment_measure(row["metric"], row["start_date"], row["end_date"])
    return {
        "id": row["id"],
        "title": row["title"],
        "metric": row["metric"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "status": row["status"],
        "due": row["status"] == "active" and datetime.now().astimezone().date().isoformat() >= row["end_date"],
        "conclusion": row["conclusion"],
        "result": result,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "ended_at": row["ended_at"],
        "causality_notice": "这些记录只能帮助观察，不能证明因果。",
    }


@app.get("/api/growth/experiments")
def list_growth_experiments():
    rows = db.q(
        "SELECT * FROM self_experiments "
        "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, "
        "end_date DESC, COALESCE(ended_at, created_at) DESC LIMIT 200"
    )
    return {"items": [_experiment_payload(row) for row in rows]}


@app.post("/api/growth/experiments", status_code=201)
def create_growth_experiment(payload: dict = Body(...)):
    title = str(payload.get("title") or "").strip()
    metric = str(payload.get("metric") or "").strip()
    if not title:
        raise HTTPException(400, "请写下想验证的假设")
    if not metric:
        raise HTTPException(400, "请填写一个可在记录中查找的观察词")
    if len(title) > 120:
        raise HTTPException(400, "假设标题请控制在 120 字以内")
    if len(metric) > 100:
        raise HTTPException(400, "观察词请控制在 100 字以内")
    start_date = _parse_date(payload.get("start_date"), "开始日期")
    end_date = _parse_date(payload.get("end_date"), "结束日期")
    if start_date > end_date:
        raise HTTPException(400, "开始日期不能晚于结束日期")
    experiment_id = db.new_id()
    now = db.now_iso()
    db.execute(
        "INSERT INTO self_experiments(id,title,metric,start_date,end_date,status,conclusion,"
        "result_json,created_at,updated_at) VALUES(?,?,?,?,?,'active','undecided','{}',?,?)",
        (experiment_id, title, metric, start_date, end_date, now, now),
    )
    logx.log("开始一项自我实验", f"《{title[:30]}》 · 观察词「{metric[:20]}」")
    return _experiment_payload(db.q1("SELECT * FROM self_experiments WHERE id=?", (experiment_id,)))


@app.post("/api/growth/experiments/{experiment_id}/finish")
def finish_growth_experiment(experiment_id: str, payload: dict | None = Body(None)):
    row = db.q1("SELECT * FROM self_experiments WHERE id=?", (experiment_id,))
    if row is None:
        raise HTTPException(404, "这项假设不存在")
    if row["status"] == "ended":
        return _experiment_payload(row, live=False)
    raw_conclusion = str((payload or {}).get("conclusion") or "").strip()
    if raw_conclusion and raw_conclusion not in EXPERIMENT_CONCLUSIONS:
        raise HTTPException(400, "conclusion 只能是 supported / refuted / insufficient / undecided")
    result = _experiment_measure(row["metric"], row["start_date"], row["end_date"])
    if raw_conclusion:
        conclusion = raw_conclusion
        result["assessment_basis"] = "由你在结束实验时结合记录作出判断"
    elif result["total_entries"] < 3 or result["active_days"] < 2:
        conclusion = "insufficient"
    else:
        conclusion = "undecided"
    now = db.now_iso()
    db.execute(
        "UPDATE self_experiments SET status='ended', conclusion=?, result_json=?, "
        "updated_at=?, ended_at=? WHERE id=?",
        (conclusion, json.dumps(result, ensure_ascii=False), now, now, experiment_id),
    )
    logx.log("结束一项自我实验", f"《{row['title'][:30]}》 · {conclusion}")
    return _experiment_payload(
        db.q1("SELECT * FROM self_experiments WHERE id=?", (experiment_id,)), live=False
    )


# ---------------- 导出 / 备份 ----------------

@app.get("/api/export/markdown")
def export_markdown(from_: str | None = Query(None, alias="from"), to: str | None = Query(None)):
    date_from = _parse_date(from_) if from_ else None
    date_to = _parse_date(to) if to else None
    path = exporter.build_markdown_zip(date_from, date_to)
    sql = "SELECT COUNT(*) AS n FROM entries WHERE deleted_at IS NULL"
    params: list = []
    if date_from:
        sql += " AND substr(occurred_at,1,10) >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND substr(occurred_at,1,10) <= ?"
        params.append(date_to)
    n = db.q1(sql, tuple(params))["n"]
    logx.log("已导出 Markdown", f"范围 {date_from or '最早'}~{date_to or '今天'}，{n} 条")
    filename = f"export-{datetime.now():%Y%m%d-%H%M%S}.zip"
    return FileResponse(
        path, media_type="application/zip", filename=filename,
        background=BackgroundTask(exporter.remove_quietly, path),
    )


@app.get("/api/export/knowledge")
def export_knowledge():
    md = exporter.knowledge_markdown()
    return Response(
        content=md.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="knowledge.md"'},
    )


@app.get("/api/export/entry/{entry_id}/html")
def export_entry_html(entry_id: str):
    page = exporter.entry_html(entry_id)
    if page is None:
        raise HTTPException(404, "记录不存在")
    logx.log("已导出单条记录", "HTML")
    return HTMLResponse(
        content=page,
        headers={"Content-Disposition": f'attachment; filename="entry-{entry_id[:8]}.html"'},
    )


@app.get("/api/export/entry/{entry_id}/markdown")
def export_entry_markdown(entry_id: str):
    result = exporter.entry_markdown(entry_id)
    if result is None:
        raise HTTPException(404, "记录不存在")
    logx.log("已导出单条记录", "Markdown")
    _e, atts, md = result
    if not atts:
        # 无附件：单文件 markdown 下载
        return Response(
            content=md.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="entry-{entry_id[:8]}.md"'},
        )
    # 有附件：zip（entry.md + media/ 原文件），链接路径去掉一级
    import zipfile
    tmp = exporter._tmp_zip("entry")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("entry.md", md.replace("](../media/", "](media/"))
        for a in atts:
            p = db.MEDIA_DIR / a["path"]
            if p.exists():
                z.write(p, f"media/{a['path']}")
    return FileResponse(
        tmp, media_type="application/zip", filename=f"entry-{entry_id[:8]}.zip",
        background=BackgroundTask(exporter.remove_quietly, tmp),
    )


@app.post("/api/backup")
def backup():
    try:
        path = exporter.build_backup_zip()
    except exporter.BackupError as e:
        logx.log("手动备份自检未通过", str(e))
        raise HTTPException(500, str(e))
    filename = f"backup-{datetime.now():%Y%m%d-%H%M%S}.zip"
    logx.log("手动备份已下载", filename)
    return FileResponse(
        path, media_type="application/zip", filename=filename,
        background=BackgroundTask(exporter.remove_quietly, path),
    )


@app.get("/api/backup/verify")
def backup_verify():
    """验证最近一次备份：重跑 testzip + manifest 校验和抽查。"""
    outdir = db.get_setting("backup_dir").strip()
    target = Path(outdir) if outdir else db.BACKUPS_DIR
    zips = []
    if target.exists():
        zips = sorted((p for p in target.glob("backup-*.zip") if not p.name.startswith(".")),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    if not zips:
        return {"ok": False, "backup_name": None, "age_hours": None, "detail": "还没有任何备份"}
    latest = zips[0]
    age_hours = round((time.time() - latest.stat().st_mtime) / 3600, 1)
    res = exporter.verify_backup(latest)
    size_mb = round(latest.stat().st_size / 1024 / 1024, 1)
    logx.log("备份验证", f"{latest.name}：{'完整可恢复' if res['ok'] else res['detail']}")
    return {"ok": res["ok"], "backup_name": latest.name, "age_hours": age_hours,
            "size_mb": size_mb, "detail": res["detail"]}


# ---------------- 健康检查（看门狗/探活，不碰 DB 写） ----------------

@app.get("/api/healthz")
def healthz():
    # version 有意暴露（M6）：供 Agent/看门狗核对服务端版本，不含敏感信息
    return {"ok": True, "uptime_s": int(time.time() - _STARTED_AT), "version": VERSION}


# ---------------- 小满发现的规律（G1） ----------------

@app.get("/api/insights")
def list_insights():
    from . import insights as insights_mod
    return {"items": insights_mod.list_active()}


@app.post("/api/insights/{iid}/dismiss")
def insight_dismiss(iid: str):
    from . import insights as insights_mod
    try:
        return insights_mod.dismiss(iid)
    except ValueError:
        raise HTTPException(404, "洞察不存在")


# ---------------- 晨间意图（G2） ----------------

@app.get("/api/intent")
def get_intent(date: str | None = Query(None)):
    day = _parse_date(date) if date else datetime.now().astimezone().date().isoformat()
    row = db.q1("SELECT text FROM daily_intents WHERE day=?", (day,))
    return {"day": day, "text": row["text"] if row else ""}


@app.put("/api/intent")
def put_intent(payload: dict = Body(...)):
    day = (_parse_date(str(payload.get("day")), "日期") if payload.get("day")
           else datetime.now().astimezone().date().isoformat())
    text = str(payload.get("text") or "").strip()[:200]
    if text:
        db.execute(
            "INSERT INTO daily_intents(day, text, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at",
            (day, text, db.now_iso()),
        )
        logx.log("今天的打算已记下", text[:30])
    else:
        db.execute("DELETE FROM daily_intents WHERE day=?", (day,))
    return {"day": day, "text": text}


# ---------------- 年度故事卡（G6） ----------------

@app.get("/api/year-review")
def year_review(year: int | None = Query(None)):
    # M1：year=0 是非法值应 400，不能被 `or` 静默成今年
    y = year if year is not None else datetime.now().astimezone().year
    if not 2000 <= y <= 2100:
        raise HTTPException(400, "year 参数不合理")
    from . import yearreview as yearreview_mod
    return yearreview_mod.get_year_review(y)


# ---------------- 时间胶囊 ----------------

class CapsuleIn(BaseModel):
    content: str
    unlock_date: str


def _capsule_dict(r) -> dict:
    return {"id": r["id"], "content": r["content"], "unlock_date": r["unlock_date"],
            "created_at": r["created_at"], "dismissed": bool(r["dismissed"])}


@app.post("/api/capsules", status_code=201)
def create_capsule(body: CapsuleIn):
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(400, "胶囊内容不能为空")
    unlock = _parse_date(body.unlock_date, "解锁日期")
    cid = db.new_id()
    db.execute("INSERT INTO capsules(id, content, unlock_date, created_at) VALUES(?,?,?,?)",
               (cid, content, unlock, db.now_iso()))
    ud = date.fromisoformat(unlock)
    logx.log("时间胶囊已收好", f"{ud.month}月{ud.day}日见")
    return _capsule_dict(db.q1("SELECT * FROM capsules WHERE id=?", (cid,)))


@app.get("/api/capsules/due")
def due_capsules():
    today = date.today().isoformat()
    rows = db.q(
        "SELECT * FROM capsules WHERE dismissed=0 AND unlock_date<=? ORDER BY unlock_date",
        (today,),
    )
    return {"items": [_capsule_dict(r) for r in rows]}


@app.post("/api/capsules/{cid}/dismiss")
def dismiss_capsule(cid: str):
    row = db.q1("SELECT id FROM capsules WHERE id=?", (cid,))
    if row is None:
        raise HTTPException(404, "时间胶囊不存在")
    db.execute("UPDATE capsules SET dismissed=1 WHERE id=?", (cid,))
    logx.log("时间胶囊已打开")
    return {"ok": True}


# ---------------- 把今天变成故事 ----------------

@app.post("/api/entries/{entry_id}/story", status_code=201)
def entry_story(entry_id: str):
    row = _get_entry_or_404(entry_id)
    if not (row["content"] or "").strip():
        raise HTTPException(400, "该记录没有正文，无法生成故事")
    if not ai_mod.strong_configured():
        raise HTTPException(400, "请先在设置页配置 AI 接口")
    try:
        story = ai_mod.generate_story(row)
    except ai_mod.AIError as e:
        logx.log("写成故事失败", str(e))
        raise HTTPException(502, str(e))
    try:
        tags = json.loads(row["tags"] or "[]")
    except ValueError:
        tags = []
    if "故事" not in tags:
        tags = tags + ["故事"]
    new_entry = create_entry_impl(EntryCreate(
        occurred_at=row["occurred_at"], title=story["title"], content=story["content"],
        category=row["category"], tags=tags,
    ))
    logx.log("已把记录写成故事", f"新记录《{story['title']}》")
    return new_entry


# ---------------- 长河数据 ----------------

@app.get("/api/river")
def river(limit: int = Query(500, ge=1, le=2000)):
    rows = db.q(
        "SELECT e.*, (SELECT a.thumb FROM attachments a WHERE a.entry_id=e.id "
        "AND a.kind='image' AND a.thumb IS NOT NULL ORDER BY a.created_at LIMIT 1) AS first_thumb "
        # 长河默认从最近记录开始，继续加载时再向更早的时间展开。
        "FROM entries e WHERE e.deleted_at IS NULL "
        "ORDER BY e.occurred_at DESC, e.created_at DESC LIMIT ?",
        (limit,),
    )
    items = []
    for r in rows:
        try:
            tags = json.loads(r["tags"] or "[]")
        except ValueError:
            tags = []
        items.append({
            "id": r["id"],
            "occurred_at": r["occurred_at"],
            "title": r["title"],
            "summary": r["summary"],
            "category": r["category"],
            "tags": tags,
            "thumb_url": f"/thumbs/{r['first_thumb']}" if r["first_thumb"] else None,
        })
    return {"items": items}


# ---------------- 类目体系 ----------------

def _category_dict(r) -> dict:
    return {
        "id": r["id"], "slug": r["slug"], "name_zh": r["name_zh"],
        "name_en": r["name_en"], "definition": r["definition"],
        "source": r["source"], "status": r["status"], "created_at": r["created_at"],
    }


@app.get("/api/categories")
def list_categories(status: str = Query("active")):
    if status not in ("active", "pending", "rejected", "all"):
        raise HTTPException(400, "status 只能是 active / pending / rejected / all")
    if status == "all":
        rows = db.q("SELECT * FROM categories ORDER BY id")
    else:
        rows = db.q("SELECT * FROM categories WHERE status=? ORDER BY id", (status,))
    return {"items": [_category_dict(r) for r in rows]}


def _category_set_status(cid: int, status: str, label: str):
    row = db.q1("SELECT * FROM categories WHERE id=?", (cid,))
    if row is None:
        raise HTTPException(404, "类目不存在")
    db.execute("UPDATE categories SET status=? WHERE id=?", (status, cid))
    logx.log(f"类目{label}", f"{row['name_zh']}（{row['slug']}）")
    return {"ok": True}


@app.post("/api/categories/{cid}/approve")
def approve_category(cid: int):
    return _category_set_status(cid, "active", "已启用")


@app.post("/api/categories/{cid}/reject")
def reject_category(cid: int):
    return _category_set_status(cid, "rejected", "已忽略")


# ---------------- 历史分类迁移 ----------------

@app.post("/api/admin/reclassify")
def admin_reclassify():
    started = scheduler.start_reclassification()
    return {"started": started, **scheduler.reclassify_status()}


@app.get("/api/admin/reclassify-status")
def admin_reclassify_status():
    return scheduler.reclassify_status()


@app.post("/api/admin/circle-recognition")
def admin_circle_recognition():
    """手动再跑一遍圈子历史追认（清 marker 重跑，幂等）。"""
    db.set_setting("circle_recognition_v3", "")
    started = scheduler.start_recognition()
    return {"started": started, "running": scheduler.recognition_running()}


@app.get("/api/notice")
def app_notice():
    """一次性全局通知队列（读完即清）：迁移完成、自动合并等轻提示。
    notices 为全部待看；notice 兼容字段=最新一条。"""
    raw = db.get_setting("app_notice")
    items: list = []
    if raw:
        try:
            v = json.loads(raw)
            items = v if isinstance(v, list) else [v]
        except ValueError:
            items = []
    if items:
        db.set_setting("app_notice", "")
    return {"notice": items[-1] if items else None, "notices": items}


# ---------------- 圈子：档案与关系网 ----------------

def _entity_detail(eid: str) -> dict:
    row = circle_mod.get_entity(eid)
    if row is None:
        raise HTTPException(404, "档案不存在")
    data = circle_mod._entity_dict(row)
    mentions = db.q(
        "SELECT m.entry_id, m.snippet, m.created_at, e.occurred_at FROM entity_mentions m "
        "JOIN entries e ON e.id = m.entry_id WHERE m.entity_id=? "
        "ORDER BY e.occurred_at DESC LIMIT 50",
        (eid,),
    )
    data["mentions"] = [
        {"entry_id": m["entry_id"], "snippet": m["snippet"],
         "created_at": m["created_at"], "occurred_at": m["occurred_at"]}
        for m in mentions
    ]
    # 同时返回出边和入边：过去只看 source_id，会让人物详情漏掉所有“别人指向他”的关系。
    relations = db.q(
        "SELECT CASE WHEN r.source_id=? THEN r.target_id ELSE r.source_id END AS target_id, "
        "CASE WHEN r.source_id=? THEN 'out' ELSE 'in' END AS direction, "
        "r.label, r.status, r.entry_id, r.snippet, r.valid_from, r.valid_to, r.certainty, "
        "e.name AS target_name FROM entity_relations r "
        "LEFT JOIN entities e ON e.id=(CASE WHEN r.source_id=? THEN r.target_id ELSE r.source_id END) "
        "WHERE r.source_id=? OR r.target_id=? "
        "ORDER BY CASE WHEN r.valid_to IS NULL THEN 0 ELSE 1 END, r.updated_at DESC",
        (eid, eid, eid, eid, eid),
    )
    data["relations"] = [
        {"target_id": r["target_id"], "target_name": r["target_name"] or "（已删除）",
         "label": r["label"], "status": r["status"], "entry_id": r["entry_id"],
         "snippet": r["snippet"], "valid_from": r["valid_from"],
         "valid_to": r["valid_to"], "certainty": r["certainty"],
         "direction": r["direction"]}
        for r in relations
    ]
    # 事实时间线：valid_to 非空的是"既往"（已封存的历史值）
    facts = db.q(
        "SELECT * FROM entity_facts WHERE entity_id=? ORDER BY created_at DESC", (eid,))
    data["facts"] = [
        {"id": f["id"], "predicate": f["predicate"], "object": f["object_text"],
         "object_text": f["object_text"],
         "valid_from": f["valid_from"], "valid_to": f["valid_to"],
         "certainty": f["certainty"], "source_entry_id": f["source_entry_id"]}
        for f in facts
    ]
    return data


@app.get("/api/circle/entities")
def circle_list(
    type: str | None = Query(None),
    status: str | None = Query(None),
    needs_review: bool | None = Query(None),
):
    """圈子档案列表。

    ``needs_review=1`` 是独立的冲突收件箱筛选，不依赖档案当前状态；
    这样 confirmed 档案出现新证据冲突时，也不会从待裁定列表里消失。
    """
    if status is not None and status not in ("active", "draft", "confirmed", "rejected", "all"):
        raise HTTPException(400, "status 只能是 active / draft / confirmed / rejected / all")
    clauses, params = [], []
    if type:
        if type not in ("person", "place", "event"):
            raise HTTPException(400, "type 只能是 person / place / event")
        clauses.append("type=?")
        params.append(type)
    if status == "all":
        pass  # 含 rejected
    elif status:
        clauses.append("status=?")
        params.append(status)
    else:
        clauses.append("status IN ('active','confirmed','draft')")
    if needs_review is True:
        # 被否决档案不属于冲突收件箱；与 /api/circle/overview 及首页提醒保持一致。
        # needs_review 仍独立于其它状态筛选，因此 confirmed/active/draft 均可进入。
        clauses.append("status != 'rejected'")
        clauses.append("needs_review=1")
    elif needs_review is False:
        clauses.append("needs_review=0")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.q(
        f"SELECT * FROM entities {where} ORDER BY last_seen DESC, created_at DESC",
        tuple(params),
    )
    return {"items": [circle_mod._entity_dict(r) for r in rows]}


@app.get("/api/circle/entities/{eid}")
def circle_detail(eid: str):
    return _entity_detail(eid)


@app.patch("/api/circle/entities/{eid}")
def circle_patch(eid: str, payload: dict = Body(...)):
    row = circle_mod.get_entity(eid)
    if row is None:
        raise HTTPException(404, "档案不存在")
    sets, params = [], []
    if "name" in payload:
        name = str(payload["name"] or "").strip()[:30]
        if not name:
            raise HTTPException(400, "名称不能为空")
        sets.append("name=?")
        params.append(name)
    for field in ("profile", "relation_to_user", "user_note"):
        if field in payload:
            sets.append(f"{field}=?")
            params.append(str(payload[field] or "")[:500])
    if "type" in payload:
        t = str(payload["type"] or "").strip()
        if t not in ("person", "place", "event"):
            raise HTTPException(400, "type 只能是 person / place / event")
        if t != row["type"]:
            clash = db.q1("SELECT id FROM entities WHERE type=? AND name=? AND id != ?",
                          (t, payload.get("name") or row["name"], eid))
            if clash is not None:
                raise HTTPException(400, "该类型下已有同名档案，建议改用合并")
            sets.append("type=?")
            params.append(t)
    # 用户编辑即 confirmed，除非显式传 status
    if "status" in payload:
        if payload["status"] not in ("active", "draft", "confirmed", "rejected"):
            raise HTTPException(400, "status 只能是 active / draft / confirmed / rejected")
        sets.append("status=?")
        params.append(payload["status"])
    elif sets:
        sets.append("status='confirmed'")
    if sets:
        sets.append("updated_at=?")
        params.append(db.now_iso())
        params.append(eid)
        db.execute(f"UPDATE entities SET {', '.join(sets)} WHERE id=?", tuple(params))
        logx.log("圈子：档案已编辑", f"《{(payload.get('name') or row['name'])[:20]}》")
    return _entity_detail(eid)


def _circle_set_status(eid: str, status: str, label: str):
    row = circle_mod.get_entity(eid)
    if row is None:
        raise HTTPException(404, "档案不存在")
    db.execute("UPDATE entities SET status=?, updated_at=? WHERE id=?",
               (status, db.now_iso(), eid))
    logx.log(f"圈子：档案{label}", f"《{row['name']}》")
    return _entity_detail(eid)


@app.post("/api/circle/entities/{eid}/confirm")
def circle_confirm(eid: str):
    result = _circle_set_status(eid, "confirmed", "已确认")
    # 级联：确认后连带整理 active 关系对端的 draft 邻居
    try:
        circle_mod.cascade_after_confirm(circle_mod.get_entity(eid))
    except Exception:
        pass
    return result


@app.post("/api/circle/entities/{eid}/reject")
def circle_reject(eid: str):
    return _circle_set_status(eid, "rejected", "已否认")


@app.post("/api/circle/entities/{eid}/revive")
def circle_revive(eid: str):
    row = circle_mod.get_entity(eid)
    if row is None:
        raise HTTPException(404, "档案不存在")
    # rejected → draft 并重评（清综合时间戳让复查捞起来）
    db.execute("UPDATE entities SET status='draft', synthesized_at=NULL, updated_at=? WHERE id=?",
               (db.now_iso(), eid))
    logx.log("圈子：档案已恢复重评", f"《{row['name']}》")
    try:
        circle_mod.synthesize_one(circle_mod.get_entity(eid))
    except Exception:
        pass
    return _entity_detail(eid)


@app.post("/api/circle/entities/{eid}/rework")
def circle_rework(eid: str, payload: dict | None = Body(None)):
    row = circle_mod.get_entity(eid)
    if row is None:
        raise HTTPException(404, "档案不存在")
    if not ai_mod.strong_configured():
        raise HTTPException(400, "请先在设置页配置 AI 接口")
    # 用户给的描述先存为 user_note（最高事实），再按它重新整理
    note = str((payload or {}).get("note") or "").strip()
    if note:
        db.execute("UPDATE entities SET user_note=?, updated_at=? WHERE id=?",
                   (note[:500], db.now_iso(), eid))
        row = circle_mod.get_entity(eid)
    try:
        circle_mod.synthesize_one(row)
    except ai_mod.AIError as e:
        raise HTTPException(502, str(e))
    if note:
        # 用户亲自给了描述：视为已确认
        db.execute("UPDATE entities SET status='confirmed', updated_at=? WHERE id=?",
                   (db.now_iso(), eid))
    # 用户已给出事实（user_note 最高优先）：清冲突标记
    db.execute("UPDATE entities SET needs_review=0, conflict_note=NULL WHERE id=?", (eid,))
    logx.log("圈子：档案已重新整理", f"《{row['name']}》")
    return _entity_detail(eid)


class MergeIn(BaseModel):
    from_id: str
    into_id: str


@app.post("/api/circle/entities/{eid}/resolve")
def circle_resolve(eid: str, payload: dict = Body(...)):
    """冲突裁定：keep_old=维持现状仅清标记；accept_new=按 user_note/最新证据重新综合并清标记。"""
    row = circle_mod.get_entity(eid)
    if row is None:
        raise HTTPException(404, "档案不存在")
    action = (payload or {}).get("action")
    if action == "keep_old":
        db.execute("UPDATE entities SET needs_review=0, conflict_note=NULL, updated_at=? WHERE id=?",
                   (db.now_iso(), eid))
        logx.log("圈子：矛盾已裁定", f"《{row['name']}》维持旧档案")
    elif action == "accept_new":
        if not ai_mod.strong_configured():
            raise HTTPException(400, "请先在设置页配置 AI 接口")
        try:
            circle_mod.synthesize_one(row)
        except ai_mod.AIError as e:
            raise HTTPException(502, str(e))
        db.execute("UPDATE entities SET needs_review=0, conflict_note=NULL, updated_at=? WHERE id=?",
                   (db.now_iso(), eid))
        logx.log("圈子：矛盾已裁定", f"《{row['name']}》按新证据重整")
    else:
        raise HTTPException(400, "action 只能是 keep_old / accept_new")
    return _entity_detail(eid)


@app.get("/api/circle/proposals")
def circle_proposals(status: str = Query("pending")):
    if status not in ("pending", "accepted", "rejected", "all"):
        raise HTTPException(400, "status 只能是 pending / accepted / rejected / all")
    status_clause = "" if status == "all" else "WHERE p.status=?"
    status_params = () if status == "all" else (status,)
    rows = db.q(
        "SELECT p.*, f.name AS from_name, f.profile AS from_profile, f.type AS from_type, "
        "t.name AS into_name, t.profile AS into_profile, t.type AS into_type "
        "FROM merge_proposals p "
        "LEFT JOIN entities f ON f.id = p.from_id "
        "LEFT JOIN entities t ON t.id = p.into_id "
        f"{status_clause} ORDER BY p.created_at DESC",
        status_params,
    )
    return {
        "items": [
            {"id": r["id"], "from_id": r["from_id"], "into_id": r["into_id"],
             "reason": r["reason"], "status": r["status"], "created_at": r["created_at"],
             "from_name": r["from_name"], "from_profile": r["from_profile"],
             "into_name": r["into_name"], "into_profile": r["into_profile"],
             "from_type": r["from_type"], "into_type": r["into_type"]}
            for r in rows
        ]
    }


@app.post("/api/circle/proposals/{pid}/accept")
def circle_proposal_accept(pid: str):
    row = db.q1("SELECT * FROM merge_proposals WHERE id=?", (pid,))
    if row is None:
        raise HTTPException(404, "提案不存在")
    if row["status"] != "pending":
        raise HTTPException(400, "提案已处理过")
    try:
        circle_mod.merge_entities(row["from_id"], row["into_id"])
    except ValueError:
        raise HTTPException(404, "档案不存在")
    db.execute("UPDATE merge_proposals SET status='accepted' WHERE id=?", (pid,))
    return _entity_detail(row["into_id"])


@app.post("/api/circle/proposals/{pid}/reject")
def circle_proposal_reject(pid: str):
    row = db.q1("SELECT * FROM merge_proposals WHERE id=?", (pid,))
    if row is None:
        raise HTTPException(404, "提案不存在")
    db.execute("UPDATE merge_proposals SET status='rejected' WHERE id=?", (pid,))
    circle_mod.add_rejected_pair(row["from_id"], row["into_id"])  # 黑名单：永不再提
    logx.log("圈子：合并提案已拒绝", "这对以后不再提")
    return {"ok": True}


@app.get("/api/circle/auto-log")
def circle_auto_log_list(days: int = Query(14, ge=1, le=90)):
    """小满最近自己拿的主意（未撤销的自动动作，含实体名与理由）。"""
    return {"items": circle_mod.auto_log_list(days)}


@app.post("/api/circle/auto-log/{aid}/undo")
def circle_auto_log_undo(aid: str):
    try:
        return circle_mod.undo_auto_action(aid)
    except ValueError as e:
        if str(e) == "not found":
            raise HTTPException(404, "记录不存在")
        if str(e) == "already undone":
            raise HTTPException(400, "这条已经撤销过了")
        raise HTTPException(400, "这条记录无法撤销")


@app.get("/api/circle/questions")
def circle_questions(status: str = Query("pending")):
    """提问按记录分组：同一条记录产生的多个问题合成一张批量卡。"""
    if status not in ("pending", "answered", "dismissed", "all"):
        raise HTTPException(400, "status 只能是 pending / answered / dismissed / all")
    where = "" if status == "all" else "WHERE status=?"
    params = () if status == "all" else (status,)
    rows = db.q(f"SELECT * FROM circle_questions {where} ORDER BY created_at", params)

    def options_for(row) -> list:
        try:
            value = json.loads(row["options_json"] or "[]")
        except (TypeError, ValueError):
            return []
        return value if isinstance(value, list) else []

    groups: dict[str, dict] = {}
    for r in rows:
        try:
            related = json.loads(r["related_json"] or "{}")
        except (TypeError, ValueError):
            related = {}
        if not isinstance(related, dict):
            related = {}
        entry_id = r["entry_id"] or related.get("entry_id") or ""
        q = {
            "id": r["id"], "question": r["question"],
            "options": options_for(r),
            "ai_suggested": r["ai_suggested"],
            "related": related,
            "status": r["status"], "answer": r["answer"], "created_at": r["created_at"],
        }
        key = entry_id or f"single-{r['id']}"  # 无 entry_id 的散问各自成组
        g = groups.get(key)
        if g is None:
            day = ""
            if entry_id:
                ent = db.q1("SELECT occurred_at FROM entries WHERE id=?", (entry_id,))
                day = (ent["occurred_at"] or "")[:10] if ent else ""
            g = groups[key] = {"entry_id": entry_id or None, "day": day, "questions": []}
        g["questions"].append(q)
    # 组按日期倒序（新的在前）
    ordered = sorted(groups.values(), key=lambda g: g["day"] or "", reverse=True)
    return {"groups": ordered}


class BatchAnswerIn(BaseModel):
    # 元素刻意不加 dict 约束：脏项由处理函数统一判 400（I-4），而非被 pydantic 422 截胡
    answers: list


@app.post("/api/circle/questions/answer-batch")
def circle_questions_answer_batch(body: BatchAnswerIn):
    """批量回答（一条记录一张卡）：先整批校验（任一非法则整批拒绝，要么全成要么全败），
    再逐题复用 answer_question。返回处理数。"""
    # I-4：超限/脏项不静默——>50 或含非 dict 项直接 400（web/agent 同获益）
    if len(body.answers) > 50:
        raise HTTPException(400, "一批最多回答 50 题")
    answers = body.answers
    if any(not isinstance(a, dict) for a in answers):
        raise HTTPException(400, "answers 每项都应为对象 {id, choice_index}")
    # 阶段 1：整批预校验——存在性、状态（M4：已答/已忽略不可再答）与下标范围，任一不过则整批不动
    for item in answers:
        qid = str(item.get("id") or "")
        row = db.q1("SELECT id, options_json, status FROM circle_questions WHERE id=?", (qid,))
        if row is None:
            raise HTTPException(404, f"问题不存在：{qid[:8]}")
        if row["status"] != "pending":
            raise HTTPException(400, f"问题已回答或已关闭：{qid[:8]}")
        try:
            choice = int(item.get("choice_index"))
        except (TypeError, ValueError):
            raise HTTPException(400, "choice_index 应为整数")
        try:
            options = json.loads(row["options_json"] or "[]")
        except (TypeError, ValueError):
            options = []
        if not isinstance(options, list) or not 0 <= choice < len(options):
            raise HTTPException(400, f"choice_index 超出范围：{qid[:8]}")
    # 阶段 2：逐题生效
    done = 0
    for item in answers:
        circle_mod.answer_question(str(item["id"]), int(item["choice_index"]))
        done += 1
    if done:
        logx.log("圈子：批量提问已回答", f"{done} 条")
    return {"answered": done, "failed": 0}


class AnswerIn(BaseModel):
    choice_index: int


@app.post("/api/circle/questions/{qid}/answer")
def circle_question_answer(qid: str, body: AnswerIn):
    try:
        result = circle_mod.answer_question(qid, body.choice_index)
    except ValueError as e:
        if str(e) == "not found":
            raise HTTPException(404, "问题不存在")
        if str(e) == "bad state":
            raise HTTPException(400, "该问题已回答或已关闭")
        raise HTTPException(400, "choice_index 超出范围")
    return {"ok": True, **result}


@app.post("/api/circle/questions/{qid}/dismiss")
def circle_question_dismiss(qid: str):
    row = db.q1("SELECT id FROM circle_questions WHERE id=?", (qid,))
    if row is None:
        raise HTTPException(404, "问题不存在")
    db.execute("UPDATE circle_questions SET status='dismissed' WHERE id=?", (qid,))
    return {"ok": True}


@app.get("/api/circle/relations")
def circle_relations(status: str | None = Query(None)):
    """关系列表：联表一次取全（供前端画图，避免 N+1）。"""
    clauses, params = [], []
    if status is not None:
        if status not in ("active", "draft", "ended", "rejected", "all"):
            raise HTTPException(400, "status 只能是 active / draft / ended / rejected / all")
        if status == "all":
            status = None
    if status:
        clauses.append("r.status=?")
        params.append(status)
        if status == "active":
            clauses.append("r.valid_to IS NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.q(
        f"SELECT r.*, s.name AS source_name, s.type AS source_type, "
        f"t.name AS target_name, t.type AS target_type "
        f"FROM entity_relations r "
        f"LEFT JOIN entities s ON s.id = r.source_id "
        f"LEFT JOIN entities t ON t.id = r.target_id {where} "
        "ORDER BY r.updated_at DESC",
        tuple(params),
    )
    return {
        "items": [
            {
                "id": r["id"], "source_id": r["source_id"],
                "source_name": r["source_name"], "source_type": r["source_type"],
                "target_id": r["target_id"],
                "target_name": r["target_name"] or "（已删除）", "target_type": r["target_type"],
                "label": r["label"], "status": r["status"], "entry_id": r["entry_id"],
                "snippet": r["snippet"], "valid_from": r["valid_from"],
                "valid_to": r["valid_to"], "certainty": r["certainty"],
            }
            for r in rows
        ]
    }


@app.post("/api/circle/entities/merge")
def circle_merge(body: MergeIn):
    if body.from_id == body.into_id:
        raise HTTPException(400, "不能合并到自己")
    try:
        circle_mod.merge_entities(body.from_id, body.into_id)
    except ValueError:
        raise HTTPException(404, "档案不存在")
    return _entity_detail(body.into_id)


@app.get("/api/circle/graph")
def circle_graph():
    """关系图：一次返回节点与边（替代多次计数请求）。"""
    return circle_mod.graph_data()


@app.get("/api/circle/overview")
def circle_overview():
    rows = db.q("SELECT status, COUNT(*) AS n FROM entities GROUP BY status")
    counts = {r["status"]: r["n"] for r in rows}
    rel = db.q1(
        "SELECT COUNT(*) AS n FROM entity_relations "
        "WHERE valid_to IS NULL AND status IN ('active','draft')"
    )["n"]
    # rejected 档案不再需要用户裁定；其余状态都可能带着新证据冲突。
    needs_review = db.q1(
        "SELECT COUNT(*) AS n FROM entities WHERE needs_review=1 AND status != 'rejected'"
    )["n"]
    return {
        "enabled": circle_mod.enabled(),
        "active": counts.get("active", 0),
        "draft": counts.get("draft", 0),
        "confirmed": counts.get("confirmed", 0),
        "rejected": counts.get("rejected", 0),
        "relations": rel,
        "needs_review": needs_review,
    }


@app.post("/api/circle/backfill")
def circle_backfill():
    if not ai_mod.strong_configured() and not ai_mod.fast_configured():
        raise HTTPException(400, "请先在设置页配置 AI 接口")
    circle_mod.start_backfill()
    return circle_mod.backfill_status()


@app.get("/api/circle/backfill-status")
def circle_backfill_status():
    return circle_mod.backfill_status()


@app.get("/api/circle/notice")
def circle_notice():
    raw = db.get_setting("circle_notice")
    notice = None
    if raw:
        try:
            notice = json.loads(raw)
        except ValueError:
            notice = None
    if notice:
        db.set_setting("circle_notice", "")
    return {"notice": notice}


# ---------------- Agent API ----------------

def agent_auth(request: Request) -> None:
    """Agent 鉴权（M5）：未配置 token = 403（功能未启用，语义区别于凭证无效）；
    配了但 Bearer 缺失/不符 = 401。compare_digest 常数时间比较，防时序侧信道。"""
    token = db.get_setting("agent_token")
    if not token:
        raise HTTPException(403, "agent api 未启用：请先在 设置 → Agent 生成 token")
    auth = request.headers.get("authorization", "")
    candidate = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if not candidate or not hmac.compare_digest(candidate, token):
        raise HTTPException(401, "agent token 无效")


@app.get("/api/agent/entries", dependencies=[Depends(agent_auth)])
def agent_list_entries(
    date: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    starred: int = Query(0, ge=0, le=1),
    work: int = Query(0, ge=0, le=1),
    category: str | None = Query(None),
):
    return list_entries_impl(date, from_, to, None, category, None, limit, offset, 0, starred,
                             work=work)


@app.get("/api/agent/entries/{entry_id}", dependencies=[Depends(agent_auth)])
def agent_get_entry(entry_id: str):
    return entry_to_dict(_get_entry_or_404(entry_id))


@app.post("/api/agent/entries", status_code=201, dependencies=[Depends(agent_auth)])
def agent_create_entry(body: EntryCreate):
    return create_entry_impl(body)


@app.patch("/api/agent/entries/{entry_id}", dependencies=[Depends(agent_auth)])
def agent_patch_entry(entry_id: str, payload: dict = Body(...)):
    """复用主站 PATCH 处理函数：字段校验、天气重补、正文大改的 rejudge/异步链全部一致。"""
    return patch_entry(entry_id, payload)


@app.delete("/api/agent/entries/{entry_id}", dependencies=[Depends(agent_auth)])
def agent_delete_entry(entry_id: str):
    """软删除进回收站（30 天内可 restore），绝不硬删。"""
    return delete_entry(entry_id)


@app.post("/api/agent/entries/{entry_id}/restore", dependencies=[Depends(agent_auth)])
def agent_restore_entry(entry_id: str):
    return restore_entry(entry_id)


@app.get("/api/agent/digest", dependencies=[Depends(agent_auth)])
def agent_digest(date: str | None = Query(None)):
    day = _parse_date(date) if date else datetime.now().astimezone().date().isoformat()
    rows = db.q(
        "SELECT * FROM entries WHERE deleted_at IS NULL AND substr(occurred_at,1,10)=? "
        "ORDER BY occurred_at",
        (day,),
    )
    blocks = []
    for r in rows:
        lines = [f"【{r['occurred_at'][11:16]}】{r['title'] or '（无标题）'}"]
        try:
            tags = json.loads(r["tags"] or "[]")
        except ValueError:
            tags = []
        meta = [f"分类：{r['category']}"]
        if tags:
            meta.append("标签：" + "、".join(str(t) for t in tags))
        lines.append(" | ".join(meta))
        try:
            w = json.loads(r["weather_json"]) if r["weather_json"] else None
        except ValueError:
            w = None
        wtext = ai_mod._fmt_weather(w)
        if wtext:
            lines.append(f"天气：{wtext}")
        if r["location_name"]:
            lines.append(f"地点：{r['location_name']}")
        if r["summary"]:
            lines.append(f"摘要：{r['summary']}")
        if r["content"]:
            lines.append(f"正文：\n{r['content']}")
        atts = db.q("SELECT filename, kind FROM attachments WHERE entry_id=? ORDER BY created_at", (r["id"],))
        if atts:
            lines.append("附件：" + "、".join(f"{a['filename']}({a['kind']})" for a in atts))
        links = db.q("SELECT url, title FROM links WHERE entry_id=? ORDER BY created_at", (r["id"],))
        for l in links:
            lines.append(f"链接：{l['title'] or l['url']}（{l['url']}）")
        blocks.append("\n".join(lines))
    text = f"# {day} 记录摘要（共 {len(rows)} 条）\n\n" + "\n\n---\n\n".join(blocks) if blocks else f"{day} 没有记录。"
    return {"date": day, "text": text}


@app.get("/api/agent/search", dependencies=[Depends(agent_auth)])
def agent_search(q: str = Query(...), limit: int = Query(20, ge=1, le=100)):
    return search_impl(q, limit)


@app.get("/api/agent/reports/latest", dependencies=[Depends(agent_auth)])
def agent_latest_report(type: str = Query("weekly")):
    if type not in REPORT_TYPES:
        raise HTTPException(400, "type 只能是 daily / weekly / monthly")
    row = db.q1("SELECT * FROM reports WHERE type=? ORDER BY created_at DESC LIMIT 1", (type,))
    if row is None:
        raise HTTPException(404, "暂无该类型报告")
    return ai_mod.report_detail(row)


@app.get("/api/agent/metrics", dependencies=[Depends(agent_auth)])
def agent_metrics():
    return stats_mod.overview()


@app.get("/api/agent/assets/{att_id}", dependencies=[Depends(agent_auth)])
def agent_asset(att_id: str):
    row = db.q1("SELECT * FROM attachments WHERE id=?", (att_id,))
    if row is None:
        raise HTTPException(404, "附件不存在")
    path = db.MEDIA_DIR / row["path"]
    if not path.exists():
        raise HTTPException(404, "附件文件已丢失")
    return FileResponse(path, media_type=row["mime"] or "application/octet-stream",
                        filename=row["filename"])


AGENT_CAPABILITIES = [
    # ---- 自我发现 / 探活 ----
    {"method": "GET", "path": "/api/agent/capabilities", "description": "自我发现：列出全部 Agent 可用端点与参数说明"},
    # ---- 记录读写 ----
    {"method": "GET", "path": "/api/agent/entries", "description": "按日期/范围列出记录（含 is_work/categories/work_related）", "params": {"date": "单日 YYYY-MM-DD", "from": "起始日", "to": "截止日", "starred": "1=只看星标高光", "work": "1=只看工作记录", "category": "类目 slug 过滤（见 /api/agent/categories）", "limit": "条数上限(默认50)", "offset": "偏移"}},
    {"method": "GET", "path": "/api/agent/entries/{id}", "description": "获取单条记录完整内容（含天气/附件/链接/模块）"},
    {"method": "POST", "path": "/api/agent/entries", "description": "创建记录，响应即完整 entry（含 id/is_work/categories）", "params": {"body": "{occurred_at?, title?, summary?, content?, tags?, blocks?, links?, is_work?: true/false（传了即为人工定论 is_work_manual=1，AI 不再改判）, exclude_from_ai?}"}},
    {"method": "PATCH", "path": "/api/agent/entries/{id}", "description": "编辑记录（与主站同一处理函数：正文大改会自动触发重判/向量/圈子流水线；回收站记录不可编辑）", "params": {"body": "{title?, summary?, content?, tags?, is_work?, occurred_at?, location_name?, blocks?, weather?, starred?, exclude_from_ai?, latitude?, longitude?}"}},
    {"method": "DELETE", "path": "/api/agent/entries/{id}", "description": "软删除：记录进回收站（30 天内可 restore 恢复，绝不硬删）"},
    {"method": "POST", "path": "/api/agent/entries/{id}/restore", "description": "从回收站恢复记录（恢复后自动重建语义向量）"},
    {"method": "GET", "path": "/api/agent/digest", "description": "某天全部记录拼成的纯文本摘要（含时间/天气/附件名）", "params": {"date": "YYYY-MM-DD，默认今天"}},
    {"method": "GET", "path": "/api/agent/search", "description": "关键词全文搜索记录", "params": {"q": "关键词（支持单字）", "limit": "默认20"}},
    # ---- 报告 / 问答 / 知识 ----
    {"method": "GET", "path": "/api/agent/reports", "description": "复盘报告列表（meta）", "params": {"type": "daily/weekly/monthly", "limit": "默认20"}},
    {"method": "GET", "path": "/api/agent/reports/{id}", "description": "完整报告（含 analysis 分析内容）"},
    {"method": "GET", "path": "/api/agent/reports/latest", "description": "某类型最新一份报告", "params": {"type": "daily/weekly/monthly，默认 weekly"}},
    {"method": "POST", "path": "/api/agent/reports/generate", "description": "生成复盘报告（同周期覆盖）", "params": {"body": "{type: 'daily'|'weekly'|'monthly', date: 'YYYY-MM-DD'}"}},
    {"method": "GET", "path": "/api/agent/knowledge", "description": "知识库条目列表", "params": {"status": "pending/accepted/rejected", "type": "experience/pitfall/case/sop/skill"}},
    {"method": "POST", "path": "/api/agent/knowledge/{kid}/accept", "description": "知识审核：入库（待确认 → 已入库）"},
    {"method": "POST", "path": "/api/agent/knowledge/{kid}/reject", "description": "知识审核：忽略（以后生成报告不再提）"},
    {"method": "POST", "path": "/api/agent/ask", "description": "基于全部记录的问答（问小满）", "params": {"body": "{question: '问题'}"}},
    # ---- 统计 / 附件 / 导出 ----
    {"method": "GET", "path": "/api/agent/metrics", "description": "统计总览（连续天数/热力图/趋势/分类/标签等）"},
    {"method": "GET", "path": "/api/agent/assets", "description": "附件列表（含所属记录 entry_id）", "params": {"kind": "image/video/audio/file", "limit": "默认100", "offset": "偏移"}},
    {"method": "GET", "path": "/api/agent/assets/{id}", "description": "下载附件原始文件"},
    {"method": "GET", "path": "/api/agent/assets/{id}/text", "description": "提取文本类附件的文字内容（≤200KB）"},
    {"method": "GET", "path": "/api/agent/calendar", "description": "月历：每天记录条数", "params": {"month": "YYYY-MM"}},
    {"method": "GET", "path": "/api/agent/export/all", "description": "一次性全量导出：全部记录/知识/报告/统计"},
    # ---- 成长 ----
    {"method": "GET", "path": "/api/agent/growth/summary", "description": "AI 成长现状总结（里程碑/优势）"},
    {"method": "GET", "path": "/api/agent/growth/roadmap", "description": "成长路线（含 direction 方向与节点状态）"},
    {"method": "GET", "path": "/api/agent/growth/forecast/latest", "description": "最新 90 天情景推演"},
    {"method": "GET", "path": "/api/agent/growth/experiments", "description": "自我实验列表（真实记录统计、结论与证据回链）"},
    {"method": "POST", "path": "/api/agent/growth/experiments", "description": "创建不用 AI 的自我实验", "params": {"body": "{title, metric, start_date: 'YYYY-MM-DD', end_date: 'YYYY-MM-DD'}"}},
    {"method": "POST", "path": "/api/agent/growth/experiments/{id}/finish", "description": "结束自我实验并固化记录证据", "params": {"body": "{conclusion?: 'supported'|'refuted'|'insufficient'|'undecided'}"}},
    # ---- 圈子（档案/关系图/审核） ----
    {"method": "GET", "path": "/api/agent/circle/entities", "description": "圈子档案列表（人物/地点/事件）", "params": {"type": "person/place/event", "status": "active/draft/confirmed/rejected/all", "needs_review": "true 时只取待裁定冲突（不受 status 限制）"}},
    {"method": "GET", "path": "/api/agent/circle/entities/{id}", "description": "档案详情（含提及证据、关系与事实时间线）"},
    {"method": "PATCH", "path": "/api/agent/circle/entities/{id}", "description": "编辑档案（用户编辑即视为 confirmed）", "params": {"body": "{name?, relation_to_user?, profile?, user_note?}"}},
    {"method": "POST", "path": "/api/agent/circle/entities/{id}/confirm", "description": "圈子审核：确认档案（连带整理相关草稿邻居）"},
    {"method": "POST", "path": "/api/agent/circle/entities/{id}/reject", "description": "圈子审核：否认档案"},
    {"method": "POST", "path": "/api/agent/circle/entities/merge", "description": "合并两份档案（from 并入 into，提及/关系/别名随迁）", "params": {"body": "{from_id, into_id}"}},
    {"method": "GET", "path": "/api/agent/circle/proposals", "description": "自动合并提案列表（小满怀疑是同一人的配对）", "params": {"status": "pending/accepted/rejected/all，默认 pending"}},
    {"method": "GET", "path": "/api/agent/circle/questions", "description": "待答提问（按记录分组的批量卡，含 AI 预选答案）", "params": {"status": "pending/answered/dismissed/all，默认 pending"}},
    {"method": "POST", "path": "/api/agent/circle/questions/answer-batch", "description": "批量回答提问（整批校验，要么全成要么全败）", "params": {"body": "{answers: [{id, choice_index}]}"}},
    {"method": "GET", "path": "/api/agent/circle/auto-log", "description": "小满最近自己拿的主意（自动确认/合并等，未撤销）", "params": {"days": "近 N 天，默认14"}},
    {"method": "POST", "path": "/api/agent/circle/auto-log/{aid}/undo", "description": "撤销一次自动动作（按快照回滚，且以后不再自动重复）"},
    {"method": "GET", "path": "/api/agent/circle/graph", "description": "圈子关系图：节点（confirmed+active 档案）与边"},
    # ---- 类目 ----
    {"method": "GET", "path": "/api/agent/categories", "description": "类目列表（is_work 之外的 AI 多标签体系）", "params": {"status": "active/pending/rejected/all，默认 active"}},
    {"method": "POST", "path": "/api/agent/categories/{cid}/approve", "description": "类目审核：启用 AI 提议的新类目"},
    {"method": "POST", "path": "/api/agent/categories/{cid}/reject", "description": "类目审核：忽略（不再参与打标）"},
    # ---- 隐性关联 ----
    {"method": "GET", "path": "/api/agent/entries/{id}/links", "description": "一条记录的隐性关联提案（可能相关的工作记录/事件/目标）"},
    {"method": "POST", "path": "/api/agent/links/{lid}/confirm", "description": "确认关联提案（目标是工作向时记录会标记 work_related，供周报参考）"},
    {"method": "POST", "path": "/api/agent/links/{lid}/dismiss", "description": "忽略关联提案（误标 work_related 会自动回滚）"},
    # ---- 规律 / 意图 / 年卡 / 胶囊 ----
    {"method": "GET", "path": "/api/agent/insights", "description": "小满发现的规律（心情相关性，达到统计门槛才出）", "params": {"status": "active/dismissed/all，默认 active"}},
    {"method": "GET", "path": "/api/agent/intent", "description": "取某天的今日意图", "params": {"date": "YYYY-MM-DD，默认今天"}},
    {"method": "PUT", "path": "/api/agent/intent", "description": "写某天的今日意图（空文本=删除）", "params": {"body": "{day: 'YYYY-MM-DD', text: '今天打算……'}"}},
    {"method": "GET", "path": "/api/agent/intents", "description": "意图列表（默认近 30 天）", "params": {"from": "起始日", "to": "截止日"}},
    {"method": "GET", "path": "/api/agent/year-review", "description": "年度故事卡（Wrapped 风数据卡+金句，每周自动重算）", "params": {"year": "默认今年"}},
    {"method": "GET", "path": "/api/agent/capsules", "description": "时间胶囊列表：未到期的只给 meta（locked=true，content 为 null），到期才给明文"},
    # ---- 设置 / 备份 / 管理 ----
    {"method": "GET", "path": "/api/agent/settings", "description": "只读脱敏设置：功能开关/模型名等白名单字段，绝不返回任何 key/token/password"},
    {"method": "GET", "path": "/api/agent/backup/verify", "description": "验证最近一次备份是否完整可恢复（重跑校验和抽查）"},
    {"method": "POST", "path": "/api/agent/admin/reclassify", "description": "触发历史记录重判分类（后台线程，需已配 AI）"},
    {"method": "GET", "path": "/api/agent/admin/reclassify-status", "description": "重判进度（running/processed/total/done）"},
]


@app.get("/api/agent/capabilities", dependencies=[Depends(agent_auth)])
def agent_capabilities():
    return {"total": len(AGENT_CAPABILITIES), "endpoints": AGENT_CAPABILITIES,
            "health": "GET /api/healthz 免鉴权探活（无需 token），可用来确认小满在线与版本",
            "auth": "全部 /api/agent/* 需请求头 Authorization: Bearer <token>（设置 → Agent 查看）；"
                    "token 未配置整域 403（功能未启用），token 缺失/错误 401"}


@app.get("/api/agent/reports", dependencies=[Depends(agent_auth)])
def agent_list_reports(type: str | None = Query(None), limit: int = Query(20, ge=1, le=200)):
    return _reports_list_payload(type, limit)


@app.get("/api/agent/reports/{report_id}", dependencies=[Depends(agent_auth)])
def agent_get_report(report_id: str):
    row = db.q1("SELECT * FROM reports WHERE id=?", (report_id,))
    if row is None:
        raise HTTPException(404, "报告不存在")
    return ai_mod.report_detail(row)


@app.post("/api/agent/reports/generate", status_code=201, dependencies=[Depends(agent_auth)])
def agent_generate_report(body: ReportIn):
    if body.type not in REPORT_TYPES:
        raise HTTPException(400, "type 只能是 daily / weekly / monthly")
    _parse_date(body.date)
    try:
        return ai_mod.generate_report(body.type, body.date)
    except ai_mod.NoDataError:
        raise HTTPException(422, "该时间段没有记录")
    except ai_mod.AIError as e:
        # M10 裁定保留：502 外抛 AIError 原文——agent 已认证、web 用户需要这条提示排查配置
        raise HTTPException(502, str(e))


@app.get("/api/agent/knowledge", dependencies=[Depends(agent_auth)])
def agent_list_knowledge(status: str | None = Query(None), type: str | None = Query(None)):
    return _knowledge_list_payload(status, type)


@app.post("/api/agent/ask", dependencies=[Depends(agent_auth)])
def agent_ask(payload: dict = Body(...)):
    return _ask_payload(str((payload or {}).get("question") or ""))


@app.get("/api/agent/assets", dependencies=[Depends(agent_auth)])
def agent_list_assets(
    kind: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return _assets_payload(kind, limit, offset)


@app.get("/api/agent/assets/{att_id}/text", dependencies=[Depends(agent_auth)])
def agent_asset_text(att_id: str):
    row = db.q1("SELECT * FROM attachments WHERE id=?", (att_id,))
    if row is None:
        raise HTTPException(404, "附件不存在")
    ext = Path(row["filename"] or "").suffix.lower()
    if ext not in ai_mod.TEXT_FILE_EXTS or (row["size"] or 0) > 200_000:
        raise HTTPException(400, "该文件不支持提取文本（类型或大小限制）")
    path = db.MEDIA_DIR / row["path"]
    if not path.exists():
        raise HTTPException(404, "附件文件已丢失")
    text = path.read_bytes()[:200_000].decode("utf-8", errors="replace")
    return {"filename": row["filename"], "text": text}


@app.get("/api/agent/calendar", dependencies=[Depends(agent_auth)])
def agent_calendar(month: str = Query(...)):
    return _calendar_payload(month)


@app.get("/api/agent/growth/summary", dependencies=[Depends(agent_auth)])
def agent_growth_summary():
    row = db.q1("SELECT * FROM growth_summaries ORDER BY created_at DESC LIMIT 1")
    return {"summary": ai_mod.summary_detail(row) if row else None}


@app.get("/api/agent/growth/roadmap", dependencies=[Depends(agent_auth)])
def agent_growth_roadmap():
    return roadmap_payload()


@app.get("/api/agent/growth/forecast/latest", dependencies=[Depends(agent_auth)])
def agent_growth_forecast():
    row = db.q1("SELECT * FROM forecasts ORDER BY generated_at DESC LIMIT 1")
    return {"forecast": ai_mod.forecast_detail(row) if row else None}


@app.get("/api/agent/growth/experiments", dependencies=[Depends(agent_auth)])
def agent_growth_experiments():
    return list_growth_experiments()


@app.post("/api/agent/growth/experiments", status_code=201, dependencies=[Depends(agent_auth)])
def agent_create_growth_experiment(payload: dict = Body(...)):
    return create_growth_experiment(payload)


@app.post("/api/agent/growth/experiments/{experiment_id}/finish", dependencies=[Depends(agent_auth)])
def agent_finish_growth_experiment(experiment_id: str, payload: dict | None = Body(None)):
    return finish_growth_experiment(experiment_id, payload)


@app.get("/api/agent/export/all", dependencies=[Depends(agent_auth)])
def agent_export_all():
    rows = db.q("SELECT * FROM entries WHERE deleted_at IS NULL ORDER BY occurred_at DESC")
    experiments = db.q(
        "SELECT * FROM self_experiments ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, "
        "end_date DESC, COALESCE(ended_at, created_at) DESC"
    )
    return {
        "exported_at": db.now_iso(),
        "entries": [entry_to_dict(r) for r in rows],
        "knowledge": _knowledge_list_payload(None, None)["items"],
        "reports": _reports_list_payload(None, 1000)["items"],
        "experiments": [_experiment_payload(r) for r in experiments],
        "stats": stats_mod.overview(),
    }


@app.get("/api/agent/circle/entities", dependencies=[Depends(agent_auth)])
def agent_circle_list(
    type: str | None = Query(None),
    status: str | None = Query(None),
    needs_review: bool | None = Query(None),
):
    # M2：非法 status 与 insights/categories 一致 400，不静默回空
    if status is not None and status not in ("active", "draft", "confirmed", "rejected", "all"):
        raise HTTPException(400, "status 只能是 active / draft / confirmed / rejected / all")
    return circle_list(type, status, needs_review)


@app.get("/api/agent/circle/entities/{eid}", dependencies=[Depends(agent_auth)])
def agent_circle_detail(eid: str):
    return _entity_detail(eid)


@app.get("/api/agent/circle/graph", dependencies=[Depends(agent_auth)])
def agent_circle_graph():
    """圈子关系图：节点（confirmed+active 档案）与边。"""
    return circle_mod.graph_data()


@app.get("/api/agent/categories", dependencies=[Depends(agent_auth)])
def agent_categories(status: str = Query("active")):
    return list_categories(status)


@app.get("/api/agent/entries/{entry_id}/links", dependencies=[Depends(agent_auth)])
def agent_entry_links(entry_id: str):
    _get_entry_or_404(entry_id)
    from . import links as links_mod
    return {"items": links_mod.links_for_entry(entry_id)}


# ---------------- Agent API：新功能读取（B） ----------------

@app.get("/api/agent/insights", dependencies=[Depends(agent_auth)])
def agent_insights(status: str = Query("active")):
    if status not in ("active", "dismissed", "all"):
        raise HTTPException(400, "status 只能是 active / dismissed / all")
    from . import insights as insights_mod
    return {"items": insights_mod.list_by_status(status)}


@app.get("/api/agent/intent", dependencies=[Depends(agent_auth)])
def agent_get_intent(date: str | None = Query(None)):
    return get_intent(date)


@app.put("/api/agent/intent", dependencies=[Depends(agent_auth)])
def agent_put_intent(payload: dict = Body(...)):
    return put_intent(payload)


@app.get("/api/agent/intents", dependencies=[Depends(agent_auth)])
def agent_list_intents(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
):
    today = datetime.now().astimezone().date()
    d0 = _parse_date(from_) if from_ else (today - timedelta(days=30)).isoformat()
    d1 = _parse_date(to) if to else today.isoformat()
    rows = db.q("SELECT * FROM daily_intents WHERE day>=? AND day<=? ORDER BY day", (d0, d1))
    return {"items": [{"day": r["day"], "text": r["text"], "updated_at": r["updated_at"]}
                      for r in rows]}


@app.get("/api/agent/year-review", dependencies=[Depends(agent_auth)])
def agent_year_review(year: int | None = Query(None)):
    return year_review(year)


@app.get("/api/agent/backup/verify", dependencies=[Depends(agent_auth)])
def agent_backup_verify():
    return backup_verify()


@app.get("/api/agent/capsules", dependencies=[Depends(agent_auth)])
def agent_capsules():
    """时间胶囊：未到期（locked=true）只给 meta，绝不给 content 明文；到期才带明文。"""
    today = date.today().isoformat()
    rows = db.q("SELECT * FROM capsules ORDER BY unlock_date")
    items = []
    for r in rows:
        locked = r["unlock_date"] > today
        items.append({
            "id": r["id"], "unlock_date": r["unlock_date"], "created_at": r["created_at"],
            "dismissed": bool(r["dismissed"]), "locked": locked,
            "content": None if locked else r["content"],
        })
    return {"items": items}


@app.get("/api/agent/settings", dependencies=[Depends(agent_auth)])
def agent_settings():
    """只读脱敏设置。原则：白名单式——只挑选功能开关/模型名等安全字段；
    任何 key / token / password 类机密一律不出这个函数（是否已配置用 *_configured
    布尔表达），新增字段前必须先过这一关。"""
    return {
        "version": VERSION,
        "ai_model": db.get_setting("ai_model"),
        "ai_fast_model": db.get_setting("ai_fast_model"),
        "embed_model": db.get_setting("embed_model"),
        "ai_rewrite_model": db.get_setting("ai_rewrite_model"),
        "ai_base_url": db.get_setting("ai_base_url"),
        "ai_fast_base_url": db.get_setting("ai_fast_base_url"),
        "embed_base_url": db.get_setting("embed_base_url"),
        "ai_vision": db.get_setting("ai_vision") == "true",
        "ai_effort": db.get_setting("ai_effort"),
        "ai_fast_effort": db.get_setting("ai_fast_effort"),
        "weather_enabled": _weather_enabled(),
        "weather_city_only": db.get_setting("weather_city_only") == "true",
        "default_city": db.get_setting("default_city"),
        "push_enabled": db.get_setting("push_enabled", "true") != "false",
        "auto_report_enabled": db.get_setting("auto_report_enabled", "true") != "false",
        "circle_enabled": circle_mod.enabled(),
        "circle_auto_confirm": db.get_setting("circle_auto_confirm", "1") not in ("0", "false"),
        "link_discovery": db.get_setting("link_discovery", "1") not in ("0", "false"),
        "category_ai": _category_ai_on(),
        "log_presets": _log_presets(),
        "ai_configured": ai_mod.strong_configured(),
        "ai_fast_configured": ai_mod.fast_configured(),
        "embed_configured": ai_mod.embed_configured(),
        "push_configured": bool(db.get_setting("push_key")),
        "access_lock_enabled": bool(db.get_setting("access_key")),
        "last_backup_at": db.get_setting("last_backup_at"),
    }


# ---------------- Agent API：审核操作（C，全部复用主站处理函数） ----------------

@app.get("/api/agent/circle/questions", dependencies=[Depends(agent_auth)])
def agent_circle_questions(status: str = Query("pending")):
    # M2：非法 status 400（口径与主站表一致）
    if status not in ("pending", "answered", "dismissed", "all"):
        raise HTTPException(400, "status 只能是 pending / answered / dismissed / all")
    return circle_questions(status)


@app.post("/api/agent/circle/questions/answer-batch", dependencies=[Depends(agent_auth)])
def agent_circle_answer_batch(body: BatchAnswerIn):
    return circle_questions_answer_batch(body)


@app.get("/api/agent/circle/auto-log", dependencies=[Depends(agent_auth)])
def agent_circle_auto_log(days: int = Query(14, ge=1, le=90)):
    return circle_auto_log_list(days)


@app.post("/api/agent/circle/auto-log/{aid}/undo", dependencies=[Depends(agent_auth)])
def agent_circle_auto_undo(aid: str):
    return circle_auto_log_undo(aid)


@app.get("/api/agent/circle/proposals", dependencies=[Depends(agent_auth)])
def agent_circle_proposals(status: str = Query("pending")):
    # M2：非法 status 400
    if status not in ("pending", "accepted", "rejected", "all"):
        raise HTTPException(400, "status 只能是 pending / accepted / rejected / all")
    return circle_proposals(status)


# 注意：merge 必须注册在 {eid} 系列之前，避免被路径参数吃掉
@app.post("/api/agent/circle/entities/merge", dependencies=[Depends(agent_auth)])
def agent_circle_merge(body: MergeIn):
    return circle_merge(body)


@app.patch("/api/agent/circle/entities/{eid}", dependencies=[Depends(agent_auth)])
def agent_circle_patch(eid: str, payload: dict = Body(...)):
    return circle_patch(eid, payload)


@app.post("/api/agent/circle/entities/{eid}/confirm", dependencies=[Depends(agent_auth)])
def agent_circle_confirm(eid: str):
    return circle_confirm(eid)


@app.post("/api/agent/circle/entities/{eid}/reject", dependencies=[Depends(agent_auth)])
def agent_circle_reject(eid: str):
    return circle_reject(eid)


@app.post("/api/agent/links/{lid}/confirm", dependencies=[Depends(agent_auth)])
def agent_link_confirm(lid: str):
    return link_confirm(lid)


@app.post("/api/agent/links/{lid}/dismiss", dependencies=[Depends(agent_auth)])
def agent_link_dismiss(lid: str):
    return link_dismiss(lid)


@app.post("/api/agent/knowledge/{kid}/accept", dependencies=[Depends(agent_auth)])
def agent_knowledge_accept(kid: str):
    return accept_knowledge(kid)


@app.post("/api/agent/knowledge/{kid}/reject", dependencies=[Depends(agent_auth)])
def agent_knowledge_reject(kid: str):
    return reject_knowledge(kid)


@app.post("/api/agent/categories/{cid}/approve", dependencies=[Depends(agent_auth)])
def agent_category_approve(cid: int):
    return approve_category(cid)


@app.post("/api/agent/categories/{cid}/reject", dependencies=[Depends(agent_auth)])
def agent_category_reject(cid: int):
    return reject_category(cid)


@app.post("/api/agent/admin/reclassify", dependencies=[Depends(agent_auth)])
def agent_admin_reclassify():
    return admin_reclassify()


@app.get("/api/agent/admin/reclassify-status", dependencies=[Depends(agent_auth)])
def agent_admin_reclassify_status():
    return admin_reclassify_status()


# ---------------- 静态文件 ----------------

@app.get("/", include_in_schema=False)
def index():
    page = STATIC_DIR / "index.html"
    if page.exists():
        return FileResponse(page)
    raise HTTPException(404, "前端文件 static/index.html 不存在")


db.ensure_dirs()  # 挂载静态目录前确保存在
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/media", StaticFiles(directory=db.MEDIA_DIR), name="media")
app.mount("/thumbs", StaticFiles(directory=db.THUMBS_DIR), name="thumbs")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=52122)
