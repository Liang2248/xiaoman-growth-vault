"""导出与备份：Markdown zip、知识 Markdown、单条 HTML、整库备份 zip（带 manifest 自检）。"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import sqlite3
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from . import db, logx


class BackupError(Exception):
    """备份自检未通过；message 为面向用户的中文原因。"""

KTYPE_LABEL = {
    "experience": "经验",
    "pitfall": "踩坑记录",
    "case": "案例",
    "sop": "SOP",
    "skill": "技能",
}
CATEGORY_LABEL = {"work": "工作", "life": "生活", "mixed": "混合"}
# A4-45：单张图片内联进 HTML 的上限，超过则只列文件名（避免导出时内存尖峰）
INLINE_IMAGE_MAX_BYTES = 8 * 1024 * 1024


def _tmp_zip(prefix: str) -> Path:
    return db.BACKUPS_DIR / f".{prefix}-{db.new_id()[:12]}.zip"


def _safe_front(value) -> str:
    """A4-49：frontmatter 的值统一走 json.dumps（YAML 是 JSON 超集，双引号串可安全承载
    换行转义），避免字段里的 \\n 破坏/注入 YAML 结构。"""
    return json.dumps(str(value if value is not None else ""), ensure_ascii=False)


def _entry_markdown(e, atts, links) -> str:
    lines = ["---"]
    lines.append(f"id: {e['id']}")
    lines.append(f"date: {e['occurred_at']}")
    lines.append(f"category: {e['category']}")
    try:
        tags = json.loads(e["tags"] or "[]")
    except ValueError:
        tags = []
    if not isinstance(tags, list):
        tags = [tags]
    lines.append("tags: " + json.dumps(tags, ensure_ascii=False))
    if e["location_name"]:
        lines.append("location: " + _safe_front(e["location_name"]))
    if e["weather_json"]:
        lines.append("weather: " + _safe_front(e["weather_json"]))
    lines.append(f"created: {e['created_at']}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {e['title'] or '（无标题）'}")
    lines.append("")
    if e["summary"]:
        lines.append(f"> {e['summary']}")
        lines.append("")
    if e["content"]:
        lines.append(e["content"])
        lines.append("")
    if atts:
        lines.append("## 附件")
        for a in atts:
            lines.append(f"- [{_md_text(a['filename'])}](../media/{a['path']}) ({a['kind']})")
        lines.append("")
    if links:
        lines.append("## 链接")
        for l in links:
            lines.append(f"- [{_md_text(l['title'] or l['url'])}]({l['url']})")
        lines.append("")
    return "\n".join(lines)


def _md_text(s) -> str:
    """A4-32：Markdown 链接文本里的 [] 会破坏语法，统一转义。"""
    return str(s or "").replace("[", "\\[").replace("]", "\\]").replace("\n", " ")


def entry_markdown(entry_id: str):
    """单条记录的 markdown 导出数据。返回 (entry_row, attachments, md_text) 或 None。"""
    e = db.q1("SELECT * FROM entries WHERE id=?", (entry_id,))
    if e is None:
        return None
    atts = db.q("SELECT * FROM attachments WHERE entry_id=? ORDER BY created_at", (entry_id,))
    links = db.q("SELECT * FROM links WHERE entry_id=? ORDER BY created_at", (entry_id,))
    return e, atts, _entry_markdown(e, atts, links)


def media_path(rel: str) -> Path | None:
    """A4-44：所有"从 attachments.path 读文件"的入口都要过这里。

    pathlib 的语义是危险的：`MEDIA_DIR / "C:/Windows/win.ini"` 会**替换**基路径。
    该列的正常写入值只可能是 sha256[:2]/sha256+ext，但历史数据/手工改库可能污染它，
    读取侧必须自己保证不逃出媒体目录。非法值时返回 None，由调用方当作"文件已丢失"。
    """
    if not rel:
        return None
    try:
        root = db.MEDIA_DIR.resolve()
        p = (db.MEDIA_DIR / rel).resolve()
        if not p.is_relative_to(root):
            logx.log("媒体路径非法，已按文件丢失处理", f"{rel}")
            return None
        return p
    except (OSError, ValueError):
        return None


def _zip_member_name(rel: str) -> str:
    """A4-44：zip 成员名也要中性化，避免 `..` 造成解压侧 zip-slip。"""
    parts = [p for p in str(rel).replace("\\", "/").split("/")
             if p not in ("", ".", "..")]
    return "/".join(parts)


def _rows_by_entry(table: str, entry_ids: list[str], order_by: str = "created_at") -> dict:
    """A4-46：把"每条记录各查一次附件/链接"的 1+2N 次查询压成按批的少量查询。"""
    out: dict[str, list] = {}
    for i in range(0, len(entry_ids), 400):
        chunk = entry_ids[i:i + 400]
        marks = ",".join("?" * len(chunk))
        for r in db.q(f"SELECT * FROM {table} WHERE entry_id IN ({marks}) ORDER BY {order_by}",
                      tuple(chunk)):
            out.setdefault(r["entry_id"], []).append(r)
    return out


def build_markdown_zip(date_from: str | None, date_to: str | None) -> Path:
    sql = "SELECT * FROM entries WHERE deleted_at IS NULL"
    params: list = []
    if date_from:
        sql += " AND substr(occurred_at,1,10) >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND substr(occurred_at,1,10) <= ?"
        params.append(date_to)
    sql += " ORDER BY occurred_at"
    rows = db.q(sql, tuple(params))

    # A4-46：原先对每条记录各发两次查询（附件、链接）→ 1+2N；这里改成两次批量查询
    ids = [e["id"] for e in rows]
    atts_by_entry = _rows_by_entry("attachments", ids)
    links_by_entry = _rows_by_entry("links", ids)

    tmp = _tmp_zip("export")
    seen_media: set[str] = set()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for e in rows:
            atts = atts_by_entry.get(e["id"], [])
            links = links_by_entry.get(e["id"], [])
            day = (e["occurred_at"] or "")[:10]
            z.writestr(f"entries/{day}-{e['id'][:8]}.md", _entry_markdown(e, atts, links))
            for a in atts:
                if a["path"] in seen_media:
                    continue
                p = media_path(a["path"])
                if p is not None and p.exists():
                    z.write(p, f"media/{_zip_member_name(a['path'])}")
                    seen_media.add(a["path"])
    return tmp


def knowledge_markdown() -> str:
    rows = db.q(
        "SELECT * FROM knowledge WHERE status='accepted' ORDER BY type, created_at"
    )
    lines = ["# 成长知识库", ""]
    current_type = None
    for r in rows:
        if r["type"] != current_type:
            current_type = r["type"]
            lines.append(f"\n## {KTYPE_LABEL.get(current_type, current_type)}")
            lines.append("")
        lines.append(f"### {r['title']}")
        lines.append("")
        lines.append(r["content"])
        lines.append("")
    if not rows:
        lines.append("\n（暂无已采纳的知识条目）")
    return "\n".join(lines)


def entry_html(entry_id: str) -> str | None:
    e = db.q1("SELECT * FROM entries WHERE id=?", (entry_id,))
    if e is None:
        return None
    atts = db.q("SELECT * FROM attachments WHERE entry_id=? ORDER BY created_at", (entry_id,))
    links = db.q("SELECT * FROM links WHERE entry_id=? ORDER BY created_at", (entry_id,))
    try:
        tags = json.loads(e["tags"] or "[]")
    except ValueError:
        tags = []
    if not isinstance(tags, list):
        # A4-48：tags 存成字符串时按字符拆分会输出错乱，统一归一为列表
        tags = [tags]
    try:
        weather = json.loads(e["weather_json"]) if e["weather_json"] else None
    except ValueError:
        weather = None
    if not isinstance(weather, dict):
        # A4-48：合法 JSON 但不是对象（[]/"晴"/123）时 .get 会抛 AttributeError
        weather = None

    def esc(s) -> str:
        return html.escape(str(s or ""))

    def safe_href(url) -> str:
        """A4-51：只做 HTML 转义挡不住 javascript: 等危险 scheme，这里加白名单。"""
        u = str(url or "")
        try:
            if urlparse(u).scheme.lower() not in ("http", "https"):
                return "#"
        except ValueError:
            return "#"
        return esc(u)

    meta = [f"时间：{esc(e['occurred_at'][:16].replace('T', ' '))}",
            f"分类：{CATEGORY_LABEL.get(e['category'], e['category'])}"]
    if tags:
        meta.append("标签：" + "、".join(esc(t) for t in tags))
    if e["location_name"]:
        meta.append("地点：" + esc(e["location_name"]))
    if weather:
        w = [esc(weather.get("text"))]
        if weather.get("temperature_c") is not None:
            w.append(f"{weather['temperature_c']}°C")
        if weather.get("humidity") is not None:
            w.append(f"湿度{weather['humidity']}%")
        meta.append("天气：" + " ".join(x for x in w if x))

    body_html = esc(e["content"]).replace("\n", "<br>\n")

    media_parts = []
    for a in atts:
        p = media_path(a["path"])
        if a["kind"] == "image" and p is not None and p.exists():
            # A4-45：不无上限地把原图 base64 内联（手机原图 5-20MB 会造成内存尖峰）
            try:
                if p.stat().st_size <= INLINE_IMAGE_MAX_BYTES:
                    b64 = base64.b64encode(p.read_bytes()).decode()
                    media_parts.append(
                        f'<figure><img src="data:{esc(a["mime"])};base64,{b64}" alt="{esc(a["filename"])}">'
                        f"<figcaption>{esc(a['filename'])}</figcaption></figure>"
                    )
                    continue
                media_parts.append(f'<li>图片：{esc(a["filename"])}（原图过大，未内联）</li>')
                continue
            except OSError:
                pass
        label = {"video": "视频", "audio": "音频"}.get(a["kind"], "文件")
        media_parts.append(f'<li>{label}：{esc(a["filename"])}</li>')
    # 图片 figure 与文件 li 混合时，把连续的 li 包一层 ul
    media_html = ""
    buf: list[str] = []
    for m in media_parts:
        if m.startswith("<li>"):
            buf.append(m)
        else:
            if buf:
                media_html += "<ul>" + "".join(buf) + "</ul>"
                buf = []
            media_html += m
    if buf:
        media_html += "<ul>" + "".join(buf) + "</ul>"

    links_html = ""
    if links:
        items = "".join(
            f'<li><a href="{safe_href(l["url"])}" rel="noopener noreferrer">'
            f'{esc(l["title"] or l["url"])}</a>'
            + (f' — {esc(l["description"])}' if l["description"] else "")
            + "</li>"
            for l in links
        )
        links_html = f"<h2>链接</h2><ul>{items}</ul>"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(e['title'] or '成长记录')}</title>
<style>
  body {{ background: #fdf6ec; color: #4a3b2a; font-family: "PingFang SC", "Microsoft YaHei", sans-serif;
         max-width: 760px; margin: 0 auto; padding: 32px 20px; line-height: 1.8; }}
  h1 {{ color: #b5651d; border-bottom: 2px solid #f0dfc8; padding-bottom: 12px; font-size: 1.6em; }}
  h2 {{ color: #c07a3a; font-size: 1.15em; margin-top: 28px; }}
  .meta {{ color: #a08c74; font-size: 0.9em; margin-bottom: 20px; }}
  .meta span {{ margin-right: 14px; }}
  .summary {{ background: #f7ead7; border-left: 4px solid #d99a4e; padding: 10px 16px; border-radius: 4px; }}
  .content {{ margin-top: 20px; white-space: normal; }}
  figure {{ margin: 16px 0; }}
  figure img {{ max-width: 100%; border-radius: 8px; box-shadow: 0 2px 10px rgba(180,140,90,.25); }}
  figcaption {{ color: #a08c74; font-size: 0.85em; margin-top: 6px; }}
  a {{ color: #c0702a; }}
  ul {{ padding-left: 22px; }}
  .footer {{ margin-top: 40px; color: #b9a78d; font-size: 0.8em; text-align: center; }}
</style>
</head>
<body>
<h1>{esc(e['title'] or '（无标题）')}</h1>
<div class="meta">{''.join(f'<span>{m}</span>' for m in meta)}</div>
{f'<p class="summary">{esc(e["summary"])}</p>' if e['summary'] else ''}
<div class="content">{body_html}</div>
{f'<h2>附件</h2>{media_html}' if media_html else ''}
{links_html}
<div class="footer">导出自 成长证据库 · {esc(e['created_at'][:10])}</div>
</body>
</html>"""


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_stream_hashed(z: zipfile.ZipFile, arc: str, p: Path) -> tuple[str, int]:
    """A4-41：一次读取同时完成"写进 zip"与"算哈希"。

    用 z.open(arc,'w') 流式写入，边读边 update 哈希——zip 里的字节与 manifest 里的
    sha256 必然来自同一份数据，且不需要把整个文件读进内存（vault.db 可能很大）。
    """
    h = hashlib.sha256()
    size = 0
    big = p.stat().st_size >= 2 * 1024 * 1024 * 1024  # 超过 2GB 时强制 zip64
    with p.open("rb") as f, z.open(arc, "w", force_zip64=big) as out:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            out.write(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _snapshot_db(tmp_db: Path) -> None:
    """把 vault.db 一致快照到 tmp_db。

    A4-43：优先用**独立连接**做在线备份，不持有全局 _lock —— `c.backup()` 会逐页复制
    整个数据库（含 FTS 影子表、向量 JSON），原先在锁内执行会把所有 HTTP 请求、后台线程
    全部阻塞到拷贝结束。SQLite 的在线备份 API 自带读事务，WAL 下写者不阻塞读者，
    这正是它的标准用法。

    独立连接万一因并发写拿不到锁而失败，回退到原先"持锁快照"的路径：宁可慢一次，
    也绝不让备份直接失败（备份是唯一的数据保全通道）。
    """
    try:
        src = sqlite3.connect(str(db.DB_PATH), timeout=10)
        try:
            dst = sqlite3.connect(str(tmp_db))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return
    except sqlite3.Error as e:
        logx.log("独立连接备份快照失败，回退持锁快照", str(e))
    with db.locked() as c:
        dst = sqlite3.connect(str(tmp_db))
        try:
            c.backup(dst)
        finally:
            dst.close()


def build_backup_zip() -> Path:
    """打包 vault.db（一致性快照）+ media/ 原件，内嵌 manifest.json（每文件 sha256+大小）。
    自检：快照库 PRAGMA integrity_check + 全量哈希复算 + zipfile.testzip()。
    失败抛 BackupError（中文原因）。"""
    tmp_db = db.BACKUPS_DIR / f".backup-snapshot-{db.new_id()[:12]}.db"
    _snapshot_db(tmp_db)
    # 快照库自检：B 树完整性 + FTS5 索引一致性（A4-13：integrity_check 不覆盖 FTS5）
    chk = sqlite3.connect(str(tmp_db))
    try:
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
        if ok == "ok":
            try:
                chk.execute("INSERT INTO entries_fts(entries_fts) VALUES('integrity-check')")
            except sqlite3.Error as e:
                logx.log("备份快照 FTS 索引异常", f"{e}（建议重启后在诊断页重建索引）")
    finally:
        chk.close()
    if ok != "ok":
        tmp_db.unlink(missing_ok=True)
        raise BackupError(f"数据库快照完整性自检未通过：{str(ok)[:120]}")

    tmp_zip = _tmp_zip("backup")
    try:
        manifest = {"version": 2, "created_at": db.now_iso(), "files": {},
                    "attachments": [], "missing": [], "skipped": []}
        # A4-42：manifest 带上"数据库引用清单"，让自检能判断"DB 引用的文件是否都在包里"
        attachments = [
            {"path": r["path"], "sha256": r["sha256"], "size": r["size"]}
            for r in db.q("SELECT path, sha256, size FROM attachments")
        ]
        manifest["attachments"] = attachments
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as z:
            # A4-41：vault.db 快照创建后无并发写入，但同样坚持"一次读取、同时算哈希"
            st = tmp_db.stat()
            db_sha, db_size = _write_stream_hashed(z, "vault.db", tmp_db)
            if db_size != st.st_size:
                raise BackupError("数据库快照在打包期间发生变化，请重试备份")
            manifest["files"]["vault.db"] = {"sha256": db_sha, "size": db_size}
            for p in sorted(db.MEDIA_DIR.rglob("*")):
                if not p.is_file() or p.name.startswith(".tmp-"):
                    continue
                arc = f"media/{p.relative_to(db.MEDIA_DIR).as_posix()}"
                # A4-41：先记录打包前的大小/修改时间，流式"读 → 算哈希 → 写进 zip"
                # （三者是同一次读取），打包后再比对一次；变了就说明文件还在被写入，
                # 本次入包的内容不可信 → 不写入 manifest，若被数据库引用则备份判为不完整。
                try:
                    st_before = p.stat()
                    sha, size = _write_stream_hashed(z, arc, p)
                    st_after = p.stat()
                except OSError as e:
                    logx.log("备份跳过无法读取的媒体文件", f"{arc}：{e}")
                    manifest["skipped"].append(arc)
                    continue
                if (st_after.st_size, st_after.st_mtime_ns) != \
                        (st_before.st_size, st_before.st_mtime_ns):
                    logx.log("媒体文件在打包期间仍被写入，本次备份不完整", arc)
                    manifest["skipped"].append(arc)
                    continue
                manifest["files"][arc] = {"sha256": sha, "size": size}
            # A4-42：DB 引用的每个媒体文件都必须成功入包。缺了就是备份不完整——
            # 备份文件仍然产出（数据先保住），但 manifest 里留下 missing[]，让
            # /api/backup/verify 报"不完整"而不是骗人的"完整可恢复"。
            for a in attachments:
                arc = f"media/{_zip_member_name(a['path'])}"
                if arc not in manifest["files"]:
                    manifest["missing"].append(a["path"])
            if manifest["missing"]:
                logx.log("备份不完整：数据库引用的媒体文件缺失",
                         f"{len(manifest['missing'])} 个，例如 {manifest['missing'][0]}")
            z.writestr("manifest.json",
                       json.dumps(manifest, ensure_ascii=False, indent=1))
        with zipfile.ZipFile(tmp_zip) as zf:
            # A4-50：用上下文管理器持有句柄，不依赖 CPython 引用计数及时回收
            bad = zf.testzip()
        if bad is not None:
            raise BackupError(f"备份压缩包自检未通过：{bad} 损坏")
        # A4-41/42：备份成功后对包内**每一个**成员复算哈希并与 manifest 比对
        _audit_zip_against_manifest(tmp_zip, manifest)
    except Exception:
        tmp_zip.unlink(missing_ok=True)
        raise
    finally:
        tmp_db.unlink(missing_ok=True)
    return tmp_zip


def _audit_zip_against_manifest(zip_path: Path, manifest: dict) -> None:
    """把"自检"做成真正独立的验证：重新打开 zip，逐成员复算哈希。"""
    files = manifest.get("files") or {}
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if name == "manifest.json":
                continue
            want = files.get(name)
            if not want or not want.get("sha256"):
                continue
            got = hashlib.sha256(z.read(name)).hexdigest()
            if got != want["sha256"]:
                raise BackupError(f"备份自检未通过：{name} 校验和不符")


def verify_backup(path: Path, sample_media: int = 5, full: bool = False) -> dict:
    """对一个备份 zip 重跑 testzip + 校验 vault.db 全量与 media 的 manifest 校验和，
    并校验"数据库引用的媒体文件是否都在包里"。返回 {ok, detail}；不抛异常。

    A4-42：抽查比例提高（默认 min(50, max(5, n//10))），full=True 时全量校验；
    报告文案明确区分"抽查通过"与"全量通过"。
    """
    try:
        with zipfile.ZipFile(path) as z:
            bad = z.testzip()
            if bad is not None:
                return {"ok": False, "detail": f"压缩包损坏：{bad}"}
            names = set(z.namelist())
            if "vault.db" not in names or "manifest.json" not in names:
                return {"ok": False, "detail": "备份缺少 vault.db 或 manifest.json（旧版备份无清单）"}
            try:
                manifest = json.loads(z.read("manifest.json").decode("utf-8"))
            except ValueError:
                return {"ok": False, "detail": "manifest.json 无法解析"}
            files = manifest.get("files") or {}
            # vault.db 全量校验
            want = files.get("vault.db") or {}
            got = hashlib.sha256(z.read("vault.db")).hexdigest()
            if want.get("sha256") and want["sha256"] != got:
                return {"ok": False, "detail": "vault.db 校验和不符，备份可能已损坏"}
            # A4-42：引用完整性——DB 声明的附件是否都在包里
            missing = list(manifest.get("missing") or [])
            attachments = manifest.get("attachments")
            if isinstance(attachments, list) and attachments:
                for a in attachments:
                    arc = f"media/{_zip_member_name(str(a.get('path') or ''))}"
                    if arc not in names and a.get("path") not in missing:
                        missing.append(a.get("path"))
            if missing:
                return {"ok": False,
                        "detail": f"备份不完整：{len(missing)} 个数据库引用的媒体文件不在包里"
                                  f"（例如 {missing[0]}）"}
            # media 校验和
            media = [n for n in sorted(names) if n.startswith("media/")]
            if full:
                targets = media
            else:
                n = max(1, min(50, max(sample_media, len(media) // 10)))
                step = max(1, len(media) // n)
                targets = media[::step][:n]
            checked = 0
            for n in targets:
                w = files.get(n) or {}
                if w.get("sha256"):
                    if hashlib.sha256(z.read(n)).hexdigest() != w["sha256"]:
                        return {"ok": False, "detail": f"{n} 校验和不符"}
                    checked += 1
            scope = "全量" if full else "抽查"
            return {"ok": True,
                    "detail": f"vault.db 校验通过，{scope}校验 {checked} 个媒体文件一致，"
                              f"共 {len(files)} 个文件在册（数据库引用 {len(attachments or [])} 个附件）"}
    except zipfile.BadZipFile:
        return {"ok": False, "detail": "文件不是有效的 zip，备份可能已损坏"}
    except OSError as e:
        return {"ok": False, "detail": f"读取备份失败：{e}"}


def cleanup_stale_tmp(prefixes=(".export-", ".backup-", ".backup-snapshot-"),
                      min_age_s: float = 3600.0) -> int:
    """A4-47：清理导出/备份中断留下的临时文件（名字以 . 开头，不被保留策略覆盖）。

    只删超过 min_age_s 的，避免误删正在进行的导出。返回清理数量。
    """
    removed = 0
    try:
        entries = list(db.BACKUPS_DIR.iterdir())
    except OSError:
        return 0
    now = time.time()
    for p in entries:
        if not p.is_file() or not any(p.name.startswith(x) for x in prefixes):
            continue
        try:
            if now - p.stat().st_mtime < min_age_s:
                continue
            p.unlink()
            removed += 1
        except OSError:
            pass
    if removed:
        logx.log("已清理中断的导出/备份临时文件", f"{removed} 个")
    return removed


def remove_quietly(path: Path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
