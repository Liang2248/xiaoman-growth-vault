"""定时自动化：自动周报/月报、自动备份。

daemon 线程每 10 分钟一轮；每步独立 try/except 静默，绝不影响主服务。
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


def start() -> None:
    threading.Thread(target=_loop, daemon=True).start()


def _loop() -> None:
    time.sleep(30)  # 启动后 30 秒先跑一轮（迁移/补算检查不用等 10 分钟）
    while True:
        try:
            tick()
        except Exception:
            pass
        time.sleep(INTERVAL_SECONDS)


def tick(now: datetime | None = None) -> None:
    now = now or datetime.now().astimezone()
    for fn in (_maybe_auto_report, _maybe_auto_backup, _maybe_backfill_embeddings,
               _maybe_backfill_enrich, _maybe_backfill_mood, _maybe_daily_push,
               _maybe_morning_push, _maybe_circle, _maybe_propose_merges,
               _maybe_reclassify, _maybe_link_discovery, _maybe_propose_categories,
               _maybe_circle_recognition, _maybe_insights):
        try:
            fn(now)
        except Exception:
            pass


# ---------------- 自动报告 ----------------

def _maybe_auto_report(now: datetime) -> None:
    if db.get_setting("auto_report_enabled", "true") == "false":
        return
    if not ai_mod.strong_configured():
        return
    today = now.date()
    # 周日 21:00 后：生成上周（周一至周日）周报
    if now.weekday() == 6 and now.hour >= AUTO_REPORT_HOUR:
        week_start = today - timedelta(days=7)
        week_start = week_start - timedelta(days=week_start.weekday())
        _gen_once("weekly", week_start, "last_auto_weekly")
    # 每月 1 日 21:00 后：生成上月月报
    if today.day == 1 and now.hour >= AUTO_REPORT_HOUR:
        month_start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        _gen_once("monthly", month_start, "last_auto_monthly")


def _gen_once(rtype: str, period_start, marker_key: str) -> None:
    ps = period_start.isoformat()
    if db.get_setting(marker_key) == ps:
        return
    if db.q1("SELECT id FROM reports WHERE type=? AND period_start=?", (rtype, ps)):
        db.set_setting(marker_key, ps)
        return
    try:
        # 周期内无记录会在 generate_report 内抛 NoDataError，下轮再试
        rep = ai_mod.generate_report(rtype, ps)
        db.set_setting(marker_key, ps)
        label = {"weekly": "周报", "monthly": "月报"}.get(rtype, "报告")
        logx.log(f"定时任务：自动{label}已生成", f"周期 {ps} 起")
        _push_report_ready(rep)
    except Exception as e:
        logx.log("定时任务：自动报告未生成", str(e)[:80])


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
    last = db.get_setting("last_backup_at")
    if last:
        try:
            if (now - datetime.fromisoformat(last)) < BACKUP_INTERVAL:
                return
        except ValueError:
            pass

    outdir = db.get_setting("backup_dir").strip()
    target_dir = Path(outdir) if outdir else db.BACKUPS_DIR
    if not _writable(target_dir):
        target_dir = db.BACKUPS_DIR  # 无效/不可写静默回退

    name = f"backup-auto-{now:%Y%m%d-%H%M}.zip"
    try:
        src = exporter.build_backup_zip()
    except exporter.BackupError as e:
        logx.log("自动备份自检未通过", str(e))
        return
    except Exception as e:
        logx.log("自动备份失败", str(e)[:80])
        return
    try:
        shutil.move(str(src), str(target_dir / name))
    except Exception:
        try:
            shutil.move(str(src), str(db.BACKUPS_DIR / name))
            target_dir = db.BACKUPS_DIR
        except Exception:
            return

    try:
        zips = sorted(target_dir.glob("backup-auto-*.zip"), key=lambda p: p.name)
        for old in zips[:-BACKUP_KEEP]:
            old.unlink(missing_ok=True)
    except Exception:
        pass
    db.set_setting("last_backup_at", now.isoformat(timespec="seconds"))
    try:
        size_mb = round((target_dir / name).stat().st_size / 1024 / 1024, 1)
    except Exception:
        size_mb = "?"
    logx.log("自动备份完成并自检通过", f"{target_dir / name}（{size_mb}MB）")


# ---------------- 嵌入向量回填 ----------------

EMBED_TRICKLE = 5


def _maybe_backfill_embeddings(now: datetime) -> None:
    """嵌入模型配置后，每轮为少量历史记录与知识条目补向量，直到全部入库（静默）。"""
    if not ai_mod.embed_configured():
        return
    rows = db.q(
        "SELECT e.id FROM entries e LEFT JOIN entry_embeddings v ON v.entry_id=e.id "
        "WHERE e.deleted_at IS NULL AND v.entry_id IS NULL ORDER BY e.occurred_at DESC LIMIT ?",
        (EMBED_TRICKLE,),
    )
    done = 0
    for r in rows:
        if not ai_mod.embed_entry(r["id"]):
            break  # 本轮失败即停，下轮再试
        done += 1
    if done:
        logx.log("语义索引回填", f"本轮补了 {done} 条记录向量")
    # 知识条目向量是语义判重的前提，一并没有的补
    krows = db.q(
        "SELECT id FROM knowledge WHERE (vector_json IS NULL OR vector_json = '') "
        "AND status IN ('pending','accepted','rejected') LIMIT ?",
        (EMBED_TRICKLE,),
    )
    kdone = 0
    for r in krows:
        if not ai_mod.embed_knowledge_item(r["id"]):
            break
        kdone += 1
    if kdone:
        logx.log("语义索引回填", f"本轮补了 {kdone} 条知识向量")
    # 圈子实体向量（语义查重的前提），每轮 5 个
    erows = db.q(
        "SELECT id FROM entities WHERE (vector_json IS NULL OR vector_json = '') "
        "AND status != 'rejected' LIMIT 5"
    )
    edone = 0
    for r in erows:
        if not ai_mod.embed_entity(r["id"]):
            break
        edone += 1
    if edone:
        logx.log("语义索引回填", f"本轮补了 {edone} 个圈子档案向量")


# ---------------- 轻量补全回填 ----------------

ENRICH_TRICKLE = 3


def _maybe_backfill_enrich(now: datetime) -> None:
    """每轮为少量「有正文但仍缺标题/摘要/标签」的历史记录自动补全（静默）。"""
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
    for row in rows:
        try:
            if main_mod._enrich_fill(row, use_category=False):
                done += 1
        except Exception:
            break  # 本轮失败即停，下轮再试
    if done:
        logx.log("历史记录补全回填", f"本轮补 {done} 条")


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
PUSHPLUS_URL = "http://www.pushplus.plus/send"

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
    """push_key 以 SCT 开头走 Server酱（form），否则 PushPlus（json）。"""
    key = db.get_setting("push_key").strip()
    if not key:
        return False, "未配置推送 Key"
    try:
        with httpx.Client(timeout=10.0) as c:
            if key.startswith("SCT"):
                r = c.post(SERVERCHAN_URL.format(key=key),
                           data={"title": title, "desp": content})
            else:
                r = c.post(PUSHPLUS_URL,
                           json={"token": key, "title": title, "content": content,
                                 "template": "markdown"})
        if r.status_code != 200:
            return False, f"推送服务返回 HTTP {r.status_code}"
        return True, "推送成功"
    except httpx.TimeoutException:
        return False, "推送超时，请检查网络"
    except Exception as e:
        return False, f"推送失败：{e}"


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

    # muse：直接用当天缓存，没有就用静态轮换句（调度里不发起 AI 调用）
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
    content = _build_daily_review(now.date())
    ok, _msg = send_push(f"小满 · 每日回顾 {now.month}月{now.day}日", content)
    if ok:
        db.set_setting("last_push_date", today)
        logx.log("晚报已推送到微信")
    else:
        logx.log("晚报推送失败", _msg)


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

    # 天气（总开关开启时尽力而为，调度里不发 AI 调用）
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
    ok, _msg = send_push(f"小满 · 晨报 {now.month}月{now.day}日", _build_morning(now.date()))
    if ok:
        db.set_setting("last_morning_push", today)
        logx.log("晨报已推送到微信")
    else:
        logx.log("晨报推送失败", _msg)


# ---------------- 圈子：档案综合与复查 ----------------

def _maybe_circle(now: datetime) -> None:
    """每轮：综合有新提及的实体（≤3），再复查搁置档案（≤2）。全部静默。"""
    from . import circle as circle_mod  # 延迟导入避免循环
    try:
        circle_mod.synthesize_due(3)
    except Exception:
        pass
    try:
        circle_mod.review_drafts(2)
    except Exception:
        pass


def _maybe_propose_merges(now: datetime) -> None:
    """合并提案（内部 3 小时节流，全静默）。"""
    from . import circle as circle_mod  # 延迟导入避免循环
    try:
        circle_mod.propose_merges(now)
    except Exception:
        pass


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
    """启动后+每日检查：有遗留（is_work 未定或无类目）就后台跑迁移，直至无遗留。"""
    if not (ai_mod.fast_configured() or ai_mod.strong_configured()):
        return
    if _reclassify_running:
        return
    if db.get_setting("reclassify_last_auto") == now.date().isoformat():
        return
    if not reclassify_pending():
        return
    db.set_setting("reclassify_last_auto", now.date().isoformat())
    if start_reclassification():
        logx.log("历史分类迁移已启动", "后台悄悄理顺旧记录的分类")


LINK_DISCOVERY_HOUR = 21


def _maybe_link_discovery(now: datetime) -> None:
    """每晚补算 link_checked=0 的记录的隐性关联（每轮 ≤10 条，静默）。"""
    if now.hour < LINK_DISCOVERY_HOUR:
        return
    from . import links as links_mod  # 延迟导入避免循环
    links_mod.nightly_batch()


def _maybe_propose_categories(now: datetime) -> None:
    """每月 1 日：未分类记录向量聚类（cosine≥0.8 成簇、≥5 条）→ 大模型命名 → pending 类目。"""
    if now.day != 1:
        return
    marker = now.strftime("%Y-%m")
    if db.get_setting("last_category_propose") == marker:
        return
    db.set_setting("last_category_propose", marker)  # 每月只试一次
    if db.get_setting("category_ai", "1") in ("0", "false"):
        return
    if not (ai_mod.embed_configured() and ai_mod.strong_configured()):
        return
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
        except Exception:
            break  # AI 挂了，明年再说
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
    """每周一凌晨（06 点前）算一次心情规律；marker=ISO 周幂等。样本不足安静不出。"""
    if not (now.weekday() == 0 and now.hour < 6):
        return
    marker = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
    if db.get_setting("last_insights_week") == marker:
        return
    db.set_setting("last_insights_week", marker)
    from . import insights as insights_mod  # 延迟导入避免循环
    try:
        insights_mod.compute_weekly()
    except Exception as e:
        logx.log("规律发现本周未算成", str(e)[:60])
