# -*- coding: utf-8 -*-
"""隐性关联发现：没标"工作"的记录，可能和工作/目标/事件暗暗相关。

算法：新记录向量 vs ① is_work=1 的记录 ② event 实体向量 ③ goals 文本向量；
各取 top-3，入围线 = max(0.70, 候选 top10 分布中位数+1σ)；排除同日/相邻日；
已 dismissed 的配对不再出现；提案 pending 后由小模型补一句"为什么相关"。
用户确认后：目标是工作记录/事件/目标 → 该记录 work_related=1（弱标记，供周报参考）。

诊断：discover_for_entry 的每个"跳过"分支都带原因码，nightly_batch 会汇总成一行日志
（旧代码有 6 种静默 return 0，出问题时没有任何痕迹）。
"""
from __future__ import annotations

import hashlib
import json
import statistics
import threading
from collections import Counter
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


# goals 向量缓存：(文本哈希, vec)。目标文本没改就不重复 embed（nightly 批量时每条都问一次）。
# 键用内容 sha256 而非 goals.updated_at——后者是秒级时间戳，老库/手工插入的行可能是空串，
# 会造成"目标改了也永远复用首个向量"或同秒两次修改命中陈旧向量（A4-26）。
_goal_vec_cache: tuple[str, list] | None = None


def _goal_vector(gtext: str) -> list | None:
    global _goal_vec_cache
    key = hashlib.sha256(gtext.encode("utf-8")).hexdigest()
    if _goal_vec_cache is not None and _goal_vec_cache[0] == key:
        return _goal_vec_cache[1]
    vec = ai_mod.embed_text(gtext)
    if vec is not None:
        _goal_vec_cache = (key, vec)
    return vec


# 同一条记录的发现过程互斥：保存钩子（main.py 后台链）与 nightly_batch 会并发跑到一起。
# 之前靠 link_proposals 的 UNIQUE 兜底不产生重复行，但会重复调用 AI 且 created 计数失真（A4-25）。
_inflight_guard = threading.Lock()
_inflight: set[str] = set()


def _skip(reason_out, code: str) -> int:
    """记下跳过原因（调用方传了 reason_out 才有痕迹）并返回 0，便于 `return _skip(...)`。"""
    if reason_out is not None:
        reason_out.append(code)
    return 0


def discover_for_entry(entry_id: str, reason_out: list | None = None) -> int:
    """为一条记录发现隐性关联，写 link_proposals（pending）。返回新建提案数。

    reason_out：可选，传入列表时会收到本次跳过的原因码（如 "未配置嵌入"），
    供 nightly_batch 汇总诊断；不传则行为与旧版一致（返回 int）。
    任何失败静默（不打断保存流程），但原因不再无处可查。
    """
    # 同一条记录已有发现在跑：直接跳过（避免重复 AI 调用与计数失真）
    with _inflight_guard:
        if entry_id in _inflight:
            return _skip(reason_out, "已有发现在进行中")
        _inflight.add(entry_id)
    try:
        return _discover_inner(entry_id, reason_out)
    finally:
        with _inflight_guard:
            _inflight.discard(entry_id)


def _discover_inner(entry_id: str, reason_out: list | None) -> int:
    if db.get_setting("link_discovery", "1") in ("0", "false"):
        return _skip(reason_out, "关联发现开关已关闭")
    if not ai_mod.embed_configured():
        return _skip(reason_out, "未配置嵌入模型")
    row = db.q1("SELECT * FROM entries WHERE id=? AND deleted_at IS NULL", (entry_id,))
    if row is None:
        return _skip(reason_out, "记录不存在或已删除")
    if row["exclude_from_ai"]:
        return _skip(reason_out, "记录已标记不让 AI 分析")
    if row["is_work"] == 1:
        # 设计本意是给"没标工作"的记录找与工作的隐性关联；已标工作的没必要跑
        db.execute("UPDATE entries SET link_checked=1 WHERE id=?", (entry_id,))
        return _skip(reason_out, "记录本身已标为工作")
    vrow = db.q1("SELECT vector_json FROM entry_embeddings WHERE entry_id=?", (entry_id,))
    vec = _load_vec(vrow["vector_json"]) if vrow else None
    if vec is None:
        if not ai_mod.embed_entry(entry_id):
            return _skip(reason_out, "向量生成失败")
        vrow = db.q1("SELECT vector_json FROM entry_embeddings WHERE entry_id=?", (entry_id,))
        vec = _load_vec(vrow["vector_json"]) if vrow else None
        if vec is None:
            return _skip(reason_out, "向量仍为空")

    # occurred_at 为 NULL（老库/外部写入）时切片会抛 TypeError，不该让整轮补算中断
    day = (row["occurred_at"] or "")[:10]
    try:
        day_d = date.fromisoformat(day)
    except (ValueError, TypeError):
        return _skip(reason_out, "记录日期无法识别")

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
            d = date.fromisoformat((r["occurred_at"] or "")[:10])
        except (ValueError, TypeError):
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

    # ③ goals 文本向量（按文本哈希缓存，目标没改就不重复 embed）
    goal = db.q1("SELECT * FROM goals WHERE id=1")
    if goal is not None:
        gtext = "\n".join(p for p in (goal["current_role"], goal["target_role"],
                                      goal["goal_6m"], goal["goal_12m"]) if p)
        if gtext.strip():
            gvec = _goal_vector(gtext)
            if gvec is not None:
                pool.append(("goal", "1", ai_mod.cosine(vec, gvec), gtext[:200]))

    if not pool:
        db.execute("UPDATE entries SET link_checked=1 WHERE id=?", (entry_id,))
        return _skip(reason_out, "没有候选对象")

    pool.sort(key=lambda t: t[2], reverse=True)
    bar = max(SCORE_FLOOR, _median_sigma([s for _, _, s, _ in pool]))
    picked: dict[str, int] = {}
    created = 0
    skipped_existing = 0
    for ttype, tid, score, ttext in pool:
        if score < bar:
            break
        if picked.get(ttype, 0) >= TOP_N:
            continue
        if (ttype, tid) in dismissed:
            continue
        if (ttype, tid) in existing:
            skipped_existing += 1
            continue  # 已有 pending/confirmed 提案：重复跑幂等，不重复计数
        picked[ttype] = picked.get(ttype, 0) + 1
        # UNIQUE(entry_id, target_type, target_id) 兜底并发；上面已查重
        reason = ai_mod.explain_link(_entry_text(row), ttext) or ""
        # 用 rowcount 判断是否真的插入：OR IGNORE 命中唯一约束时不算新增，
        # 否则日志里"找到 N 条"会与实际入库数不符（A4-25）
        with db.locked() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO link_proposals(id, entry_id, target_type, target_id, "
                "score, reason, status, created_at) VALUES(?,?,?,?,?,?, 'pending', ?)",
                (db.new_id(), entry_id, ttype, tid, round(score, 4), reason, db.now_iso()),
            )
            inserted = cur.rowcount > 0
        existing.add((ttype, tid))
        if inserted:
            created += 1
    db.execute("UPDATE entries SET link_checked=1 WHERE id=?", (entry_id,))
    if created:
        title = row["title"] or (row["content"] or "")[:16]
        logx.log("关联发现", f"《{title}》找到 {created} 条可能的关联（入围线 {bar:.2f}），等你过目")
    elif reason_out is not None:
        # 区分"没够线"与"都已有提案"：后者是正常的幂等重跑，不是失败
        reason_out.append("候选都已有提案" if skipped_existing else "候选分数均低于入围线")
    return created


def nightly_batch(limit: int = NIGHT_BATCH) -> int:
    """每晚补算：link_checked=0 的旧记录逐条发现。

    单条记录失败不再中断整轮（旧代码 `break` 会让一条必然抛错的记录把它之后的所有记录
    永久饿死——按 occurred_at DESC 取前 N 条时它一直占着队首），改为跳过并汇总原因（A4-24）。
    """
    if db.get_setting("link_discovery", "1") in ("0", "false"):
        return 0
    rows = db.q(
        "SELECT id FROM entries WHERE deleted_at IS NULL AND exclude_from_ai=0 "
        "AND link_checked=0 AND trim(content) != '' ORDER BY occurred_at DESC LIMIT ?",
        (limit,),
    )
    done = 0
    reasons: Counter = Counter()
    failed: list[str] = []
    for r in rows:
        skip_reasons: list[str] = []
        try:
            discover_for_entry(r["id"], reason_out=skip_reasons)
            done += 1
        except Exception as e:
            failed.append(r["id"])
            reasons[f"异常 {type(e).__name__}"] += 1
            logx.log("关联发现单条失败", f"记录 {r['id']} 本轮跳过：{type(e).__name__} {e}"[:200])
        for code in skip_reasons:
            reasons[code] += 1
    # 跳过/失败的分项汇总：让"今晚没补算/为什么没提案"可见，而不是安静地什么都不做（A4-24）
    if done:
        logx.log("关联发现补算", f"今晚补了 {done} 条记录")
    elif rows:
        logx.log("关联发现补算", f"今晚 {len(rows)} 条都没跑通")
    if reasons:
        detail = "、".join(f"{k} {v} 条" for k, v in reasons.most_common(5))
        logx.log("关联发现跳过原因", detail)
    if failed:
        logx.log("关联发现补算跳过", f"共 {len(failed)} 条记录因异常被跳过：{('、'.join(failed[:5]))}"
                                  f"{' 等' if len(failed) > 5 else ''}")
    return done


def _titles_for(rows, c=None) -> tuple[dict[str, str], dict[str, str]]:
    """批量取提案目标的标题，避免逐行查询（A4-27）。

    返回 (entry 标题表, entity 名表)；一次 IN 查询取全部，缺行由调用方给占位文案。
    """
    entry_ids = [r["target_id"] for r in rows if r["target_type"] == "entry"]
    ent_ids = [r["target_id"] for r in rows if r["target_type"] == "entity"]
    entry_titles: dict[str, str] = {}
    ent_names: dict[str, str] = {}
    if entry_ids:
        marks = ",".join("?" for _ in entry_ids)
        sql = f"SELECT id, title, content FROM entries WHERE id IN ({marks})"
        found = c.execute(sql, tuple(entry_ids)).fetchall() if c is not None else db.q(sql, tuple(entry_ids))
        for t in found:
            entry_titles[t["id"]] = t["title"] or (t["content"] or "")[:20]
    if ent_ids:
        marks = ",".join("?" for _ in ent_ids)
        sql = f"SELECT id, name FROM entities WHERE id IN ({marks})"
        found = c.execute(sql, tuple(ent_ids)).fetchall() if c is not None else db.q(sql, tuple(ent_ids))
        for t in found:
            ent_names[t["id"]] = t["name"]
    return entry_titles, ent_names


def link_dict(r, entry_titles: dict | None = None, ent_names: dict | None = None) -> dict:
    """提案 + 目标标题（entry→记录标题，entity→实体名，goal→'成长目标'）。

    传入批量取好的 entry_titles/ent_names 时不发查询（供列表页复用，A4-27）；
    不传则按需查询，保持单独调用的可用性。
    """
    target_title = ""
    if r["target_type"] == "entry":
        if entry_titles is not None:
            target_title = entry_titles.get(r["target_id"], "（记录已删除）")
        else:
            t = db.q1("SELECT title, content FROM entries WHERE id=?", (r["target_id"],))
            target_title = (t["title"] or (t["content"] or "")[:20]) if t else "（记录已删除）"
    elif r["target_type"] == "entity":
        if ent_names is not None:
            target_title = ent_names.get(r["target_id"], "（档案已删除）")
        else:
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
    # 一次批量取目标标题（原来 1 + N 次查询，A4-27）
    entry_titles, ent_names = _titles_for(rows)
    return [link_dict(r, entry_titles, ent_names) for r in rows]


def _is_work_ward(c, target_type: str, target_id: str) -> bool:
    """目标算不算"工作向"：event/goal 实体，或 is_work=1 的记录。

    c 为事务内连接时直接用同一条快照读（避免跨事务读到中间态）。
    """
    if target_type in ("entity", "goal"):
        return True
    if target_type == "entry":
        sql = "SELECT is_work FROM entries WHERE id=?"
        if c is not None:
            t = c.execute(sql, (target_id,)).fetchone()
        else:
            t = db.q1(sql, (target_id,))
        return bool(t and t["is_work"] == 1)
    return False


def _target_is_work_ward(target_type: str, target_id: str) -> bool:
    """保留旧签名（外部可引用）。"""
    return _is_work_ward(None, target_type, target_id)


# 该记录是否还有"其他已确认的工作向关联"：一次 JOIN 判定，替代逐行 _target_is_work_ward（A4-23）
_WORK_WARD_OTHERS_SQL = (
    "SELECT 1 FROM link_proposals p LEFT JOIN entries e ON e.id = p.target_id "
    "WHERE p.entry_id=? AND p.status='confirmed' AND p.id != ? "
    "AND (p.target_type IN ('entity','goal') OR (p.target_type='entry' AND e.is_work=1)) LIMIT 1"
)


def confirm_link(lid: str) -> dict:
    """用户说"对"：pending → confirmed；目标是工作记录/事件/目标 → 该记录 work_related=1。

    两次写入放在同一个 db.locked() 事务里：旧代码分两次 commit，故障窗口会留下
    "提案已 confirmed 但 work_related 仍为 0"的状态，而重新确认被 bad state 拒绝，
    用户无法自救（A4-22）。locked() 现在成功即自动提交。
    """
    with db.locked() as c:
        r = c.execute("SELECT * FROM link_proposals WHERE id=?", (lid,)).fetchone()
        if r is None:
            raise ValueError("not found")
        if r["status"] != "pending":
            raise ValueError("bad state")  # 已确认/已忽略的不能再确认
        now = db.now_iso()
        c.execute("UPDATE link_proposals SET status='confirmed', decided_at=? WHERE id=?",
                  (now, lid))
        mark = _is_work_ward(c, r["target_type"], r["target_id"])
        if mark:
            c.execute("UPDATE entries SET work_related=1 WHERE id=?", (r["entry_id"],))
    if mark:
        logx.log("关联已确认", "这条记录已标记为「与工作相关」，周报会参考")
    return {"ok": True, "work_related": mark}


def dismiss_link(lid: str) -> dict:
    """用户说"不像"：pending/confirmed → dismissed。
    若它之前把记录标成了 work_related：该记录没有其他已确认的工作向关联时才回滚标记。

    状态改写、"还有没有其他工作向关联"的判定、work_related 回滚都在同一个事务里完成：
    旧代码三次独立写入/查询，故障窗口会留下"已 dismissed 但 work_related 仍为 1"的反向
    不一致，且 others 的判定可能与提交不在同一快照（A4-23）。
    """
    with db.locked() as c:
        r = c.execute("SELECT * FROM link_proposals WHERE id=?", (lid,)).fetchone()
        if r is None:
            raise ValueError("not found")
        if r["status"] == "dismissed":
            raise ValueError("bad state")
        was_confirmed_work = (r["status"] == "confirmed"
                              and _is_work_ward(c, r["target_type"], r["target_id"]))
        c.execute("UPDATE link_proposals SET status='dismissed', decided_at=? WHERE id=?",
                  (db.now_iso(), lid))
        rollback = False
        if was_confirmed_work:
            other = c.execute(_WORK_WARD_OTHERS_SQL, (r["entry_id"], lid)).fetchone()
            if other is None:
                c.execute("UPDATE entries SET work_related=0 WHERE id=?", (r["entry_id"],))
                rollback = True
    if rollback:
        logx.log("关联已撤销", "这条记录的「与工作相关」标记已摘掉")
    return {"ok": True}
