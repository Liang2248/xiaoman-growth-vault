# -*- coding: utf-8 -*-
"""小满发现的规律（相关性发现引擎，G1）。

近 180 天记录按天聚合，对每个特征算「有该特征的天 vs 没有的天」的心情均值差与
点二列相关 r；同日与滞后 1 日（今天有→明天心情）各算一遍。
门槛从严：特征出现 ≥8 天且对照 ≥8 天、|r|≥0.25 才出；取 |r| Top6。
dismiss 过的 feature 永不再出；无心情数据/样本不足 → 安静不出（不报错、不通知）。
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta

from . import ai as ai_mod, db, logx

DAYS = 180
MIN_DAYS = 8          # 特征与对照各至少 8 天
MIN_R = 0.25          # 点二列相关绝对值下限
TOP_N = 6


def _daily_mood() -> dict[str, float]:
    """每天的心情均分（只取有 mood_score 的天）。"""
    rows = db.q(
        "SELECT substr(occurred_at,1,10) AS d, AVG(mood_score) AS m FROM entries "
        "WHERE deleted_at IS NULL AND mood_score IS NOT NULL "
        "AND substr(occurred_at,1,10) >= ? GROUP BY d",
        ((date.today() - timedelta(days=DAYS)).isoformat(),),
    )
    return {r["d"]: r["m"] for r in rows}


def _point_biserial(days_with: list[float], days_without: list[float]) -> float:
    """点二列相关 r。两组都为空/无方差返回 0。"""
    n1, n0 = len(days_with), len(days_without)
    if n1 < MIN_DAYS or n0 < MIN_DAYS:
        return 0.0
    allv = days_with + days_without
    n = len(allv)
    mean = sum(allv) / n
    var = sum((v - mean) ** 2 for v in allv) / n
    if var <= 0:
        return 0.0
    m1 = sum(days_with) / n1
    m0 = sum(days_without) / n0
    p = n1 / n
    return (m1 - m0) / math.sqrt(var) * math.sqrt(p * (1 - p))


def _feature_days(start: date) -> dict[str, set[str]]:
    """特征 → 出现日期集合。特征清单：12 类目（有无）、Top 人物、天气晴/雨、
    23 点后有记录、字数超中位数、一天记了 2 条以上。"""
    feats: dict[str, set[str]] = {}
    since = start.isoformat()

    # 类目（entry_categories JOIN categories，仅 active）
    for r in db.q(
        "SELECT DISTINCT substr(e.occurred_at,1,10) AS d, c.slug, c.name_zh "
        "FROM entries e JOIN entry_categories ec ON ec.entry_id=e.id "
        "JOIN categories c ON c.id=ec.category_id "
        "WHERE e.deleted_at IS NULL AND c.status='active' AND substr(e.occurred_at,1,10) >= ?",
        (since,),
    ):
        feats.setdefault(f"cat:{r['slug']}", set()).add(r["d"])

    # Top 人物：提及天数 ≥3 的 confirmed/active person（封顶 8 个）
    persons = db.q(
        "SELECT en.id, en.name, COUNT(DISTINCT substr(e.occurred_at,1,10)) AS nd "
        "FROM entity_mentions m JOIN entries e ON e.id=m.entry_id "
        "JOIN entities en ON en.id=m.entity_id "
        "WHERE e.deleted_at IS NULL AND en.type='person' AND en.status IN ('confirmed','active') "
        "AND substr(e.occurred_at,1,10) >= ? GROUP BY en.id HAVING nd >= 3 "
        "ORDER BY nd DESC LIMIT 8",
        (since,),
    )
    for p in persons:
        days = db.q(
            "SELECT DISTINCT substr(e.occurred_at,1,10) AS d FROM entity_mentions m "
            "JOIN entries e ON e.id=m.entry_id WHERE m.entity_id=? AND e.deleted_at IS NULL "
            "AND substr(e.occurred_at,1,10) >= ?",
            (p["id"], since),
        )
        feats[f"person:{p['id']}"] = {r["d"] for r in days}

    # 天气（晴/雨）、熬夜（23 点后）、字数、记录数
    day_rows = db.q(
        "SELECT substr(occurred_at,1,10) AS d, weather_json, "
        "substr(occurred_at,12,2) AS h, "
        "(length(content)+length(summary)+length(title)) AS w FROM entries "
        "WHERE deleted_at IS NULL AND substr(occurred_at,1,10) >= ?",
        (since,),
    )
    words_by_day: dict[str, int] = {}
    count_by_day: dict[str, int] = {}
    for r in day_rows:
        count_by_day[r["d"]] = count_by_day.get(r["d"], 0) + 1
        words_by_day[r["d"]] = words_by_day.get(r["d"], 0) + (r["w"] or 0)
        try:
            wtext = (json.loads(r["weather_json"]) or {}).get("text", "") if r["weather_json"] else ""
        except ValueError:
            wtext = ""
        if "雨" in wtext:
            feats.setdefault("weather:雨", set()).add(r["d"])
        if any(k in wtext for k in ("晴", "多云")):
            feats.setdefault("weather:晴", set()).add(r["d"])
        if r["h"] and int(r["h"]) >= 23:
            feats.setdefault("habit:熬夜", set()).add(r["d"])
    if words_by_day:
        med = sorted(words_by_day.values())[len(words_by_day) // 2]
        for d, w in words_by_day.items():
            if w > med and med > 0:
                feats.setdefault("habit:写字多", set()).add(d)
    for d, c in count_by_day.items():
        if c >= 2:
            feats.setdefault("habit:记录多", set()).add(d)
    return feats


def _feature_label(fid: str) -> str:
    if fid.startswith("cat:"):
        row = db.q1("SELECT name_zh FROM categories WHERE slug=?", (fid[4:],))
        return f"有「{row['name_zh']}」的日子" if row else fid
    if fid.startswith("person:"):
        row = db.q1("SELECT name FROM entities WHERE id=?", (fid[7:],))
        return f"提到《{row['name']}》的日子" if row else fid
    if fid.startswith("weather:"):
        return f"{fid[8:]}天"
    return {"habit:熬夜": "23 点后还在记录的日子", "habit:写字多": "写字多的日子",
            "habit:记录多": "一天记了好几条的日子"}.get(fid, fid)


def _dismissed_features() -> set[str]:
    return {r["feature"] for r in db.q("SELECT DISTINCT feature FROM insights WHERE status='dismissed'")}


def _render_text(label: str, ev: dict) -> str:
    """小模型润色成一句温暖人话；AI 不可用/失败时用克制模板。都要自然带出「相关不等于因果」。"""
    diff = ev["avg_with"] - ev["avg_without"]
    direction = "高" if diff >= 0 else "低"
    plain = (f"{label}，你的心情平均 {ev['avg_with']:.1f} 分，比没有的日子{direction} "
             f"{abs(diff):.1f} 分（{ev['days_with']} 天对比 {ev['days_without']} 天"
             f"{'，看的是第二天的心情' if ev['lag'] else ''}；相关不等于因果，只是个小线索）")
    if not (ai_mod.fast_configured() or ai_mod.strong_configured()):
        return plain
    try:
        text = ai_mod.chat([
            {"role": "system", "content":
             "把一条统计发现润色成一句温暖的人话（不超过50字）。必须保留具体数字与对比天数，"
             "自然带出「相关不等于因果」的意思（不要生硬说教）。只输出这句话本身。"},
            {"role": "user", "content": plain},
        ], timeout=30.0, slot="fast", attempts=1).strip().strip('"').split("\n")[0]
        return text[:80] or plain
    except Exception:
        return plain


def compute_weekly() -> int:
    """每周一算一次。返回新产生的 insight 条数；样本不足/无心情数据 → 0（安静）。"""
    moods = _daily_mood()
    if len(moods) < 2 * MIN_DAYS:
        return 0  # 心情数据太少，安静不出
    start = date.today() - timedelta(days=DAYS)
    feats = _feature_days(start)
    dismissed = _dismissed_features()

    candidates = []
    all_days = sorted(moods)
    day_index = {d: i for i, d in enumerate(all_days)}
    for fid, days in feats.items():
        if fid in dismissed:
            continue
        for lag in (0, 1):
            with_m, without_m = [], []
            for d, m in moods.items():
                if lag:
                    # 滞后 1 日：特征在"昨天"有 → 看今天心情
                    prev = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
                    has = prev in days
                else:
                    has = d in days
                (with_m if has else without_m).append(m)
            if len(with_m) < MIN_DAYS or len(without_m) < MIN_DAYS:
                continue
            r = _point_biserial(with_m, without_m)
            if abs(r) < MIN_R:
                continue
            candidates.append({
                "feature": fid, "lag": lag, "r": r,
                "evidence": {"days_with": len(with_m), "days_without": len(without_m),
                             "avg_with": round(sum(with_m) / len(with_m), 2),
                             "avg_without": round(sum(without_m) / len(without_m), 2),
                             "lag": lag},
            })
    candidates.sort(key=lambda c: abs(c["r"]), reverse=True)
    top = candidates[:TOP_N]

    # 本周重算：替换仍 active 的（dismissed 已在上游被排除，永不复活）
    now = db.now_iso()
    with db.locked() as c:
        c.execute("DELETE FROM insights WHERE status='active'")
        for cand in top:
            label = _feature_label(cand["feature"])
            text = _render_text(label, cand["evidence"])
            c.execute(
                "INSERT INTO insights(id, kind, feature, text, evidence_json, score, status, created_at) "
                "VALUES(?,?,?,?,?,?, 'active', ?)",
                (db.new_id(), "mood_pattern", cand["feature"], text,
                 json.dumps(cand["evidence"], ensure_ascii=False),
                 round(abs(cand["r"]), 3), now),
            )
        c.commit()
    if top:
        logx.log("规律发现", f"本周算出 {len(top)} 条心情规律，去数据页看看")
    return len(top)


def _insight_dict(r) -> dict:
    try:
        ev = json.loads(r["evidence_json"] or "{}")
    except ValueError:
        ev = {}
    return {"id": r["id"], "kind": r["kind"], "feature": r["feature"],
            "text": r["text"], "evidence": ev, "score": r["score"],
            "status": r["status"], "created_at": r["created_at"],
            "dismissed_at": r["dismissed_at"]}


def list_by_status(status: str = "active") -> list[dict]:
    """active / dismissed / all（主站与 Agent 接口共用）。"""
    if status == "all":
        rows = db.q("SELECT * FROM insights ORDER BY score DESC, created_at DESC")
    else:
        rows = db.q("SELECT * FROM insights WHERE status=? ORDER BY score DESC, created_at DESC",
                    (status,))
    return [_insight_dict(r) for r in rows]


def list_active() -> list[dict]:
    return list_by_status("active")


def dismiss(iid: str) -> dict:
    r = db.q1("SELECT * FROM insights WHERE id=?", (iid,))
    if r is None:
        raise ValueError("not found")
    db.execute("UPDATE insights SET status='dismissed', dismissed_at=? WHERE id=?",
               (db.now_iso(), iid))
    logx.log("规律已忽略", f"「{r['feature']}」以后不再出现")
    return {"ok": True}
