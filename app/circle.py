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
# 关系图明细清单的服务端上限（A9-11）：图本身（nodes/links）不受限，只有 relations
# 明细截断，并如实回 relations_total / relations_truncated 供前端提示。
GRAPH_RELATIONS_CAP = 2000

_backfill_lock = threading.Lock()

# certainty → 置信度地板：explicit 原文明写直通 0.95；inferred 待定；ambiguous 保持低位
CERTAINTY_FLOOR = {"explicit": 0.95, "inferred": 0.6, "ambiguous": 0.4}
CERTAINTY_RANK = {"ambiguous": 0, "inferred": 1, "explicit": 2}


def enabled() -> bool:
    """圈子总开关（默认开）。关闭时：不提取、不综合、不复查、不注入、不回填。"""
    return db.get_setting("circle_enabled", "true") != "false"


def _parse_ts(raw: str) -> datetime | None:
    """宽容解析设置项里的时间戳；无法解析或**无时区**（naive）一律返回 None。

    A6b-11：`now` 是 aware（`tick` 传 `datetime.now().astimezone()`），与 naive 值相减抛
    `TypeError`，而调用点原先只捕 `ValueError` → 异常冒泡到 tick 的静默兜底，
    合并提案流程**永久停摆**。naive/脏值一律当"无法判断"，由调用方按"已过期，继续执行"
    处理（安全方向是继续做，而不是永久跳过）。
    """
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo is not None else None


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
    M4：仅 pending 可答；已答/已忽略重复答抛 bad state（主站单答/批量/agent 同获益）。
    A3-26：选项指向的档案已被合并/删除时抛 stale，不再"什么都不做却回成功"——
    用户会以为记录被整理过，实际数据零变化（related_json 无外键，实体合并后 ID 会变陈旧）。"""
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
            if target is None:
                # A3-26：选中的候选档案已被合并/删除 → 静默 no-op 会让用户以为
                # "这个称呼已经归好了"。抛错让路由回 409 并提示刷新提问列表，
                # 且**不**把问题置 answered，用户仍可在刷新后的新卡上作答。
                raise ValueError("stale")
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
                    # A6b-09：原先直接 `DELETE FROM entities` —— 这是 A3-01 同根因的
                    # 第二条触发路径。entity_facts 是 ON DELETE CASCADE + 外键已开，
                    # 孤儿档案的历史事实会被静默级联删除；而 entity_relations.target_id
                    # 没有外键（db.py），以它为 target 的边会变成悬空边（图上消失、
                    # 详情页显示"（已删除）"，两种界面给出不同事实）。
                    # 既然用户刚确认"这个称呼就是《target》"，孤儿的事实/关系本就
                    # 该归到 target：迁移后收口，再删空壳。
                    with db.locked() as c:
                        _migrate_child_facts(c, orphan["id"], target["id"])
                        c.execute("UPDATE entity_relations SET source_id=? WHERE source_id=?",
                                  (target["id"], orphan["id"]))
                        c.execute("UPDATE entity_relations SET target_id=? WHERE target_id=?",
                                  (target["id"], orphan["id"]))
                        c.execute("DELETE FROM entity_relations WHERE source_id=target_id")
                        c.execute("DELETE FROM entities WHERE id=?", (orphan["id"],))
                        c.commit()
                    logx.log("圈子：清理空壳档案",
                             f"《{orphan['name']}》已并入《{target['name']}》，事实与关系一并迁移")
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


def _migrate_child_facts(c, from_id: str, into_id: str) -> dict:
    """把 from 档案的时序事实迁到 into，并做同 predicate 去重收口（须在同一事务内调用）。

    A3-01：`entity_facts.entity_id` 是 `REFERENCES entities(id) ON DELETE CASCADE`，
    而连接开着 `PRAGMA foreign_keys=ON`（db.py）。任何 `DELETE FROM entities` 都会
    **静默级联删除**该档案的全部时序事实（不可恢复），因此删档案前必须先迁移。

    迁移后必须收口：两个档案各自可能都有「任职于/居住在」这类 predicate，直接迁移
    会造出同一 predicate 两条 `valid_to IS NULL` 的活跃值，破坏 temporal.py 依赖的
    「同 predicate 至多一个活跃值」不变式。按 (valid_from, created_at) 保留最新一条，
    其余封存；封存日取保留者的生效日，并保证不早于被封存者自身的生效日（防倒挂）。

    返回快照 `{"facts": 迁移条数, "sealed": 收口封存条数}`，供调用方写审计日志。
    """
    if not from_id or not into_id or from_id == into_id:
        return {"facts": 0, "sealed": 0}
    row = c.execute("SELECT COUNT(*) AS n FROM entity_facts WHERE entity_id=?",
                    (from_id,)).fetchone()
    moved = row["n"] if row is not None else 0
    if not moved:
        return {"facts": 0, "sealed": 0}
    c.execute("UPDATE entity_facts SET entity_id=? WHERE entity_id=?", (into_id, from_id))
    today = db.now_iso()[:10]
    active = c.execute(
        "SELECT id, predicate, valid_from, created_at FROM entity_facts "
        "WHERE entity_id=? AND valid_to IS NULL "
        "ORDER BY predicate, valid_from DESC, created_at DESC",
        (into_id,),
    ).fetchall()
    keep: dict[str, str] = {}
    sealed = 0
    for f in active:
        pred = f["predicate"]
        if pred not in keep:
            keep[pred] = str(f["valid_from"] or "")
            continue
        own_from = str(f["valid_from"] or "")
        end = keep[pred] or today
        if own_from and end < own_from:
            end = own_from
        c.execute("UPDATE entity_facts SET valid_to=? WHERE id=?", (end, f["id"]))
        sealed += 1
    return {"facts": moved, "sealed": sealed}


def _apply_facts(entity_id: str, entity_name: str, facts: list, old_profile: str) -> None:
    """时序事实落库（双时态）：同 predicate 的活跃 fact 与新值不同（归一化比较）→ 旧值
    用**新值的实际生效日**封存，新 fact 生效并写可撤销日志；没有活跃 fact 则直接建档。
    ambiguous 的变化不封存，只 needs_review 等用户过目；confirmed 实体不降级。

    A0-16/A0-17（写入端）：valid_from 与关系侧同口径做 ISO 校验；封存日不再一律写"今天"。
    """
    today = db.now_iso()[:10]
    src = db.q1("SELECT entry_id FROM entity_mentions WHERE entity_id=? "
                "ORDER BY created_at DESC LIMIT 1", (entity_id,))
    for f in facts[:8]:
        predicate = str(f.get("predicate") or "").strip()[:20]
        obj = str(f.get("object") or "").strip()[:60]
        if not (predicate and obj):
            continue
        certainty = f.get("certainty") if f.get("certainty") in ("explicit", "inferred", "ambiguous") else "inferred"
        # A0-17：复用关系侧的 ISO 校验（原先只做 [:10] 截断不校验，"2026-3-5"/"去年"这类
        # 非规范值会被字符串比较永久判为"未生效"，事实从所有快照里静默消失）。
        valid_from = _relation_day(f.get("valid_from"), today)
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
            # A0-16：封存日取"新值的实际生效日"而不是一律"今天"。旧写法在用历史记录回填时
            # 会让旧值 [..., valid_to=今天] 与新值 [valid_from=历史日, ...] 区间重叠十几个
            # 月，同一 predicate 在同一时点同时"有效"，模型会拿到两个互相矛盾的事实。
            # 关系侧的 end_day = valid_to or evidence_day or now[:10]（circle.py:799）已是
            # 正确口径，此处与之对齐；新值没有可信生效日（回退 today）或新值更早时才退回今天。
            old_from = str(active["valid_from"] or "")[:10]
            end = valid_from if (old_from and valid_from > old_from) else today
            if old_from and end < old_from:
                end = old_from  # 防倒挂：封存日不得早于旧值自身的生效日
            db.execute("UPDATE entity_facts SET valid_to=? WHERE id=?", (end, active["id"]))
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
    """把新认识的实体名追加进 circle_notice 队列（前端取走即清）。

    A3-16：读-改-写放进同一临界区。原先 `get_setting` → `set_setting` 两步之间锁已释放，
    与 main.py 的 `circle_notice` 路由（F3 已改成单事务取走+清空）交错会**永久丢通知**：
    路由读到 [A] → 本函数读 [A] 追加 B 写回 [A,B] → 路由写 "" ⇒ B 消失。
    """
    if not names:
        return
    with db.locked() as c:
        row = c.execute("SELECT value FROM settings WHERE key='circle_notice'").fetchone()
        raw = row["value"] if row is not None else ""
        pending = []
        if raw:
            try:
                pending = json.loads(raw).get("names") or []
            except (ValueError, AttributeError):
                pending = []
        for n in names:
            if n not in pending:
                pending.append(n)
        text = f"小满新认识了 {len(pending)} 位朋友：" + "".join(f"《{n}》" for n in pending)
        c.execute(
            "INSERT INTO settings(key, value) VALUES('circle_notice', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps({"text": text, "names": pending, "ts": db.now_iso()},
                        ensure_ascii=False),),
        )


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

def graph_data(relations_cap: int = GRAPH_RELATIONS_CAP) -> dict:
    """关系图只展示当前有效关系，附带清单所需的已结束历史。

    draft 是尚未坐实的候选档案，不能参与关系图，否则会把猜测当成事实，
    也会让节点和边数量随扫描过程膨胀。前端若需要查看 draft，仍可在实体列表里筛选。

    A9-11：补服务端上限。原先一次返回全部节点与全部关系、无分页无上限，档案与关系到千级
    时前端要把每条关系都塞进 DOM（`relationsGroupHtml` 对每条生成两个 chip），是明确的内存
    与主线程压力。这里只对 `relations` 明细设上限（默认 2000，远超正常使用规模），并把
    真实总数与是否截断如实回给前端；`nodes`/`links`（图本身）不受限，避免图形缺节点。
    """
    rows = db.q(
        "SELECT id, name, type, status, mention_count FROM entities "
        "WHERE status IN ('confirmed','active')"
    )
    node_ids = {r["id"] for r in rows}
    relations = []
    visual_pairs: dict[tuple[str, str], dict] = {}
    degree: dict[str, int] = {}
    truncated = False
    # draft 关系本身也不画；同时再次校验两端节点，避免历史脏数据漏进图
    # （entity_relations.target_id 没有外键，删实体后可能留下悬空边）。
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
    active_total = len(relations)
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
    relations_total = len(relations)
    if relations_total > relations_cap:
        # 截断发生在明细清单上；图（nodes/links）不受影响，前端可据 total 提示"仅显示前 N 条"
        relations = relations[:relations_cap]
        truncated = True
    nodes = [
        {"id": r["id"], "name": r["name"], "type": r["type"], "status": r["status"],
         "mention_count": r["mention_count"], "degree": degree.get(r["id"], 0)}
        for r in rows
    ]
    return {"nodes": nodes, "links": links, "relations": relations,
            "relations_total": relations_total, "relations_truncated": truncated,
            "active_relations_total": active_total}


# ---------------- 合并 ----------------

def merge_entities(from_id: str, into_id: str) -> None:
    """把 from 档案并入 into（提及/关系/时序事实/别名随迁），并写一条可审计日志。

    A3-01：原先只迁移 mentions 与 relations 就 `DELETE FROM entities`，源档案的全部
    entity_facts 被外键级联静默删除（不可恢复）——这是本模块最严重的单点缺陷。
    现在在同一事务内、DELETE 之前先迁移事实并做同 predicate 去重收口。
    """
    # 纵深防御：路由层已挡 from==into，但 merge_proposals 的 id 列无外键/约束，
    # 脏数据会让下面的 DELETE 把档案自己删掉（A6b-28）。
    if from_id == into_id:
        raise ValueError("same entity")
    src, dst = get_entity(from_id), get_entity(into_id)
    if src is None or dst is None:
        raise ValueError("not found")
    # 合并前的快照：facts 条数与收口结果都要进审计日志（用户要能看出这次合并动了什么）
    facts_snapshot = db.q1(
        "SELECT COUNT(*) AS n FROM entity_facts WHERE entity_id=?", (from_id,))["n"]
    mentions_snapshot = db.q1(
        "SELECT COUNT(*) AS n FROM entity_mentions WHERE entity_id=?", (from_id,))["n"]
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
        # 时序事实迁移 + 同 predicate 去重收口（必须在 DELETE 之前，否则被级联删除）
        fact_stat = _migrate_child_facts(c, from_id, into_id)
        # 迁移后可能产生自环（自己指向自己），清掉
        c.execute("DELETE FROM entity_relations WHERE source_id=target_id")
        # 迁移后残留的幽灵提案：其余 pending 提案里指向已删 from 的行永远无法接受
        # （merge_entities 抛 not found → 路由 404，且永久卡 pending，A6b-28）。
        # 标记为 stale 而不是删除，保留"曾经提过这一对"的可读状态。
        c.execute("UPDATE merge_proposals SET status='stale' "
                  "WHERE status='pending' AND (from_id=? OR into_id=?)", (from_id, from_id))
        # 黑名单也要跟着档案走：rejected_pairs 是裸 TEXT 无外键，指向已删 from 的行会
        # 永久占位（且 a_id/b_id 必须保持「排序后成对」的不变式，见 _pair_key）。
        # 用户否决的是"X 与 from 不是同一个"；from 并入 into 后该判断应记在 into 名下，
        # 才能继续阻止"再把 X 并进这一簇"。重排键值后按主键幂等写回，再删旧行。
        dead_pairs = c.execute(
            "SELECT a_id, b_id FROM rejected_pairs WHERE a_id=? OR b_id=?",
            (from_id, from_id),
        ).fetchall()
        for p in dead_pairs:
            a = into_id if p["a_id"] == from_id else p["a_id"]
            b = into_id if p["b_id"] == from_id else p["b_id"]
            if a == b:
                continue  # 自配对无意义
            a, b = _pair_key(a, b)
            c.execute("INSERT OR IGNORE INTO rejected_pairs(a_id, b_id, created_at) "
                      "VALUES(?,?,?)", (a, b, db.now_iso()))
        c.execute("DELETE FROM rejected_pairs WHERE a_id=? OR b_id=?", (from_id, from_id))
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
    # A6b-03/A6b-04：顺手修正两处既有缺口——
    # (1) first_seen 原写法 `MIN(COALESCE(first_seen, ?), ?)` 在 src 侧为 NULL/'' 时
    #     SQLite 标量 min 返回 NULL，会把 into 的 first_seen 抹空；改为只在 src 有
    #     合法日期时才取 min（与 last_seen 的 COALESCE 风格对称）。
    # (2) 合并后 into 的向量是按旧名字算的，别名并入后已成脏值；置 NULL 让语义查重
    #     回到"未建向量"状态（由回填任务重算），避免用脏向量做 0.88 静默归并判定。
    src_first = str(src["first_seen"] or "")[:10]
    if src_first:
        db.execute(
            "UPDATE entities SET aliases_json=?, mention_count=?, "
            "first_seen=MIN(COALESCE(NULLIF(first_seen, ''), ?), ?), "
            "last_seen=COALESCE(?, last_seen), vector_json=NULL, updated_at=? WHERE id=?",
            (json.dumps(aliases, ensure_ascii=False), count, src_first, src_first,
             last_day, db.now_iso(), into_id),
        )
    else:
        db.execute(
            "UPDATE entities SET aliases_json=?, mention_count=?, "
            "last_seen=COALESCE(?, last_seen), vector_json=NULL, updated_at=? WHERE id=?",
            (json.dumps(aliases, ensure_ascii=False), count, last_day, db.now_iso(), into_id),
        )
    # A3-01/A3-19：补写审计日志，让合并在「小满最近自己拿的主意」里可见、可追溯，
    # 且 detail 里带上 from 的 id/name/facts 数量快照（原先手动合并完全无痕）。
    # kind='entity_merge' 与自动路径的 'alias_attach' 区分开：后者可被 undo 还原别名，
    # 前者是物理合并、无法拆分回滚（undo 会明确回 unsupported 而不是假装成功）。
    _auto_log("merge", {
        "kind": "entity_merge", "from_id": from_id, "from_name": src["name"],
        "into_id": into_id, "into_name": dst["name"], "into_type": dst["type"],
        "facts": facts_snapshot, "facts_moved": fact_stat["facts"],
        "facts_sealed": fact_stat["sealed"], "mentions": mentions_snapshot,
        "reason": "手动/提案合并",
    })
    extra = f"，迁移事实 {fact_stat['facts']} 条" if fact_stat["facts"] else ""
    if fact_stat["sealed"]:
        extra += f"（同项封存 {fact_stat['sealed']} 条）"
    logx.log("圈子：合并档案",
             f"《{src['name']}》并入《{dst['name']}》：提及 {mentions_snapshot} 条{extra}")


# ---------------- 合并提案 ----------------

PROPOSE_INTERVAL = timedelta(hours=3)
PROPOSE_MAX_PAIRS = 5


def _shared_substring(a: str, b: str, min_len: int = 3) -> bool:
    """规范化名有共同子串（含互相包含）。

    A6b-19：min_len 从 2 提到 3。中文姓名普遍共用「小/老/大/阿」等前缀与常见姓氏用字，
    2 字连续相同极易假命中（"小王" vs "小汪"、"张伟" vs "张伟丽"），而每轮只有
    PROPOSE_MAX_PAIRS=5 次 AI 判定预算，假候选会把预算全部吃掉，真正该合并的一对
    当轮拿不到机会。互相包含仍照旧命中（"张伟"⊂"张伟丽" 走上面那个分支）。
    """
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
    """每轮 tick 调用；内部节流 ≥3 小时。返回新建提案数。

    A6b-06/A6b-11：节流时间戳移到所有可失败步骤**之后**——原先在候选查询与 AI 判定之前就写，
    一次 `judge_same_entity` 异常（`break`）便把 3 小时的机会整段吃掉；且
    `datetime.fromisoformat` 的裸 `except ValueError` 捕不到 aware−naive 的 TypeError，
    脏值会让合并提案流程**永久停摆**。改用 _parse_ts：无法判断就当"已过期，继续执行"。
    """
    if not enabled() or not ai_mod.fast_configured() and not ai_mod.strong_configured():
        return 0
    now = now or datetime.now().astimezone()
    last = _parse_ts(db.get_setting("circle_last_propose_at"))
    if last is not None:
        try:
            if (now - last) < PROPOSE_INTERVAL:
                return 0
        except TypeError:
            pass  # 解析不出可比时区：按"已过期"继续，不让脏值卡死提案流程
    # 注意：这里**不**写节流键；只有本轮确实跑完（没有 AI 中断）才写（见函数末尾）。

    rows = db.q(
        "SELECT * FROM entities WHERE status IN ('draft','active','confirmed') "
        "AND mention_count > 0 ORDER BY mention_count DESC LIMIT 30"
    )
    if len(rows) < 2:
        db.set_setting("circle_last_propose_at", now.isoformat(timespec="seconds"))
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
    interrupted = False
    for i in range(len(rows)):
        if judged >= PROPOSE_MAX_PAIRS or interrupted:
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
                # AI 挂了本轮停。原写法只 break 内层 j 循环，外层继续对下一个 i 再调 AI
                # （同一故障下反复失败），标 interrupted 让外层也停，且不消费节流键。
                interrupted = True
                break
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
    if interrupted:
        logx.log("圈子：合并提案本轮中断", f"AI 判定失败，已建 {created} 条，稍后重试（不消耗节流窗口）")
    else:
        # 本轮完整跑完才写节流键：否则一次 AI 抖动就吃掉 3 小时机会（A6b-06）。
        # （中断时故意不写，让下个 tick 立刻重试；_proposal_exists 保证不会重复建提案。）
        db.set_setting("circle_last_propose_at", now.isoformat(timespec="seconds"))
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
        # A3-17 同源：档案已被合并/删除时，原先静默 no-op 后照样置 undone=1 并回"成功"，
        # 用户既看不出没生效，也永久失去重试机会。同样在置位前抛错。
        if ent is None:
            raise ValueError("blocked")
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
        # alias_attach 是唯一可撤销的 merge 形态（称呼挂靠，摘别名即可）；kind=
        # 'entity_merge' 是实体级物理合并，源档案及其从表行已被删除，无法拆分回滚
        # （A6b-01/A3-19）。明确抛 unsupported，让路由如实回「这条记录无法撤销」，
        # 而不是把 undone 置 1 假装成功、永久夺走用户的知情权。
        if d.get("kind") != "alias_attach":
            raise ValueError("unsupported")
        ent = get_entity(d.get("entity_id") or "")
        alias = d.get("alias") or ""
        if ent is None:
            raise ValueError("blocked")  # 目标档案已不存在：置位前抛错，不写 undone
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
        # A3-17：撤销 retype 时若旧类型下已有同名档案，原先只写一行日志「未执行」，
        # 却继续落到下面的 undone=1 并 return 成功——接口明确告诉用户"撤销成功"，
        # 实际什么都没撤销，且日志已置位，用户再点一次得到 400「已经撤销过了」，
        # 永久失去重试机会。改为在置位前抛 blocked（路由按 ValueError 文案分流）。
        if ent is None:
            raise ValueError("blocked")  # 档案已被合并/删除：没有任何可撤销的对象
        if old_type not in ("person", "place", "event"):
            raise ValueError("bad snapshot")
        clash = db.q1("SELECT id FROM entities WHERE type=? AND name=? AND id != ?",
                      (old_type, ent["name"], eid))
        if clash is not None:
            # 保持 undone=0：用户把同名档案处理掉之后还能再撤一次
            logx.log("圈子：撤销类型纠正未执行",
                     f"《{ent['name']}》旧类型下已有同名档案，请先合并或改名")
            raise ValueError("blocked")
        db.execute("UPDATE entities SET type=?, updated_at=? WHERE id=?",
                   (old_type, now, eid))
        logx.log("圈子：撤销类型纠正",
                 f"《{ent['name']}》回到「{TYPE_LABEL.get(old_type, old_type)}」")
    else:
        raise ValueError("unsupported")

    db.execute("UPDATE circle_auto_log SET undone=1 WHERE id=?", (aid,))
    return {"ok": True, "action": action}


# ---------------- 历史追认（v3 阈值重估 + 类型复核 + draft 加速消化） ----------------

RECOGNITION_MARKER = "circle_recognition_v3"  # 带版本号：将来逻辑升级可再跑一轮
# A6b-05：中途 AI 失败不再写 marker（原先无条件写 done，一次失败即"永久完成"，
# 剩余实体永远停在未复核状态且用户无感知）；退避 30 分钟再试，连续失败 5 次才认输。
RECOGNITION_RETRY_KEY = "circle_recognition_retry_at"
RECOGNITION_RETRY = timedelta(minutes=30)
RECOGNITION_MAX_FAILS = 5


def recognition_needed() -> bool:
    """是否还需要跑历史追认（marker 未 done）。失败后退避期内不再重复起线程。"""
    if db.get_setting(RECOGNITION_MARKER) == "done":
        return False
    retry_at = _parse_ts(db.get_setting(RECOGNITION_RETRY_KEY))
    if retry_at is not None and datetime.now().astimezone() < retry_at:
        return False
    return True


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

    A6b-05：marker 改为**有条件**写入——中途 AI 失败（`break`）时保持未完成并退避重试，
    否则一次失败就等于"永久完成"，剩余实体永远停在未复核状态且用户完全无感知。
    """
    result = {"confirmed": 0, "retyped": 0, "synthesized": 0}
    interrupted = False

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
                interrupted = True
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
                interrupted = True
                break
            if n == 0:
                break
            result["synthesized"] += n
        if result["synthesized"]:
            logx.log("圈子：draft 加速消化", f"综合了 {result['synthesized']} 份搁置档案")

    if interrupted:
        # 不写 done：保持"未完成"，退避 30 分钟后再试（避免 AI 长期不可用时每次启动都空跑）。
        # 连续失败达到上限才认输并写明"部分条目未复核"，不让它无声地永远重试。
        fails = 0
        try:
            fails = int(db.get_setting("circle_recognition_fails") or 0)
        except ValueError:
            fails = 0
        fails += 1
        total = result["confirmed"] + result["retyped"] + result["synthesized"]
        if fails >= RECOGNITION_MAX_FAILS:
            db.set_setting(RECOGNITION_MARKER, "done")
            db.set_setting("circle_recognition_fails", "0")
            db.set_setting(RECOGNITION_RETRY_KEY, "")
            logx.log("圈子追认未完成",
                     f"AI 连续 {fails} 次中途失败，已放弃剩余条目（本轮完成 {total} 项）")
            db.push_notice("圈子历史追认只跑完了一部分：AI 多次中断，可在圈子页手动再试")
        else:
            db.set_setting("circle_recognition_fails", str(fails))
            nxt = datetime.now().astimezone() + RECOGNITION_RETRY
            db.set_setting(RECOGNITION_RETRY_KEY, nxt.isoformat(timespec="seconds"))
            logx.log("圈子追认未完成",
                     f"AI 中途失败，{RECOGNITION_RETRY.seconds // 60} 分钟后重试"
                     f"（第 {fails}/{RECOGNITION_MAX_FAILS} 次，本轮完成 {total} 项）")
        return result

    db.set_setting(RECOGNITION_MARKER, "done")
    db.set_setting("circle_recognition_fails", "0")
    db.set_setting(RECOGNITION_RETRY_KEY, "")
    total = result["confirmed"] + result["retyped"] + result["synthesized"]
    logx.log("圈子追认完成", f"追认 {result['confirmed']}、纠类型 {result['retyped']}、"
                           f"综合 {result['synthesized']}")
    if total:
        db.push_notice(f"小满把圈子又理顺了一遍：追认 {result['confirmed']} 份、"
                       f"纠正类型 {result['retyped']} 个")
    return result
