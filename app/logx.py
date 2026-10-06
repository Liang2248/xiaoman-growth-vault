# -*- coding: utf-8 -*-
"""小满中文日志：黑窗口和数据目录里的 app.log 都说人话。

log("做了什么", "结果/原因")  ->  [08-05 07:32] 做了什么 ｜ 结果/原因
- 控制台输出经过 GBK 安全处理（Windows 中文控制台不支持 emoji 等字符时自动替换，不崩）
- 同时写入 data/logs/app.log（UTF-8），超过约 2MB 自动轮转为 app.log.1
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

_LOG_FILE: Path | None = None
_MAX_BYTES = 2 * 1024 * 1024


def init(log_dir: Path) -> None:
    global _LOG_FILE
    log_dir.mkdir(parents=True, exist_ok=True)
    _LOG_FILE = log_dir / "app.log"


def _gbk_safe(text: str) -> str:
    try:
        text.encode("gbk")
        return text
    except UnicodeEncodeError:
        return text.encode("gbk", errors="replace").decode("gbk")


def log(action: str, detail: str = "") -> None:
    """打一行中文日志：动作 + 竖线分隔的结果/原因/状态。"""
    line = f"[{datetime.now():%m-%d %H:%M}] {action}" + (f" ｜ {detail}" if detail else "")
    try:
        print(_gbk_safe(line), flush=True)
    except Exception:
        pass
    if _LOG_FILE is not None:
        try:
            if _LOG_FILE.exists() and _LOG_FILE.stat().st_size > _MAX_BYTES:
                old = _LOG_FILE.with_name("app.log.1")
                old.unlink(missing_ok=True)
                _LOG_FILE.rename(old)
            with _LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
