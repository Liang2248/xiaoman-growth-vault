#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""小满 · MCP 适配器（stdio）

让你的 Agent 平台把「小满 · 成长证据库」直接挂载为一组工具。
协议：MCP over stdio（换行分隔的 JSON-RPC 2.0 消息）。

用法（在 Agent 平台的 MCP 配置里）：
    command: <项目目录>/.venv/Scripts/python.exe
    args: ["<项目目录>/mcp_server.py"]
    env:
      XIAOMAN_URL   默认 http://127.0.0.1:52122
      XIAOMAN_TOKEN 设置页 → Agent 里的 token（必填）

无任何第三方依赖之外的安装（httpx 已在项目环境里）。
"""
from __future__ import annotations

import json
import os
import sys

import httpx

BASE_URL = os.environ.get("XIAOMAN_URL", "http://127.0.0.1:52122").rstrip("/")
TOKEN = os.environ.get("XIAOMAN_TOKEN", "").strip()

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "xiaoman-journal", "version": "1.1.0"}

TOOLS = [
    {
        "name": "journal_list_entries",
        "description": "列出成长记录。可按某一天或时间段过滤，返回标题、摘要、正文、标签、天气地点、附件列表等完整信息。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "某一天，YYYY-MM-DD"},
                "from": {"type": "string", "description": "开始日期，YYYY-MM-DD"},
                "to": {"type": "string", "description": "结束日期，YYYY-MM-DD"},
                "starred": {"type": "boolean", "description": "true=只看星标高光记录"},
                "work": {"type": "boolean", "description": "true=只看工作记录"},
                "category": {"type": "string", "description": "类目 slug 过滤（见 journal_list_categories）"},
                "limit": {"type": "integer", "description": "最多返回条数，默认 20"},
            },
        },
    },
    {
        "name": "journal_get_entry",
        "description": "按 id 获取一条记录的完整内容（含附件与链接）。",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "记录 id"}},
            "required": ["id"],
        },
    },
    {
        "name": "journal_search",
        "description": "全文关键词搜索记录（支持中文单字/短语）。",
        "inputSchema": {
            "type": "object",
            "properties": {"q": {"type": "string", "description": "搜索关键词"}},
            "required": ["q"],
        },
    },
    {
        "name": "journal_ask",
        "description": "用自然语言向成长记录提问（语义检索 + AI 回答，附出处记录 id）。例如：'我上个月踩过哪些坑？'",
        "inputSchema": {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "要问的问题"}},
            "required": ["question"],
        },
    },
    {
        "name": "journal_daily_digest",
        "description": "获取某一天的纯文本日报（当天全部记录的汇总文本）。",
        "inputSchema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
            "required": ["date"],
        },
    },
    {
        "name": "journal_list_reports",
        "description": "列出已生成的 AI 报告（日报/周报/月报）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["daily", "weekly", "monthly"], "description": "可选，按类型过滤"},
                "limit": {"type": "integer", "description": "默认 10"},
            },
        },
    },
    {
        "name": "journal_get_report",
        "description": "按 id 获取一份报告的完整内容（摘要、成就、挑战、经验、建议、事实时间线、思维导图等）。",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "报告 id"}},
            "required": ["id"],
        },
    },
    {
        "name": "journal_generate_report",
        "description": "触发 AI 生成一份报告（较慢，可能需要一两分钟）。周报/月报传该周期内任意一天即可。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["daily", "weekly", "monthly"]},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["type", "date"],
        },
    },
    {
        "name": "journal_get_metrics",
        "description": "获取统计数据：连续记录天数、总记录数、趋势、分类/标签分布、心情曲线等。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "journal_get_growth",
        "description": "获取成长相关内容：type=summary 成长轨迹总结 / roadmap 未来路线 / forecast 情景推演。",
        "inputSchema": {
            "type": "object",
            "properties": {"type": {"type": "string", "enum": ["summary", "roadmap", "forecast"]}},
            "required": ["type"],
        },
    },
    {
        "name": "journal_create_entry",
        "description": "创建一条新记录（只有 content 是必填的，其他都可选）。is_work 点亮/熄灭「工作」标记；旧参数 category 保留兼容（work→is_work=true，life→false，mixed→交给 AI）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "正文"},
                "title": {"type": "string"},
                "category": {"type": "string", "enum": ["work", "life", "mixed"],
                             "description": "旧版分类参数（兼容用，建议改用 is_work）"},
                "is_work": {"type": "boolean", "description": "是否工作相关（true/false；不传=交给小满判断）"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "occurred_at": {"type": "string", "description": "ISO8601，默认现在"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "journal_random_entry",
        "description": "随机抽一条旧记录（回忆用）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "journal_circle_entities",
        "description": "圈子档案：列出人物/地点/事件档案；传 id 则取单份档案详情（含提及证据、关系、事实时间线）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "档案 id（可选；传了取详情）"},
                "type": {"type": "string", "enum": ["person", "place", "event"], "description": "可选，按类型过滤"},
                "status": {"type": "string", "description": "active/draft/confirmed/rejected/all，默认启用中+待确认"},
            },
        },
    },
    {
        "name": "journal_circle_graph",
        "description": "圈子关系图：返回全部已确认/待确认档案节点与关系边（一次取全）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "journal_list_categories",
        "description": "列出记录类目（12 个预设类目 + AI 提议的新类目；每条记录会被 AI 打 1-3 个类目标签）。",
        "inputSchema": {
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["active", "pending", "rejected", "all"],
                                      "description": "默认 active"}},
        },
    },
    {
        "name": "journal_entry_links",
        "description": "查看一条记录的隐性关联提案（小满发现的「可能相关」的工作记录/事件/目标，附理由与置信分）。",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "记录 id"}},
            "required": ["id"],
        },
    },
    {
        "name": "journal_update_entry",
        "description": "编辑一条记录（只改传入的字段，显式传 null 可清除该字段）。正文大改会自动触发小满的 AI 重判/流水线，无需额外操作。回收站里的记录不可编辑（先 restore）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "记录 id"},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "content": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "is_work": {"type": "boolean", "description": "true/false=人工定论；null=交还小满判断"},
                "occurred_at": {"type": "string", "description": "ISO8601"},
                "location_name": {"type": "string"},
                "blocks": {"type": "array", "items": {"type": "object"},
                           "description": "结构化模块（[{type, text}]）；传入会拍平覆盖 content，null/空数组=清除模块"},
                "weather": {"type": "object", "description": "天气对象；null=清除"},
                "starred": {"type": "boolean", "description": "星标（高光时刻）"},
                "exclude_from_ai": {"type": "boolean", "description": "true=不让 AI 分析这条"},
                "latitude": {"type": "number"},
                "longitude": {"type": "number"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "journal_delete_entry",
        "description": "把一条记录移入回收站（软删除，30 天内可恢复）；传 restore=true 则从回收站恢复。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "记录 id"},
                "restore": {"type": "boolean", "description": "传 true 表示恢复而不是删除"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "journal_list_insights",
        "description": "小满发现的规律：如「某类事出现的日子心情更好」（达到统计门槛才出，注明相关不等于因果）。",
        "inputSchema": {
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["active", "dismissed", "all"],
                                      "description": "默认 active"}},
        },
    },
    {
        "name": "journal_get_intent",
        "description": "取某天的「今日意图」（用户早上写的「今天打算……」）。",
        "inputSchema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD，默认今天"}},
        },
    },
    {
        "name": "journal_set_intent",
        "description": "写某天的「今日意图」（空文本=删除当天意图）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "day": {"type": "string", "description": "YYYY-MM-DD，默认今天"},
                "text": {"type": "string", "description": "今天打算……（≤200字）"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "journal_year_review",
        "description": "年度故事卡：超大数字数据卡 + 从记录里选出的金句（Wrapped 风）。",
        "inputSchema": {
            "type": "object",
            "properties": {"year": {"type": "integer", "description": "默认今年"}},
        },
    },
    {
        "name": "journal_backup_verify",
        "description": "验证最近一次自动备份是否完整可恢复（重跑 zip 校验和清单抽查）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "journal_circle_pending",
        "description": "圈子待办一次拿全：待确认档案（draft）+ 待答提问（按记录分组，含 AI 预选答案）+ 待处理合并提案。配合 journal_circle_act 处理。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "journal_circle_act",
        "description": "圈子审核动作打包：confirm/reject=确认/否认档案（entity_id）；merge=合并档案（from_id 并入 into_id）；undo_auto=撤销小满的自动动作（auto_id）；answer_question=回答提问（question_id + choice_index 选项下标）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["confirm", "reject", "merge", "undo_auto", "answer_question"]},
                "entity_id": {"type": "string", "description": "confirm/reject 用：档案 id"},
                "from_id": {"type": "string", "description": "merge 用：被并走的档案 id"},
                "into_id": {"type": "string", "description": "merge 用：并入的档案 id"},
                "auto_id": {"type": "string", "description": "undo_auto 用：自动动作 id"},
                "question_id": {"type": "string", "description": "answer_question 用：问题 id"},
                "choice_index": {"type": "integer", "description": "answer_question 用：选项下标（从 0 起）"},
            },
            "required": ["action"],
        },
    },
]

RESOURCES_META = [
    {"uri": "journal://entries/{id}", "name": "单条记录", "mimeType": "application/json"},
    {"uri": "journal://reports/{id}", "name": "单份报告", "mimeType": "application/json"},
]


def _call(method: str, path: str, **kwargs):
    """调用小满 REST，返回 (ok, data)。"""
    if not TOKEN:
        return False, "未设置 XIAOMAN_TOKEN（在小满 设置 → Agent 里查看）"
    try:
        with httpx.Client(base_url=BASE_URL, timeout=300.0,
                          headers={"Authorization": f"Bearer {TOKEN}"}) as c:
            r = c.request(method, path, **kwargs)
        try:
            data = r.json()
        except ValueError:
            data = r.text[:2000]
        if r.status_code >= 400:
            detail = data.get("detail") if isinstance(data, dict) else data
            return False, f"HTTP {r.status_code}：{detail}"
        return True, data
    except httpx.HTTPError as e:
        return False, f"连不上小满（{BASE_URL}）：{e}。请确认 start.bat 正在运行。"


def _text_result(data) -> dict:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}]}


def _tool_call(name: str, args: dict) -> dict:
    a = args or {}
    if name == "journal_list_entries":
        params = {k: v for k, v in (("date", a.get("date")), ("from", a.get("from")),
                                    ("to", a.get("to")), ("category", a.get("category")),
                                    ("limit", a.get("limit", 20))) if v is not None}
        if a.get("starred") is not None:
            params["starred"] = 1 if a["starred"] else 0
        if a.get("work") is not None:
            params["work"] = 1 if a["work"] else 0
        ok, data = _call("GET", "/api/agent/entries", params=params)
    elif name == "journal_get_entry":
        ok, data = _call("GET", f"/api/agent/entries/{a.get('id', '')}")
    elif name == "journal_search":
        ok, data = _call("GET", "/api/agent/search", params={"q": a.get("q", "")})
    elif name == "journal_ask":
        ok, data = _call("POST", "/api/agent/ask", json={"question": a.get("question", "")})
    elif name == "journal_daily_digest":
        ok, data = _call("GET", "/api/agent/digest", params={"date": a.get("date", "")})
    elif name == "journal_list_reports":
        params = {k: v for k, v in (("type", a.get("type")), ("limit", a.get("limit", 10))) if v is not None}
        ok, data = _call("GET", "/api/agent/reports", params=params)
    elif name == "journal_get_report":
        ok, data = _call("GET", f"/api/agent/reports/{a.get('id', '')}")
    elif name == "journal_generate_report":
        ok, data = _call("POST", "/api/agent/reports/generate",
                         json={"type": a.get("type", "weekly"), "date": a.get("date", "")})
    elif name == "journal_get_metrics":
        ok, data = _call("GET", "/api/agent/metrics")
    elif name == "journal_get_growth":
        t = a.get("type", "summary")
        path = {"summary": "/api/agent/growth/summary", "roadmap": "/api/agent/growth/roadmap",
                "forecast": "/api/agent/growth/forecast/latest"}.get(t)
        if not path:
            return {"content": [{"type": "text", "text": "type 只能是 summary / roadmap / forecast"}], "isError": True}
        ok, data = _call("GET", path)
    elif name == "journal_create_entry":
        body = {k: v for k, v in a.items() if v is not None}
        # 旧 category 参数映射到 is_work（显式 is_work 优先）；mixed 无法确定则不传交给 AI
        if "is_work" not in body and body.get("category") in ("work", "life"):
            body["is_work"] = body["category"] == "work"
        ok, data = _call("POST", "/api/agent/entries", json=body)
    elif name == "journal_random_entry":
        ok, data = _call("GET", "/api/agent/entries", params={"limit": 200})
        if ok:
            import random
            items = data.get("items", [])
            data = random.choice(items) if items else {"detail": "还没有任何记录"}
    elif name == "journal_circle_entities":
        if a.get("id"):
            ok, data = _call("GET", f"/api/agent/circle/entities/{a['id']}")
        else:
            params = {k: v for k, v in (("type", a.get("type")), ("status", a.get("status"))) if v is not None}
            ok, data = _call("GET", "/api/agent/circle/entities", params=params)
    elif name == "journal_circle_graph":
        ok, data = _call("GET", "/api/agent/circle/graph")
    elif name == "journal_list_categories":
        ok, data = _call("GET", "/api/agent/categories", params={"status": a.get("status", "active")})
    elif name == "journal_entry_links":
        ok, data = _call("GET", f"/api/agent/entries/{a.get('id', '')}/links")
    elif name == "journal_update_entry":
        body = {k: v for k, v in a.items()
                if k in ("title", "summary", "content", "tags", "is_work",
                         "occurred_at", "location_name", "blocks", "weather",
                         "starred", "exclude_from_ai", "latitude", "longitude")}
        ok, data = _call("PATCH", f"/api/agent/entries/{a.get('id', '')}", json=body)
    elif name == "journal_delete_entry":
        if a.get("restore"):
            ok, data = _call("POST", f"/api/agent/entries/{a.get('id', '')}/restore")
        else:
            ok, data = _call("DELETE", f"/api/agent/entries/{a.get('id', '')}")
    elif name == "journal_list_insights":
        ok, data = _call("GET", "/api/agent/insights", params={"status": a.get("status", "active")})
    elif name == "journal_get_intent":
        params = {"date": a["date"]} if a.get("date") else {}
        ok, data = _call("GET", "/api/agent/intent", params=params)
    elif name == "journal_set_intent":
        body = {"text": a.get("text", "")}
        if a.get("day"):
            body["day"] = a["day"]
        ok, data = _call("PUT", "/api/agent/intent", json=body)
    elif name == "journal_year_review":
        params = {"year": a["year"]} if a.get("year") else {}
        ok, data = _call("GET", "/api/agent/year-review", params=params)
    elif name == "journal_backup_verify":
        ok, data = _call("GET", "/api/agent/backup/verify")
    elif name == "journal_circle_pending":
        # 组合视图：待确认档案 + 待答提问（分组）+ 待处理合并提案，三次调用合并返回
        ok1, entities = _call("GET", "/api/agent/circle/entities", params={"status": "draft"})
        ok2, questions = _call("GET", "/api/agent/circle/questions", params={"status": "pending"})
        ok3, proposals = _call("GET", "/api/agent/circle/proposals", params={"status": "pending"})
        if not (ok1 and ok2 and ok3):
            bad = [d for ok_, d in ((ok1, entities), (ok2, questions), (ok3, proposals)) if not ok_]
            return {"content": [{"type": "text", "text": f"调用失败：{bad[0]}"}], "isError": True}
        ok, data = True, {
            "pending_entities": (entities or {}).get("items", []),
            "question_groups": (questions or {}).get("groups", []),
            "merge_proposals": (proposals or {}).get("items", []),
        }
    elif name == "journal_circle_act":
        action = a.get("action")
        if action in ("confirm", "reject"):
            ok, data = _call("POST", f"/api/agent/circle/entities/{a.get('entity_id', '')}/{action}")
        elif action == "merge":
            ok, data = _call("POST", "/api/agent/circle/entities/merge",
                             json={"from_id": a.get("from_id", ""), "into_id": a.get("into_id", "")})
        elif action == "undo_auto":
            ok, data = _call("POST", f"/api/agent/circle/auto-log/{a.get('auto_id', '')}/undo")
        elif action == "answer_question":
            # I-3：缺参必须报错——绝不用默认值替用户作答（选项 0 会真实写库）
            qid = str(a.get("question_id") or "").strip()
            if not qid or a.get("choice_index") is None:
                return {"content": [{"type": "text",
                                     "text": "answer_question 需要 question_id 与 choice_index（从 0 起的整数）"}],
                        "isError": True}
            ok, data = _call("POST", "/api/agent/circle/questions/answer-batch",
                             json={"answers": [{"id": qid, "choice_index": a["choice_index"]}]})
        else:
            return {"content": [{"type": "text",
                                 "text": "action 只能是 confirm / reject / merge / undo_auto / answer_question"}],
                    "isError": True}
    else:
        return {"content": [{"type": "text", "text": f"未知工具：{name}"}], "isError": True}
    if not ok:
        return {"content": [{"type": "text", "text": f"调用失败：{data}"}], "isError": True}
    return _text_result(data)


def _resource_read(uri: str) -> dict:
    if uri.startswith("journal://entries/"):
        ok, data = _call("GET", f"/api/agent/entries/{uri.rsplit('/', 1)[-1]}")
    elif uri.startswith("journal://reports/"):
        ok, data = _call("GET", f"/api/agent/reports/{uri.rsplit('/', 1)[-1]}")
    else:
        return {"contents": [{"uri": uri, "mimeType": "text/plain", "text": "不支持的资源"}]}
    text = json.dumps(data, ensure_ascii=False, indent=2) if ok else f"读取失败：{data}"
    return {"contents": [{"uri": uri, "mimeType": "application/json", "text": text}]}


def _handle(msg: dict):
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": SERVER_INFO,
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        result = _tool_call(params.get("name", ""), params.get("arguments"))
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"resources": RESOURCES_META}}
    if method == "resources/read":
        uri = (msg.get("params") or {}).get("uri", "")
        return {"jsonrpc": "2.0", "id": mid, "result": _resource_read(uri)}
    if mid is None:
        return None  # 通知类消息不需要响应
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> None:
    # Windows 控制台默认 GBK，MCP 协议要求 UTF-8
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        # I-2：畸形帧/处理异常绝不崩进程——一条坏消息不能带走全部 25 个工具
        if not isinstance(msg, dict):
            sys.stdout.write(json.dumps(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32600, "message": "invalid request: 帧应为 JSON 对象"}},
                ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue
        try:
            resp = _handle(msg)
        except Exception as e:
            resp = {"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32603,
                              "message": f"internal error: {type(e).__name__}"}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
