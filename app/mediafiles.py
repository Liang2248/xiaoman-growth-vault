"""附件处理：上传存储、sha256 去重、缩略图、媒体信息探测。

- 存储路径：data/media/<sha256[:2]>/<sha256><原扩展名>，同 sha256 复用文件
- 图片用 Pillow 校验并生成缩略图 data/thumbs/<sha256>.jpg（最长边 480）
- 视频/音频在有 ffprobe/ffmpeg 时取时长/截图，缺失时优雅跳过
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from . import db

MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 单文件 500MB
CHUNK = 1024 * 1024


class UploadError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _safe_ext(filename: str) -> str:
    ext = Path(filename).suffix.lower()[:16]
    return re.sub(r"[^a-z0-9.]", "", ext)


def _kind_for(mime: str) -> str:
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "file"


def save_upload(entry_id: str, upload) -> str:
    """保存一个 UploadFile 并写入 attachments 行，返回附件 id。

    校验失败抛 UploadError（message 为中文，status 为 HTTP 状态码）。
    """
    filename = upload.filename or "未命名文件"
    ext = _safe_ext(filename)

    sha = hashlib.sha256()
    tmp_path = db.MEDIA_DIR / f".tmp-{uuid.uuid4().hex}"
    size = 0
    try:
        with open(tmp_path, "wb") as f:
            while True:
                chunk = upload.file.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise UploadError("单个文件不能超过 500MB", status=413)
                sha.update(chunk)
                f.write(chunk)
    except UploadError:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise UploadError("文件保存失败，请重试")

    digest = sha.hexdigest()
    rel_path = f"{digest[:2]}/{digest}{ext}"
    final_path = db.MEDIA_DIR / rel_path
    file_existed = final_path.exists()
    if file_existed:
        tmp_path.unlink(missing_ok=True)
    else:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_path, final_path)

    mime = upload.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return _store_final(entry_id, filename, mime, size, digest, rel_path, final_path, file_existed)


def store_bytes(entry_id: str, filename: str, mime: str, data: bytes) -> str:
    """把内存中的字节存为附件（扫描件/裁图用），复用 sha256 去重，返回附件 id。"""
    digest = hashlib.sha256(data).hexdigest()
    ext = _safe_ext(filename)
    rel_path = f"{digest[:2]}/{digest}{ext}"
    final_path = db.MEDIA_DIR / rel_path
    file_existed = final_path.exists()
    if not file_existed:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_bytes(data)
    return _store_final(entry_id, filename, mime, len(data), digest, rel_path, final_path, file_existed)


def _store_final(entry_id: str, filename: str, mime: str, size: int, digest: str,
                 rel_path: str, final_path: Path, file_existed: bool) -> str:
    """文件已落盘后的收尾：媒体信息探测 + 写 attachments 行。"""
    kind = _kind_for(mime)
    width = height = duration_ms = None
    thumb = None

    if kind == "image":
        try:
            width, height, thumb = _process_image(final_path, digest, file_existed)
        except UploadError:
            # 图片损坏：刚写入的文件要清掉（已存在的说明别人在用，保留）
            if not file_existed:
                final_path.unlink(missing_ok=True)
            raise
    elif kind in ("video", "audio"):
        duration_ms = _probe_duration(final_path)
        if kind == "video":
            thumb = _video_thumb(final_path, digest)

    att_id = db.new_id()
    db.execute(
        "INSERT INTO attachments(id, entry_id, sha256, mime, filename, size, "
        "width, height, duration_ms, kind, path, thumb, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (att_id, entry_id, digest, mime, filename, size,
         width, height, duration_ms, kind, rel_path, thumb, db.now_iso()),
    )
    return att_id


def _process_image(final_path: Path, digest: str, file_existed: bool):
    """Pillow 校验 + 尺寸 + 缩略图。失败抛 UploadError。"""
    try:
        from PIL import Image
    except ImportError:
        raise UploadError("服务器缺少 Pillow，无法处理图片", status=500)
    try:
        with Image.open(final_path) as im:
            im.verify()
        with Image.open(final_path) as im:
            width, height = im.size
            thumb_name = f"{digest}.jpg"
            thumb_path = db.THUMBS_DIR / thumb_name
            if not thumb_path.exists():
                im.thumbnail((480, 480))
                im.convert("RGB").save(thumb_path, "JPEG", quality=85)
        return width, height, thumb_name
    except UploadError:
        raise
    except Exception:
        raise UploadError("图片文件无法识别或已损坏")


def _probe_duration(path: Path) -> int | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return int(float(out.stdout.strip()) * 1000)
    except Exception:
        return None


def _video_thumb(path: Path, digest: str) -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    thumb_name = f"{digest}.jpg"
    thumb_path = db.THUMBS_DIR / thumb_name
    if thumb_path.exists():
        return thumb_name
    try:
        subprocess.run(
            [ffmpeg, "-y", "-ss", "1", "-i", str(path), "-frames:v", "1",
             "-vf", "scale='min(480,iw)':-2", str(thumb_path)],
            capture_output=True, timeout=60,
        )
    except Exception:
        return None
    return thumb_name if thumb_path.exists() else None


def delete_attachment(att_id: str) -> bool:
    """删除附件行；无其他行引用同 sha256 时才删物理文件。返回是否存在。"""
    row = db.q1("SELECT * FROM attachments WHERE id=?", (att_id,))
    if row is None:
        return False
    with db.locked() as c:
        c.execute("DELETE FROM attachments WHERE id=?", (att_id,))
        refs = c.execute(
            "SELECT COUNT(*) AS n FROM attachments WHERE sha256=?", (row["sha256"],)
        ).fetchone()["n"]
        c.commit()
    if refs == 0:
        (db.MEDIA_DIR / row["path"]).unlink(missing_ok=True)
        if row["thumb"]:
            (db.THUMBS_DIR / row["thumb"]).unlink(missing_ok=True)
    return True
