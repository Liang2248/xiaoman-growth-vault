# -*- coding: utf-8 -*-
"""小满中文日志：黑窗口和数据目录里的 app.log 都说人话。

log("做了什么", "结果/原因")  ->  [2026-08-05 07:32] 做了什么 ｜ 结果/原因
- 控制台输出按当前 stdout 的实际编码做安全降级（终端编不了的字符替换成 ?，不崩）；
  文件日志始终是 UTF-8 原文，因此两者内容可能不一致（控制台只是给人看的副本）
- 同时写入 data/logs/app.log（UTF-8），超过约 2MB 自动轮转为 app.log.<日期-时刻>
- 文件写入失败不会阻断主流程，但会往 stderr 报错（否则日志坏了没人知道）
"""
from __future__ import annotations

import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

_LOG_FILE: Path | None = None
_MAX_BYTES = 2 * 1024 * 1024
_MAX_ARCHIVES = 7   # 归档最多留 7 份，更老的删掉

# 轮转/写入的模块锁：log() 会被 HTTP 线程池与定时任务线程并发调用，
# 检查大小与改名必须整体串行化，否则会互删刚轮转出来的历史文件。
_LOCK = threading.Lock()
_WRITE_FAILS = 0
_ROTATE_FAILS = 0

# 换行会把一条日志伪装成多条（含伪造的合法日志行），
# 其余 C0 控制字符（ESC 等）能在终端里执行清屏/改标题。
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def init(log_dir: Path) -> None:
    """初始化日志文件位置。日志是辅助组件，失败也不能阻断应用启动。"""
    global _LOG_FILE
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        _LOG_FILE = log_dir / "app.log"
    except Exception as e:
        _LOG_FILE = None
        print(f"[logx] 初始化日志目录失败，仅输出到控制台：{e}", file=sys.stderr, flush=True)


def _stream_safe(text: str) -> str:
    """按当前 stdout 的实际编码做替换，而不是假设控制台是 GBK。

    硬编码 gbk 会在两种情况下出错：非中文代码页（cp437/cp1252）下中文虽能被
    GBK 编码却打印不出来，整行被吞；UTF-8 终端下 emoji 本可打印却被替换成 ?。
    """
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(enc)
        return text
    except (UnicodeEncodeError, LookupError):
        try:
            return text.encode(enc, errors="replace").decode(enc, errors="replace")
        except LookupError:
            return text.encode("ascii", errors="replace").decode("ascii")


def _one_line(text) -> str:
    """把外部可控文本压成单行并清掉控制字符，防止伪造日志行/终端转义。"""
    return _CTRL.sub("", str(text if text is not None else "").replace("\r", " ").replace("\n", " "))


def _note_write_failure(e: Exception) -> None:
    """文件写失败的前几次往 stderr 出声，之后静默计数（避免刷屏）。"""
    global _WRITE_FAILS
    _WRITE_FAILS += 1
    if _WRITE_FAILS <= 3:
        print(f"[logx] 日志写文件失败（第 {_WRITE_FAILS} 次）：{e}", file=sys.stderr, flush=True)


def _note_rotate_failure(e: Exception) -> None:
    """轮转失败的单独计数：行本身仍写进了文件，不能报成"写失败"。"""
    global _ROTATE_FAILS
    _ROTATE_FAILS += 1
    if _ROTATE_FAILS <= 3:
        print(f"[logx] 日志轮转失败（第 {_ROTATE_FAILS} 次，日志仍在写入）：{e}",
              file=sys.stderr, flush=True)


def _prune_archives() -> None:
    """只留最近 _MAX_ARCHIVES 份归档；异常一律忽略（清理不是关键路径）。

    按修改时间排序（同一秒内多次轮转会带 -2/-3 后缀，纯字典序不等于时间序）。
    """
    try:
        archives = list(_LOG_FILE.parent.glob("app.log.????????-??????*"))
        archives.sort(key=lambda p: (p.stat().st_mtime, p.name))
        for p in archives[:-_MAX_ARCHIVES]:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception:
        pass


def _archive_name(now: datetime) -> str:
    """归档名带日期+时刻：同一天多次轮转不会互相覆盖（旧实现只留 app.log.1）。"""
    base = f"app.log.{now:%Y%m%d-%H%M%S}"
    name, i = base, 1
    while (_LOG_FILE.parent / name).exists() and i < 1000:
        i += 1
        name = f"{base}-{i}"
    return name


def log(action: str, detail: str = "") -> None:
    """打一行中文日志：动作 + 竖线分隔的结果/原因/状态。"""
    now = datetime.now().astimezone()
    line = f"[{now:%Y-%m-%d %H:%M}] {_one_line(action)}" + (f" ｜ {_one_line(detail)}" if detail else "")
    try:
        print(_stream_safe(line), flush=True)
    except Exception:
        pass
    if _LOG_FILE is None:
        return
    with _LOCK:
        try:
            if _LOG_FILE.exists() and _LOG_FILE.stat().st_size > _MAX_BYTES:
                # 归档名带日期时刻：同一天多次轮转不再互相覆盖。
                old = _LOG_FILE.with_name(_archive_name(now))
                tmp = _LOG_FILE.with_name("app.log.rotating")
                moved = False
                try:
                    os.replace(_LOG_FILE, tmp)   # 原子摘走，避免 unlink 删掉别的线程刚轮转的文件
                    moved = True
                except OSError as e:
                    # 文件被其它程序占用（Windows 共享冲突）：本轮不轮转，但日志照写
                    _note_rotate_failure(e)
                if moved:
                    try:
                        os.replace(tmp, old)
                    except OSError as e:
                        _note_rotate_failure(e)
                        try:
                            os.replace(tmp, _LOG_FILE)   # 归档失败就把内容放回，绝不弄丢日志
                        except OSError:
                            pass
                    else:
                        _prune_archives()
            with _LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            _note_write_failure(e)
