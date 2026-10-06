"""统计：总览数据与情景推演所需的周序列，全部由 SQL/程序计算。"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, timedelta

from . import db


def _entry_rows(include_excluded: bool = True) -> list:
    sql = (
        "SELECT substr(occurred_at,1,10) AS d, category, tags, "
        "substr(occurred_at,12,2) AS h, location_name, "
        "(length(content)+length(summary)+length(title)) AS w "
        "FROM entries WHERE deleted_at IS NULL"
    )
    if not include_excluded:
        sql += " AND exclude_from_ai=0"
    return db.q(sql)


def overview() -> dict:
    rows = _entry_rows()
    today = date.today()

    day_counts: Counter = Counter()
    day_words: Counter = Counter()
    tag_counts: Counter = Counter()
    hour_counts: Counter = Counter()
    loc_counts: Counter = Counter()
    total_words = 0

    for r in rows:
        d = r["d"]
        day_counts[d] += 1
        day_words[d] += r["w"] or 0
        total_words += r["w"] or 0
        if r["h"]:
            hour_counts[int(r["h"])] += 1
        if r["location_name"]:
            loc_counts[r["location_name"]] += 1
        try:
            for t in json.loads(r["tags"] or "[]"):
                if t:
                    tag_counts[str(t)] += 1
        except (ValueError, TypeError):
            pass

    days_with_entries = set(day_counts)

    # 连续记录天数：从今天（或昨天）往前数
    streak = 0
    cursor = None
    if today.isoformat() in days_with_entries:
        cursor = today
    elif (today - timedelta(days=1)).isoformat() in days_with_entries:
        cursor = today - timedelta(days=1)
    while cursor is not None and cursor.isoformat() in days_with_entries:
        streak += 1
        cursor -= timedelta(days=1)

    heatmap_start = today - timedelta(days=179)
    heatmap = [
        {"date": d, "count": day_counts[d]}
        for d in sorted(day_counts)
        if d >= heatmap_start.isoformat()
    ]

    trend_start = today - timedelta(days=89)
    daily_trend = []
    for i in range(90):
        d = (trend_start + timedelta(days=i)).isoformat()
        daily_trend.append({"date": d, "count": day_counts.get(d, 0), "words": day_words.get(d, 0)})

    media_count = db.q1(
        "SELECT COUNT(*) AS n FROM attachments a JOIN entries e ON a.entry_id=e.id "
        "WHERE e.deleted_at IS NULL"
    )["n"]

    # 分类统计：新类目体系（entry_categories JOIN categories），保持 {名称: 条数} 字典形状
    cat_rows = db.q(
        "SELECT c.name_zh AS name, COUNT(DISTINCT ec.entry_id) AS n "
        "FROM entry_categories ec "
        "JOIN categories c ON c.id = ec.category_id "
        "JOIN entries e ON e.id = ec.entry_id "
        "WHERE e.deleted_at IS NULL AND c.status='active' "
        "GROUP BY c.id ORDER BY n DESC"
    )
    category_counts = {r["name"]: r["n"] for r in cat_rows}

    return {
        "streak_days": streak,
        "total_entries": len(rows),
        "total_words": total_words,
        "media_count": media_count,
        "days_active": len(days_with_entries),
        "heatmap": heatmap,
        "daily_trend": daily_trend,
        "category_counts": category_counts,
        "tag_counts": [
            {"tag": t, "count": c} for t, c in tag_counts.most_common(12)
        ],
        "hourly": [{"hour": h, "count": hour_counts.get(h, 0)} for h in range(24)],
        "locations": [
            {"name": n, "count": c} for n, c in loc_counts.most_common(10)
        ],
        "knowledge_counts": _knowledge_counts(),
        "weekly_12": _weekly_12(today, day_counts),
        "mood_30": _mood_30(today),
        "period_compare": _period_compare(today),
        "keyword_trends": _keyword_trends(today),
        "entity_trends": _entity_trends(today),
        "fun_facts": {
            "total_words": total_words,
            "novels_eq": (f"约等于 {total_words // 20000} 篇 2 万字中篇"
                          if total_words >= 20000 else
                          f"再写 {20000 - total_words} 字就凑够一篇 2 万字中篇"),
        },
    }


def _week_buckets(today: date, weeks: int = 12) -> list[date]:
    monday = _iso_monday(today)
    return [monday - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]


def _keyword_trends(today: date) -> list[dict]:
    """程序计算近12周标签趋势，给前端显示新出现/持续/回落，不让 AI 猜趋势。"""
    buckets = _week_buckets(today)
    start = buckets[0].isoformat()
    rows = db.q(
        "SELECT occurred_at, tags FROM entries WHERE deleted_at IS NULL "
        "AND substr(occurred_at,1,10) >= ?",
        (start,),
    )
    totals: Counter = Counter()
    weekly: dict[str, Counter] = {}
    for r in rows:
        try:
            d = date.fromisoformat(r["occurred_at"][:10])
            week = _iso_monday(d).isoformat()
            tags = json.loads(r["tags"] or "[]")
        except (ValueError, TypeError):
            continue
        for tag in tags if isinstance(tags, list) else []:
            tag = str(tag).strip()
            if tag:
                totals[tag] += 1
                weekly.setdefault(tag, Counter())[week] += 1
    out = []
    for tag, total in totals.most_common(8):
        vals = [weekly.get(tag, Counter()).get(w.isoformat(), 0) for w in buckets]
        recent, earlier = sum(vals[-4:]), sum(vals[:4])
        if total <= 1 and earlier == 0:
            state = "新出现"
        elif recent > earlier * 1.25:
            state = "上升"
        elif recent * 1.25 < earlier:
            state = "回落"
        else:
            state = "持续"
        out.append({"tag": tag, "total": total, "weekly": vals, "state": state})
    return out


def _entity_trends(today: date) -> list[dict]:
    """人物/事件的提及趋势：活跃、沉寂、重新出现均由 SQL + 周计数得出。"""
    buckets = _week_buckets(today)
    start = buckets[0].isoformat()
    rows = db.q(
        "SELECT en.id, en.name, en.type, MAX(substr(e.occurred_at,1,10)) AS last_seen, "
        "COUNT(m.id) AS total FROM entity_mentions m "
        "JOIN entities en ON en.id=m.entity_id "
        "JOIN entries e ON e.id=m.entry_id "
        "WHERE e.deleted_at IS NULL AND en.status IN ('confirmed','active') "
        "AND substr(e.occurred_at,1,10) >= ? "
        "GROUP BY en.id ORDER BY total DESC, last_seen DESC LIMIT 8",
        (start,),
    )
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    marks = ",".join("?" for _ in ids)
    mentions = db.q(
        f"SELECT m.entity_id, substr(e.occurred_at,1,10) AS d FROM entity_mentions m "
        f"JOIN entries e ON e.id=m.entry_id WHERE e.deleted_at IS NULL AND m.entity_id IN ({marks}) "
        f"AND substr(e.occurred_at,1,10) >= ?",
        tuple(ids) + (start,),
    )
    by_id = {eid: Counter() for eid in ids}
    for r in mentions:
        try:
            by_id[r["entity_id"]][_iso_monday(date.fromisoformat(r["d"])).isoformat()] += 1
        except (ValueError, TypeError):
            pass
    out = []
    for r in rows:
        vals = [by_id[r["id"]].get(w.isoformat(), 0) for w in buckets]
        last = r["last_seen"] or ""
        age = (today - date.fromisoformat(last)).days if last else 9999
        if vals[-1] > 0 and sum(vals[:-3]) > 0:
            state = "重新出现"
        elif age <= 21:
            state = "活跃"
        else:
            state = "沉寂"
        out.append({"id": r["id"], "name": r["name"], "type": r["type"],
                    "total": r["total"], "last_seen": last, "weekly": vals, "state": state})
    return out


def _month_stats(day_from: str, day_to: str) -> dict:
    rows = db.q(
        "SELECT (length(content)+length(summary)+length(title)) AS w, mood_score, tags "
        "FROM entries WHERE deleted_at IS NULL "
        "AND substr(occurred_at,1,10) BETWEEN ? AND ?",
        (day_from, day_to),
    )
    words = sum(r["w"] or 0 for r in rows)
    moods = [r["mood_score"] for r in rows if r["mood_score"] is not None]
    mood_avg = round(sum(moods) / len(moods), 1) if moods else None
    tag_counts: Counter = Counter()
    for r in rows:
        try:
            for t in json.loads(r["tags"] or "[]"):
                if t:
                    tag_counts[str(t)] += 1
        except (ValueError, TypeError):
            pass
    top_tag = tag_counts.most_common(1)[0][0] if tag_counts else None
    return {"entries": len(rows), "words": words, "mood_avg": mood_avg, "top_tag": top_tag}


def _period_compare(today: date) -> dict:
    """近 30 天 vs 之前 30 天（滚动窗口——按月切会在月初对比上月空窗期，全是误导性的零）。"""
    this_start = today - timedelta(days=29)
    last_end = today - timedelta(days=30)
    last_start = today - timedelta(days=59)
    return {
        "this": {"label": "近 30 天", **_month_stats(this_start.isoformat(), today.isoformat())},
        "last": {"label": "之前 30 天", **_month_stats(last_start.isoformat(), last_end.isoformat())},
    }


def _mood_30(today: date) -> list:
    """近 30 天有心情分数的天：当天均分 + 当天首个 label。"""
    start = (today - timedelta(days=29)).isoformat()
    rows = db.q(
        "SELECT substr(occurred_at,1,10) AS d, mood_score, mood_label FROM entries "
        "WHERE deleted_at IS NULL AND mood_score IS NOT NULL "
        "AND substr(occurred_at,1,10) >= ? ORDER BY occurred_at",
        (start,),
    )
    days: dict[str, dict] = {}
    for r in rows:
        slot = days.setdefault(r["d"], {"scores": [], "label": None})
        slot["scores"].append(r["mood_score"])
        if slot["label"] is None and r["mood_label"]:
            slot["label"] = r["mood_label"]
    return [
        {"date": d, "score": round(sum(v["scores"]) / len(v["scores"]), 1), "label": v["label"]}
        for d, v in sorted(days.items())
    ]


def _knowledge_counts() -> dict:
    rows = db.q(
        "SELECT type, COUNT(*) AS n FROM knowledge WHERE status='accepted' GROUP BY type"
    )
    counts = {r["type"]: r["n"] for r in rows}
    return {t: counts.get(t, 0) for t in ("experience", "pitfall", "case", "sop", "skill")}


def _weekly_12(today: date, day_counts) -> list:
    """近 12 周每周记录数（补零），week 标签为周一的 MM.DD。"""
    this_monday = today - timedelta(days=today.isoweekday() - 1)
    out = []
    for i in range(11, -1, -1):
        monday = this_monday - timedelta(weeks=i)
        sunday = monday + timedelta(days=6)
        count = sum(
            c for d, c in day_counts.items()
            if monday.isoformat() <= d <= sunday.isoformat()
        )
        out.append({"week": monday.strftime("%m.%d"), "count": count})
    return out


def _iso_monday(d: date) -> date:
    return d - timedelta(days=d.isoweekday() - 1)


def weekly_series() -> list[dict]:
    """有记录的 ISO 周序列（连续补零），[{week: '2026-W29', count: n}, ...]。"""
    rows = _entry_rows(include_excluded=False)
    if not rows:
        return []
    week_counts: Counter = Counter()
    for r in rows:
        try:
            d = date.fromisoformat(r["d"])
        except ValueError:
            continue
        week_counts[_iso_monday(d)] += 1
    if not week_counts:
        return []
    first, last = min(week_counts), max(week_counts)
    series = []
    cursor = first
    while cursor <= last:
        iso = cursor.isocalendar()
        series.append({"week": f"{iso[0]}-W{iso[1]:02d}", "count": week_counts.get(cursor, 0)})
        cursor += timedelta(days=7)
    return series


def forecast_inputs() -> dict:
    """情景推演所需的数值输入。周数不足抛 ValueError(已有周数)。"""
    series = weekly_series()
    data_weeks = sum(1 for s in series if s["count"] > 0)
    if data_weeks < 8:
        raise ValueError(data_weeks)

    counts = [s["count"] for s in series]
    n = len(counts)
    xs = list(range(n))
    x_bar = sum(xs) / n
    y_bar = sum(counts) / n
    denom = sum((x - x_bar) ** 2 for x in xs)
    slope = round(sum((x - x_bar) * (y - y_bar) for x, y in zip(xs, counts)) / denom, 3) if denom else 0.0
    moving_avg_4 = round(sum(counts[-4:]) / min(4, n), 2)

    totals = db.q1(
        "SELECT COUNT(*) AS n, COALESCE(SUM(length(content)+length(summary)+length(title)),0) AS w "
        "FROM entries WHERE deleted_at IS NULL AND exclude_from_ai=0"
    )
    return {
        "data_weeks": data_weeks,
        "stats": {
            "weekly_counts": series,
            "slope": slope,
            "moving_avg_4": moving_avg_4,
            "total_entries": totals["n"],
            "total_words": totals["w"],
        },
    }
