"""导出与备份：Markdown zip、知识 Markdown、单条 HTML、整库备份 zip（带 manifest 自检）。"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

from . import db


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


def _tmp_zip(prefix: str) -> Path:
    return db.BACKUPS_DIR / f".{prefix}-{db.new_id()[:12]}.zip"


def _entry_markdown(e, atts, links) -> str:
    lines = ["---"]
    lines.append(f"id: {e['id']}")
    lines.append(f"date: {e['occurred_at']}")
    lines.append(f"category: {e['category']}")
    try:
        tags = json.loads(e["tags"] or "[]")
    except ValueError:
        tags = []
    lines.append("tags: " + json.dumps(tags, ensure_ascii=False))
    if e["location_name"]:
        lines.append(f"location: {e['location_name']}")
    if e["weather_json"]:
        lines.append(f"weather: {e['weather_json']}")
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
            lines.append(f"- [{a['filename']}](../media/{a['path']}) ({a['kind']})")
        lines.append("")
    if links:
        lines.append("## 链接")
        for l in links:
            lines.append(f"- [{l['title'] or l['url']}]({l['url']})")
        lines.append("")
    return "\n".join(lines)


def entry_markdown(entry_id: str):
    """单条记录的 markdown 导出数据。返回 (entry_row, attachments, md_text) 或 None。"""
    e = db.q1("SELECT * FROM entries WHERE id=?", (entry_id,))
    if e is None:
        return None
    atts = db.q("SELECT * FROM attachments WHERE entry_id=? ORDER BY created_at", (entry_id,))
    links = db.q("SELECT * FROM links WHERE entry_id=? ORDER BY created_at", (entry_id,))
    return e, atts, _entry_markdown(e, atts, links)


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

    tmp = _tmp_zip("export")
    seen_media: set[str] = set()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for e in rows:
            atts = db.q("SELECT * FROM attachments WHERE entry_id=? ORDER BY created_at", (e["id"],))
            links = db.q("SELECT * FROM links WHERE entry_id=? ORDER BY created_at", (e["id"],))
            day = e["occurred_at"][:10]
            z.writestr(f"entries/{day}-{e['id'][:8]}.md", _entry_markdown(e, atts, links))
            for a in atts:
                if a["path"] in seen_media:
                    continue
                p = db.MEDIA_DIR / a["path"]
                if p.exists():
                    z.write(p, f"media/{a['path']}")
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
    try:
        weather = json.loads(e["weather_json"]) if e["weather_json"] else None
    except ValueError:
        weather = None

    def esc(s) -> str:
        return html.escape(str(s or ""))

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
        p = db.MEDIA_DIR / a["path"]
        if a["kind"] == "image" and p.exists():
            try:
                b64 = base64.b64encode(p.read_bytes()).decode()
                media_parts.append(
                    f'<figure><img src="data:{esc(a["mime"])};base64,{b64}" alt="{esc(a["filename"])}">'
                    f"<figcaption>{esc(a['filename'])}</figcaption></figure>"
                )
                continue
            except Exception:
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
            f'<li><a href="{esc(l["url"])}">{esc(l["title"] or l["url"])}</a>'
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


def build_backup_zip() -> Path:
    """打包 vault.db（一致性快照）+ media/ 原件，内嵌 manifest.json（每文件 sha256+大小）。
    自检：快照库 PRAGMA integrity_check + zipfile.testzip()。失败抛 BackupError（中文原因）。"""
    tmp_db = db.BACKUPS_DIR / f".backup-snapshot-{db.new_id()[:12]}.db"
    with db.locked() as c:
        dst = sqlite3.connect(str(tmp_db))
        try:
            c.backup(dst)
        finally:
            dst.close()
    # 快照库完整性自检
    chk = sqlite3.connect(str(tmp_db))
    try:
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        chk.close()
    if ok != "ok":
        tmp_db.unlink(missing_ok=True)
        raise BackupError(f"数据库快照完整性自检未通过：{str(ok)[:120]}")

    tmp_zip = _tmp_zip("backup")
    try:
        manifest = {"version": 1, "created_at": db.now_iso(), "files": {}}
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(tmp_db, "vault.db")
            manifest["files"]["vault.db"] = {
                "sha256": _sha256_file(tmp_db), "size": tmp_db.stat().st_size}
            for p in sorted(db.MEDIA_DIR.rglob("*")):
                if p.is_file() and not p.name.startswith(".tmp-"):
                    arc = f"media/{p.relative_to(db.MEDIA_DIR).as_posix()}"
                    z.write(p, arc)
                    manifest["files"][arc] = {
                        "sha256": _sha256_file(p), "size": p.stat().st_size}
            z.writestr("manifest.json",
                       json.dumps(manifest, ensure_ascii=False, indent=1))
        bad = zipfile.ZipFile(tmp_zip).testzip()
        if bad is not None:
            raise BackupError(f"备份压缩包自检未通过：{bad} 损坏")
    except Exception:
        tmp_zip.unlink(missing_ok=True)
        raise
    finally:
        tmp_db.unlink(missing_ok=True)
    return tmp_zip


def verify_backup(path: Path, sample_media: int = 5) -> dict:
    """对一个备份 zip 重跑 testzip + 校验 vault.db 全量与抽查 media 的 manifest 校验和。
    返回 {ok, detail}；不抛异常。"""
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
            # media 抽查
            media = [n for n in names if n.startswith("media/")]
            checked = 0
            for n in media[:: max(1, len(media) // max(1, sample_media))][:sample_media]:
                w = files.get(n) or {}
                if w.get("sha256"):
                    if hashlib.sha256(z.read(n)).hexdigest() != w["sha256"]:
                        return {"ok": False, "detail": f"{n} 校验和不符"}
                    checked += 1
            return {"ok": True,
                    "detail": f"vault.db 校验通过，抽查 {checked} 个媒体文件一致，"
                              f"共 {len(files)} 个文件在册"}
    except zipfile.BadZipFile:
        return {"ok": False, "detail": "文件不是有效的 zip，备份可能已损坏"}
    except OSError as e:
        return {"ok": False, "detail": f"读取备份失败：{e}"}


def remove_quietly(path: Path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
