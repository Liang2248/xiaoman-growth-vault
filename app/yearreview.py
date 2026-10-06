# -*- coding: utf-8 -*-
"""年度故事卡数据（G6）。

GET /api/year-review?year= → 缓存超 7 天或没有则重算。
聚合全年真实统计；AI（strong）生成 6-8 张 Wrapped 风故事卡 + 一张金句卡（用户原话+日期）。
AI 未配置 → 纯数据卡兜底（数字卡不依赖 AI），金句卡跳过。
进行中的年份一律用「至今」口径。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from . import ai as ai_mod, db, logx

CACHE_DAYS = 7


def _year_stats(year: int) -> dict:
    y0, y1 = f"{year}-01-01", f"{year}-12-31"
    rows = db.q(
        "SELECT substr(occurred_at,1,10) AS d, "
        "(length(content)+length(summary)+length(title)) AS w FROM entries "
        "WHERE deleted_at IS NULL AND substr(occurred_at,1,10) BETWEEN ? AND ?",
        (y0, y1),
    )
    day_words: dict[str, int] = {}
    month_words: dict[str, int] = {}
    total_words = 0
    for r in rows:
        w = r["w"] or 0
        total_words += w
        day_words[r["d"]] = day_words.get(r["d"], 0) + w
        month_words[r["d"][:7]] = month_words.get(r["d"][:7], 0) + w

    # 最长连续记录天数
    longest = cur = 0
    prev = None
    for d in sorted(day_words):
        dd = date.fromisoformat(d)
        if prev is not None and (dd - prev).days == 1:
            cur += 1
        else:
            cur = 1
        longest = max(longest, cur)
        prev = dd

    best_day = max(day_words.items(), key=lambda kv: kv[1]) if day_words else None

    cats = db.q(
        "SELECT c.name_zh AS name, COUNT(DISTINCT ec.entry_id) AS n "
        "FROM entry_categories ec JOIN categories c ON c.id=ec.category_id "
        "JOIN entries e ON e.id=ec.entry_id "
        "WHERE e.deleted_at IS NULL AND c.status='active' "
        "AND substr(e.occurred_at,1,10) BETWEEN ? AND ? "
        "GROUP BY c.id ORDER BY n DESC LIMIT 6",
        (y0, y1),
    )

    def top_entities(mtype: str, k: int = 3) -> list[dict]:
        rows2 = db.q(
            "SELECT en.name, COUNT(*) AS n FROM entity_mentions m "
            "JOIN entries e ON e.id=m.entry_id JOIN entities en ON en.id=m.entity_id "
            "WHERE e.deleted_at IS NULL AND en.type=? AND en.status != 'rejected' "
            "AND substr(e.occurred_at,1,10) BETWEEN ? AND ? "
            "GROUP BY en.id ORDER BY n DESC LIMIT ?",
            (mtype, y0, y1, k),
        )
        return [{"name": r["name"], "count": r["n"]} for r in rows2]

    return {
        "year": year,
        "total_entries": len(rows),
        "total_words": total_words,
        "days_with_entries": len(day_words),
        "longest_streak": longest,
        "best_day": {"date": best_day[0], "words": best_day[1]} if best_day else None,
        "monthly_words": dict(sorted(month_words.items())),
        "categories": [{"name": r["name"], "count": r["n"]} for r in cats],
        "top_persons": top_entities("person"),
        "top_places": top_entities("place"),
    }


def _quote_candidates(year: int) -> list[dict]:
    """金句候选：全年记录里 20-80 字的正文片段（带日期），取最近 40 条。"""
    rows = db.q(
        "SELECT substr(occurred_at,1,10) AS d, content FROM entries "
        "WHERE deleted_at IS NULL AND exclude_from_ai=0 "
        "AND substr(occurred_at,1,10) BETWEEN ? AND ? ORDER BY occurred_at DESC LIMIT 60",
        (f"{year}-01-01", f"{year}-12-31"),
    )
    out = []
    for r in rows:
        for line in (r["content"] or "").split("\n"):
            line = line.strip().strip("。；;")
            if 20 <= len(line) <= 80 and not line.startswith("#"):
                out.append({"date": r["d"], "text": line})
                break
        if len(out) >= 40:
            break
    return out


def _data_cards(stats: dict, year: int) -> list[dict]:
    """纯数据卡兜底（不依赖 AI）。进行中年份用「至今」口径。"""
    current = date.today().year == year
    scope = f"{year} 年至今" if current else f"{year} 年"
    cards = []

    def card(big, title, text):
        cards.append({"kind": "card", "big": str(big), "title": title, "text": text})

    card(stats["total_entries"], f"{scope}的记录", f"一共写下 {stats['total_entries']} 条，每一步都算数。")
    card(stats["total_words"], "写下的字", f"{scope}共 {stats['total_words']} 字。")
    if stats["longest_streak"]:
        card(f"{stats['longest_streak']} 天", "最长连续记录", "连续不断的日子，最见心性。")
    if stats["best_day"]:
        card(stats["best_day"]["words"], "最高产的一天",
             f"{stats['best_day']['date']}，一天写了 {stats['best_day']['words']} 字。")
    if stats["categories"]:
        c0 = stats["categories"][0]
        card(c0["count"], f"最常记录：{c0['name']}", f"「{c0['name']}」出现了 {c0['count']} 次。")
    if stats["top_persons"]:
        p = stats["top_persons"][0]
        card(p["count"], "最常提起的人", f"《{p['name']}》被提起 {p['count']} 次。")
    if stats["top_places"]:
        pl = stats["top_places"][0]
        card(pl["count"], "最常去的地方", f"《{pl['name']}》出现了 {pl['count']} 次。")
    return cards


def get_year_review(year: int) -> dict:
    """缓存超 7 天或没有则重算。返回 {year, cards, quote, generated_at, ai: bool}。"""
    row = db.q1("SELECT * FROM year_reviews WHERE year=?", (year,))
    if row is not None:
        try:
            gen = datetime.fromisoformat(row["generated_at"])
            if (datetime.now().astimezone() - gen).days < CACHE_DAYS:
                payload = json.loads(row["payload_json"])
                return {"year": year, "generated_at": row["generated_at"], **payload}
        except (ValueError, TypeError):
            pass

    stats = _year_stats(year)
    ai_used = False
    quote = None
    cards: list[dict] = []
    if ai_mod.strong_configured() and stats["total_entries"] >= 5:
        try:
            result = ai_mod.generate_year_cards(stats, _quote_candidates(year))
            cards = result["cards"]
            quote = result.get("quote")
            ai_used = True
        except Exception as e:
            logx.log("年度故事卡 AI 生成失败", f"改用纯数据卡：{str(e)[:60]}")
    if not cards:
        cards = _data_cards(stats, year)
    payload = {"cards": cards, "quote": quote, "ai": ai_used, "stats": stats}
    now = db.now_iso()
    db.execute(
        "INSERT INTO year_reviews(year, payload_json, generated_at) VALUES(?,?,?) "
        "ON CONFLICT(year) DO UPDATE SET payload_json=excluded.payload_json, "
        "generated_at=excluded.generated_at",
        (year, json.dumps(payload, ensure_ascii=False), now),
    )
    logx.log("年度故事卡已生成", f"{year} 年，{len(cards)} 张卡"
                              + ("（含 AI 金句卡）" if ai_used else "（纯数据卡）"))
    return {"year": year, "generated_at": now, **payload}
