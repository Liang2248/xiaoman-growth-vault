"""按历史日期读取圈子事实的轻量快照。

这是问答层可选的上下文提供器：只读取 ``valid_from <= as_of`` 且尚未
在该日期前失效的事实/关系。没有来源记录的条目仍可返回，但会被标为
``source_missing``，调用方应使用保守措辞而不是把它写成确定事实。
"""
from __future__ import annotations

from datetime import date

from . import db


def _day(value: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        raise ValueError("as_of 必须是 YYYY-MM-DD")


def _active(row, as_of: str) -> bool:
    start = str(row["valid_from"] or "")[:10]
    end = str(row["valid_to"] or "")[:10]
    # 缺少起始日的旧数据不伪造历史：仅在有明确来源时按创建日兜底。
    if not start:
        start = str(row["created_at"] or "")[:10]
    if start and start > as_of:
        return False
    # valid_to 是失效日，按左闭右开处理。
    return not end or as_of < end


def _source_missing(r, source_id: str | None, as_of: str) -> bool:
    if not source_id or not r["evidence_id"] or r["evidence_deleted_at"]:
        return True
    source_day = str(r["evidence_occurred_at"] or "")[:10]
    return bool(source_day and source_day > as_of)


def _fact_dict(r, as_of: str) -> dict:
    return {
        "id": r["id"], "entity_id": r["entity_id"],
        "entity_name": r["entity_name"],
        "predicate": r["predicate"], "object": r["object_text"],
        "valid_from": (r["valid_from"] or "")[:10],
        "valid_to": (r["valid_to"] or "")[:10] or None,
        "source_entry_id": r["source_entry_id"],
        "certainty": r["certainty"] or "inferred",
        "source_missing": _source_missing(r, r["source_entry_id"], as_of),
    }


def _relation_dict(r, as_of: str) -> dict:
    return {
        "id": r["id"], "source_id": r["source_id"],
        "source_name": r["source_name"],
        "target_id": r["target_id"], "target_name": r["target_name"],
        "label": r["label"],
        "valid_from": (r["valid_from"] or "")[:10],
        "valid_to": (r["valid_to"] or "")[:10] or None,
        "entry_id": r["entry_id"], "snippet": r["snippet"] or "",
        "certainty": r["certainty"] or "inferred",
        "source_missing": _source_missing(r, r["entry_id"], as_of),
    }


def snapshot(as_of: str, entity_id: str | None = None) -> dict:
    """返回某日有效的实体事实与关系，并附保守性元数据。"""
    day = _day(as_of)
    facts = db.q(
        "SELECT f.*, e.name AS entity_name, se.id AS evidence_id, "
        "se.occurred_at AS evidence_occurred_at, se.deleted_at AS evidence_deleted_at, "
        "se.exclude_from_ai AS evidence_excluded "
        "FROM entity_facts f "
        "JOIN entities e ON e.id=f.entity_id "
        "LEFT JOIN entries se ON se.id=f.source_entry_id "
        "WHERE e.status != 'rejected' "
        "ORDER BY f.valid_from, f.created_at"
    )
    relations = db.q(
        "SELECT r.*, s.name AS source_name, t.name AS target_name, "
        "se.id AS evidence_id, se.occurred_at AS evidence_occurred_at, "
        "se.deleted_at AS evidence_deleted_at, se.exclude_from_ai AS evidence_excluded "
        "FROM entity_relations r JOIN entities s ON s.id=r.source_id "
        "JOIN entities t ON t.id=r.target_id "
        "LEFT JOIN entries se ON se.id=r.entry_id "
        "WHERE s.status != 'rejected' AND t.status != 'rejected' "
        "ORDER BY r.valid_from, r.created_at"
    )
    if entity_id:
        facts = [r for r in facts if r["entity_id"] == entity_id]
        relations = [r for r in relations if r["source_id"] == entity_id or r["target_id"] == entity_id]
    # 用户明确设为“不让 AI 分析”的来源不能通过结构化事实旁路进入问答。
    fs = [_fact_dict(r, day) for r in facts
          if _active(r, day) and not (r["source_entry_id"] and r["evidence_excluded"])]
    rs = [_relation_dict(r, day) for r in relations
          if (_active(r, day) and r["status"] in ("active", "confirmed", "ended")
              and not (r["entry_id"] and r["evidence_excluded"]))]
    return {
        "as_of": day, "facts": fs, "relations": rs,
        "source_missing_count": sum(x["source_missing"] for x in fs + rs),
        "conservative": any(x["source_missing"] for x in fs + rs),
    }


def prompt_context(as_of: str, entity_id: str | None = None) -> str:
    """将快照压缩成可直接拼入 AI 提示词的上下文。"""
    data = snapshot(as_of, entity_id)
    lines = [f"截至 {data['as_of']} 的历史事实快照（不是当前状态）："]
    for f in data["facts"]:
        mark = "（来源缺失，仅作线索）" if f["source_missing"] else f"（来源记录 {f['source_entry_id']}）"
        lines.append(f"- 事实：《{f['entity_name']}》{f['predicate']}={f['object']}；{mark}")
    for r in data["relations"]:
        mark = "（来源缺失，仅作线索）" if r["source_missing"] else f"（来源记录 {r['entry_id']}）"
        lines.append(f"- 关系：《{r['source_name']}》 -[{r['label']}]-> 《{r['target_name']}》；{mark}")
    if len(lines) == 1:
        lines.append("- 没有找到在该日期已经生效且仍有效的结构化事实；不要用今天的状态倒推过去。")
    return "\n".join(lines)
