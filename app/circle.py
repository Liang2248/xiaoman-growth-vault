# -*- coding: utf-8 -*-
"""小满的圈子：人物/地点/事件档案与关系网。

规则（用户定下）：
- 提及按 certainty 分级：explicit（原文明写）→ 直接 confirmed 不问；inferred → 综合达
  confirm_threshold（事件 0.7 / 人物地点 0.85）自动 confirmed；0.5–阈值 → active（待确认）；
  <0.5 → draft（搁置）；ambiguous 的人物/地点才提问
- 一切自动动作（确认/合并/事实更新）写 circle_auto_log，可一键撤销
- 用户拒绝过的合并配对进 rejected_pairs 黑名单，永不再提
- 用户可确认/否认/改写/合并；user_note 永远是最高事实
- exclude_from_ai 的记录绝不参与
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta

from . import ai as ai_mod, db, logx

SYNTH_INTERVAL = timedelta(hours=1)
TYPE_LABEL = {"person": "人物", "place": "地点", "event": "事件"}

_backfill_lock = threading.Lock()

# certainty → 置信度地板：explicit 原文明写直通 0.95；inferred 待定；ambiguous 保持低位
CERTAINTY_FLOOR = {"explicit": 0.95, "inferred": 0.6, "ambiguous": 0.4}
CERTAINTY_RANK = {"ambiguous": 0, "inferred": 1, "explicit": 2}


def enabled() -> bool:
    """圈子总开关（默认开）。关闭时：不提取、不综合、不复查、不注入、不回填。"""
    return db.get_setting("circle_enabled", "true") != "false"


def auto_confirm_on() -> bool:
    """自动确认开关（默认开）。关掉后：达标也只停在 active，不写自动日志。"""
    return db.get_setting("circle_auto_confirm", "1") not in ("0", "false")


def confirm_threshold(entity_type: str) -> float:
    """自动确认门槛：事件是明确发生的事，0.7；人物/地点 0.85。"""
    return 0.7 if entity_type == "event" else 0.85


ACTIVE_FLOOR = 0.5  # 0.5–confirm_threshold → active（待确认）；<0.5 → draft


def _auto_log(action: str, detail: dict) -> str:
    """写一条可撤销自动动作日志（detail 含回滚快照）。返回日志 id。"""
    aid = db.new_id()
    db.execute(
        "INSERT INTO circle_auto_log(id, action, detail_json, created_at) VALUES(?,?,?,?)",
        (aid, action, json.dumps(detail, ensure_ascii=False), db.now_iso()),
    )
    return aid


def norm_name(name: str) -> str:
    """规范化：去全部空白、转小写。匹配合并的统一入口。"""
    return re.sub(r"\s+", "", name or "").lower()


def _entity_dict(r) -> dict:
    try:
        aliases = json.loads(r["aliases_json"] or "[]")
    except ValueError:
        aliases = []
    return {
        "id": r["id"], "type": r["type"], "name": r["name"], "aliases": aliases,
        "profile": r["profile"], "relation_to_user": r["relation_to_user"],
        "user_note": r["user_note"], "status": r["status"],
        "confidence": r["confidence"], "mention_count": r["mention_count"],
        "needs_review": bool(r["needs_review"]), "conflict_note": r["conflict_note"],
        "first_seen": r["first_seen"], "last_seen": r["last_seen"],
        "created_at": r["created_at"], "updated_at": r["updated_at"],
    }


def get_entity(eid: str):
    return db.q1("SELECT * FROM entities WHERE id=?", (eid,))


# ---------------- 提取与建档 ----------------

def _find_by_name(name: str, mtype: str):
    """按规范化名或别名命中已有实体（含 rejected，由调用方决定跳不跳过）。"""
    target = norm_name(name)
    if not target:
        return None
    for r in db.q("SELECT * FROM entities WHERE type=?", (mtype,)):
        if norm_name(r["name"]) == target:
            return r
        try:
            aliases = json.loads(r["aliases_json"] or "[]")
        except ValueError:
            aliases = []
        if any(norm_name(a) == target for a in aliases):
            return r
    return None


def _find_enabled_by_name(name: str):
    """按规范化名/别名命中'启用中'（active/confirmed）的实体（消歧归并用，不限类型）。"""
    target = norm_name(name)
    if not target:
        return None
    for r in db.q("SELECT * FROM entities WHERE status IN ('active','confirmed')"):
        if norm_name(r["name"]) == target:
            return r
        try:
            aliases = json.loads(r["aliases_json"] or "[]")
        except ValueError:
            aliases = []
        if any(norm_name(a) == target for a in aliases):
            return r
    return None


def _merge_alias(row, alias: str) -> bool:
    """把别名并入实体（规范化去重、不含本名）。返回是否新增。"""
    alias = (alias or "").strip()[:30]
    if not alias or norm_name(alias) == norm_name(row["name"]):
        return False
    try:
        aliases = json.loads(row["aliases_json"] or "[]")
    except ValueError:
        aliases = []
    if any(norm_name(a) == norm_name(alias) for a in aliases):
        return False
    aliases.append(alias)
    db.execute("UPDATE entities SET aliases_json=?, updated_at=? WHERE id=?",
               (json.dumps(aliases, ensure_ascii=False), db.now_iso(), row["id"]))
    return True


VECTOR_MERGE_THRESHOLD = 0.88    # ≥：静默归并
VECTOR_EVENT_MERGE = 0.85        # 事件线索制：跨天同名/高相似事件 ≥0.85 归入同一档案
VECTOR_JUDGE_THRESHOLD = 0.75    # 0.75–0.88 灰区：交小模型判
VECTOR_PROPOSE_CONF = 0.6        # 小模型 same 但置信 0.6–0.8：发合并提案
JUDGE_AUTO_CONF = 0.8            # 小模型 same 且 ≥0.8：自动合并（可撤销）


def _best_vector_match(vec: list, mtype: str):
    """同类型、非 rejected、已有向量的实体里最像者。返回 (row, score) 或 (None, 0.0)。"""
    best, best_score = None, 0.0
    for r in db.q(
        "SELECT * FROM entities WHERE type=? AND status != 'rejected' "
        "AND vector_json IS NOT NULL AND vector_json != ''",
        (mtype,),
    ):
        try:
            other = json.loads(r["vector_json"])
        except ValueError:
            continue
        score = ai_mod.cosine(vec, other)
        if score > best_score:
            best, best_score = r, score
    return best, best_score


def _vector_disambiguate(name: str, vec: list, mtype: str) -> tuple:
    """三级消歧。返回 (要挂靠的实体 row 或 None, 需发合并提案的目标实体 row 或 None)。
    ≥0.88（事件 ≥0.85）静默归并；0.75–0.88 小模型判：same≥0.8 挂靠+可撤销日志，
    same 0.6–0.8 各自新建但发合并提案，其余各自独立。"""
    best, score = _best_vector_match(vec, mtype)
    if best is None or score < VECTOR_JUDGE_THRESHOLD:
        return None, None
    # 用户撤销过"把这个称呼并进该实体"：永不再自动归并/挂靠（C2）
    if is_rejected_alias(best["id"], name):
        return None, None
    silent_bar = VECTOR_EVENT_MERGE if mtype == "event" else VECTOR_MERGE_THRESHOLD
    if score >= silent_bar:
        if _merge_alias(best, name):
            logx.log("圈子：归并称呼", f"把'{name}'认作《{best['name']}》（相似度 {score:.2f}）")
        return best, None
    # 灰区 0.75–0.88：小模型判一判
    snippets = [m["snippet"] for m in db.q(
        "SELECT snippet FROM entity_mentions WHERE entity_id=? LIMIT 3", (best["id"],))]
    try:
        verdict = ai_mod.judge_same_entity(
            {"name": name, "aliases": [], "profile": "", "relation_to_user": "",
             "snippets": []},
            _entity_side(best, snippets))
    except Exception:
        return None, None  # AI 挂了：保守新建，合并提案流程稍后再兜底
    if not verdict["same"] or verdict["confidence"] < VECTOR_PROPOSE_CONF:
        return None, None
    if verdict["confidence"] >= JUDGE_AUTO_CONF and auto_confirm_on():
        if _merge_alias(best, name):
            _auto_log("merge", {
                "kind": "alias_attach", "entity_id": best["id"], "alias": name,
                "from_name": name, "into_name": best["name"], "into_id": best["id"],
                "score": round(score, 3), "reason": verdict["reason"] or "很像"})
            logx.log("圈子：小满自己合并了称呼",
                     f"把'{name}'并进《{best['name']}》（{verdict['reason']}），"
                     f"不对可在圈子页撤销")
            _push_app_notice(f"小满把《{name}》和《{best['name']}》认成了同一个，去圈子看看？")
        return best, None
    # same 但置信 0.6–0.8：新建独立档案，由调用方补一条合并提案
    return None, best


def _push_app_notice(text: str) -> None:
    """一次性轻提示（入队，多条共存；前端启动/切页时取走全部）。"""
    db.push_notice(text)


# ---------------- 合并黑名单（用户拒绝过的配对/称呼，永不再提） ----------------

def _pair_key(a_id: str, b_id: str) -> tuple[str, str]:
    return (a_id, b_id) if a_id < b_id else (b_id, a_id)


def add_rejected_pair(a_id: str, b_id: str) -> None:
    a, b = _pair_key(a_id, b_id)
    db.execute("INSERT OR IGNORE INTO rejected_pairs(a_id, b_id, created_at) VALUES(?,?,?)",
               (a, b, db.now_iso()))


def is_rejected_pair(a_id: str, b_id: str) -> bool:
    a, b = _pair_key(a_id, b_id)
    return db.q1("SELECT 1 AS x FROM rejected_pairs WHERE a_id=? AND b_id=?", (a, b)) is not None


# 名字维度的否决：b_id 存 "name:<规范化名>"——用户撤销过"把称呼 X 并进实体 E"后，
# 向量归并/挂靠路径先查它，永不再自动并回（C2）。
def add_rejected_alias(entity_id: str, name: str) -> None:
    db.execute("INSERT OR IGNORE INTO rejected_pairs(a_id, b_id, created_at) VALUES(?,?,?)",
               (entity_id, "name:" + norm_name(name), db.now_iso()))


def is_rejected_alias(entity_id: str, name: str) -> bool:
    return db.q1("SELECT 1 AS x FROM rejected_pairs WHERE a_id=? AND b_id=?",
                 (entity_id, "name:" + norm_name(name))) is not None


def upsert_mention(name: str, mtype: str, snippet: str, entry_id: str, entry_day: str,
                   known_as: str | None = None, certainty: str = "inferred") -> str | None:
    """命中则补提及，否则新建 draft。rejected 跳过。返回实体 id 或 None。

    known_as：消歧结果——能命中启用中的实体时，提及挂到它名下并把原称呼并入别名。
    certainty：explicit（原文明写）置信度直通 0.95，且自动确认开关开着时直接 confirmed
    （写 circle_auto_log 可撤销）；inferred/ambiguous 只抬置信度地板，等综合裁定。
    新建前先按向量语义查重（三级制见 _vector_disambiguate），只在新建候选时 embed。
    """
    if not entry_day and entry_id:
        # entry_day 不能为空串：空串会把 first_seen/last_seen 抹成 ''（C3），按 entry 回查真实日期
        _e = db.q1("SELECT occurred_at FROM entries WHERE id=?", (entry_id,))
        if _e is not None:
            entry_day = (_e["occurred_at"] or "")[:10]
    row = None
    if known_as:
        target = _find_enabled_by_name(known_as)
        # 类型一致才自动归并；跨类型的"归属"宁可新建，等复查或用户裁定
        if target is not None and target["type"] == mtype:
            if _merge_alias(target, name):
                logx.log("圈子：归并称呼", f"把'{name}'认作《{target['name']}》")
            row = target
    vec = None
    propose_with = None
    if row is None:
        row = _find_by_name(name, mtype)
    if row is None and ai_mod.embed_configured():
        vec = ai_mod.embed_text(name)
        if vec is not None:
            matched, propose_with = _vector_disambiguate(name, vec, mtype)
            row = matched
    now = db.now_iso()
    if row is not None:
        if row["status"] == "rejected":
            return None
        eid = row["id"]
    else:
        eid = db.new_id()
        db.execute(
            "INSERT INTO entities(id, type, name, status, confidence, first_seen, last_seen, "
            "vector_json, created_at, updated_at) VALUES(?,?,?,'draft',0.4,?,?,?,?,?)",
            (eid, mtype, name.strip()[:30], entry_day, entry_day,
             json.dumps(vec) if vec is not None else None, now, now),
        )
        # 灰区判定"可能是同一个但拿不准"：各自建档，同时发合并提案让用户裁定。
        # 名字维度的用户否决已在 _vector_disambiguate 里查过；这里只需查提案去重。
        if propose_with is not None and not _proposal_exists(eid, propose_with["id"]):
            db.execute(
                "INSERT INTO merge_proposals(id, from_id, into_id, reason, created_at) "
                "VALUES(?,?,?,?,?)",
                (db.new_id(), eid, propose_with["id"],
                 "疑似同一" + TYPE_LABEL.get(mtype, "对象"), now),
            )
            logx.log("圈子：发现疑似重复档案",
                     f"《{name}》和《{propose_with['name']}》")
    dup = db.q1(
        "SELECT id FROM entity_mentions WHERE entity_id=? AND entry_id=?",
        (eid, entry_id),
    )
    if dup is None:
        db.execute(
            "INSERT INTO entity_mentions(id, entity_id, entry_id, snippet, created_at) "
            "VALUES(?,?,?,?,?)",
            (db.new_id(), eid, entry_id, snippet, now),
        )
    count = db.q1("SELECT COUNT(*) AS n FROM entity_mentions WHERE entity_id=?", (eid,))["n"]
    db.execute(
        "UPDATE entities SET mention_count=?, last_seen=?, first_seen=MIN(COALESCE(first_seen, ?), ?), "
        "updated_at=? WHERE id=?",
        (count, entry_day, entry_day, entry_day, now, eid),
    )
    # certainty 地板与 explicit 直通
    floor = CERTAINTY_FLOOR.get(certainty, 0.4)
    ent = db.q1("SELECT * FROM entities WHERE id=?", (eid,))
    pre_conf = ent["confidence"] if ent is not None else None  # 快照要抬地板之前的值
    if ent is not None and floor > (ent["confidence"] or 0):
        db.execute("UPDATE entities SET confidence=? WHERE id=?", (floor, eid))
        ent = db.q1("SELECT * FROM entities WHERE id=?", (eid,))
    if (certainty == "explicit" and ent is not None
            and ent["status"] in ("draft", "active") and auto_confirm_on()
            and not ent["auto_confirm_blocked"]):
        _auto_log("confirm", {"entity_id": eid, "old_status": ent["status"],
                              "old_confidence": pre_conf,
                              "confidence": floor, "mention_count": ent["mention_count"],
                              "basis": "记录原文明写"})
        db.execute("UPDATE entities SET status='confirmed', updated_at=? WHERE id=?", (now, eid))
        logx.log(f"圈子：小满自己确认了《{ent['name']}》",
                 f"依据 {ent['mention_count']} 条记录（原文明写），确信度 {floor:.1f}")
    return eid


def run_extraction_for_entry(entry_id: str) -> int:
    """对单条记录跑提及提取（fast）。返回新增提及数。exclude_from_ai 绝不参与。"""
    if not enabled():
        return 0
    row = db.q1("SELECT * FROM entries WHERE id=? AND deleted_at IS NULL", (entry_id,))
    if row is None or row["exclude_from_ai"]:
        return 0
    if not (row["content"] or "").strip():
        return 0
    mentions = ai_mod.extract_mentions(row["title"], row["content"])
    n = 0
    for m in mentions:
        known_as = m.get("known_as")
        certainty = m.get("certainty") or "inferred"
        # 只有 ambiguous 的人物/地点关系才提问；explicit/inferred 与事件一律不问
        if (certainty == "ambiguous" or m.get("unsure")) and known_as:
            if m["type"] in ("person", "place"):
                maybe_create_question(m, known_as, entry_id)
            known_as = None
        if upsert_mention(m["name"], m["type"], m["snippet"], entry_id,
                          row["occurred_at"][:10], known_as=known_as, certainty=certainty):
            n += 1
    return n


# ---------------- 主动提问 ----------------

def maybe_create_question(mention: dict, known_as: str, entry_id: str) -> str | None:
    """unsure 消歧：先拿上下文自查，能自己搞明白就不打扰用户；真拿不准才建提问。"""
    if not enabled():
        return None
    name = mention["name"]
    # 查重覆盖 pending/answered/dismissed：问过（含答「都不是」、被忽略）的称呼不再反复问（I7）
    for r in db.q("SELECT id, related_json FROM circle_questions "
                  "WHERE status IN ('pending','answered','dismissed')"):
        try:
            if json.loads(r["related_json"] or "{}").get("mention_name") == name:
                return None
        except ValueError:
            continue
    # 候选：known_as 命中的启用实体 + 名字沾边的其他实体
    candidates = []
    hit = _find_enabled_by_name(known_as)
    if hit is not None:
        candidates.append(hit)
    target_norm = norm_name(name)
    for r in db.q("SELECT * FROM entities WHERE status IN ('active','confirmed','draft')"):
        if len(candidates) >= 3:
            break
        if any(r["id"] == c["id"] for c in candidates):
            continue
        rn = norm_name(r["name"])
        if len(target_norm) >= 2 and (target_norm in rn or rn in target_norm):
            candidates.append(r)
    options = [c["name"] for c in candidates] + ["都不是"]

    # 先自查：把本条记录 + 全文检索到的相关记录 + 候选档案交给小模型判断
    ai_suggested = None
    if candidates:
        try:
            ctx_parts = []
            ent = db.q1("SELECT title, content, occurred_at FROM entries WHERE id=?", (entry_id,))
            entry_day = ((ent["occurred_at"] or "")[:10]) if ent is not None else ""
            if ent is not None:
                ctx_parts.append(f"本条记录《{ent['title'] or '（无标题）'}》：\n{(ent['content'] or '')[:600]}")
            for hit_row in db.search_rowids(name, limit=3):
                rel = db.q1(
                    "SELECT title, content FROM entries WHERE deleted_at IS NULL AND rowid=? AND id != ?",
                    (hit_row, entry_id))
                if rel is not None:
                    ctx_parts.append(f"相关记录《{rel['title'] or '（无标题）'}》：\n{(rel['content'] or '')[:400]}")
            for c in candidates[:2]:
                if c["profile"]:
                    ctx_parts.append(f"档案《{c['name']}》：{c['profile'][:120]}")
            ctx = "\n\n".join(ctx_parts)
            answer = ai_mod.resolve_mention_identity(name, options[:-1], ctx)
            if answer:
                hit2 = _find_enabled_by_name(answer)
                if hit2 is not None and hit2["type"] == mention.get("type", "person"):
                    # 自己查明了：静默归并，不打扰用户（传真实日期，不能抹掉实体时间戳 C3）
                    upsert_mention(name, mention.get("type", "person"), mention.get("snippet", ""),
                                   entry_id, entry_day, known_as=answer)
                    logx.log("圈子：自己查明了", f"「{name}」就是《{answer}》，不用再问你")
                    return None
                # 答案命中的是 draft/类型不符：归并未发生，照实说并照常建提问（不吹牛）
                logx.log("圈子：自查有线索但没坐实", f"「{name}」可能是《{answer}》，还是问你一句")
            # 真拿不准：请小模型猜一个预选答案（仅高亮提示，用户说了算）
            ai_suggested = ai_mod.suggest_question_option(name, options[:-1], ctx)
        except Exception:
            pass  # 自查失败不阻塞提问流程

    qid = db.new_id()
    db.execute(
        "INSERT INTO circle_questions(id, question, options_json, related_json, entry_id, "
        "ai_suggested, created_at) VALUES(?,?,?,?,?,?,?)",
        (qid, f"记录里的「{name}」指的是谁？",
         json.dumps(options, ensure_ascii=False),
         json.dumps({"mention_name": name, "entity_ids": [c["id"] for c in candidates],
                     "entry_id": entry_id, "snippet": mention.get("snippet", "")},
                    ensure_ascii=False),
         entry_id, ai_suggested,
         db.now_iso()),
    )
    logx.log("圈子：有个问题等你确认", f"「{name}」指的是谁？")
    return qid


def answer_question(qid: str, choice_index: int) -> dict:
    """回答提问：选中候选→提及挂到该实体+别名并入；'都不是'→仅关闭。
    M4：仅 pending 可答；已答/已忽略重复答抛 bad state（主站单答/批量/agent 同获益）。"""
    row = db.q1("SELECT * FROM circle_questions WHERE id=?", (qid,))
    if row is None:
        raise ValueError("not found")
    if row["status"] != "pending":
        raise ValueError("bad state")
    try:
        options = json.loads(row["options_json"] or "[]")
    except (TypeError, ValueError):
        options = []
    try:
        related = json.loads(row["related_json"] or "{}")
    except (TypeError, ValueError):
        related = {}
    if not isinstance(options, list):
        options = []
    if not isinstance(related, dict):
        related = {}
    if not 0 <= choice_index < len(options):
        raise ValueError("bad index")
    choice = options[choice_index]

    if choice != "都不是":
        entity_ids = related.get("entity_ids") or []
        if choice_index < len(entity_ids):
            target = get_entity(entity_ids[choice_index])
            if target is not None:
                mention_name = related.get("mention_name") or ""
                entry_id = related.get("entry_id") or ""
                snippet = related.get("snippet") or ""
                # orphan 收窄到与答案目标同类型（R2：跨类型"同名"极易认错，宁可不迁移）
                orphan = _find_by_name(mention_name, target["type"])
                if orphan is not None and orphan["id"] == target["id"]:
                    orphan = None
                if orphan is not None:
                    # 有 orphan：直接迁移它的提及（先按 entry 去重，避免同 entry 双行），
                    # 不再 upsert——upsert 会重复插入同一 (entity, entry) 提及（I1）
                    with db.locked() as c:
                        c.execute(
                            "DELETE FROM entity_mentions WHERE entity_id=? AND entry_id IN "
                            "(SELECT entry_id FROM entity_mentions WHERE entity_id=?)",
                            (orphan["id"], target["id"]),
                        )
                        c.execute("UPDATE entity_mentions SET entity_id=? WHERE entity_id=?",
                                  (target["id"], orphan["id"]))
                        c.commit()
                    cnt = db.q1("SELECT COUNT(*) AS n FROM entity_mentions WHERE entity_id=?",
                                (target["id"],))["n"]
                    db.execute("UPDATE entities SET mention_count=?, updated_at=? WHERE id=?",
                               (cnt, db.now_iso(), target["id"]))
                    # 空壳 draft 清理
                    left = db.q1("SELECT COUNT(*) AS n FROM entity_mentions WHERE entity_id=?",
                                 (orphan["id"],))["n"]
                    if left == 0 and orphan["status"] == "draft" and not orphan["user_note"]:
                        db.execute("DELETE FROM entities WHERE id=?", (orphan["id"],))
                else:
                    # 无 orphan：提及挂到目标实体（日期按 entry 回查，不抹时间戳）
                    upsert_mention(target["name"], target["type"], snippet or mention_name,
                                   entry_id, "", known_as=target["name"])
                _merge_alias(target, mention_name)
                logx.log("圈子：称呼已确认", f"「{mention_name}」就是《{target['name']}》")
    db.execute("UPDATE circle_questions SET status='answered', answer=? WHERE id=?",
               (choice, qid))
    return {"answer": choice}


# ---------------- 综合 ----------------

def _norm_fact(s: str) -> str:
    """事实值归一化：去空白/常见标点/大小写。用于新旧值比较，防止措辞微差来回封存（I6）。"""
    return re.sub(r"[\s，。、；：,.;:·!？?\"'“”‘’（）()\-—_]+", "", (s or "").lower())


def _same_fact_value(a: str, b: str) -> bool:
    """归一化后相等，或一方包含另一方（"绿源农场" vs "绿源农场基地"）视为同值。"""
    na, nb = _norm_fact(a), _norm_fact(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def _apply_facts(entity_id: str, entity_name: str, facts: list, old_profile: str) -> None:
    """时序事实落库（双时态）：同 predicate 的活跃 fact 与新值不同（归一化比较）→ 旧值
    valid_to=今天封存，新 fact 生效并写可撤销日志；没有活跃 fact 则直接建档。
    ambiguous 的变化不封存，只 needs_review 等用户过目；confirmed 实体不降级。"""
    today = db.now_iso()[:10]
    src = db.q1("SELECT entry_id FROM entity_mentions WHERE entity_id=? "
                "ORDER BY created_at DESC LIMIT 1", (entity_id,))
    for f in facts[:8]:
        predicate = str(f.get("predicate") or "").strip()[:20]
        obj = str(f.get("object") or "").strip()[:60]
        if not (predicate and obj):
            continue
        certainty = f.get("certainty") if f.get("certainty") in ("explicit", "inferred", "ambiguous") else "inferred"
        valid_from = str(f.get("valid_from") or "").strip()[:10] or today
        active = db.q1(
            "SELECT * FROM entity_facts WHERE entity_id=? AND predicate=? AND valid_to IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (entity_id, predicate),
        )
        if active is not None and _same_fact_value(active["object_text"], obj):
            continue  # 值没变（措辞微差算同值），不动
        if active is not None and certainty == "ambiguous":
            # 拿不准的事实变化：不动事实，只标记等用户过目（confirmed 不降级）
            db.execute(
                "UPDATE entities SET needs_review=1, conflict_note=?, updated_at=? WHERE id=?",
                (f"「{predicate}」似乎变成了「{obj}」，等你过目", db.now_iso(), entity_id))
            continue
        if active is not None:
            db.execute("UPDATE entity_facts SET valid_to=? WHERE id=?", (today, active["id"]))
        fid = db.new_id()
        db.execute(
            "INSERT INTO entity_facts(id, entity_id, predicate, object_text, valid_from, "
            "source_entry_id, certainty, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (fid, entity_id, predicate, obj, valid_from,
             src["entry_id"] if src else None, certainty, db.now_iso()),
        )
        if active is not None:
            _auto_log("fact_update", {
                "entity_id": entity_id, "predicate": predicate,
                "old_object": active["object_text"], "new_object": obj,
                "old_fact_id": active["id"], "new_fact_id": fid,
                "old_profile": old_profile})
            logx.log(f"圈子：事实更新《{entity_name}》",
                     f"「{predicate}」{active['object_text']} → {obj}（旧值已封存进历史，可撤销）")
        else:
            logx.log(f"圈子：记下事实《{entity_name}》", f"「{predicate}」{obj}")


def _apply_synthesis(row, result) -> tuple[str, bool]:
    """落库综合结果。返回 (新 status, 是否本次新启用)。

    状态机（draft 时按 confidence 定级）：
      ≥confirm_threshold（事件 0.7/人物地点 0.85）→ confirmed（自动确认，写可撤销日志；
        开关关掉则只停在 active）；0.5–阈值 → active（待确认）；<0.5 → 保持 draft。
    冲突时：置 needs_review/conflict_note，本次不覆盖 profile 等描述；
    active 降级 draft（confirmed 不降级）。无冲突则清冲突标记。
    """
    now = db.now_iso()
    confidence = result["confidence"]
    old_status = row["status"]
    conflict = result.get("conflict") or {}

    if conflict.get("has"):
        # 矛盾有两类：证据变了（如离职）与疑似重名（同名不同人）。
        # 重名时用户可编辑其中一个的名字加以区分；两条路都先标记等用户裁定。
        detail = str(conflict.get("detail") or "").strip()[:60]
        new_status = old_status
        if old_status == "active":
            new_status = "draft"  # 矛盾未裁定前降级回搁置；confirmed 不动
        db.execute(
            "UPDATE entities SET needs_review=1, conflict_note=?, status=?, "
            "synthesized_at=?, updated_at=? WHERE id=?",
            (detail, new_status, now, now, row["id"]),
        )
        logx.log(f"圈子：发现矛盾《{row['name']}》", detail or "新证据与旧档案不一致")
        return new_status, False

    new_status = old_status
    auto_confirmed = False
    if old_status == "draft":
        threshold = confirm_threshold(row["type"])
        # 用户撤销过自动确认（auto_confirm_blocked=1）的实体：达标也只停在 active，永不再自动确认
        if confidence >= threshold and auto_confirm_on() and not row["auto_confirm_blocked"]:
            new_status = "confirmed"
            auto_confirmed = True
        elif confidence >= ACTIVE_FLOOR:
            new_status = "active"
        # <0.5 保持 draft（不动）
    activated = old_status == "draft" and new_status in ("active", "confirmed")

    # 别名并入（规范化去重，不含本名）
    try:
        aliases = json.loads(row["aliases_json"] or "[]")
    except ValueError:
        aliases = []
    name_norm = norm_name(row["name"])
    for a in result["aliases"]:
        if norm_name(a) and norm_name(a) != name_norm and not any(norm_name(x) == norm_name(a) for x in aliases):
            aliases.append(a)

    db.execute(
        "UPDATE entities SET profile=?, relation_to_user=?, confidence=?, status=?, "
        "aliases_json=?, synthesized_at=?, updated_at=?, needs_review=0, conflict_note=NULL WHERE id=?",
        (result["profile"], result["relation_to_user"], confidence, new_status,
         json.dumps(aliases, ensure_ascii=False), now, now, row["id"]),
    )
    # 类型自纠：AI 发现类型标错时改正（同名同类型冲突则放弃，交给合并流程）
    st = result.get("suggest_type")
    if st and st in ("person", "place", "event") and st != row["type"]:
        clash = db.q1("SELECT id FROM entities WHERE type=? AND name=? AND id != ?",
                      (st, row["name"], row["id"]))
        if clash is None:
            db.execute("UPDATE entities SET type=? WHERE id=?", (st, row["id"]))
            logx.log("圈子：纠正类型", f"《{row['name']}》{TYPE_LABEL.get(row['type'])}→{TYPE_LABEL.get(st)}")
    # 自动改名：证据给出更正式的名字且无 (type, name) 冲突时改名，旧名并入别名；
    # 有冲突则不改名，留给合并提案流程。user_note 永不被改动。
    better = result.get("better_name")
    if better and norm_name(better) != norm_name(row["name"]):
        clash = db.q1("SELECT id FROM entities WHERE type=? AND name=? AND id != ?",
                      (row["type"], better, row["id"]))
        if clash is None:
            old_name = row["name"]
            cur = db.q1("SELECT aliases_json FROM entities WHERE id=?", (row["id"],))
            try:
                aliases_now = json.loads(cur["aliases_json"] or "[]")
            except ValueError:
                aliases_now = []
            if not any(norm_name(a) == norm_name(old_name) for a in aliases_now):
                aliases_now.append(old_name)
            db.execute("UPDATE entities SET name=?, aliases_json=?, updated_at=? WHERE id=?",
                       (better[:30], json.dumps(aliases_now, ensure_ascii=False), now, row["id"]))
            logx.log("圈子：改名", f"《{old_name}》→《{better[:30]}》")
    _upsert_relations(row["id"], result["relations"], confidence)
    _apply_facts(row["id"], row["name"], result.get("facts") or [], row["profile"])
    if auto_confirmed:
        _auto_log("confirm", {"entity_id": row["id"], "old_status": old_status,
                              "old_confidence": row["confidence"],
                              "confidence": confidence, "mention_count": row["mention_count"],
                              "basis": "综合达标"})
        logx.log(f"圈子：小满自己确认了《{row['name']}》",
                 f"依据 {row['mention_count']} 条记录，确信度 {confidence:.1f}")
    return new_status, activated


def _find_by_name_any_type(name: str):
    """跨类型按规范化名/别名查找：人物优先，其次地点、事件。"""
    for t in ("person", "place", "event"):
        r = _find_by_name(name, t)
        if r is not None:
            return r
    return None


def _relation_day(value: str | None, fallback: str | None = None) -> str:
    """只接受 ISO 日期，避免模型返回自然语言日期污染时序字段。"""
    raw = str(value or "").strip()[:10]
    if raw and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            datetime.strptime(raw, "%Y-%m-%d")
            return raw
        except ValueError:
            pass
    return fallback or ""


def _upsert_relations(source_id: str, relations: list, confidence: float) -> None:
    now = db.now_iso()
    # 综合结果目前只带日期+片段，未必能逐条回传 entry_id。
    # 先用该实体最近一条可参与 AI 的提及记录作为保守证据回链，
    # 后续若模型明确返回 entry_id 则优先使用模型值。
    latest_mention = db.q1(
        "SELECT m.entry_id FROM entity_mentions m "
        "JOIN entries e ON e.id=m.entry_id "
        "WHERE m.entity_id=? AND e.deleted_at IS NULL AND e.exclude_from_ai=0 "
        "ORDER BY e.occurred_at DESC, m.created_at DESC LIMIT 1",
        (source_id,),
    )
    default_entry_id = latest_mention["entry_id"] if latest_mention else None
    for rel in relations:
        if not isinstance(rel, dict):
            continue
        label = str(rel.get("label") or "").strip()[:20]
        target_name = str(rel.get("target_name") or "").strip()[:30]
        if not label or not target_name:
            continue
        candidate_entry_id = str(rel.get("entry_id") or "").strip()
        raw_entry_id = candidate_entry_id
        evidence_mention = None
        if candidate_entry_id:
            evidence_mention = db.q1(
                "SELECT m.snippet FROM entity_mentions m "
                "JOIN entries e ON e.id=m.entry_id "
                "WHERE m.entity_id=? AND m.entry_id=? "
                "AND e.deleted_at IS NULL AND e.exclude_from_ai=0 LIMIT 1",
                (source_id, candidate_entry_id),
            )
            if evidence_mention is None:
                candidate_entry_id = ""
        entry_id = candidate_entry_id or default_entry_id
        evidence_day = ""
        if entry_id:
            evidence = db.q1(
                "SELECT substr(occurred_at,1,10) AS day FROM entries "
                "WHERE id=? AND deleted_at IS NULL AND exclude_from_ai=0",
                (entry_id,),
            )
            evidence_day = evidence["day"] if evidence else ""
        valid_from = _relation_day(rel.get("valid_from"), evidence_day or now[:10])
        valid_to = _relation_day(rel.get("valid_to"), "")
        if valid_to and valid_from and valid_to < valid_from:
            valid_to = ""
        certainty = rel.get("certainty") if rel.get("certainty") in (
            "explicit", "inferred", "ambiguous") else "inferred"
        state = rel.get("state") if rel.get("state") in ("active", "ended", "uncertain") else "active"
        snippet = str(rel.get("snippet") or "").strip()[:120]
        if entry_id and not evidence_mention:
            evidence_mention = db.q1(
                "SELECT snippet FROM entity_mentions WHERE entity_id=? AND entry_id=? LIMIT 1",
                (source_id, entry_id),
            )
        # 模型给出的“依据片段”若不是已保存的原文提及，就退回真实提及，避免详情展示伪引文。
        if evidence_mention:
            source_snippet = str(evidence_mention["snippet"] or "").strip()[:120]
            if snippet and _norm_fact(snippet) not in _norm_fact(source_snippet):
                snippet = source_snippet
            elif not snippet:
                snippet = source_snippet
        # 关系“结束”属于破坏性状态变化：只有原文明确陈述且模型标记 explicit 才能封存，
        # inferred/ambiguous 结束猜测一律等待下一轮证据，避免把“暂时没联系”当作终止。
        ambiguous_relation = certainty == "ambiguous" or state == "uncertain" or (
            state == "ended" and certainty != "explicit"
        )
        # 结束关系必须能回链到本轮明确来源；缺 entry_id 时不做破坏性封存。
        if state == "ended" and (not raw_entry_id or not evidence_mention):
            continue
        target_type = rel.get("target_type") if rel.get("target_type") in ("person", "place", "event") else ""
        target = (_find_by_name(target_name, target_type)
                  if target_type else _find_by_name_any_type(target_name))
        # 结束声明不能凭空创建一个目标档案；只有已有实体关系才能封存。
        if target is None and (state == "ended" or ambiguous_relation):
            continue
        if target is None:
            target_id = db.new_id()
            target_type = target_type or "person"
            db.execute(
                "INSERT INTO entities(id, type, name, status, confidence, created_at, updated_at) "
                "VALUES(?, ?, ?, 'draft', 0.4, ?, ?)",
                (target_id, target_type, target_name, now, now),
            )
        else:
            if target["status"] == "rejected" or target["id"] == source_id:
                continue
            target_id = target["id"]
        # 新边只有“原文明写”才能直接进入图；推测关系留在档案详情等待更多证据。
        rel_status = "active" if confidence >= 0.75 and certainty == "explicit" else "draft"
        existing = db.q1(
            "SELECT * FROM entity_relations WHERE source_id=? AND target_id=? AND label=? "
            "AND valid_to IS NULL ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, updated_at DESC LIMIT 1",
            (source_id, target_id, label),
        )
        if ambiguous_relation and existing is not None:
            continue  # 拿不准的变化不覆盖已经坐实的关系
        if state == "ended":
            # 只封存同一条明确关系；缺少结束日期时使用来源记录日期，避免用运行当天篡改历史。
            if existing is None:
                candidates = db.q(
                    "SELECT * FROM entity_relations WHERE source_id=? AND target_id=? "
                    "AND valid_to IS NULL ORDER BY updated_at DESC LIMIT 2",
                    (source_id, target_id),
                )
                # 模型可能把原来的“同事”写成“已离职”：优先从结束证据片段中找出
                # 被明确终止的旧标签；只有找不到标签且仅一条当前边时才保守匹配。
                snippet_norm = _norm_fact(snippet)
                matched = [c for c in candidates if c["label"] and _norm_fact(c["label"]) in snippet_norm]
                if len(matched) == 1:
                    existing = matched[0]
                elif len(candidates) == 1:
                    existing = candidates[0]
            if existing is not None:
                end_day = valid_to or evidence_day or now[:10]
                if existing["valid_from"] and end_day < existing["valid_from"]:
                    end_day = existing["valid_from"]
                db.execute(
                    "UPDATE entity_relations SET valid_to=?, status='ended', certainty=?, "
                    "snippet=CASE WHEN ?!='' THEN ? ELSE snippet END, "
                    "entry_id=COALESCE(?, entry_id), updated_at=? WHERE id=?",
                    (end_day, certainty, snippet, snippet, entry_id, now, existing["id"]),
                )
            continue
        if existing:
            next_status = "active" if existing["status"] == "active" or rel_status == "active" else rel_status
            current_from = existing["valid_from"] or ""
            next_from = min(x for x in (current_from, valid_from) if x) if (current_from or valid_from) else ""
            next_certainty = certainty if CERTAINTY_RANK.get(certainty, 1) >= CERTAINTY_RANK.get(existing["certainty"], 1) else existing["certainty"]
            db.execute(
                "UPDATE entity_relations SET confidence=?, status=?, certainty=?, valid_from=?, "
                "snippet=CASE WHEN ?!='' THEN ? ELSE snippet END, updated_at=?, "
                "entry_id=COALESCE(entry_id, ?) WHERE id=?",
                (max(float(existing["confidence"] or 0), confidence), next_status, next_certainty,
                 next_from, snippet, snippet, now, entry_id, existing["id"]),
            )
        else:
            db.execute(
                "INSERT INTO entity_relations(id, source_id, target_id, label, status, "
                "confidence, entry_id, snippet, valid_from, certainty, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (db.new_id(), source_id, target_id, label, rel_status,
                 confidence, entry_id, snippet, valid_from, certainty, now, now),
            )


def _queue_notice(names: list[str]) -> None:
    if not names:
        return
    pending = []
    raw = db.get_setting("circle_notice")
    if raw:
        try:
            pending = json.loads(raw).get("names") or []
        except ValueError:
            pending = []
    for n in names:
        if n not in pending:
            pending.append(n)
    text = f"小满新认识了 {len(pending)} 位朋友：" + "".join(f"《{n}》" for n in pending)
    db.set_setting("circle_notice", json.dumps(
        {"text": text, "names": pending, "ts": db.now_iso()}, ensure_ascii=False))


def synthesize_one(row) -> bool:
    """综合一个实体（strong）。返回是否成功。user_note 为最高事实（prompt 层保证）。"""
    mentions = db.q(
        "SELECT m.entry_id, m.snippet, substr(e.occurred_at,1,10) AS day FROM entity_mentions m "
        "JOIN entries e ON e.id = m.entry_id "
        "WHERE m.entity_id=? AND e.deleted_at IS NULL AND e.exclude_from_ai=0 "
        "ORDER BY e.occurred_at DESC LIMIT 10",
        (row["id"],),
    )
    mention_rows = [(m["day"], m["snippet"], m["entry_id"]) for m in reversed(mentions)]
    result = ai_mod.synthesize_entity_profile(row, mention_rows)
    new_status, activated = _apply_synthesis(row, result)
    n = len(mention_rows)
    if activated:
        rel = f"（{result['relation_to_user']}）" if result["relation_to_user"] else ""
        logx.log(f"圈子：新认识《{row['name']}》{rel}", f"依据 {n} 条记录，确信度 {result['confidence']:.1f}")
        _queue_notice([row["name"]])
    else:
        state = "已启用" if new_status in ("active", "confirmed") else "仍搁置"
        logx.log(f"圈子：更新档案《{row['name']}》",
                 f"依据 {n} 条记录，确信度 {result['confidence']:.1f}，{state}")
    return True


def synthesize_due(max_n: int = 3) -> int:
    """有新动静（updated_at 晚于上次综合）且距上次综合 ≥1 小时的实体，每轮最多 max_n 个。
    用 updated_at 而非 last_seen 判断新动静——last_seen 只到日期，同日新提及会被漏掉。"""
    if not enabled() or not ai_mod.strong_configured():
        return 0
    cutoff = (datetime.now().astimezone() - SYNTH_INTERVAL).isoformat(timespec="seconds")
    rows = db.q(
        "SELECT * FROM entities WHERE status != 'rejected' AND mention_count > 0 "
        "AND updated_at > COALESCE(synthesized_at, '') "
        "AND (synthesized_at IS NULL OR synthesized_at <= ?) "
        "ORDER BY last_seen DESC LIMIT ?",
        (cutoff, max_n),
    )
    done = 0
    for r in rows:
        try:
            synthesize_one(r)
            done += 1
        except Exception:
            break  # AI 挂了本轮就停，下轮再试
    return done


def review_drafts(max_n: int = 2) -> int:
    """复查：draft 且 mention_count≥2、有新动静（updated_at 晚于上次综合）→ 重新综合。
    needs_review 的（等用户裁定）不参与；没有新动静的不反复综合（防止每轮空转刷调用）。"""
    if not enabled() or not ai_mod.strong_configured():
        return 0
    rows = db.q(
        "SELECT * FROM entities WHERE status = 'draft' AND needs_review = 0 "
        "AND mention_count >= 2 "
        "AND (synthesized_at IS NULL OR updated_at > synthesized_at) "
        "ORDER BY mention_count DESC, last_seen DESC LIMIT ?",
        (max_n,),
    )
    done = 0
    for r in rows:
        try:
            synthesize_one(r)
            done += 1
        except Exception:
            break
    return done


# ---------------- 上下文注入 ----------------

def circle_context_with_names(limit: int = 12) -> tuple[str, list[str]]:
    """取 active/confirmed 实体生成'背景档案'文本。confirmed 优先，按 last_seen 倒序。"""
    if not enabled():
        return '', []
    rows = db.q(
        "SELECT * FROM entities WHERE status IN ('active','confirmed') "
        "ORDER BY CASE status WHEN 'confirmed' THEN 0 ELSE 1 END, last_seen DESC LIMIT ?",
        (limit,),
    )
    names = [r["name"] for r in rows]
    lines = []
    for r in rows:
        rel = f"·{r['relation_to_user']}" if r["relation_to_user"] else ""
        label = TYPE_LABEL.get(r["type"], r["type"])
        lines.append(f"{r['name']}（{label}{rel}）：{(r['profile'] or '')[:50]}")
    return "\n".join(lines), names


def circle_context(limit: int = 12) -> str:
    return circle_context_with_names(limit)[0]


def circle_context_for_entries(entry_ids: list, limit: int = 12) -> tuple[str, list[str]]:
    """按记录范围挑选相关档案（启用中的）——报告/问答按本期或命中记录精确取人，而非泛泛的最近 12 份。"""
    if not enabled() or not entry_ids:
        return "", []
    placeholders = ",".join("?" for _ in entry_ids)
    rows = db.q(
        f"SELECT e.*, COUNT(m.id) AS hits FROM entities e "
        f"JOIN entity_mentions m ON m.entity_id = e.id "
        f"WHERE e.status IN ('active','confirmed') AND m.entry_id IN ({placeholders}) "
        f"GROUP BY e.id "
        f"ORDER BY CASE e.status WHEN 'confirmed' THEN 0 ELSE 1 END, hits DESC, e.last_seen DESC "
        f"LIMIT ?",
        (*entry_ids, limit),
    )
    names = [r["name"] for r in rows]
    lines = []
    for r in rows:
        rel = f"·{r['relation_to_user']}" if r["relation_to_user"] else ""
        label = TYPE_LABEL.get(r["type"], r["type"])
        lines.append(f"{r['name']}（{label}{rel}）：{(r['profile'] or '')[:50]}")
    return "\n".join(lines), names


# ---------------- 全量回填 ----------------

def backfill_status() -> dict:
    return {
        "done": db.get_setting("circle_backfill_done") == "true",
        "processed_days": int(db.get_setting("circle_backfill_progress") or 0),
        "total_days": int(db.get_setting("circle_backfill_total") or 0),
    }


def start_backfill() -> None:
    if not enabled():
        return
    if db.get_setting("circle_backfill_done") == "true":
        return
    if not _backfill_lock.acquire(blocking=False):
        return  # 已在跑
    threading.Thread(target=_backfill_worker, daemon=True).start()


def _backfill_worker() -> None:
    try:
        days = [r[0] for r in db.q(
            "SELECT DISTINCT substr(occurred_at,1,10) AS d FROM entries "
            "WHERE deleted_at IS NULL AND exclude_from_ai=0 ORDER BY d"
        )]
        total = len(days)
        db.set_setting("circle_backfill_total", str(total))
        db.set_setting("circle_backfill_progress", "0")
        for i, day in enumerate(days, 1):
            try:
                rows = db.q(
                    "SELECT id, title, content FROM entries WHERE deleted_at IS NULL "
                    "AND exclude_from_ai=0 AND substr(occurred_at,1,10)=? "
                    "AND trim(content) != '' ORDER BY occurred_at",
                    (day,),
                )
                if rows:
                    _backfill_one_day(day, rows)
            except Exception:
                pass  # 当天失败跳过，不阻塞整体
            db.set_setting("circle_backfill_progress", str(i))
            if i % 3 == 0 or i == total:
                logx.log("圈子回填", f"{i}/{total} 天")
        db.set_setting("circle_backfill_done", "true")
        logx.log("圈子回填完成", f"共 {total} 天，开始整理档案")
        # 触发综合队列：分批跑完
        for _ in range(40):
            try:
                if synthesize_due(3) == 0:
                    break
            except Exception:
                break
        review_drafts(10)
        logx.log("圈子档案整理完成")
    finally:
        _backfill_lock.release()


def _backfill_one_day(day: str, rows) -> None:
    """每天一次 AI 调用：把当天记录打包提取提及（带 entry 回链）。"""
    blocks = []
    for r in rows:
        blocks.append(f"[记录 entry_id={r['id']}]\n标题：{r['title'] or '（无）'}\n正文：{(r['content'] or '')[:800]}")
    text = "\n---\n".join(blocks)[:12000]
    data = ai_mod._chat_json([
        {"role": "system", "content": ai_mod.CIRCLE_EXTRACT_SYSTEM +
         "\n注意：输入含多条记录（各有 entry_id 标记），mentions 里每项要额外带 entry_id 字段指明出处。"},
        {"role": "user", "content": text},
    ], slot="fast")
    valid_ids = {r["id"] for r in rows}
    for m in (data.get("mentions") or [])[:20] if isinstance(data, dict) else []:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or "").strip()[:30]
        mtype = str(m.get("type") or "").strip()
        snippet = str(m.get("snippet") or "").strip()[:80]
        entry_id = str(m.get("entry_id") or "").strip()
        if entry_id not in valid_ids:
            entry_id = rows[0]["id"]
        if name and mtype in ("person", "place", "event") and snippet:
            certainty = str(m.get("certainty") or "").strip()
            if certainty not in ("explicit", "inferred", "ambiguous"):
                certainty = "inferred"
            upsert_mention(name, mtype, snippet, entry_id, day, certainty=certainty)


# ---------------- 关系图数据 ----------------

def graph_data() -> dict:
    """关系图只展示当前有效关系，附带清单所需的已结束历史。

    draft 是尚未坐实的候选档案，不能参与关系图，否则会把猜测当成事实，
    也会让节点和边数量随扫描过程膨胀。前端若需要查看 draft，仍可在实体列表里筛选。
    """
    rows = db.q(
        "SELECT id, name, type, status, mention_count FROM entities "
        "WHERE status IN ('confirmed','active')"
    )
    node_ids = {r["id"] for r in rows}
    relations = []
    visual_pairs: dict[tuple[str, str], dict] = {}
    degree: dict[str, int] = {}
    # draft 关系本身也不画；同时再次校验两端节点，避免历史脏数据漏进图。
    for l in db.q(
        "SELECT source_id, target_id, label, status, entry_id, snippet, valid_from, "
        "valid_to, certainty, updated_at FROM entity_relations "
        "WHERE status='active' AND valid_to IS NULL ORDER BY updated_at DESC"
    ):
        if l["source_id"] not in node_ids or l["target_id"] not in node_ids:
            continue
        item = {"source": l["source_id"], "target": l["target_id"],
                "label": l["label"], "status": l["status"],
                "entry_id": l["entry_id"], "snippet": l["snippet"],
                "valid_from": l["valid_from"], "valid_to": l["valid_to"],
                "certainty": l["certainty"]}
        relations.append(item)
        # 图只画实体之间有没有联系。同一对实体的近义/反向边聚成一条，避免线条叠加、
        # 力导向重复计算和 tooltip 信息互相遮挡；完整方向与来源仍在 relations 返回。
        pair = tuple(sorted((l["source_id"], l["target_id"])))
        grouped = visual_pairs.get(pair)
        if grouped is None:
            # 保留最新关系的方向作为展示主方向；pair key 仅用于去重，不能反转“老板→员工”等语义。
            grouped = {**item, "labels": [], "evidence": [], "relation_count": 0}
            visual_pairs[pair] = grouped
            degree[pair[0]] = degree.get(pair[0], 0) + 1
            degree[pair[1]] = degree.get(pair[1], 0) + 1
        if l["label"] and l["label"] not in grouped["labels"]:
            grouped["labels"].append(l["label"])
        if l["entry_id"] and not any(e["entry_id"] == l["entry_id"] for e in grouped["evidence"]):
            grouped["evidence"].append({"entry_id": l["entry_id"], "label": l["label"]})
        grouped["relation_count"] += 1
        if l["valid_from"] and (not grouped["valid_from"] or l["valid_from"] < grouped["valid_from"]):
            grouped["valid_from"] = l["valid_from"]
    # 力导向图只画当前关系；下方折叠清单保留已结束的关系段，用户才能看见
    # “以前是什么、何时结束”，而不是把人物关系误读成永远不变。
    for l in db.q(
        "SELECT source_id, target_id, label, status, entry_id, snippet, valid_from, "
        "valid_to, certainty, updated_at FROM entity_relations "
        "WHERE status='ended' OR valid_to IS NOT NULL ORDER BY updated_at DESC"
    ):
        if l["source_id"] not in node_ids or l["target_id"] not in node_ids:
            continue
        relations.append({
            "source": l["source_id"], "target": l["target_id"],
            "label": l["label"], "status": "ended",
            "entry_id": l["entry_id"], "snippet": l["snippet"],
            "valid_from": l["valid_from"], "valid_to": l["valid_to"],
            "certainty": l["certainty"],
        })
    links = []
    for grouped in visual_pairs.values():
        labels = grouped["labels"]
        grouped["label"] = "、".join(labels[:2]) + (f"等{len(labels)}种关联" if len(labels) > 2 else "")
        grouped["evidence"] = grouped["evidence"][:5]
        links.append(grouped)
    nodes = [
        {"id": r["id"], "name": r["name"], "type": r["type"], "status": r["status"],
         "mention_count": r["mention_count"], "degree": degree.get(r["id"], 0)}
        for r in rows
    ]
    return {"nodes": nodes, "links": links, "relations": relations}


# ---------------- 合并 ----------------

def merge_entities(from_id: str, into_id: str) -> None:
    src, dst = get_entity(from_id), get_entity(into_id)
    if src is None or dst is None:
        raise ValueError("not found")
    with db.locked() as c:
        # 提及迁移（同 entry 去重）
        c.execute(
            "DELETE FROM entity_mentions WHERE entity_id=? AND entry_id IN "
            "(SELECT entry_id FROM entity_mentions WHERE entity_id=?)",
            (from_id, into_id),
        )
        c.execute("UPDATE entity_mentions SET entity_id=? WHERE entity_id=?", (into_id, from_id))
        # 关系迁移
        c.execute("UPDATE entity_relations SET source_id=? WHERE source_id=?", (into_id, from_id))
        c.execute("UPDATE entity_relations SET target_id=? WHERE target_id=?", (into_id, from_id))
        # 迁移后可能产生自环（自己指向自己），清掉
        c.execute("DELETE FROM entity_relations WHERE source_id=target_id")
        c.execute("DELETE FROM entities WHERE id=?", (from_id,))
        c.commit()
    # 别名并入：from 的名字和别名都归给 into
    try:
        aliases = json.loads(dst["aliases_json"] or "[]")
    except ValueError:
        aliases = []
    try:
        src_aliases = json.loads(src["aliases_json"] or "[]")
    except ValueError:
        src_aliases = []
    candidates = [src["name"]] + src_aliases
    dst_norms = {norm_name(a) for a in aliases} | {norm_name(dst["name"])}
    for a in candidates:
        if norm_name(a) and norm_name(a) not in dst_norms:
            aliases.append(a)
            dst_norms.add(norm_name(a))
    count = db.q1("SELECT COUNT(*) AS n FROM entity_mentions WHERE entity_id=?", (into_id,))["n"]
    last_day = db.q1(
        "SELECT MAX(substr(e.occurred_at,1,10)) AS d FROM entity_mentions m "
        "JOIN entries e ON e.id=m.entry_id WHERE m.entity_id=?", (into_id,))["d"]
    db.execute(
        "UPDATE entities SET aliases_json=?, mention_count=?, "
        "first_seen=MIN(COALESCE(first_seen, ?), ?), "
        "last_seen=COALESCE(?, last_seen), updated_at=? WHERE id=?",
        (json.dumps(aliases, ensure_ascii=False), count, src["first_seen"], src["first_seen"],
         last_day, db.now_iso(), into_id),
    )
    logx.log("圈子：合并档案", f"《{src['name']}》并入《{dst['name']}》")


# ---------------- 合并提案 ----------------

PROPOSE_INTERVAL = timedelta(hours=3)
PROPOSE_MAX_PAIRS = 5


def _shared_substring(a: str, b: str, min_len: int = 2) -> bool:
    """规范化名有共同子串（含互相包含）。"""
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    for i in range(len(a) - min_len + 1):
        if a[i:i + min_len] in b:
            return True
    return False


def _entity_side(row, snippets) -> dict:
    try:
        aliases = json.loads(row["aliases_json"] or "[]")
    except ValueError:
        aliases = []
    return {"name": row["name"], "aliases": aliases, "profile": row["profile"],
            "relation_to_user": row["relation_to_user"], "snippets": snippets}


def _proposal_exists(from_id: str, into_id: str) -> bool:
    return db.q1(
        "SELECT id FROM merge_proposals WHERE status IN ('pending','rejected') "
        "AND ((from_id=? AND into_id=?) OR (from_id=? AND into_id=?))",
        (from_id, into_id, into_id, from_id),
    ) is not None


def propose_merges(now: datetime | None = None) -> int:
    """每轮 tick 调用；内部节流 ≥3 小时。返回新建提案数。"""
    if not enabled() or not ai_mod.fast_configured() and not ai_mod.strong_configured():
        return 0
    now = now or datetime.now().astimezone()
    last = db.get_setting("circle_last_propose_at")
    if last:
        try:
            if (now - datetime.fromisoformat(last)) < PROPOSE_INTERVAL:
                return 0
        except ValueError:
            pass
    db.set_setting("circle_last_propose_at", now.isoformat(timespec="seconds"))

    rows = db.q(
        "SELECT * FROM entities WHERE status IN ('draft','active','confirmed') "
        "AND mention_count > 0 ORDER BY mention_count DESC LIMIT 30"
    )
    if len(rows) < 2:
        return 0

    # 可选：嵌入向量候选
    vecs: dict[str, list] = {}
    if ai_mod.embed_configured():
        for r in rows:
            v = ai_mod.embed_text((r["name"] or "") + "\n" + (r["profile"] or "")[:200])
            if v:
                vecs[r["id"]] = v

    def alias_set(r):
        try:
            return {norm_name(x) for x in json.loads(r["aliases_json"] or "[]")}
        except ValueError:
            return set()

    created = 0
    judged = 0
    for i in range(len(rows)):
        if judged >= PROPOSE_MAX_PAIRS:
            break
        for j in range(i + 1, len(rows)):
            if judged >= PROPOSE_MAX_PAIRS:
                break
            a, b = rows[i], rows[j]
            if a["type"] != b["type"]:
                continue
            if is_rejected_pair(a["id"], b["id"]):
                continue  # 用户拒绝过的配对，永不再提
            na, nb = norm_name(a["name"]), norm_name(b["name"])
            candidate = (
                _shared_substring(na, nb)
                or bool((alias_set(a) | {na}) & (alias_set(b) | {nb}))
                or (a["id"] in vecs and b["id"] in vecs
                    and ai_mod.cosine(vecs[a["id"]], vecs[b["id"]]) >= 0.8)
            )
            if not candidate or _proposal_exists(a["id"], b["id"]):
                continue
            judged += 1
            snippets_a = [m["snippet"] for m in db.q(
                "SELECT snippet FROM entity_mentions WHERE entity_id=? LIMIT 3", (a["id"],))]
            snippets_b = [m["snippet"] for m in db.q(
                "SELECT snippet FROM entity_mentions WHERE entity_id=? LIMIT 3", (b["id"],))]
            try:
                verdict = ai_mod.judge_same_entity(
                    _entity_side(a, snippets_a), _entity_side(b, snippets_b))
            except Exception:
                break  # AI 挂了本轮停
            if verdict["same"] and verdict["confidence"] >= 0.8:
                # from=提及少的一方，into=提及多的一方
                from_row, into_row = (a, b) if a["mention_count"] <= b["mention_count"] else (b, a)
                if _proposal_exists(from_row["id"], into_row["id"]):
                    continue
                db.execute(
                    "INSERT INTO merge_proposals(id, from_id, into_id, reason, created_at) "
                    "VALUES(?,?,?,?,?)",
                    (db.new_id(), from_row["id"], into_row["id"],
                     verdict["reason"] or "疑似同一人", db.now_iso()),
                )
                created += 1
                logx.log("圈子：发现疑似重复档案",
                         f"《{from_row['name']}》和《{into_row['name']}》（{verdict['reason']}）")
    return created


# ---------------- 级联确认 ----------------

def cascade_after_confirm(row, max_n: int = 3) -> int:
    """确认后：把该实体 active 关系对端的 draft 邻居逐个综合（置信度上去就自动启用）。"""
    rels = db.q(
        "SELECT r.target_id FROM entity_relations r WHERE r.source_id=? AND r.status='active'",
        (row["id"],),
    )
    done = 0
    for rel in rels:
        if done >= max_n:
            break
        neighbor = get_entity(rel["target_id"])
        if neighbor is None or neighbor["status"] != "draft" or neighbor["needs_review"]:
            continue
        try:
            synthesize_one(neighbor)
            done += 1
            logx.log(f"圈子：因《{row['name']}》确认", f"连带整理《{neighbor['name']}》")
        except Exception:
            break
    return done


# ---------------- 自动动作日志：查询与撤销 ----------------

def _entity_name(eid: str | None) -> str:
    if not eid:
        return "（已删除）"
    r = db.q1("SELECT name FROM entities WHERE id=?", (eid,))
    return r["name"] if r else "（已删除）"


def _auto_log_text(action: str, d: dict) -> str:
    if action == "confirm":
        return (f"小满自己确认了《{_entity_name(d.get('entity_id'))}》"
                f"（{d.get('basis') or '达标'}，确信度 {float(d.get('confidence') or 0):.1f}）")
    if action == "merge":
        return (f"小满把《{d.get('from_name') or '（已删除）'}》并进了"
                f"《{d.get('into_name') or _entity_name(d.get('into_id'))}》"
                f"（{d.get('reason') or '很像'}）")
    if action == "fact_update":
        return (f"小满更新了《{_entity_name(d.get('entity_id'))}》的「{d.get('predicate') or '事实'}」："
                f"{d.get('old_object') or '（无）'} → {d.get('new_object') or '（无）'}")
    if action == "retype":
        return (f"小满把《{_entity_name(d.get('entity_id'))}》的类型从"
                f"「{TYPE_LABEL.get(d.get('old_type') or '', d.get('old_type') or '?')}」改成了"
                f"「{TYPE_LABEL.get(d.get('new_type') or '', d.get('new_type') or '?')}」")
    return action


def auto_log_list(days: int = 14) -> list[dict]:
    """近 N 天未撤销的自动动作（含实体名与理由）。"""
    cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = db.q(
        "SELECT * FROM circle_auto_log WHERE undone=0 AND created_at>=? ORDER BY created_at DESC",
        (cutoff,),
    )
    items = []
    for r in rows:
        try:
            d = json.loads(r["detail_json"] or "{}")
        except ValueError:
            d = {}
        items.append({"id": r["id"], "action": r["action"], "detail": d,
                      "created_at": r["created_at"], "text": _auto_log_text(r["action"], d)})
    return items


def undo_auto_action(aid: str) -> dict:
    """按 detail_json 快照回滚一次自动动作。
    confirm→回退旧 status/confidence 并置 auto_confirm_blocked（不再自动确认，C1）；
    merge（当前唯一形态为 alias_attach 称呼挂靠）→摘别名并记名字维度黑名单（不再并回，C2）；
    fact_update→恢复旧 fact 与旧 profile；retype→还原旧类型（有同名冲突则跳过）。"""
    row = db.q1("SELECT * FROM circle_auto_log WHERE id=?", (aid,))
    if row is None:
        raise ValueError("not found")
    if row["undone"]:
        raise ValueError("already undone")
    try:
        d = json.loads(row["detail_json"] or "{}")
    except ValueError:
        raise ValueError("bad snapshot")
    action = row["action"]
    now = db.now_iso()

    if action == "confirm":
        eid = d.get("entity_id")
        ent = get_entity(eid) if eid else None
        if ent is not None:
            # 撤销 = 用户否决这次自动确认：回退 status 与 confidence 快照，
            # 并置 auto_confirm_blocked——之后 explicit 直通与综合达标都不再自动确认（C1），
            # 只能用户手动确认（手动路径不看这个标记）。
            old_conf = d.get("old_confidence")
            db.execute(
                "UPDATE entities SET status=?, confidence=?, auto_confirm_blocked=1, "
                "updated_at=? WHERE id=?",
                (d.get("old_status") or "draft",
                 float(old_conf) if old_conf is not None else ent["confidence"],
                 now, eid),
            )
            logx.log("圈子：撤销自动确认",
                     f"《{ent['name']}》回到「{d.get('old_status') or 'draft'}」，以后不再自动确认")
    elif action == "merge":
        # alias_attach 是当前唯一的自动 merge 形态（称呼挂靠，无实体拆分可言）
        if d.get("kind") != "alias_attach":
            raise ValueError("unsupported")
        ent = get_entity(d.get("entity_id") or "")
        alias = d.get("alias") or ""
        if ent is not None:
            try:
                aliases = json.loads(ent["aliases_json"] or "[]")
            except ValueError:
                aliases = []
            kept = [a for a in aliases if norm_name(a) != norm_name(alias)]
            db.execute("UPDATE entities SET aliases_json=?, updated_at=? WHERE id=?",
                       (json.dumps(kept, ensure_ascii=False), now, ent["id"]))
            # 名字维度黑名单：下一条同名记录不再被向量归并/挂靠回来（C2）。
            # 注：该称呼已有的提及仍留在目标实体上——这是刻意的：提及是"这个名字出现过"的
            # 证据，撤销的是"它们算同一个"的推断，不是抹掉证据（R1）。
            if alias:
                add_rejected_alias(ent["id"], alias)
            logx.log("圈子：撤销自动合并",
                     f"《{ent['name']}》摘掉了别名「{alias}」，以后不再自动并回")
    elif action == "fact_update":
        eid = d.get("entity_id")
        if d.get("new_fact_id"):
            db.execute("DELETE FROM entity_facts WHERE id=?", (d["new_fact_id"],))
        if d.get("old_fact_id"):
            db.execute("UPDATE entity_facts SET valid_to=NULL WHERE id=?", (d["old_fact_id"],))
            # 更新链 A→B→C 中撤销中段（如撤 A→B）：旧值复活后，同 predicate 可能还有
            # 更晚的活跃值（C 由另一条日志产生）。一并封存它们，保证同 predicate 至多一个活跃值；
            # C 自己的 auto_log 仍在，用户可单独再撤。
            if eid and d.get("predicate"):
                db.execute(
                    "UPDATE entity_facts SET valid_to=? WHERE entity_id=? AND predicate=? "
                    "AND valid_to IS NULL AND id != ?",
                    (now[:10], eid, d["predicate"], d["old_fact_id"]),
                )
        if eid and "old_profile" in d:
            db.execute("UPDATE entities SET profile=?, updated_at=? WHERE id=?",
                       (d.get("old_profile") or "", now, eid))
        logx.log("圈子：撤销事实更新", f"《{_entity_name(eid)}》的「{d.get('predicate') or ''}」已还原")
    elif action == "retype":
        eid = d.get("entity_id")
        ent = get_entity(eid) if eid else None
        old_type = d.get("old_type")
        if ent is not None and old_type in ("person", "place", "event"):
            clash = db.q1("SELECT id FROM entities WHERE type=? AND name=? AND id != ?",
                          (old_type, ent["name"], eid))
            if clash is None:
                db.execute("UPDATE entities SET type=?, updated_at=? WHERE id=?",
                           (old_type, now, eid))
                logx.log("圈子：撤销类型纠正",
                         f"《{ent['name']}》回到「{TYPE_LABEL.get(old_type, old_type)}」")
            else:
                logx.log("圈子：撤销类型纠正未执行",
                         f"《{ent['name']}》旧类型下已有同名档案")
    else:
        raise ValueError("unsupported")

    db.execute("UPDATE circle_auto_log SET undone=1 WHERE id=?", (aid,))
    return {"ok": True, "action": action}


# ---------------- 历史追认（v3 阈值重估 + 类型复核 + draft 加速消化） ----------------

RECOGNITION_MARKER = "circle_recognition_v3"  # 带版本号：将来逻辑升级可再跑一轮


def recognition_needed() -> bool:
    return db.get_setting(RECOGNITION_MARKER) != "done"


def run_recognition() -> dict:
    """历史追认（幂等，可重复跑）。需要 AI（fast 判类型、strong 综合 draft）；
    未配置时优雅跳过、不写 marker，下一轮 tick 再试。
    a) active 实体按新阈值重估：达标 → confirmed + auto_log('confirm', 理由「历史追认」）；
       auto_confirm_blocked=1（用户撤销过的）一律不动；不达标的保持 active。
    b) 类型复核：fast 批量（20 个/批）核对 confirmed+active+draft 实体类型（draft 空壳也
       复核——真实库里「产业部」「梨树」这类错标正是 draft），不符且无同名冲突
       → 改 type + auto_log('retype')；拿不准（unknown）不动。
    c) draft 加速消化：mention_count≥2 且 needs_review=0 的 draft 逐批综合（每轮 10 个，
       跑完为止），让该有关系的实体长出关系。
    """
    result = {"confirmed": 0, "retyped": 0, "synthesized": 0}

    if not (ai_mod.fast_configured() or ai_mod.strong_configured()):
        # AI 未配置：整体优雅跳过、不写 marker（下轮 tick 再试）；每天最多记一行日志
        if db.get_setting("circle_recognition_log_day") != db.now_iso()[:10]:
            db.set_setting("circle_recognition_log_day", db.now_iso()[:10])
            logx.log("圈子追认暂缓", "AI 未配置，配好接口后自动补跑")
        return result

    # a) 阈值追认：纯本地判定，不需要 AI 调用；但尊重「自动确认」总开关
    if auto_confirm_on():
        for r in db.q("SELECT * FROM entities WHERE status='active' AND auto_confirm_blocked=0"):
            if (r["confidence"] or 0) >= confirm_threshold(r["type"]):
                _auto_log("confirm", {"entity_id": r["id"], "old_status": "active",
                                      "old_confidence": r["confidence"],
                                      "confidence": r["confidence"],
                                      "mention_count": r["mention_count"],
                                      "basis": "历史追认"})
                db.execute("UPDATE entities SET status='confirmed', updated_at=? WHERE id=?",
                           (db.now_iso(), r["id"]))
                result["confirmed"] += 1
        if result["confirmed"]:
            logx.log("圈子历史追认", f"按新阈值把 {result['confirmed']} 份档案转为已确认")
    else:
        logx.log("圈子历史追认", "自动确认开关已关，阈值追认跳过")

    # b) 类型复核（fast）
    if ai_mod.fast_configured() or ai_mod.strong_configured():
        rows = db.q("SELECT id, type, name, profile FROM entities "
                    "WHERE status IN ('confirmed','active','draft') ORDER BY mention_count DESC")
        for i in range(0, len(rows), 20):
            batch = rows[i:i + 20]
            items = [{"name": r["name"], "profile": r["profile"]} for r in batch]
            try:
                verdicts = ai_mod.judge_entity_types(items)
            except Exception:
                break  # AI 挂了本轮停，剩下的下轮再试
            for idx, new_type in verdicts.items():
                r = batch[idx]
                if new_type == r["type"]:
                    continue
                clash = db.q1("SELECT id FROM entities WHERE type=? AND name=? AND id != ?",
                              (new_type, r["name"], r["id"]))
                if clash is not None:
                    continue  # 目标类型下有同名档案，交给合并流程
                _auto_log("retype", {"entity_id": r["id"],
                                     "old_type": r["type"], "new_type": new_type})
                db.execute("UPDATE entities SET type=?, updated_at=? WHERE id=?",
                           (new_type, db.now_iso(), r["id"]))
                result["retyped"] += 1
                logx.log("圈子：纠正类型",
                         f"《{r['name']}》{TYPE_LABEL.get(r['type'])}→{TYPE_LABEL.get(new_type)}")

    # c) draft 加速消化（strong）
    if ai_mod.strong_configured():
        for _ in range(50):  # 上限 500 个，防意外死循环
            try:
                n = review_drafts(10)
            except Exception:
                break
            if n == 0:
                break
            result["synthesized"] += n
        if result["synthesized"]:
            logx.log("圈子：draft 加速消化", f"综合了 {result['synthesized']} 份搁置档案")

    db.set_setting(RECOGNITION_MARKER, "done")
    total = result["confirmed"] + result["retyped"] + result["synthesized"]
    logx.log("圈子追认完成", f"追认 {result['confirmed']}、纠类型 {result['retyped']}、"
                           f"综合 {result['synthesized']}")
    if total:
        db.push_notice(f"小满把圈子又理顺了一遍：追认 {result['confirmed']} 份、"
                       f"纠正类型 {result['retyped']} 个")
    return result
