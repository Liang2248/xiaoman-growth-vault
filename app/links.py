# -*- coding: utf-8 -*-
"""隐性关联发现：没标"工作"的记录，可能和工作/目标/事件暗暗相关。

算法：新记录向量 vs ① is_work=1 的记录 ② event 实体向量 ③ goals 文本向量；
各取 top-3，入围线 = max(0.70, 候选 top10 分布中位数+1σ)；排除同日/相邻日；
已 dismissed 的配对不再出现；提案 pending 后由小模型补一句"为什么相关"。
用户确认后：目标是工作记录/事件/目标 → 该记录 work_related=1（弱标记，供周报参考）。
"""
from __future__ import annotations

import json
import statistics
from datetime import date

from . import ai as ai_mod, db, logx

SCORE_FLOOR = 0.70     # 绝对下限
TOP_N = 3              # 每类最多提 3 条
NIGHT_BATCH = 10       # 每晚补算上限


def _entry_text(r) -> str:
    return "\n".join(p for p in (r["title"], r["summary"], (r["content"] or "")[:300]) if p)


def _load_vec(raw) -> list | None:
    try:
        v = json.loads(raw or "")
        return v if isinstance(v, list) and v else None
    except ValueError:
        return None


def _median_sigma(scores: list[float]) -> float:
    """top10 分布的中位数 + 1σ（相对阈值，抗"整体都高/都低"的噪声）。
    样本 <3 时退化返回 0.0 → 外层 max(0.70, …) 生效，即退回绝对下限——候选太少时
    分布统计没有意义，这是刻意的边界行为。"""
    top = scores[:10]
    if len(top) < 3:
        return 0.0
    med = statistics.median(top)
    sigma = statistics.pstdev(top)
    return med + sigma


# goals 向量缓存：(updated_at, vec)。目标没改就不重复 embed（nightly 批量时每条都问一次）
_goal_vec_cache: tuple[str, list] | None = None


def _goal_vector(gtext: str, updated_at: str) -> list | None:
    global _goal_vec_cache
    if _goal_vec_cache is not None and _goal_vec_cache[0] == updated_at:
        return _goal_vec_cache[1]
    vec = ai_mod.embed_text(gtext)
    if vec is not None:
        _goal_vec_cache = (updated_at, vec)
    return vec


def discover_for_entry(entry_id: str) -> int:
    """为一条记录发现隐性关联，写 link_proposals（pending）。返回新建提案数。任何失败静默。"""
    if db.get_setting("link_discovery", "1") in ("0", "false"):
        return 0
    if not ai_mod.embed_configured():
        return 0
    row = db.q1("SELECT * FROM entries WHERE id=? AND deleted_at IS NULL", (entry_id,))
    if row is None or row["exclude_from_ai"]:
        return 0
    if row["is_work"] == 1:
        # 设计本意是给"没标工作"的记录找与工作的隐性关联；已标工作的没必要跑
        db.execute("UPDATE entries SET link_checked=1 WHERE id=?", (entry_id,))
        return 0
    vrow = db.q1("SELECT vector_json FROM entry_embeddings WHERE entry_id=?", (entry_id,))
    vec = _load_vec(vrow["vector_json"]) if vrow else None
    if vec is None:
        if not ai_mod.embed_entry(entry_id):
            return 0
        vrow = db.q1("SELECT vector_json FROM entry_embeddings WHERE entry_id=?", (entry_id,))
        vec = _load_vec(vrow["vector_json"]) if vrow else None
        if vec is None:
            return 0

    day = row["occurred_at"][:10]
    try:
        day_d = date.fromisoformat(day)
    except ValueError:
        return 0

    dismissed = {
        (r["target_type"], r["target_id"])
        for r in db.q("SELECT target_type, target_id FROM link_proposals "
                      "WHERE entry_id=? AND status='dismissed'", (entry_id,))
    }
    existing = {
        (r["target_type"], r["target_id"])
        for r in db.q("SELECT target_type, target_id FROM link_proposals "
                      "WHERE entry_id=? AND status IN ('pending','confirmed')", (entry_id,))
    }

    # 候选池：(target_type, target_id, score, 文本)
    pool: list[tuple[str, str, float, str]] = []

    # ① is_work=1 的记录（排除同日/相邻日/自己）
    for r in db.q(
        "SELECT e.id, e.occurred_at, e.title, e.summary, e.content, v.vector_json "
        "FROM entries e JOIN entry_embeddings v ON v.entry_id=e.id "
        "WHERE e.deleted_at IS NULL AND e.exclude_from_ai=0 AND e.is_work=1 AND e.id != ?",
        (entry_id,),
    ):
        try:
            d = date.fromisoformat(r["occurred_at"][:10])
        except ValueError:
            continue
        if abs((d - day_d).days) <= 1:
            continue  # 同日/相邻日不算"隐性"，本来就是同一摊子事
        other = _load_vec(r["vector_json"])
        if other is None:
            continue
        pool.append(("entry", r["id"], ai_mod.cosine(vec, other), _entry_text(r)))

    # ② event 实体向量
    for r in db.q(
        "SELECT id, name, profile, vector_json FROM entities "
        "WHERE type='event' AND status IN ('active','confirmed') "
        "AND vector_json IS NOT NULL AND vector_json != ''"
    ):
        other = _load_vec(r["vector_json"])
        if other is None:
            continue
        pool.append(("entity", r["id"], ai_mod.cosine(vec, other),
                     f"{r['name']}：{(r['profile'] or '')[:120]}"))

    # ③ goals 文本向量（按 goals.updated_at 缓存，目标没改就不重复 embed）
    goal = db.q1("SELECT * FROM goals WHERE id=1")
    if goal is not None:
        gtext = "\n".join(p for p in (goal["current_role"], goal["target_role"],
                                      goal["goal_6m"], goal["goal_12m"]) if p)
        if gtext.strip():
            gvec = _goal_vector(gtext, goal["updated_at"] or "")
            if gvec is not None:
                pool.append(("goal", "1", ai_mod.cosine(vec, gvec), gtext[:200]))

    if not pool:
        db.execute("UPDATE entries SET link_checked=1 WHERE id=?", (entry_id,))
        return 0

    pool.sort(key=lambda t: t[2], reverse=True)
    bar = max(SCORE_FLOOR, _median_sigma([s for _, _, s, _ in pool]))
    picked: dict[str, int] = {}
    created = 0
    for ttype, tid, score, ttext in pool:
        if score < bar:
            break
        if picked.get(ttype, 0) >= TOP_N:
            continue
        if (ttype, tid) in dismissed:
            continue
        if (ttype, tid) in existing:
            continue  # 已有 pending/confirmed 提案：重复跑幂等，不重复计数
        picked[ttype] = picked.get(ttype, 0) + 1
        # UNIQUE(entry_id, target_type, target_id) 兜底并发；上面已查重
        reason = ai_mod.explain_link(_entry_text(row), ttext) or ""
        db.execute(
            "INSERT OR IGNORE INTO link_proposals(id, entry_id, target_type, target_id, "
            "score, reason, status, created_at) VALUES(?,?,?,?,?,?, 'pending', ?)",
            (db.new_id(), entry_id, ttype, tid, round(score, 4), reason, db.now_iso()),
        )
        existing.add((ttype, tid))
        created += 1
    db.execute("UPDATE entries SET link_checked=1 WHERE id=?", (entry_id,))
    if created:
        title = row["title"] or (row["content"] or "")[:16]
        logx.log("关联发现", f"《{title}》找到 {created} 条可能的关联（入围线 {bar:.2f}），等你过目")
    return created


def nightly_batch(limit: int = NIGHT_BATCH) -> int:
    """每晚补算：link_checked=0 的旧记录逐条发现（静默）。"""
    if db.get_setting("link_discovery", "1") in ("0", "false"):
        return 0
    rows = db.q(
        "SELECT id FROM entries WHERE deleted_at IS NULL AND exclude_from_ai=0 "
        "AND link_checked=0 AND trim(content) != '' ORDER BY occurred_at DESC LIMIT ?",
        (limit,),
    )
    done = 0
    for r in rows:
        try:
            discover_for_entry(r["id"])
            done += 1
        except Exception:
            break  # AI/嵌入挂了本轮停，明晚再试
    if done:
        logx.log("关联发现补算", f"今晚补了 {done} 条记录")
    return done


def link_dict(r) -> dict:
    """提案 + 目标标题（entry→记录标题，entity→实体名，goal→'成长目标'）。"""
    target_title = ""
    if r["target_type"] == "entry":
        t = db.q1("SELECT title, content FROM entries WHERE id=?", (r["target_id"],))
        target_title = (t["title"] or (t["content"] or "")[:20]) if t else "（记录已删除）"
    elif r["target_type"] == "entity":
        t = db.q1("SELECT name FROM entities WHERE id=?", (r["target_id"],))
        target_title = t["name"] if t else "（档案已删除）"
    elif r["target_type"] == "goal":
        target_title = "成长目标"
    return {
        "id": r["id"], "entry_id": r["entry_id"],
        "target_type": r["target_type"], "target_id": r["target_id"],
        "target_title": target_title,
        "score": r["score"], "reason": r["reason"], "status": r["status"],
        "created_at": r["created_at"],
    }


def links_for_entry(entry_id: str) -> list[dict]:
    rows = db.q(
        "SELECT * FROM link_proposals WHERE entry_id=? AND status IN ('pending','confirmed') "
        "ORDER BY score DESC",
        (entry_id,),
    )
    return [link_dict(r) for r in rows]


def _target_is_work_ward(target_type: str, target_id: str) -> bool:
    """目标算不算"工作向"：event/goal 实体，或 is_work=1 的记录。"""
    if target_type in ("entity", "goal"):
        return True
    if target_type == "entry":
        t = db.q1("SELECT is_work FROM entries WHERE id=?", (target_id,))
        return bool(t and t["is_work"] == 1)
    return False


def confirm_link(lid: str) -> dict:
    """用户说"对"：pending → confirmed；目标是工作记录/事件/目标 → 该记录 work_related=1。"""
    r = db.q1("SELECT * FROM link_proposals WHERE id=?", (lid,))
    if r is None:
        raise ValueError("not found")
    if r["status"] != "pending":
        raise ValueError("bad state")  # 已确认/已忽略的不能再确认
    now = db.now_iso()
    db.execute("UPDATE link_proposals SET status='confirmed', decided_at=? WHERE id=?",
               (now, lid))
    mark = _target_is_work_ward(r["target_type"], r["target_id"])
    if mark:
        db.execute("UPDATE entries SET work_related=1 WHERE id=?", (r["entry_id"],))
        logx.log("关联已确认", "这条记录已标记为「与工作相关」，周报会参考")
    return {"ok": True, "work_related": mark}


def dismiss_link(lid: str) -> dict:
    """用户说"不像"：pending/confirmed → dismissed。
    若它之前把记录标成了 work_related：该记录没有其他已确认的工作向关联时才回滚标记。"""
    r = db.q1("SELECT * FROM link_proposals WHERE id=?", (lid,))
    if r is None:
        raise ValueError("not found")
    if r["status"] == "dismissed":
        raise ValueError("bad state")
    was_confirmed_work = r["status"] == "confirmed" and _target_is_work_ward(r["target_type"], r["target_id"])
    db.execute("UPDATE link_proposals SET status='dismissed', decided_at=? WHERE id=?",
               (db.now_iso(), lid))
    if was_confirmed_work:
        others = db.q(
            "SELECT target_type, target_id FROM link_proposals "
            "WHERE entry_id=? AND status='confirmed' AND id != ?",
            (r["entry_id"], lid),
        )
        if not any(_target_is_work_ward(o["target_type"], o["target_id"]) for o in others):
            db.execute("UPDATE entries SET work_related=0 WHERE id=?", (r["entry_id"],))
            logx.log("关联已撤销", "这条记录的「与工作相关」标记已摘掉")
    return {"ok": True}
