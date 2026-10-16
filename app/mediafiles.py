"""附件处理：上传存储、sha256 去重、缩略图、媒体信息探测。

- 存储路径：data/media/<sha256[:2]>/<sha256><原扩展名>，同 sha256 复用文件
- 图片用 Pillow 校验并生成缩略图 data/thumbs/<sha256>.jpg（最长边 480）
- 视频/音频在有 ffprobe/ffmpeg 时取时长/截图，缺失时优雅跳过
- 所有落盘都走"写 .tmp- → fsync → os.replace"，并做长度校验（A4-34）
"""
from __future__ import annotations

import errno
import hashlib
import mimetypes
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from . import db, logx

MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 单文件 500MB
CHUNK = 1024 * 1024
# A4-39：上传前的最低剩余空间要求（拿不到 Content-Length 时的保守下限）
MIN_FREE_BYTES = 64 * 1024 * 1024


class UploadError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _safe_ext(filename: str) -> str:
    """取扩展名（只保留最后一个后缀，A4-40）。

    仅用于 MIME 提示与人工辨识，**不参与任何安全判定**：磁盘目录完全由 sha256
    决定，且这里已剥掉所有非 [a-z0-9.] 字符，用户输入无法影响路径。
    """
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


def _file_is_ok(path: Path, expected_size: int | None = None) -> bool:
    """A4-34：复用判定不能只看 exists()——半写文件（大小与预期不符）要当损坏处理。"""
    try:
        if not path.is_file():
            return False
    except OSError:
        return False
    if expected_size is None:
        return path.stat().st_size > 0
    return path.stat().st_size == expected_size


def _atomic_replace(staged: Path, final_path: Path) -> None:
    """把已写好的临时文件原子地放到最终路径（调用方已 fsync 过内容）。"""
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged, final_path)


def _save_image_atomic(im, thumb_path: Path) -> None:
    """缩略图同样原子写：先存 .tmp- 再 replace（A4-34）。"""
    tmp = thumb_path.parent / f".tmp-{uuid.uuid4().hex}"
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        im.convert("RGB").save(tmp, "JPEG", quality=85)
        os.replace(tmp, thumb_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _thumb_is_ok(thumb_path: Path) -> bool:
    """A4-34：缩略图只看 exists() 会导致半截 JPG 永不重建，这里实际解码一次。"""
    if not _file_is_ok(thumb_path):
        return False
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        with Image.open(thumb_path) as im:
            im.verify()
        return True
    except Exception:
        return False


def _free_bytes() -> int:
    try:
        return shutil.disk_usage(str(db.MEDIA_DIR)).free
    except OSError:
        return -1


def _check_disk_space(need_hint: int | None) -> None:
    """A4-39：上传前预检剩余空间，磁盘满时给出明确中文原因而不是"保存失败，请重试"。"""
    free = _free_bytes()
    if free < 0:
        return
    need = max(MIN_FREE_BYTES, (need_hint or 0) * 2 + MIN_FREE_BYTES)
    if free < need:
        raise UploadError(
            f"磁盘空间不足（剩余 {free // 1024 // 1024}MB），请清理后重试", status=507)


def save_upload(entry_id: str, upload) -> str:
    """保存一个 UploadFile 并写入 attachments 行，返回附件 id。

    校验失败抛 UploadError（message 为中文，status 为 HTTP 状态码）。
    """
    filename = upload.filename or "未命名文件"
    ext = _safe_ext(filename)
    _check_disk_space(getattr(upload, "size", None))

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
            f.flush()
            os.fsync(f.fileno())
    except UploadError:
        tmp_path.unlink(missing_ok=True)
        raise
    except OSError as e:
        tmp_path.unlink(missing_ok=True)
        if e.errno == errno.ENOSPC:
            raise UploadError("磁盘空间不足，请清理后重试", status=507)
        raise UploadError(f"文件保存失败（{e}），请重试", status=500)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise UploadError("文件保存失败，请重试")

    digest = sha.hexdigest()
    rel_path = f"{digest[:2]}/{digest}{ext}"
    mime = upload.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    try:
        return _store_final(entry_id, filename, mime, size, digest, rel_path, tmp_path)
    finally:
        # 收尾无论成败都清掉暂存文件（成功时已被 os.replace 移走，unlink 是空操作）
        tmp_path.unlink(missing_ok=True)


def store_bytes(entry_id: str, filename: str, mime: str, data: bytes) -> str:
    """把内存中的字节存为附件（扫描件/裁图用），复用 sha256 去重，返回附件 id。

    A4-34：先写临时文件，再在临界区内原子替换到内容寻址路径。
    """
    digest = hashlib.sha256(data).hexdigest()
    ext = _safe_ext(filename)
    rel_path = f"{digest[:2]}/{digest}{ext}"
    tmp_path = db.MEDIA_DIR / f".tmp-{uuid.uuid4().hex}"
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        tmp_path.unlink(missing_ok=True)
        if e.errno == errno.ENOSPC:
            raise UploadError("磁盘空间不足，请清理后重试", status=507)
        raise UploadError(f"文件保存失败（{e}），请重试", status=500)
    return _store_final(entry_id, filename, mime, len(data), digest, rel_path, tmp_path)


def _store_final(entry_id: str, filename: str, mime: str, size: int, digest: str,
                 rel_path: str, staged_path: Path) -> str:
    """把 staged_path 放到最终路径并写 attachments 行。

    A4-34/35/38：复用判定带长度校验（半写文件重写）；"落盘 + 插行"在同一临界区内完成，
    与 delete_attachment 的"删行 + 删文件"串行，不会再产生"DB 有行、文件已删"的悬空附件；
    插行失败时清理本次刚落下的文件，不留孤儿。
    """
    final_path = db.MEDIA_DIR / rel_path
    kind = _kind_for(mime)

    # 媒体信息探测（可能调 ffprobe/ffmpeg，耗时且不该持锁）在临界区外，对临时文件做。
    width = height = duration_ms = None
    thumb = None
    if kind == "image":
        try:
            width, height, thumb = _process_image(staged_path, digest)
        except UploadError:
            # 内容坏/处理失败：临时文件由调用方丢弃，已存在的同名文件不受影响。
            staged_path.unlink(missing_ok=True)
            raise
    elif kind in ("video", "audio"):
        duration_ms = _probe_duration(staged_path)
        if kind == "video":
            thumb = _video_thumb(staged_path, digest)

    att_id = db.new_id()
    placed = False
    existing_id = None
    try:
        with db.locked() as c:
            # 已有同内容完好文件则丢弃临时文件；缺失或长度不符（半写/被删）则重写。
            if _file_is_ok(final_path, size):
                staged_path.unlink(missing_ok=True)
            else:
                _atomic_replace(staged_path, final_path)
                placed = True
            # A4-09/37：唯一索引 + OR IGNORE，重复上传同一记录同一内容时幂等（不再插重复行）
            cur = c.execute(
                "INSERT OR IGNORE INTO attachments(id, entry_id, sha256, mime, filename, size, "
                "width, height, duration_ms, kind, path, thumb, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (att_id, entry_id, digest, mime, filename, size,
                 width, height, duration_ms, kind, rel_path, thumb, db.now_iso()),
            )
            if not cur.rowcount:
                row = c.execute(
                    "SELECT id FROM attachments WHERE entry_id=? AND sha256=?",
                    (entry_id, digest),
                ).fetchone()
                existing_id = row["id"] if row else att_id
    except Exception:
        # A4-38：插行失败则回收刚写下的文件，避免永久孤儿。
        # 注意存储是**全局内容寻址**的：若还有别的行引用同一 sha256，文件必须留着。
        staged_path.unlink(missing_ok=True)
        if placed:
            try:
                still_ref = db.q1("SELECT 1 AS x FROM attachments WHERE sha256=? LIMIT 1", (digest,))
            except Exception:
                still_ref = True  # 查不到就先别删，宁可留孤儿也不能弄坏别人的附件
            if not still_ref:
                final_path.unlink(missing_ok=True)
        raise
    if existing_id is not None:
        logx.log("附件已存在，复用原有记录", f"{filename}（同一记录内同内容不重复入库）")
        return existing_id
    return att_id


def _process_image(staged_path: Path, digest: str):
    """Pillow 校验 + 尺寸 + 缩略图（缩略图原子写）。失败抛 UploadError。

    A4-34：区分"无法识别（内容坏）"与"写入失败（磁盘满/权限/占用）"——
    后者不报成"图片损坏"，也不让调用方删掉原件。
    """
    try:
        from PIL import Image
        from PIL import UnidentifiedImageError
    except ImportError:
        raise UploadError("服务器缺少 Pillow，无法处理图片", status=500)
    thumb_name = f"{digest}.jpg"
    thumb_path = db.THUMBS_DIR / thumb_name
    try:
        with Image.open(staged_path) as im:
            im.verify()
        with Image.open(staged_path) as im:
            width, height = im.size
            if not _thumb_is_ok(thumb_path):
                im.thumbnail((480, 480))
                _save_image_atomic(im, thumb_path)
        return width, height, thumb_name
    except UnidentifiedImageError:
        raise UploadError("图片文件无法识别或已损坏")
    except OSError as e:
        if e.errno == errno.ENOSPC:
            raise UploadError("磁盘空间不足，缩略图写入失败，请清理后重试", status=507)
        # 内容截断/损坏（如 "image file is truncated"）仍按 400 报，让调用方清理
        raise UploadError(f"图片读取或处理失败：{e}", status=400)
    except UploadError:
        raise
    except Exception as e:
        raise UploadError(f"图片处理失败：{e}", status=500)


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
    if _thumb_is_ok(thumb_path):
        return thumb_name
    # A4-34：不再让 ffmpeg 直写目标路径，改成先写临时文件再原子替换
    tmp = db.THUMBS_DIR / f".tmp-{uuid.uuid4().hex}.jpg"
    try:
        subprocess.run(
            [ffmpeg, "-y", "-ss", "1", "-i", str(path), "-frames:v", "1",
             "-vf", "scale='min(480,iw)':-2", str(tmp)],
            capture_output=True, timeout=60,
        )
        if not _file_is_ok(tmp):
            # ffmpeg 没产出有效截图（常见：装不了/源文件有问题）：清掉半成品
            tmp.unlink(missing_ok=True)
            return None
        os.replace(tmp, thumb_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        return None
    return thumb_name


def delete_attachment(att_id: str) -> bool:
    """删除附件行；无其他行引用同 sha256 时才删物理文件。返回是否存在。

    A4-35/36：物理删除放在同一临界区内（与 _store_final 的落盘+插行串行，不再有
    "DB 有行、文件已删"的窗口）；unlink 的 OSError 只记日志——文件删不掉不该致命，
    更不能让启动路径（purge_old_deleted）整体失败。
    """
    with db.locked() as c:
        row = c.execute("SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone()
        if row is None:
            return False
        c.execute("DELETE FROM attachments WHERE id=?", (att_id,))
        refs = c.execute(
            "SELECT COUNT(*) AS n FROM attachments WHERE sha256=?", (row["sha256"],)
        ).fetchone()["n"]
        if refs == 0:
            for target in ((db.MEDIA_DIR / row["path"]),
                           (db.THUMBS_DIR / row["thumb"]) if row["thumb"] else None):
                if target is None:
                    continue
                try:
                    target.unlink(missing_ok=True)
                except OSError as e:
                    logx.log("附件文件删除失败", f"{target.name}：{e}（不影响记录删除）")
    return True

