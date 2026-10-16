"""定时自动化：自动周报/月报、自动备份。

daemon 线程每 10 分钟一轮；每步独立 try/except，出错只记一行日志、绝不影响主服务
（A6b-08：原先全部静默吞异常，"备份停了但没人知道"）。
核心逻辑抽成函数（tick / _maybe_auto_report / _maybe_auto_backup），便于单测。
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from . import ai as ai_mod, db, exporter, logx, stats as stats_mod

INTERVAL_SECONDS = 600
AUTO_REPORT_HOUR = 21
BACKUP_INTERVAL = timedelta(hours=20)
BACKUP_KEEP = 14
# 自动报告失败后的最短重试间隔（A6b-06 的"节流键要能区分已完成与已尝试"）。
REPORT_RETRY = timedelta(hours=6)
# 推送失败后的最短重试间隔：不给 10 分钟一轮的硬重试（最坏一小时 6 次外呼 + 刷日志）。
PUSH_RETRY = timedelta(minutes=30)
# 自动备份确定性失败（内容不完整）后的退避：重试同一次必然失败的备份没有意义。
BACKUP_RETRY = timedelta(hours=6)


def start() -> None:
    threading.Thread(target=_loop, daemon=True).start()


def _parse_ts(raw: str) -> datetime | None:
    """宽容解析设置项里的时间戳；无法解析或**无时区**（naive）一律返回 None。

    A6b-11：`now` 是 aware（`tick` 传 `datetime.now().astimezone()`），与 naive 值相减
    抛 `TypeError`，而调用点原先只捕 `ValueError` → 异常冒泡到 tick 的静默兜底，
    自动备份/合并提案**永久停摆**且用户毫无感知。naive/脏值一律当"无法判断"，
    由调用方按"已过期，继续执行"处理（安全方向是继续做，而不是永久跳过）。
    """
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo is not None else None


def _loop() -> None:
    time.sleep(30)  # 启动后 30 秒先跑一轮（迁移/补算检查不用等 10 分钟）
    while True:
        try:
            tick()
        except BaseException as e:  # A6b-33：BaseException 会让调度线程静默死亡
            # 原先 `except Exception: pass` 连一行日志都不写：线程活着但什么都没做，
            # 而 /api/healthz 只看 uptime，看门狗也发现不了。这里必须留痕。
            try:
                logx.log("定时任务整体异常", f"{type(e).__name__}: {str(e)[:120]}")
            except Exception:
                pass
        time.sleep(INTERVAL_SECONDS)


def tick(now: datetime | None = None) -> None:
    now = now or datetime.now().astimezone()
    # A6b-12：推送类任务（晨报/晚报）排在最前——它们有硬时间窗口（晨报 7:30–9:59），
    # 而备份/回填是纯维护任务，没有时效性。原先按"报告→备份→三组回填→晚报→晨报"
    # 串行执行，前序步骤慢（如备份搬运大量媒体）会把晨报推出窗口，当天永久缺失。
    for fn in (_maybe_morning_push, _maybe_daily_push,
               _maybe_auto_report, _maybe_auto_backup,
               _maybe_backfill_embeddings, _maybe_backfill_enrich, _maybe_backfill_mood,
               _maybe_circle, _maybe_propose_merges, _maybe_reclassify,
               _maybe_link_discovery, _maybe_propose_categories,
               _maybe_circle_recognition, _maybe_insights):
        try:
            fn(now)
        except Exception as e:
            # A6b-08：原先逐个 `except Exception: pass`，任何在函数内日志语句**之前**抛出的
            # 异常（db 错误、Path 解析、stats 统计的 KeyError…）都变成"什么都没发生"。
            # 对自动备份这类关键任务，沉默的代价是"备份停了但没人知道"。
            try:
                logx.log(f"定时任务失败：{fn.__name__}",
                         f"{type(e).__name__}: {str(e)[:120]}")
            except Exception:
                pass


# ---------------- 自动报告 ----------------

def _maybe_auto_report(now: datetime) -> None:
    """自动周报/月报。

    A6b-07：原实现是"硬时间窗口 + 一次性 marker"——只认"周日 21:00 后"与"1 日 21:00 后"，
    本地单用户应用晚上关机是常态，一旦跨过窗口这一期就**永远不会**自动生成（周一开机
    `weekday()==0` 不匹配，直接跳过）。改为幂等补算：周期已结束且库里没有该周期的报告
    就补一份（`AUTO_REPORT_HOUR` 降级为"当天最早几点开始尝试"的软门槛）。
    """
    if db.get_setting("auto_report_enabled", "true") == "false":
        return
    if not ai_mod.strong_configured():
        return
    today = now.date()
    # A6b-06：失败退避。原先失败也只会被 _gen_once 内部 catch 掉，每轮（10 分钟）重试；
    # 这里用一个独立的重试时间戳做 6 小时退避，避免 AI 长期不可用时高频空转（但不放弃）。
    retry_at = _parse_ts(db.get_setting("auto_report_retry_at"))
    if retry_at is not None and now < retry_at:
        return
    pending = 0
    # 周报：上周（周一至周日）。周期已结束即可补（不再要求"今天是周日且已过 21 点"）。
    week_start = today - timedelta(days=7)
    week_start = week_start - timedelta(days=week_start.weekday())
    if _report_due("weekly", week_start, today):
        pending += 1
    # 月报：上月 1 日。当月 1 日之后即可补。
    month_start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    if _report_due("monthly", month_start, today):
        pending += 1
    if not pending:
        return
    # 软门槛：不是"开机瞬间就补"，而是"当天 21 点起才开始补"——保持原有的"晚上生成"体感，
    # 同时把补偿窗口从"仅周日晚 21:00–24:00"放宽为"此后任意一天的 21 点后"。
    if now.hour < AUTO_REPORT_HOUR:
        return
    failed = False
    if _report_due("weekly", week_start, today):
        failed = not _gen_once("weekly", week_start, "last_auto_weekly")
    if _report_due("monthly", month_start, today):
        failed = (not _gen_once("monthly", month_start, "last_auto_monthly")) or failed
    # A6b-06：失败退避 6 小时（而不是每 10 分钟重试一次必然失败的 AI 调用）；
    # 成功即清空，下一期不用等退避窗口。
    if failed:
        nxt = now + REPORT_RETRY
        db.set_setting("auto_report_retry_at", nxt.isoformat(timespec="seconds"))
    else:
        db.set_setting("auto_report_retry_at", "")


def _report_due(rtype: str, period_start: date, today: date) -> bool:
    """该周期的报告是否还缺（幂等补算的判定：周期已结束且库里没有 → 缺）。"""
    ps = period_start.isoformat()
    if ps >= today.isoformat():
        return False  # 周期还没结束
    return db.q1("SELECT id FROM reports WHERE type=? AND period_start=?",
                 (rtype, ps)) is None


def _gen_once(rtype: str, period_start, marker_key: str) -> bool:
    """生成一次报告。返回是否成功（A6b-06：调用方据此决定是否退避重试）。"""
    ps = period_start.isoformat()
    if db.get_setting(marker_key) == ps:
        return True
    if db.q1("SELECT id FROM reports WHERE type=? AND period_start=?", (rtype, ps)):
        db.set_setting(marker_key, ps)
        return True
    try:
        # 周期内无记录会在 generate_report 内抛 NoDataError，下轮再试
        rep = ai_mod.generate_report(rtype, ps)
        db.set_setting(marker_key, ps)
        label = {"weekly": "周报", "monthly": "月报"}.get(rtype, "报告")
        logx.log(f"定时任务：自动{label}已生成", f"周期 {ps} 起")
        _push_report_ready(rep)
        return True
    except ai_mod.NoDataError:
        # 该周期本来就没有记录：不是失败，写 marker 停止重试（否则新加的补算逻辑
        # 会每 6 小时对着一个永远生不出报告的周期空转）。
        db.set_setting(marker_key, ps)
        logx.log("定时任务：该周期无记录，不生成报告", f"周期 {ps} 起")
        return True
    except Exception as e:
        logx.log("定时任务：自动报告未生成", str(e)[:80])
        return False


def _push_report_ready(rep: dict) -> None:
    """自动报告生成后即时推送摘要；有新圈高光一并告知。"""
    if db.get_setting("push_enabled", "true") == "false":
        return
    if not db.get_setting("push_key").strip():
        return
    try:
        label = {"weekly": "周报", "monthly": "月报"}.get(rep.get("type"), "报告")
        summary = (rep.get("analysis", {}).get("executive_summary") or "")[:150]
        lines = [f"你的{label}（{rep.get('period_start')} ~ {rep.get('period_end')}）已经生成：", "", summary, ""]
        for h in rep.get("new_highlights") or []:
            lines.append(f"⭐ 顺手为你收藏了一颗高光：《{h['title']}》")
        if rep.get("new_highlights"):
            lines.append("")
        lines.append("打开小满「报告」页查看完整内容。")
        ok, msg = send_push(f"小满 · {label}已生成", "\n".join(lines))
        if ok:
            logx.log(f"{label}生成推送已发送")
        else:
            logx.log(f"{label}生成推送失败", msg)
    except Exception as e:
        logx.log("报告生成推送失败", str(e)[:80])


# ---------------- 自动备份 ----------------

def _maybe_auto_backup(now: datetime) -> None:
    """自动备份：间隔判定 + 失败退避 + 落盘后复验。

    A6b-11：时间戳解析改走 `_parse_ts`。原写法 `except ValueError` 捕不到 aware−naive 的
    TypeError，而该异常在 `tick` 里被静默吞掉 → `last_backup_at` 永不更新、自动备份
    **永久停摆**且用户毫无感知（主页只会看到一个越来越旧的 last_backup_at）。
    A6b-14：`BackupError`（确定性失败，如"数据库引用的媒体文件不在包里"）改为退避 6 小时 +
    一行可见提示，不再每 10 分钟重试同一次必然失败的备份；移动后跑一次 `verify_backup`，
    避免跨盘 `shutil.move` 中断留下"文件名合法但内容截断"的坏备份挤掉好备份。
    """
    # 确定性失败（备份内容不完整）的退避：6 小时内不再重试同一个必然失败的备份
    blocked_until = _parse_ts(db.get_setting("backup_retry_at"))
    if blocked_until is not None and now < blocked_until:
        return
    last = _parse_ts(db.get_setting("last_backup_at"))
    if last is not None:
        try:
            if (now - last) < BACKUP_INTERVAL:
                return
        except TypeError:
            pass  # 无法比较时按"已过期"继续执行，而不是永久跳过（安全方向是继续做）

    outdir = db.get_setting("backup_dir").strip()
    target_dir = Path(outdir) if outdir else db.BACKUPS_DIR
    if not _writable(target_dir):
        target_dir = db.BACKUPS_DIR  # 无效/不可写静默回退

    name = f"backup-auto-{now:%Y%m%d-%H%M}.zip"
    try:
        src = exporter.build_backup_zip()
    except exporter.BackupError as e:
        # 确定性失败：重试无用（用户得先修好缺失的附件/媒体），退避并留一条可见提示
        logx.log("自动备份自检未通过", str(e))
        db.set_setting("backup_retry_at", (now + BACKUP_RETRY).isoformat(timespec="seconds"))
        if db.get_setting("backup_blocked_reason") != str(e)[:200]:
            db.set_setting("backup_blocked_reason", str(e)[:200])
            db.push_notice(f"自动备份暂时停住了：{str(e)[:80]}（可在设置页做一次手动备份）")
        return
    except Exception as e:
        logx.log("自动备份失败", str(e)[:80])
        db.set_setting("backup_retry_at", (now + BACKUP_RETRY).isoformat(timespec="seconds"))
        return
    try:
        shutil.move(str(src), str(target_dir / name))
    except Exception:
        try:
            shutil.move(str(src), str(db.BACKUPS_DIR / name))
            target_dir = db.BACKUPS_DIR
        except Exception:
            return
    # A6b-14：跨盘 move 会退化为"复制+删除"，中断/磁盘满会留下截断的 zip 而文件名完全合法。
    # 移动后重跑一次校验（exporter.verify_backup 已存在且不抛异常），不通过就删掉并计为失败。
    try:
        check = exporter.verify_backup(target_dir / name)
    except Exception as e:
        check = {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:80]}"}
    if not check.get("ok"):
        logx.log("自动备份落盘后复验未通过", f"{check.get('detail') or '未知原因'}（已删除该文件）")
        try:
            (target_dir / name).unlink(missing_ok=True)
        except Exception:
            pass
        db.set_setting("backup_retry_at", (now + BACKUP_RETRY).isoformat(timespec="seconds"))
        return

    # 清理无条件执行（原先只在一切成功的正常路径上做；两处 move 都失败时旧备份永不清理）
    try:
        zips = sorted(target_dir.glob("backup-auto-*.zip"), key=lambda p: p.name)
        for old in zips[:-BACKUP_KEEP]:
            old.unlink(missing_ok=True)
    except Exception:
        pass
    db.set_setting("last_backup_at", now.isoformat(timespec="seconds"))
    db.set_setting("backup_retry_at", "")
    db.set_setting("backup_blocked_reason", "")
    try:
        size_mb = round((target_dir / name).stat().st_size / 1024 / 1024, 1)
    except Exception:
        size_mb = "?"
    logx.log("自动备份完成并自检通过", f"{target_dir / name}（{size_mb}MB）")


# ---------------- 嵌入向量回填 ----------------

EMBED_TRICKLE = 5
# A5-21（F5 备忘）：把"配置/凭证类错误"与"该条数据本身失败"区分开。
# 判据用可观测的**整轮结果**，不依赖 ai.py 内部错误码（embed_text 的对外契约永远是
# "失败返回 None"）：一轮里 attempted>0 且一条都没成功 ⇒ 视为配置/凭证类问题
# （Key 失效、模型名写错、端点 404、配额耗尽），告警一次并按指数退避；
# 有成功 ⇒ 清零。单条失败则跳过继续下一条（原先一律 `break`，一条必然失败的记录
# 会把它之后的所有记录永久饿死——它一直占着队首）。
EMBED_FAILS_KEY = "embed_backfill_fails"
EMBED_BACKOFF_KEY = "embed_backfill_retry_at"
EMBED_BACKOFF_BASE = timedelta(minutes=30)
EMBED_BACKOFF_MAX = timedelta(hours=6)


def _embed_backoff_active(now: datetime) -> bool:
    until = _parse_ts(db.get_setting(EMBED_BACKOFF_KEY))
    return until is not None and now < until


def _note_embed_round(now: datetime, attempted: int, succeeded: int) -> None:
    """一轮嵌入回填结束后的错误分类与退避（A5-21）。"""
    if not attempted:
        return
    if succeeded:
        if db.get_setting(EMBED_FAILS_KEY) not in ("", "0"):
            db.set_setting(EMBED_FAILS_KEY, "0")
            db.set_setting(EMBED_BACKOFF_KEY, "")
        return
    try:
        fails = int(db.get_setting(EMBED_FAILS_KEY) or 0) + 1
    except ValueError:
        fails = 1
    delay = min(EMBED_BACKOFF_BASE * (2 ** min(fails - 1, 8)), EMBED_BACKOFF_MAX)
    minutes = int(delay.total_seconds() // 60)
    db.set_setting(EMBED_FAILS_KEY, str(fails))
    db.set_setting(EMBED_BACKOFF_KEY, (now + delay).isoformat(timespec="seconds"))
    logx.log("语义索引回填暂停",
             f"本轮 {attempted} 条全部失败（连续第 {fails} 次），{minutes} 分钟后重试；"
             f"请检查嵌入模型的 Base URL / Key / 模型名（详见 app.log 的嵌入调用失败行）")
    if fails == 1:
        db.push_notice("语义索引（向量检索）没补上：嵌入接口可能没配好或 Key 已失效，去设置页看看？")


def _maybe_backfill_embeddings(now: datetime) -> None:
    """嵌入模型配置后，每轮为少量历史记录与知识条目补向量，直到全部入库（静默）。

    F5/A5-21：单条失败跳过继续（不 `break` 饿死后续记录）；整轮全失败按配置类错误
    告警 + 指数退避。
    """
    if not ai_mod.embed_configured():
        return
    if _embed_backoff_active(now):
        return  # 配置类错误退避期内不重复外呼（避免每小时空转 + 刷日志）
    attempted = 0
    succeeded = 0
    rows = db.q(
        "SELECT e.id FROM entries e LEFT JOIN entry_embeddings v ON v.entry_id=e.id "
        "WHERE e.deleted_at IS NULL AND v.entry_id IS NULL ORDER BY e.occurred_at DESC LIMIT ?",
        (EMBED_TRICKLE,),
    )
    done = 0
    failed_ids: list[str] = []
    for r in rows:
        attempted += 1
        if ai_mod.embed_entry(r["id"]):
            done += 1
            succeeded += 1
        else:
            failed_ids.append(r["id"][:8])
    if done:
        logx.log("语义索引回填", f"本轮补了 {done} 条记录向量")
    if failed_ids:
        logx.log("语义索引回填跳过", f"{len(failed_ids)} 条本轮未成功（已跳过，下轮再试）："
                                     f"{'、'.join(failed_ids[:5])}")
    # 知识条目向量是语义判重的前提，一并没有的补
    krows = db.q(
        "SELECT id FROM knowledge WHERE (vector_json IS NULL OR vector_json = '') "
        "AND status IN ('pending','accepted','rejected') LIMIT ?",
        (EMBED_TRICKLE,),
    )
    kdone = 0
    for r in krows:
        attempted += 1
        if ai_mod.embed_knowledge_item(r["id"]):
            kdone += 1
            succeeded += 1
    if kdone:
        logx.log("语义索引回填", f"本轮补了 {kdone} 条知识向量")
    # 圈子实体向量（语义查重的前提），每轮 5 个
    erows = db.q(
        "SELECT id FROM entities WHERE (vector_json IS NULL OR vector_json = '') "
        "AND status != 'rejected' LIMIT 5"
    )
    edone = 0
    for r in erows:
        attempted += 1
        if ai_mod.embed_entity(r["id"]):
            edone += 1
            succeeded += 1
    if edone:
        logx.log("语义索引回填", f"本轮补了 {edone} 个圈子档案向量")
    _note_embed_round(now, attempted, succeeded)


# ---------------- 轻量补全回填 ----------------

ENRICH_TRICKLE = 3


def _maybe_backfill_enrich(now: datetime) -> None:
    """每轮为少量「有正文但仍缺标题/摘要/标签」的历史记录自动补全（静默）。

    F5/A5-21 同款错误分类：`AIError`（配置/凭证/网络类，整批都会失败）立刻停本轮并记一行
    告警；其它异常视为**该条数据本身**的问题，跳过继续下一条（原先一律 `break`，一条必然
    抛错的记录把它之后的所有记录永久饿死——它按 occurred_at DESC 一直占着队首）。
    """
    if not (ai_mod.strong_configured() or ai_mod.fast_configured()):
        return
    rows = db.q(
        "SELECT * FROM entries WHERE deleted_at IS NULL AND exclude_from_ai=0 "
        "AND trim(content) != '' AND (trim(title) = '' OR trim(summary) = '' OR trim(tags) IN ('', '[]')) "
        "ORDER BY occurred_at DESC LIMIT ?",
        (ENRICH_TRICKLE,),
    )
    if not rows:
        return
    from . import main as main_mod  # 延迟导入避免循环依赖
    done = 0
    skipped = 0
    for row in rows:
        try:
            if main_mod._enrich_fill(row, use_category=False):
                done += 1
        except ai_mod.AIError as e:
            # 配置/凭证/网络类：本轮剩下的必然同样失败，停下并留下线索（不刷屏）
            logx.log("历史记录补全回填暂停", f"AI 调用失败：{str(e)[:100]}")
            break
        except Exception as e:
            skipped += 1
            logx.log("历史记录补全回填跳过",
                     f"记录 {str(row['id'])[:8]} 本轮跳过：{type(e).__name__} {str(e)[:80]}")
    if done:
        logx.log("历史记录补全回填", f"本轮补 {done} 条")
    if skipped and not done:
        logx.log("历史记录补全回填", f"本轮 {skipped} 条都没跑通（已跳过，下轮再试）")


def _writable(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


# ---------------- 心情回填 ----------------

def _maybe_backfill_mood(now: datetime) -> None:
    """每轮最多 5 条：有正文且无 mood_score 的未删除记录补推断心情（静默）。"""
    if not (ai_mod.strong_configured() or ai_mod.fast_configured()):
        return
    rows = db.q(
        "SELECT id, title, content FROM entries WHERE deleted_at IS NULL "
        "AND exclude_from_ai=0 AND mood_score IS NULL AND trim(content) != '' "
        "ORDER BY occurred_at DESC LIMIT 5"
    )
    done = 0
    for r in rows:
        try:
            score, label = ai_mod.infer_mood(r["title"], r["content"])
            if score is not None:
                db.execute("UPDATE entries SET mood_score=?, mood_label=? WHERE id=?",
                           (score, label, r["id"]))
                done += 1
        except Exception:
            pass
    if done:
        logx.log("心情回填", f"本轮补 {done} 条")


# ---------------- 每日回顾推送 ----------------

PUSH_HOUR = 21
MORNING_HOUR, MORNING_MIN = 7, 30
SERVERCHAN_URL = "https://sctapi.ftqq.com/{key}.send"
# A6b-13：改 HTTPS。原为 http://，推送 token 走明文传输（json body 里带 token）。
PUSHPLUS_URL = "https://www.pushplus.plus/send"

# 24 节气近似区间（展示用，无需精确到时刻）
_SOLAR_TERMS = [
    ("小寒", 1, 5), ("大寒", 1, 20), ("立春", 2, 3), ("雨水", 2, 18), ("惊蛰", 3, 5),
    ("春分", 3, 20), ("清明", 4, 4), ("谷雨", 4, 19), ("立夏", 5, 5), ("小满", 5, 20),
    ("芒种", 6, 5), ("夏至", 6, 21), ("小暑", 7, 6), ("大暑", 7, 22), ("立秋", 8, 7),
    ("处暑", 8, 22), ("白露", 9, 7), ("秋分", 9, 22), ("寒露", 10, 8), ("霜降", 10, 23),
    ("立冬", 11, 7), ("小雪", 11, 22), ("大雪", 12, 6), ("冬至", 12, 21),
]


def _solar_term(d: date) -> str:
    for i in range(len(_SOLAR_TERMS) - 1, -1, -1):
        name, m, day = _SOLAR_TERMS[i]
        if (d.month, d.day) >= (m, day):
            return name
    return "小寒"


def send_push(title: str, content: str) -> tuple[bool, str]:
    """push_key 以 SCT 开头走 Server酱（form），否则 PushPlus（json）。

    A6b-13：不再只看 HTTP 状态码。两个服务的约定都是「HTTP 200 + body 内业务错误码」
    （token 失效、当日配额用尽都会返回 200），原先会被判成"推送成功"→ 写 last_push_date
    → 当天再也不会重试，用户什么都没收到却以为配好了。拿不准时**宁可判失败**（失败会重试，
    假成功会丢当天内容）。错误文案不回显完整 URL 或 key。
    """
    key = db.get_setting("push_key").strip()
    if not key:
        return False, "未配置推送 Key"
    try:
        with httpx.Client(timeout=10.0) as c:
            if key.startswith("SCT"):
                r = c.post(SERVERCHAN_URL.format(key=key),
                           data={"title": title, "desp": content})
                provider = "Server酱"
            else:
                r = c.post(PUSHPLUS_URL,
                           json={"token": key, "title": title, "content": content,
                                 "template": "markdown"})
                provider = "PushPlus"
        if r.status_code != 200:
            return False, f"推送服务返回 HTTP {r.status_code}"
        # 解析响应体判业务成功：拿不准的响应结构一律判失败（宁可重试，不要静默丢内容）
        try:
            body = r.json()
        except ValueError:
            return False, f"{provider} 返回的不是合法 JSON，无法确认是否送达"
        if not isinstance(body, dict):
            return False, f"{provider} 返回结构无法识别，无法确认是否送达"
        if provider == "Server酱":
            if body.get("code") == 0:
                return True, "推送成功"
            return False, f"Server酱返回 code={body.get('code')}：{_redact_push_msg(body.get('message'), key)}"
        code = body.get("code")
        if str(code) == "200":
            return True, "推送成功"
        return False, f"PushPlus返回 code={code}：{_redact_push_msg(body.get('msg'), key)}"
    except httpx.TimeoutException:
        return False, "推送超时，请检查网络"
    except Exception as e:
        # R2-09/A6b-13 脱敏：httpx 的异常字符串可能带上完整 URL（内含 Server酱的 key path），
        # 而这段文案会经 POST /api/settings/test-push 回到前端。只回类型名，不回原文。
        return False, f"推送失败：{type(e).__name__}"


def _redact_push_msg(raw, key: str) -> str:
    """上游响应里的错误说明：剥掉可能回显的 key，压成单行并截断（R2-09 脱敏）。"""
    text = str(raw or "").replace("\n", " ").replace("\r", " ").strip()
    if key and key in text:
        text = text.replace(key, "***")
    return text[:60]


def _build_daily_review(today: date) -> str:
    yesterday = (today - timedelta(days=1)).isoformat()
    rows = db.q(
        "SELECT title, content FROM entries WHERE deleted_at IS NULL "
        "AND substr(occurred_at,1,10)=? ORDER BY occurred_at",
        (yesterday,),
    )
    lines = ["## 昨日记录", ""]
    if rows:
        lines.append(f"共 {len(rows)} 条：")
        for r in rows:
            lines.append(f"- {r['title'] or (r['content'] or '')[:30]}")
    else:
        lines.append("昨天没有记录。")
    lines.append("")

    # 晨晚对照（G2）：今天写了「今天打算」的话，晚间回顾里温柔对照一句（今日 intent ↔ 今日记录）
    try:
        it = db.q1("SELECT text FROM daily_intents WHERE day=?", (today.isoformat(),))
        if it and it["text"].strip() and (
                ai_mod.fast_configured() or ai_mod.strong_configured()):
            today_rows = db.q(
                "SELECT title, content FROM entries WHERE deleted_at IS NULL "
                "AND substr(occurred_at,1,10)=? ORDER BY occurred_at",
                (today.isoformat(),),
            )
            if today_rows:
                digest = "\n".join(f"- {r['title']}：{(r['content'] or '')[:200]}" for r in today_rows)
                cmp_text = ai_mod.compare_intent(it["text"], digest)
                if cmp_text:
                    lines += ["## 今天你说要……", "", f"早上的打算：{it['text']}", "", cmp_text, ""]
    except Exception:
        pass

    for n in (30, 60, 90):
        d = (today - timedelta(days=n)).isoformat()
        r = db.q1(
            "SELECT title, content FROM entries WHERE deleted_at IS NULL "
            "AND substr(occurred_at,1,10)=? ORDER BY occurred_at LIMIT 1",
            (d,),
        )
        if r is not None:
            lines += [f"## {n} 天前的今天", "", f"- {r['title'] or (r['content'] or '')[:30]}", ""]
            break

    # muse：直接用当天缓存，没有就用静态轮换句（此段不发 AI 调用；本函数的 AI 调用只有
    # 上方"今天你说要……"的 compare_intent——A6b-12）
    muse_text = ""
    cached = db.get_setting("muse_cache")
    if cached:
        try:
            c = json.loads(cached)
            if c.get("date") == today.isoformat() and c.get("text"):
                muse_text = c["text"]
        except ValueError:
            pass
    if not muse_text:
        from . import main as main_mod  # 延迟导入避免循环
        muse_text = main_mod.MUSE_STATIC[today.toordinal() % len(main_mod.MUSE_STATIC)]
    lines += ["## 今日一问", "", muse_text, ""]

    streak = stats_mod.overview()["streak_days"]
    mline = _milestone_line()
    if mline:
        lines.append(mline)
    pending = db.q1("SELECT COUNT(*) AS n FROM knowledge WHERE status='pending'")["n"]
    if pending >= 3:
        lines.append(f"知识库里有 **{pending}** 条新提炼的知识等你确认，有空去看看。")
    lines.append(f"已连续记录 **{streak}** 天，继续加油。")
    # A6b-08：备份健康度。本应用的"数据安全"完全依赖自动备份，而备份失败原先只在日志里
    # （tick 还会静默吞异常）。超过 2 个备份周期没成功就明确告诉用户一次。
    # 注意避免"新装/刚开机还来得及备份"的误报：从没备份成功过时，只有确实已有记录
    # 且备份目录里一份自动备份都没有，才算异常。
    try:
        last_bak = _parse_ts(db.get_setting("last_backup_at"))
        stale = False
        if last_bak is not None:
            stale = (datetime.now().astimezone() - last_bak) > BACKUP_INTERVAL * 2
        else:
            has_entries = db.q1("SELECT COUNT(*) AS n FROM entries")["n"] > 0
            try:
                has_zip = any(db.BACKUPS_DIR.glob("backup-auto-*.zip"))
            except Exception:
                has_zip = False
            stale = has_entries and not has_zip
        if stale:
            reason = db.get_setting("backup_blocked_reason")
            tip = f"（{reason[:60]}）" if reason else ""
            lines.append(f"⚠️ 自动备份好像停了，建议打开电脑上的小满手动备份一次{tip}。")
    except Exception:
        pass
    # 圈子提醒：待确认档案 / 待裁定矛盾各一行
    try:
        draft_n = db.q1("SELECT COUNT(*) AS n FROM entities WHERE status='draft'")["n"]
        # rejected 档案不会出现在冲突收件箱里，提醒统计必须与 /api/circle/overview
        # 和 /api/circle/entities?needs_review=1 保持同一口径，避免点击后列表为空。
        conflict_n = db.q1(
            "SELECT COUNT(*) AS n FROM entities WHERE needs_review=1 AND status != 'rejected'"
        )["n"]
        if draft_n:
            lines.append(f"圈子有 {draft_n} 份新档案等你过目")
        if conflict_n:
            lines.append(f"有 {conflict_n} 份档案发现矛盾，等你裁定")
    except Exception:
        pass
    return "\n".join(lines)


def _maybe_daily_push(now: datetime) -> None:
    if db.get_setting("push_enabled", "true") == "false":
        return
    if not db.get_setting("push_key").strip():
        return
    if now.hour < PUSH_HOUR:
        return
    today = now.date().isoformat()
    if db.get_setting("last_push_date") == today:
        return
    # A6b-13：失败退避。失败不写 last_push_date（正确，当天还能重试），但原先每 10 分钟
    # 硬重试一次（21:00–23:59 最多 18 次），每次都等 10s 超时 → 无效外呼 + 刷日志。
    retry_at = _parse_ts(db.get_setting("push_retry_at"))
    if retry_at is not None and now < retry_at:
        return
    content = _build_daily_review(now.date())
    ok, _msg = send_push(f"小满 · 每日回顾 {now.month}月{now.day}日", content)
    if ok:
        db.set_setting("last_push_date", today)
        db.set_setting("push_retry_at", "")
        logx.log("晚报已推送到微信")
    else:
        db.set_setting("push_retry_at", (now + PUSH_RETRY).isoformat(timespec="seconds"))
        logx.log("晚报推送失败", f"{_msg}（{PUSH_RETRY.seconds // 60} 分钟后重试）")


# ---------------- 晨报 ----------------

_MILESTONE_STREAKS = (7, 21, 30, 60, 100)
_MILESTONE_TOTALS = (10, 50, 100, 200, 365, 500, 1000)


def _muse_today(today: date) -> str:
    cached = db.get_setting("muse_cache")
    if cached:
        try:
            c = json.loads(cached)
            if c.get("date") == today.isoformat() and c.get("text"):
                return c["text"]
        except ValueError:
            pass
    from . import main as main_mod  # 延迟导入避免循环
    return main_mod.MUSE_STATIC[today.toordinal() % len(main_mod.MUSE_STATIC)]


def _milestone_line() -> str:
    s = stats_mod.overview()
    if s["streak_days"] in _MILESTONE_STREAKS:
        return f"🎉 连续记录 {s['streak_days']} 天啦，小满渐盈。"
    if s["total_entries"] in _MILESTONE_TOTALS:
        return f"🎉 已经是第 {s['total_entries']} 条记录了，每一步都算数。"
    return ""


def _build_morning(today: date) -> str:
    weekdays = "一二三四五六日"
    lines = [f"## 早安，{today.month}月{today.day}日 周{weekdays[today.weekday()]} · {_solar_term(today)}", ""]

    # 天气（总开关开启时尽力而为，此段不发 AI 调用）——注意：本函数整体**会**发 AI 调用，
    # 见下方"今日接力"的 extract_intents 与"晨晚对照"的 compare_intent（A6b-12 修正注释：
    # 原注释"调度里不发 AI 调用"只对天气段成立，容易被误读为整函数无 AI）。
    if db.get_setting("weather_enabled", "true") != "false":
        try:
            from . import weather as weather_mod
            city = db.get_setting("default_city").strip()
            geo = weather_mod.geocode_city(city) if city else None
            w = weather_mod.fetch_weather(geo[0], geo[1], today.isoformat()) if geo else None
            if w and w.get("text"):
                parts = [w["text"]]
                if w.get("temperature_c") is not None:
                    parts.append(f"{w['temperature_c']}°C")
                lines += ["今天天气：" + " ".join(parts), ""]
        except Exception:
            pass

    # 今日接力：昨天写下的"明天要做的事"
    items: list = []
    try:
        cached = db.get_setting("intents_cache")
        c = json.loads(cached) if cached else {}
        if c.get("date") == today.isoformat() and c.get("v") == 2:
            items = c.get("items") or []
        elif ai_mod.strong_configured() or ai_mod.fast_configured():
            yesterday = (today - timedelta(days=1)).isoformat()
            rows = db.q(
                "SELECT title, content FROM entries WHERE deleted_at IS NULL "
                "AND exclude_from_ai=0 AND substr(occurred_at,1,10)=? AND trim(content) != ''",
                (yesterday,),
            )
            if rows:
                digest = "\n".join(f"- {r['title']}：{(r['content'] or '')[:300]}" for r in rows)
                items = ai_mod.extract_intents(digest)
                db.set_setting("intents_cache", json.dumps(
                    {"date": today.isoformat(), "items": items, "v": 2}, ensure_ascii=False))
    except Exception:
        items = []
    if items:
        lines.append("## 昨天你说今天要")
        lines += [f"- {t}" for t in items]
        lines.append("")

    # 晨晚对照（G2）：昨天写下的「今天打算」 vs 昨天的实际记录，温柔对照一句
    try:
        yesterday = (today - timedelta(days=1)).isoformat()
        it = db.q1("SELECT text FROM daily_intents WHERE day=?", (yesterday,))
        if it and it["text"].strip() and (
                ai_mod.fast_configured() or ai_mod.strong_configured()):
            yrows = db.q(
                "SELECT title, content FROM entries WHERE deleted_at IS NULL "
                "AND substr(occurred_at,1,10)=? ORDER BY occurred_at",
                (yesterday,),
            )
            if yrows:
                digest = "\n".join(f"- {r['title']}：{(r['content'] or '')[:200]}" for r in yrows)
                cmp_text = ai_mod.compare_intent(it["text"], digest)
                if cmp_text:
                    lines += ["## 昨天的打算", "", f"你说要：{it['text']}", "", cmp_text, ""]
    except Exception:
        pass

    # N 天前的今天
    for n in (30, 60, 90):
        d = (today - timedelta(days=n)).isoformat()
        r = db.q1(
            "SELECT title, content FROM entries WHERE deleted_at IS NULL "
            "AND substr(occurred_at,1,10)=? ORDER BY occurred_at LIMIT 1",
            (d,),
        )
        if r is not None:
            lines += [f"## {n} 天前的今天", "", f"- {r['title'] or (r['content'] or '')[:30]}", ""]
            break

    lines += ["## 今日一问", "", _muse_today(today), ""]
    return "\n".join(lines)


def _maybe_morning_push(now: datetime) -> None:
    if db.get_setting("push_enabled", "true") == "false":
        return
    if not db.get_setting("push_key").strip():
        return
    if now.hour < MORNING_HOUR or (now.hour == MORNING_HOUR and now.minute < MORNING_MIN):
        return
    if now.hour > 9:  # 起晚了就不打扰（服务中午才开机时不补发）
        return
    today = now.date().isoformat()
    if db.get_setting("last_morning_push") == today:
        return
    # A6b-13：失败退避，避免窗口内每 10 分钟硬重试（窗口内最多 15 次，每次 10s 超时）
    retry_at = _parse_ts(db.get_setting("push_retry_at"))
    if retry_at is not None and now < retry_at:
        return
    ok, _msg = send_push(f"小满 · 晨报 {now.month}月{now.day}日", _build_morning(now.date()))
    if ok:
        db.set_setting("last_morning_push", today)
        db.set_setting("push_retry_at", "")
        logx.log("晨报已推送到微信")
    else:
        db.set_setting("push_retry_at", (now + PUSH_RETRY).isoformat(timespec="seconds"))
        logx.log("晨报推送失败", f"{_msg}（{PUSH_RETRY.seconds // 60} 分钟后重试）")


# ---------------- 圈子：档案综合与复查 ----------------

def _maybe_circle(now: datetime) -> None:
    """每轮：综合有新提及的实体（≤3），再复查搁置档案（≤2）。

    R2-06：原先两处 `except Exception: pass` 全静默，与 A6b-08"消灭调度静默"的意图不一致
    ——单条实体综合失败时用户既看不到日志，也不知道圈子为什么没进展。改为各记一行（带
    异常类型），仍保持"一步失败不影响另一步"。
    """
    from . import circle as circle_mod  # 延迟导入避免循环
    try:
        circle_mod.synthesize_due(3)
    except Exception as e:
        logx.log("圈子调度失败：档案综合",
                 f"{type(e).__name__}: {str(e)[:80]}")
    try:
        circle_mod.review_drafts(2)
    except Exception as e:
        logx.log("圈子调度失败：搁置复查",
                 f"{type(e).__name__}: {str(e)[:80]}")


def _maybe_propose_merges(now: datetime) -> None:
    """合并提案（内部 3 小时节流）。R2-06：失败不再静默，记一行日志。"""
    from . import circle as circle_mod  # 延迟导入避免循环
    try:
        circle_mod.propose_merges(now)
    except Exception as e:
        logx.log("圈子调度失败：合并提案",
                 f"{type(e).__name__}: {str(e)[:80]}")


# ---------------- 历史分类迁移（is_work + AI 多标签） ----------------

_reclassify_lock = threading.Lock()
_reclassify_running = False


def reclassify_status() -> dict:
    return {
        "running": _reclassify_running,
        "processed": int(db.get_setting("reclassify_progress") or 0),
        "total": int(db.get_setting("reclassify_total") or 0),
        "done": db.get_setting("reclassify_done") == "true",
    }


def reclassify_pending() -> bool:
    """是否还有遗留：is_work 未定、或无类目的记录。
    口径与 run_reclassification 的处理范围一致（排除 exclude_from_ai 与空正文），
    否则被排除的记录会让 pending 永不归零、每天空跑并刷假通知（I2）。"""
    r = db.q1(
        "SELECT COUNT(*) AS n FROM entries WHERE deleted_at IS NULL AND exclude_from_ai=0 "
        "AND trim(content) != '' AND ("
        "is_work IS NULL OR "
        "id NOT IN (SELECT entry_id FROM entry_categories))"
    )
    return r["n"] > 0


def start_reclassification() -> bool:
    """后台线程跑迁移；已在跑或 AI 未配置返回 False。"""
    global _reclassify_running
    if not (ai_mod.fast_configured() or ai_mod.strong_configured()):
        return False
    if not _reclassify_lock.acquire(blocking=False):
        return False
    _reclassify_running = True
    threading.Thread(target=_reclassify_worker, daemon=True).start()
    return True


def _reclassify_worker() -> None:
    global _reclassify_running
    try:
        run_reclassification()
    except Exception as e:
        logx.log("历史分类迁移中断", str(e)[:80])
    finally:
        _reclassify_running = False
        _reclassify_lock.release()


def run_reclassification() -> dict:
    """历史分类迁移（幂等，可重复跑）。
    阶段1：category work→is_work=1、life→0（仅 is_work IS NULL）；mixed 逐条小模型重判（拿不准→0）。
    阶段2：无 entry_categories 的记录每 20 条一批补打类目。
    """
    from . import main as main_mod  # 延迟导入避免循环依赖
    done_count = 0

    # 阶段 1a：旧 category 直接映射（不涉及 AI）。
    # 口径与 reclassify_pending 一致：exclude_from_ai / 空正文的记录不纳入（它们永不进 AI 流程，
    # 不统一的话 pending 永不归零、每天空跑——I2）。
    with db.locked() as c:
        cur = c.execute("UPDATE entries SET is_work=1 WHERE is_work IS NULL AND category='work' "
                        "AND deleted_at IS NULL AND exclude_from_ai=0 AND trim(content) != ''")
        n_work = cur.rowcount
        cur = c.execute("UPDATE entries SET is_work=0 WHERE is_work IS NULL AND category='life' "
                        "AND deleted_at IS NULL AND exclude_from_ai=0 AND trim(content) != ''")
        n_life = cur.rowcount
        c.commit()
    done_count += max(n_work, 0) + max(n_life, 0)
    if n_work or n_life:
        logx.log("分类迁移：旧标签映射", f"工作 {n_work} 条、生活 {n_life} 条直接落定")

    # 阶段 1b：mixed 逐条小模型重判，拿不准→0（口径与 reclassify_pending 一致：排除空正文）
    mixed = db.q(
        "SELECT * FROM entries WHERE is_work IS NULL AND category='mixed' "
        "AND deleted_at IS NULL AND exclude_from_ai=0 AND trim(content) != '' "
        "ORDER BY occurred_at"
    )
    total = len(mixed) + db.q1(
        "SELECT COUNT(*) AS n FROM entries WHERE deleted_at IS NULL AND exclude_from_ai=0 "
        "AND trim(content) != '' AND id NOT IN (SELECT entry_id FROM entry_categories)"
    )["n"]
    db.set_setting("reclassify_total", str(total))
    db.set_setting("reclassify_progress", "0")
    db.set_setting("reclassify_done", "false")
    for r in mixed:
        try:
            result = ai_mod.enrich_entry_fields(r["title"], r["summary"], r["content"],
                                                json.loads(r["tags"] or "[]"), r["category"])
            is_work = result.get("is_work")
            db.execute("UPDATE entries SET is_work=? WHERE id=?",
                       (1 if is_work is True else 0, r["id"]))  # 拿不准(None)→0
            if result.get("categories"):
                main_mod._write_entry_categories(r["id"], result["categories"])
            done_count += 1
        except Exception as e:
            logx.log("分类迁移：mixed 重判失败", f"{r['id'][:8]}：{str(e)[:60]}")
        db.set_setting("reclassify_progress", str(done_count))

    # 阶段 2：无类目记录每 20 条一批补打类目（只打类目，不动其它字段）。
    # 一次性取出待处理 id 再分批：避免"补不上类目的条目被反复捞到"造成死循环；
    # 运行期间新产生的记录留给下一次迁移（幂等，随时可再跑）。
    pending_ids = [r["id"] for r in db.q(
        "SELECT id FROM entries WHERE deleted_at IS NULL AND exclude_from_ai=0 "
        "AND trim(content) != '' AND id NOT IN (SELECT entry_id FROM entry_categories) "
        "ORDER BY occurred_at"
    )]
    for i in range(0, len(pending_ids), 20):
        for eid in pending_ids[i:i + 20]:
            r = db.q1("SELECT * FROM entries WHERE id=?", (eid,))
            if r is None:
                continue
            try:
                result = ai_mod.enrich_entry_fields(r["title"], r["summary"], r["content"],
                                                    json.loads(r["tags"] or "[]"), r["category"])
                if result.get("categories"):
                    main_mod._write_entry_categories(r["id"], result["categories"])
                if r["is_work"] is None and result.get("is_work") is not None:
                    db.execute("UPDATE entries SET is_work=? WHERE id=? AND is_work_manual=0",
                               (1 if result["is_work"] else 0, r["id"]))
                done_count += 1
            except Exception as e:
                logx.log("分类迁移：补类目失败", f"{eid[:8]}：{str(e)[:60]}")
            db.set_setting("reclassify_progress", str(done_count))
        logx.log("分类迁移：补类目进度", f"已处理 {done_count}/{total} 条")

    db.set_setting("reclassify_done", "true")
    logx.log("历史分类迁移完成", f"共理顺 {done_count} 条")
    if done_count > 0:
        db.push_notice(f"历史分类已理顺 {done_count} 条")
    return {"processed": done_count, "total": total}


# ---------------- 调度挂载：迁移 / 关联发现 / 月度新类目 ----------------

def _maybe_reclassify(now: datetime) -> None:
    """启动后+每日检查：有遗留（is_work 未定或无类目）就后台跑迁移，直至无遗留。

    A6b-06：日期 marker 移到 `start_reclassification()` **成功之后**。原先先写 marker 再启动，
    若此刻恰好有另一个 worker 正在起（`_reclassify_running` 仍为 False 但锁已被占），
    `start_reclassification()` 返回 False，而当天已不再重试。
    """
    if not (ai_mod.fast_configured() or ai_mod.strong_configured()):
        return
    if _reclassify_running:
        return
    if db.get_setting("reclassify_last_auto") == now.date().isoformat():
        return
    if not reclassify_pending():
        return
    if start_reclassification():
        db.set_setting("reclassify_last_auto", now.date().isoformat())
        logx.log("历史分类迁移已启动", "后台悄悄理顺旧记录的分类")


LINK_DISCOVERY_HOUR = 21


def _maybe_link_discovery(now: datetime) -> None:
    """每晚补算 link_checked=0 的记录的隐性关联（每轮 ≤10 条）。

    A6b-24：补**每天一次**的 marker。原先只有 `now.hour >= 21` 一条判定，21:00–23:59
    每 10 分钟一轮、每轮 10 条 ⇒ 单晚最多约 178 条记录的关联发现（每条都会调嵌入与
    `explain_link`），是无上限的夜间 AI 消耗；其它后台任务都有幂等 marker，只有它没有。
    """
    if now.hour < LINK_DISCOVERY_HOUR:
        return
    if db.get_setting("last_link_discovery_date") == now.date().isoformat():
        return
    db.set_setting("last_link_discovery_date", now.date().isoformat())
    from . import links as links_mod  # 延迟导入避免循环
    links_mod.nightly_batch()


def _maybe_propose_categories(now: datetime) -> None:
    """每月 1 日：未分类记录向量聚类（cosine≥0.8 成簇、≥5 条）→ 大模型命名 → pending 类目。

    A6b-06：marker 从"函数开头"移到**所有前置条件检查之后、AI 调用之前**。原先 marker
    在 748 行就写，之后才检查 `category_ai` 与 AI 配置 —— 用户当月后半月才配好 AI，
    本月的提案机会就永久作废（节流键承担了"已完成"与"已尝试"两种语义）。
    """
    if now.day != 1:
        return
    marker = now.strftime("%Y-%m")
    if db.get_setting("last_category_propose") == marker:
        return
    # 前置条件先判：不满足就**不写** marker，下个月（或同月稍后再开机）还有机会
    if db.get_setting("category_ai", "1") in ("0", "false"):
        return
    if not (ai_mod.embed_configured() and ai_mod.strong_configured()):
        return
    db.set_setting("last_category_propose", marker)  # 条件齐备后才消费本月机会
    rows = db.q(
        "SELECT e.id, e.title, e.summary, e.content, v.vector_json FROM entries e "
        "JOIN entry_embeddings v ON v.entry_id=e.id "
        "WHERE e.deleted_at IS NULL AND e.exclude_from_ai=0 "
        "AND e.id NOT IN (SELECT entry_id FROM entry_categories) LIMIT 200"
    )
    pool = []
    for r in rows:
        try:
            vec = json.loads(r["vector_json"])
        except ValueError:
            continue
        if isinstance(vec, list) and vec:
            pool.append((r, vec))
    # 贪心聚簇：与簇首向量 ≥0.8 即入簇
    clusters: list[list] = []
    for r, vec in pool:
        for c in clusters:
            if ai_mod.cosine(vec, c[0][1]) >= 0.8:
                c.append((r, vec))
                break
        else:
            clusters.append([(r, vec)])
    made = 0
    for c in clusters:
        if len(c) < 5:
            continue
        snippets = [
            ((r["title"] or "") + " " + (r["summary"] or (r["content"] or "")[:60])).strip()
            for r, _v in c
        ]
        try:
            named = ai_mod.name_category_cluster(snippets)
        except Exception as e:
            # AI 挂了：本次机会已消费（本月不再重试），但必须留痕而不是静默"明年再说"
            logx.log("新类目提案失败", f"AI 调用出错：{type(e).__name__} {str(e)[:80]}")
            break
        if not named:
            continue
        if db.q1("SELECT id FROM categories WHERE slug=?", (named["slug"],)):
            continue
        db.execute(
            "INSERT INTO categories(slug, name_zh, definition, source, status, created_at) "
            "VALUES(?,?,?, 'ai', 'pending', ?)",
            (named["slug"], named["name_zh"], named["definition"], db.now_iso()),
        )
        made += 1
        logx.log("新类目提案", f"《{named['name_zh']}》：{len(c)} 条未分类记录似乎是一类，等你确认")
    if made:
        db.push_notice(f"小满发现了 {made} 个可能的新类目，去设置页看看？")


# ---------------- 圈子历史追认（启动后首轮 tick 触发；marker 带版本号） ----------------

_recognition_lock = threading.Lock()
_recognition_running = False


def start_recognition() -> bool:
    """后台线程跑追认；已在跑返回 False。AI 未配置时也允许起线程（内部优雅跳过）。"""
    global _recognition_running
    if not _recognition_lock.acquire(blocking=False):
        return False
    _recognition_running = True
    threading.Thread(target=_recognition_worker, daemon=True).start()
    return True


def recognition_running() -> bool:
    return _recognition_running


def _recognition_worker() -> None:
    global _recognition_running
    try:
        from . import circle as circle_mod  # 延迟导入避免循环
        circle_mod.run_recognition()
    except Exception as e:
        logx.log("圈子追认中断", str(e)[:80])
    finally:
        _recognition_running = False
        _recognition_lock.release()


def _maybe_circle_recognition(now: datetime) -> None:
    """首轮 tick（启动后 30s）起检查：marker 未 done 且未在跑 → 后台追认一次。"""
    from . import circle as circle_mod  # 延迟导入避免循环
    if not circle_mod.recognition_needed():
        return
    if _recognition_running:
        return
    start_recognition()


# ---------------- 每周规律发现（G1） ----------------

def _maybe_insights(now: datetime) -> None:
    """每周一凌晨（06 点前）算一次心情规律；marker=ISO 周幂等。样本不足安静不出。

    A6b-06：marker 移到 `compute_weekly()` **成功之后**。原先先写 marker 再算，算失败
    （AI 超时等）本周就永久放弃，而周一 06:00 前的窗口只有这一个（tick 10 分钟一轮）。
    """
    if not (now.weekday() == 0 and now.hour < 6):
        return
    marker = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
    if db.get_setting("last_insights_week") == marker:
        return
    from . import insights as insights_mod  # 延迟导入避免循环
    try:
        insights_mod.compute_weekly()
        db.set_setting("last_insights_week", marker)  # 只有真算完才消费本周窗口
    except Exception as e:
        logx.log("规律发现本周未算成", f"{type(e).__name__}: {str(e)[:60]}（窗口内稍后重试）")
