"""AI 模块：OpenAI 兼容接口调用、报告生成、知识提取、路线生成、情景推演。

所有对外抛出的 AIError 都是友好中文信息；端点负责映射为 HTTP 状态码。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import re
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

from . import db, stats as stats_mod

PROMPT_VERSION = "v2-evidence"
KNOWLEDGE_TYPES = {"experience", "pitfall", "case", "sop", "skill"}
HORIZONS = (30, 90, 180, 365)


class AIError(Exception):
    """message 为面向用户的中文错误信息。"""


class NoDataError(Exception):
    """时间段内没有可用记录。"""


class InsufficientDataError(Exception):
    def __init__(self, weeks: int):
        super().__init__(str(weeks))
        self.weeks = weeks


# ---------------- 基础调用 ----------------

def fast_configured() -> bool:
    """小模型槽位三件套是否配齐。"""
    return all(db.get_setting(k).strip()
               for k in ("ai_fast_base_url", "ai_fast_key", "ai_fast_model"))


def strong_configured() -> bool:
    return all(db.get_setting(k).strip()
               for k in ("ai_base_url", "ai_key", "ai_model"))


def _require_settings(slot: str = "strong") -> tuple[str, str, str, bool]:
    """取槽位配置。fast 未配齐（三缺任一）时回落 strong。"""
    vision = db.get_setting("ai_vision") == "true"
    if slot == "fast" and fast_configured():
        return (db.get_setting("ai_fast_base_url").strip(),
                db.get_setting("ai_fast_key").strip(),
                db.get_setting("ai_fast_model").strip(), vision)
    if strong_configured():
        return (db.get_setting("ai_base_url").strip(),
                db.get_setting("ai_key").strip(),
                db.get_setting("ai_model").strip(), vision)
    raise AIError("请先在设置页配置 AI 接口（base_url、API Key、模型名称）")


def _provider_kind(base_url: str, model: str) -> str:
    """按 host/model 做保守的供应商识别；识别不到就返回 unknown。"""
    host = (base_url or "").lower()
    name = (model or "").lower()
    if "deepseek" in host or "deepseek" in name:
        return "deepseek"
    if any(x in host for x in ("dashscope", "aliyuncs.com", "bailian")) or name.startswith(("qwen", "qwq", "qwen3")):
        return "qwen"
    if "anthropic" in host or name.startswith(("claude", "anthropic")):
        return "claude"
    if "generativelanguage.googleapis.com" in host or name.startswith("gemini"):
        return "gemini"
    return "unknown"


def _reasoning_effort_for(base_url: str, model: str, requested: str) -> str | None:
    """将通用档位映射成 OpenAI-compatible reasoning_effort 值。

    该函数只服务于支持 `reasoning_effort` 的协议；DeepSeek/Qwen 的原生参数
    由 :func:`_reasoning_payload_for` 单独组装，避免把 GPT 参数硬套到别家。
    """
    requested = (requested or "").strip().lower()
    if requested in ("", "none", "auto"):
        return None
    host = (base_url or "").lower()
    name = (model or "").lower()
    is_openai_reasoning = (
        "api.openai.com" in host or "openai.azure.com" in host
        or name.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))
        or "reasoning" in name or "terra" in name
    )
    if is_openai_reasoning:
        return requested
    # 未知供应商、Qwen、Gemini、Claude 等不保证该字段语义，交给各自默认策略。
    return None


def _reasoning_payload_for(base_url: str, model: str, requested: str) -> dict:
    """返回可直接合并进 Chat Completions 请求的供应商专用思考配置。

    通用档位只是 UI 语言，不假装它们在各家含义相同：
    OpenAI/兼容 reasoning 模型使用 reasoning_effort；DeepSeek 使用同名档位，
    Qwen 使用 enable_thinking + thinking_budget；其余供应商保持默认并由 400
    自动回退。预算是有序的近似档位，不声称等于模型真实推理 token 数。
    """
    requested = (requested or "").strip().lower()
    if requested in ("", "auto"):
        return {}
    kind = _provider_kind(base_url, model)
    if kind == "deepseek":
        # DeepSeek 的 OpenAI-compatible 文档支持 low/high/max，none 可关闭思考。
        if requested == "none":
            return {"reasoning_effort": "none"}
        mapped = {"low": "low", "medium": "high", "high": "high",
                  "xhigh": "high", "max": "max"}.get(requested)
        return {"reasoning_effort": mapped} if mapped else {}
    if kind == "qwen":
        if requested == "none":
            return {"enable_thinking": False}
        budget = {"low": 1024, "medium": 4096, "high": 8192,
                  "xhigh": 16384, "max": 32768}.get(requested)
        return ({"enable_thinking": True, "thinking_budget": budget}
                if budget else {})
    if requested == "none":
        return {}
    effort = _reasoning_effort_for(base_url, model, requested)
    return {"reasoning_effort": effort} if effort else {}


def _chat_url(base_url: str) -> str:
    b = base_url.rstrip("/")
    return b + "/chat/completions" if "/v1" in b else b + "/v1/chat/completions"


def chat(messages: list, timeout: float = 180.0, slot: str = "strong", attempts: int = 3,
         model_override: str | None = None) -> str:
    """调用 chat completions，返回文本内容；网络层错误（超时/断连/SSL 中断）自动重试，
    最终失败抛 AIError（中文）。中转服务偶发断连很常见，重试能挡掉大部分。
    思考深度（reasoning_effort）按槽位设置下发；接口不认该参数（400）时自动降级重试。
    model_override 用于设置页指定的专用模型（如润色模型），覆盖槽位模型。"""
    base_url, key, model, _ = _require_settings(slot)
    if model_override:
        model = model_override
    payload = {"model": model, "messages": messages, "temperature": 0.3}
    effort_key = "ai_fast_effort" if slot == "fast" else "ai_effort"
    effort = db.get_setting(effort_key).strip()
    payload.update(_reasoning_payload_for(base_url, model, effort))
    r = None
    last: httpx.HTTPError | None = None
    tries = max(1, attempts)
    for i in range(tries):
        try:
            with httpx.Client(timeout=timeout) as c:
                r = c.post(
                    _chat_url(base_url),
                    json=payload,
                    headers={"Authorization": f"Bearer {key}"},
                )
            last = None
            break
        except httpx.HTTPError as e:  # TimeoutException / ConnectError / SSL 中断等网络层错误
            last = e
            if i < tries - 1:
                time.sleep(2 + i * 3)
    if last is not None:
        if isinstance(last, httpx.TimeoutException):
            raise AIError(f"连接超时（已自动重试 {tries} 次），请检查 base_url 是否正确、网络是否可用")
        if isinstance(last, httpx.ConnectError):
            raise AIError(f"无法连接到 AI 接口（已自动重试 {tries} 次），请检查 base_url 是否填写正确")
        raise AIError(f"网络请求失败（已自动重试 {tries} 次仍被中断）：{last}")

    # 兼容代理或旧版接口：不认识思考参数时逐级降级重试，避免换模型后整次调用失败。
    thinking_keys = ("enable_thinking", "thinking_budget")
    if r.status_code == 400 and (
            "reasoning_effort" in payload or any(key in payload for key in thinking_keys)):
        payload.pop("reasoning_effort", None)
        for reasoning_key in thinking_keys:
            payload.pop(reasoning_key, None)
        try:
            with httpx.Client(timeout=timeout) as c:
                r = c.post(
                    _chat_url(base_url),
                    json=payload,
                    headers={"Authorization": f"Bearer {key}"},
                )
        except httpx.HTTPError as e:
            raise AIError(f"网络请求失败：{e}")

    if r.status_code != 200:
        code = r.status_code
        if code in (401, 403):
            raise AIError(f"API Key 无效或没有权限（HTTP {code}），请检查设置")
        if code == 404:
            raise AIError("接口地址不存在（HTTP 404），请检查 base_url 是否填写正确")
        if code == 429:
            raise AIError("请求过于频繁，已被 AI 服务限流（HTTP 429），请稍后重试")
        if code >= 500:
            raise AIError(f"AI 服务内部错误（HTTP {code}），请稍后重试")
        body = r.text[:200]
        raise AIError(f"AI 接口返回错误（HTTP {code}）：{body}")

    try:
        return r.json()["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        raise AIError("AI 接口返回的数据格式无法识别")


def test_ai(slot: str = "strong") -> tuple[bool, str]:
    """设置页"测试连接"用：短消息探测，20s 超时。fast 未配置时不回落、直接提示。"""
    if slot == "fast" and not fast_configured():
        return False, "小模型未配置（留空则与大模型共用）"
    try:
        chat([{"role": "user", "content": "回复ok两个字母"}], timeout=20.0, slot=slot, attempts=2)
        return True, "连接成功，模型响应正常"
    except AIError as e:
        return False, str(e)


def _probe_error_message(code: int, body_text: str = "") -> str:
    if code in (401, 403):
        return f"API Key 无效或没有权限（HTTP {code}），请检查设置"
    if code == 404:
        return "接口地址不存在（HTTP 404），请检查 Base URL 是否填写正确"
    if code == 429:
        return "请求过于频繁，已被 AI 服务限流（HTTP 429），请稍后重试"
    if code >= 500:
        return f"AI 服务内部错误（HTTP {code}），请稍后重试"
    return f"接口返回错误（HTTP {code}）：{body_text[:120]}"


def test_chat_with(base_url: str, key: str, model: str) -> tuple[bool, str]:
    """用表单里的临时配置直接探测对话模型（不用先保存）。"""
    try:
        payload = {"model": model, "messages": [{"role": "user", "content": "回复ok两个字母"}], "temperature": 0.3}
        with httpx.Client(timeout=20.0) as c:
            r = c.post(_chat_url(base_url), json=payload, headers={"Authorization": f"Bearer {key}"})
        if r.status_code != 200:
            return False, _probe_error_message(r.status_code, r.text)
        try:
            r.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            return False, "接口返回的数据格式无法识别"
        return True, "连接成功，模型响应正常"
    except httpx.TimeoutException:
        return False, "连接超时，请检查 Base URL 是否正确、网络是否可用"
    except httpx.ConnectError:
        return False, "无法连接到接口，请检查 Base URL 是否填写正确"
    except httpx.HTTPError as e:
        return False, f"网络请求失败：{e}"


def test_embed_with(base_url: str, key: str, model: str) -> tuple[bool, str]:
    """用表单里的临时配置直接探测嵌入模型（不用先保存）。"""
    try:
        with httpx.Client(timeout=20.0) as c:
            r = c.post(_embed_url(base_url), json={"model": model, "input": "测试"},
                       headers={"Authorization": f"Bearer {key}"})
        if r.status_code != 200:
            return False, _probe_error_message(r.status_code, r.text)
        try:
            r.json()["data"][0]["embedding"]
        except (ValueError, KeyError, IndexError, TypeError):
            return False, "接口返回的数据格式无法识别（不是标准嵌入接口？）"
        return True, "连接成功，嵌入向量返回正常"
    except httpx.TimeoutException:
        return False, "连接超时，请检查 Base URL 是否正确、网络是否可用"
    except httpx.ConnectError:
        return False, "无法连接到接口，请检查 Base URL 是否填写正确"
    except httpx.HTTPError as e:
        return False, f"网络请求失败：{e}"


# ---------------- JSON 解析与修复 ----------------

def _extract_json(text: str):
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            return json.loads(t[start:end + 1])
        raise ValueError("响应不是合法 JSON")


def _chat_json(messages: list, slot: str = "strong", model_override: str | None = None):
    """调模型并解析 JSON；失败时发起一次修复请求，再失败抛 AIError。"""
    raw = chat(messages, slot=slot, model_override=model_override)
    try:
        return _extract_json(raw)
    except ValueError:
        pass
    repair = messages + [
        {"role": "assistant", "content": raw},
        {"role": "user", "content": "上次输出不是合法JSON，请只输出JSON"},
    ]
    raw2 = chat(repair, slot=slot, model_override=model_override)
    try:
        return _extract_json(raw2)
    except ValueError:
        raise AIError("AI 返回的内容无法解析为 JSON，请重试")


def _str_list(value) -> list[str]:
    """把 AI 返回的列表清洗为字符串数组；宽容处理 {text/title/content} 对象项。"""
    if not isinstance(value, list):
        return []
    out = []
    for x in value:
        if isinstance(x, dict):
            x = x.get("text") or x.get("title") or x.get("content") or ""
        s = str(x).strip()
        if s:
            out.append(s)
    return out


# ---------------- 轻量任务：标题/摘要/标签/分类补全（fast 槽位） ----------------

ENRICH_SYSTEM = """你是个人日志助手。根据用户提供的日志正文与现有信息，生成更完善的元信息。
只输出一个 JSON 对象（不要输出任何其他文字，不要用 markdown 代码块包裹）：
{"title": "日志标题，不超过15字，朴实不矫情", "summary": "一句话摘要，不超过60字", "tags": ["短词标签，最多5个"], "is_work": true 或 false 或 null, "categories": ["类目slug，1到3个"]}
要求：
1. is_work：与实习/工作/求职/职业技能直接相关→true，其余→false，拿不准→null。
2. categories：从下方类目清单中选 1-3 个最贴切的 slug，宁少勿滥。
3. 标签是短词，不要句子。
4. 不要编造正文里没有的事实。"""


def _category_defs_text() -> str:
    """从 categories 表动态读 active 类目定义拼进 prompt；表不可用时退回预设清单。"""
    try:
        rows = db.q("SELECT slug, name_zh, definition FROM categories WHERE status='active' ORDER BY id")
        if rows:
            return "\n".join(f"- {r['slug']}（{r['name_zh']}）：{r['definition']}" for r in rows)
    except Exception:
        pass
    return "\n".join(f"- {slug}（{zh}）：{defi}" for slug, zh, _en, defi in db.PRESET_CATEGORIES)


def _active_category_slugs() -> set:
    try:
        return {r["slug"] for r in db.q("SELECT slug FROM categories WHERE status='active'")}
    except Exception:
        return {slug for slug, _zh, _en, _defi in db.PRESET_CATEGORIES}


def enrich_entry_fields(title: str, summary: str, content: str,
                        tags: list, category: str) -> dict:
    """轻量补全：标题/摘要/标签 + is_work 硬标签 + 1-3 个类目 slug。用 fast 槽位，失败抛 AIError（中文）。
    （category 形参保留兼容旧调用方，新体系不再使用它。）"""
    user_text = (
        f"现有标题：{title or '（空）'}\n"
        f"现有摘要：{summary or '（空）'}\n"
        f"现有标签：{('、'.join(str(t) for t in tags)) or '（空）'}\n\n"
        f"类目清单（slug（名称）：定义）：\n{_category_defs_text()}\n\n"
        f"正文：\n{(content or '')[:4000]}"
    )
    data = _chat_json([
        {"role": "system", "content": ENRICH_SYSTEM},
        {"role": "user", "content": user_text},
    ], slot="fast")
    if not isinstance(data, dict):
        raise AIError("AI 返回的内容结构不正确，请重试")

    new_title = str(data.get("title") or "").strip()[:15]
    new_summary = str(data.get("summary") or "").strip()[:60]
    new_tags = []
    for t in _str_list(data.get("tags"))[:5]:
        t = t[:20]
        if t not in new_tags:
            new_tags.append(t)
    raw_is_work = data.get("is_work")
    is_work = raw_is_work if isinstance(raw_is_work, bool) else None
    known = _active_category_slugs()
    cats = []
    for slug in _str_list(data.get("categories")):
        slug = slug.strip()[:40]
        if slug in known and slug not in cats:
            cats.append(slug)
    return {"title": new_title, "summary": new_summary, "tags": new_tags,
            "is_work": is_work, "categories": cats[:3]}


# ---------------- 隐性关联解释 / 月度新类目命名 ----------------

EXPLAIN_LINK_SYSTEM = """你在判断两条个人日志为什么可能相关。只输出一句话理由（不超过40字），
说人话、具体、不要"可能""或许"这类虚词开头；看不出关联就只输出 null。"""


def explain_link(text_a: str, text_b: str) -> str | None:
    """fast 槽：给两段文本生成一句"为什么相关"（≤40字）。失败或看不出返回 None。"""
    try:
        raw = chat([
            {"role": "system", "content": EXPLAIN_LINK_SYSTEM},
            {"role": "user", "content": f"记录A：\n{(text_a or '')[:500]}\n\n记录B：\n{(text_b or '')[:500]}"},
        ], timeout=30.0, slot="fast", attempts=1)
    except AIError:
        return None
    text = raw.strip().strip('"').split("\n")[0].strip()
    if not text or text.lower() == "null":
        return None
    return text[:40]


NAME_CLUSTER_SYSTEM = """你在为一簇未归类的个人日志起一个新的类目名。
只输出一个 JSON 对象（不要输出任何其他文字）：
{"name_zh": "类目中文名，2-6字", "slug": "小写英文snake_case", "definition": "一句话定义，不超过30字"}
要求：名字要具体、朴实，能覆盖这组记录的共同主题；不要与"工作/生活"这种泛词重复。"""


def name_category_cluster(snippets: list[str]) -> dict | None:
    """strong 槽：为一簇未分类记录命名新类目。返回 {name_zh, slug, definition} 或 None。"""
    samples = "\n".join(f"- {(s or '')[:120]}" for s in snippets[:12] if s)
    if not samples:
        return None
    try:
        data = _chat_json([
            {"role": "system", "content": NAME_CLUSTER_SYSTEM},
            {"role": "user", "content": f"这组记录的摘录：\n{samples}"},
        ], slot="strong")
    except AIError:
        return None
    if not isinstance(data, dict):
        return None
    name_zh = str(data.get("name_zh") or "").strip()[:10]
    slug = re.sub(r"[^a-z0-9_]+", "_", str(data.get("slug") or "").strip().lower()).strip("_")[:40]
    definition = str(data.get("definition") or "").strip()[:50]
    if not (name_zh and slug):
        return None
    return {"name_zh": name_zh, "slug": slug, "definition": definition}


# ---------------- 报告生成 ----------------

REPORT_SYSTEM = """你是一位严谨而温暖的成长导师（mentor），正在帮助一位应届毕业生复盘他/她的成长记录。
你的第一要务是**忠实**：这份报告必须经得起与原始记录的逐条对照。

工作方法（必须按此顺序思考）：
1. 先逐条仔细阅读所有记录，按时间先后列出"原子事实"：每条一句话，只写记录里明确发生的事，标注日期和 entry_id。
2. 检查"环境变化"信号：入职、离职、换岗、换项目、搬家、长假等。若有，分析必须按变化前后分段，**禁止跨段建立因果关系**。
3. 只有在事实清单的支撑下，才动笔写报告。

因果与时间纪律：
- 只有同一天或相邻日期的记录明确呈现先后/因果关系时，才允许这样表述（"因为…所以…""随后…"）。
- 不相邻、无明确关联的事，不得压缩成同一时间段，更不得拼接成因果链。
- 拿不准的推断必须带限定词（"可能""似乎""从记录看倾向于"），否则不要写。
- 没有记录的空白日期，不得虚构任何内容。

输出要求：
1. 只输出一个 JSON 对象，不要输出任何其他文字，不要用 markdown 代码块包裹。
2. JSON 结构（字段顺序就是你的思考顺序）：
{
  "timeline_facts": [{"date": "YYYY-MM-DD", "fact": "原子事实一句话", "entry_id": "来源记录id", "quote": "原文短句"}],
  "context_notes": "发现的环境变化及前后分段说明；没有则为 null",
  "executive_summary": "一段话总结本期整体状态与收获，温暖而具体",
  "accomplishments": ["本期值得肯定的成果或进步，要指出具体事实"],
  "challenges": ["遇到的困难或暴露的问题"],
  "learnings": ["从记录中提炼出的经验教训"],
  "suggestions": ["下个周期可执行的建议，具体、可操作"],
  "evidence": [{"claim": "报告中的重要结论", "entry_id": "支撑该结论的记录id", "quote": "原文短句"}],
  "highlights": ["本期最值得纪念的记录的 entry_id（高光时刻：第一次做成的事、突破、有意义的瞬间），0-3条；没有就空数组"],
  "knowledge_items": [{"type": "experience|pitfall|case|sop|skill", "title": "知识标题", "content": "可复用的知识内容", "evidence": [{"entry_id": "来源记录id", "quote": "逐字摘自记录的短句"}]}],
  "mindmap_mermaid": "mindmap 语法的思维导图代码（根节点为本期主题）；无法生成则为 null"
}
3. evidence 的每条 claim 必须能在 timeline_facts 中找到对应事实；entry_id 不得编造。quote 必须逐字摘自对应记录。
4. knowledge_items 的每条内容也必须有 evidence；entry_id 只能来自本期记录，quote 必须逐字摘自对应记录。无法找到直接证据时不要提取该知识。
5. 不要编造记录中不存在的事实；不确定的内容不要写。
6. 语气：温暖、具体、真诚，像一位了解他/她的 mentor，避免空泛套话。"""

CATEGORY_LABEL = {"work": "工作", "life": "生活", "mixed": "混合"}
TYPE_LABEL = {"daily": "日报", "weekly": "周报", "monthly": "月报"}


def _period(rtype: str, day: date) -> tuple[date, date]:
    if rtype == "daily":
        return day, day
    if rtype == "weekly":
        start = day - timedelta(days=day.isoweekday() - 1)
        return start, start + timedelta(days=6)
    if rtype == "monthly":
        start = day.replace(day=1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        return start, end
    raise ValueError(rtype)


# 报告/总结时可直接读入内容的文本附件扩展名（≤200KB、截取前 3000 字）
TEXT_FILE_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".log", ".py", ".js", ".ts",
    ".xml", ".yml", ".yaml", ".ini", ".toml", ".html", ".htm", ".css", ".sql",
    ".java", ".c", ".cpp", ".h", ".go", ".rs", ".vue", ".sh", ".bat",
}


def _fmt_weather(w: dict | None) -> str:
    if not w:
        return ""
    parts = [w.get("text") or ""]
    if w.get("temperature_c") is not None:
        parts.append(f"{w['temperature_c']}°C")
    if w.get("humidity") is not None:
        parts.append(f"湿度{w['humidity']}%")
    return " ".join(p for p in parts if p)


def _entry_verifiable_text(row) -> str:
    """返回一条记录可逐字核验的文本来源，包含受支持的文本附件摘录。"""
    parts = [str(row[key] or "") for key in ("title", "summary", "content")]
    for attachment in db.q(
            "SELECT filename, path, size FROM attachments WHERE entry_id=? ORDER BY created_at",
            (row["id"],)):
        ext = Path(attachment["filename"] or "").suffix.lower()
        if ext not in TEXT_FILE_EXTS or (attachment["size"] or 0) > 200_000:
            continue
        try:
            raw = (db.MEDIA_DIR / attachment["path"]).read_bytes()[:12000]
            text = raw.decode("utf-8", errors="replace").strip()
        except Exception:
            text = ""
        if text:
            parts.append(text[:3000])
    return "\n".join(part for part in parts if part)


def _entries_text(rows: list) -> str:
    weekdays = "一二三四五六日"
    blocks = []
    for r in rows:
        lines = [f"[记录 entry_id={r['id']}]"]
        when = r['occurred_at'][:16].replace('T', ' ')
        try:
            wd = date.fromisoformat(r['occurred_at'][:10])
            when += f"（周{weekdays[wd.weekday()]}）"
        except ValueError:
            pass
        lines.append(f"时间：{when}")
        meta = [f"分类：{CATEGORY_LABEL.get(r['category'], r['category'])}"]
        try:
            tags = json.loads(r["tags"] or "[]")
        except ValueError:
            tags = []
        if tags:
            meta.append("标签：" + "、".join(str(t) for t in tags))
        lines.append(" | ".join(meta))
        loc_w = []
        if r["location_name"]:
            loc_w.append(f"地点：{r['location_name']}")
        try:
            w = json.loads(r["weather_json"]) if r["weather_json"] else None
        except ValueError:
            w = None
        wtext = _fmt_weather(w)
        if wtext:
            loc_w.append(f"天气：{wtext}")
        if loc_w:
            lines.append(" | ".join(loc_w))
        if r["title"]:
            lines.append(f"标题：{r['title']}")
        if r["summary"]:
            lines.append(f"摘要：{r['summary']}")
        content = (r["content"] or "")[:2000]
        if content:
            lines.append(f"正文：\n{content}")
        atts = db.q(
            "SELECT filename, kind, duration_ms, path, size FROM attachments WHERE entry_id=? ORDER BY created_at",
            (r["id"],),
        )
        if atts:
            desc = []
            for a in atts:
                d = f"{a['filename']}({a['kind']}"
                if a["duration_ms"]:
                    d += f"，时长{round(a['duration_ms'] / 1000)}秒"
                desc.append(d + ")")
            lines.append("附件：" + "；".join(desc))
            # 文本类附件直接附上内容摘录，AI 报告/总结能真正读到文件
            for a in atts:
                ext = Path(a["filename"] or "").suffix.lower()
                if ext not in TEXT_FILE_EXTS or (a["size"] or 0) > 200_000:
                    continue
                try:
                    raw = (db.MEDIA_DIR / a["path"]).read_bytes()[:12000]
                    txt = raw.decode("utf-8", errors="replace").strip()
                except Exception:
                    txt = ""
                if txt:
                    lines.append(f"附件《{a['filename']}》内容摘录：\n{txt[:3000]}")
        links = db.q("SELECT url, title FROM links WHERE entry_id=? ORDER BY created_at", (r["id"],))
        for l in links:
            lines.append(f"链接：{l['title'] or l['url']}（{l['url']}）")
        blocks.append("\n".join(lines))

    text, used = [], 0
    for i, b in enumerate(blocks):
        if used + len(b) > 24000:
            text.append(f"（其余 {len(blocks) - i} 条记录因长度限制省略）")
            break
        text.append(b)
        used += len(b)
    return "\n---\n".join(text)


def _vision_content(entry_ids: list[str], user_text: str) -> list:
    """ai_vision=true 时构造 OpenAI vision 格式的 content 数组；失败回退纯文本。"""
    try:
        from PIL import Image

        placeholders = ",".join("?" for _ in entry_ids)
        rows = db.q(
            f"SELECT a.path, a.mime FROM attachments a JOIN entries e ON a.entry_id=e.id "
            f"WHERE a.kind='image' AND e.deleted_at IS NULL AND e.exclude_from_ai=0 "
            f"AND a.entry_id IN ({placeholders}) ORDER BY a.created_at LIMIT 4",
            tuple(entry_ids),
        )
        content = [{"type": "text", "text": user_text}]
        for r in rows:
            p = db.MEDIA_DIR / r["path"]
            with Image.open(p) as im:
                im.thumbnail((768, 768))
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "JPEG", quality=70)
            b64 = base64.b64encode(buf.getvalue()).decode()
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })
        return content if len(content) > 1 else user_text
    except Exception:
        return user_text


def generate_report(rtype: str, day_str: str) -> dict:
    try:
        day = date.fromisoformat(day_str)
    except ValueError:
        raise AIError("日期格式应为 YYYY-MM-DD")
    start, end = _period(rtype, day)
    rows = db.q(
        "SELECT * FROM entries WHERE deleted_at IS NULL AND exclude_from_ai=0 "
        "AND substr(occurred_at,1,10) BETWEEN ? AND ? ORDER BY occurred_at",
        (start.isoformat(), end.isoformat()),
    )
    if not rows:
        raise NoDataError("该时间段没有记录")

    # 月报重复障碍雷达：注入上一次月报的 challenges
    prev_challenges = []
    if rtype == "monthly":
        prev = db.q1(
            "SELECT analysis_json FROM reports WHERE type='monthly' AND period_start < ? "
            "ORDER BY period_start DESC LIMIT 1",
            (start.isoformat(),),
        )
        if prev:
            try:
                prev_challenges = (json.loads(prev["analysis_json"] or "{}").get("challenges") or [])
            except ValueError:
                prev_challenges = []

    # 已有知识清单：从源头防止重复提取（含已否决的，AI 不应再提）
    existing_k = db.q(
        "SELECT title, status FROM knowledge WHERE status IN ('pending','accepted','rejected') "
        "AND trim(title) != '' ORDER BY created_at DESC LIMIT 60"
    )
    k_hint = ""
    if existing_k:
        titles = "；".join(k["title"] for k in existing_k)
        k_hint = (
            "\n\n用户知识库中已存在以下知识（含已被否决的）：" + titles +
            "。knowledge_items 只提取全新的、与上述任何一条意思都不同的内容；"
            "同一事件换种说法也算重复，宁可少提取也不要重复。"
        )

    # 时间骨架：明确告诉 AI 周期长度、有记录的天数、以及空白日（防止虚构连续性）
    days_with = sorted({r["occurred_at"][:10] for r in rows})
    total_days = (end - start).days + 1
    gap_info = ""
    if total_days <= 62:
        all_days = [(start + timedelta(days=i)).isoformat() for i in range(total_days)]
        gaps = [d for d in all_days if d not in days_with]
        if gaps:
            gap_info = f"\n无记录的空白日（这些日子不要写任何事）：{'、'.join(gaps)}"
    user_text = (
        f"时间段：{start.isoformat()} 至 {end.isoformat()}（{TYPE_LABEL[rtype]}，共 {total_days} 天，"
        f"其中有记录 {len(days_with)} 天）。{gap_info}\n"
        f"以下是按时间顺序排列的 {len(rows)} 条记录，请先读完全部记录，再按系统要求的顺序输出 JSON：\n\n{_entries_text(rows)}"
    )
    # 周报/月报：work_related=1 且非工作的记录作为「相关素材」附在工作记录之后，不混入工作统计
    if rtype in ("weekly", "monthly"):
        related = [r for r in rows if r["work_related"] and r["is_work"] != 1]
        if related:
            period_label = "本周" if rtype == "weekly" else "本月"
            extra = [f"\n\n以下生活记录与{period_label}工作相关，供参考"
                     f"（只是背景素材，不要混入工作成果统计）："]
            for r in related:
                excerpt = (r["summary"] or r["title"] or (r["content"] or "")[:80]).strip()
                extra.append(f"- [{r['occurred_at'][:10]}] {excerpt}")
            user_text += "\n".join(extra)
    if prev_challenges:
        user_text += (
            "\n\n以下是上月挑战：" + "；".join(str(c) for c in prev_challenges) +
            "。若某项本月再次出现，请在 JSON 中额外输出 recurring 字段"
            "（数组，写明哪项挑战复发及依据）；没有复发则输出空数组。"
        )
    user_text += _circle_inject(f"生成{TYPE_LABEL[rtype]}", [r["id"] for r in rows])
    user_text += k_hint
    source_hash = hashlib.sha256(user_text.encode("utf-8")).hexdigest()

    _, _, model, vision = _require_settings()
    if vision:
        user_content = _vision_content([r["id"] for r in rows], user_text)
    else:
        user_content = user_text
    messages = [
        {"role": "system", "content": REPORT_SYSTEM},
        {"role": "user", "content": user_content},
    ]
    data = _chat_json(messages, slot="strong")
    if not isinstance(data, dict):
        raise AIError("AI 返回的内容结构不正确，请重试")

    valid_ids = {r["id"] for r in rows}
    source_by_id = {r["id"]: _entry_verifiable_text(r) for r in rows}
    analysis = {
        "timeline_facts": [],
        "context_notes": None,
        "executive_summary": str(data.get("executive_summary") or "").strip(),
        "accomplishments": _str_list(data.get("accomplishments")),
        "challenges": _str_list(data.get("challenges")),
        "learnings": _str_list(data.get("learnings")),
        "suggestions": _str_list(data.get("suggestions")),
        "evidence": [],
        "mindmap_mermaid": None,
        "recurring": [],
    }
    # AI 自动圈定高光：只标星、永不取消；entry_id 必须属于本期记录
    new_highlights = []
    for hid in _str_list(data.get("highlights"))[:3]:
        if hid in valid_ids:
            row = db.q1("SELECT starred, title, content FROM entries WHERE id=?", (hid,))
            if row is not None:
                if not row["starred"]:
                    new_highlights.append({"id": hid, "title": row["title"] or (row["content"] or "")[:20]})
                db.execute("UPDATE entries SET starred=1 WHERE id=?", (hid,))
    # 原子事实清单：entry_id 必须属于本期记录，日期必须在周期内，否则丢弃
    for item in data.get("timeline_facts") or []:
        if isinstance(item, dict):
            fid = str(item.get("entry_id") or "").strip()
            fdate = str(item.get("date") or "").strip()
            fact = str(item.get("fact") or "").strip()
            quote = str(item.get("quote") or "").strip()[:120]
            if (fid in valid_ids and fact and quote and quote in source_by_id.get(fid, "")
                    and start.isoformat() <= fdate <= end.isoformat()):
                analysis["timeline_facts"].append({"date": fdate, "fact": fact,
                                                    "entry_id": fid, "quote": quote})
    cn = data.get("context_notes")
    if isinstance(cn, str) and cn.strip():
        analysis["context_notes"] = cn.strip()
    fact_entries = {f["entry_id"] for f in analysis["timeline_facts"]}
    for item in data.get("evidence") or []:
        if isinstance(item, dict):
            cid = str(item.get("entry_id") or "").strip()
            claim = str(item.get("claim") or "").strip()
            quote = str(item.get("quote") or "").strip()[:120]
            if (cid in valid_ids and cid in fact_entries and claim and quote
                    and quote in source_by_id.get(cid, "")):
                analysis["evidence"].append({"claim": claim, "entry_id": cid, "quote": quote})
    mm = data.get("mindmap_mermaid")
    if isinstance(mm, str) and mm.strip():
        analysis["mindmap_mermaid"] = mm.strip()
    if rtype == "monthly" and prev_challenges:
        # 只有注入了上月挑战时才采纳 AI 的 recurring（防止无上下文时模型乱编）
        analysis["recurring"] = _str_list(data.get("recurring"))

    # 同周期报告重新生成 = 覆盖而非新增（id 不变）。先确定 id，供知识条目保存来源。
    old = db.q1(
        "SELECT id FROM reports WHERE type=? AND period_start=? AND period_end=?",
        (rtype, start.isoformat(), end.isoformat()),
    )
    report_id = old["id"] if old is not None else db.new_id()

    # 知识条目落库：按规范化标题去重，pending 更新、已审跳过、新增计 pending。
    # 只有经过 entry_id + 原文 quote 校验的证据才会成为可点击来源。
    knowledge_extracted = 0
    items = data.get("knowledge_items")
    if isinstance(items, list):
        for item in items[:10]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            content = str(item.get("content") or "").strip()
            if not (title and content):
                continue
            ktype = str(item.get("type") or "").strip()
            if ktype not in KNOWLEDGE_TYPES:
                ktype = "experience"
            evidence = []
            raw_evidence = item.get("evidence")
            if isinstance(raw_evidence, dict):
                raw_evidence = [raw_evidence]
            if not isinstance(raw_evidence, list):
                raw_evidence = []
            # 兼容早期/部分模型使用扁平 entry_id + quote 字段的输出。
            if not raw_evidence and item.get("entry_id"):
                raw_evidence = [{"entry_id": item.get("entry_id"), "quote": item.get("quote", "")}]
            for ev in raw_evidence[:5]:
                if not isinstance(ev, dict):
                    continue
                eid = str(ev.get("entry_id") or "").strip()
                quote = str(ev.get("quote") or "").strip()[:240]
                if eid in valid_ids and quote and quote in source_by_id.get(eid, ""):
                    evidence.append({"entry_id": eid, "quote": quote})
            if not evidence and raw_evidence:
                # 模型给了来源但来源无法核验：宁可不入库，也不留下假链接。
                continue
            existing, vec = _knowledge_dedupe_match(ktype, title, content)
            vec_json = json.dumps(vec) if vec is not None else None
            if existing is not None:
                if existing["status"] == "pending":
                    if evidence:
                        db.execute(
                            "UPDATE knowledge SET content=?, type=?, entry_id=?, source_report_id=?, evidence_json=?, vector_json=? WHERE id=?",
                            (content, ktype, evidence[0]["entry_id"], report_id,
                             json.dumps(evidence, ensure_ascii=False), vec_json, existing["id"]),
                        )
                    else:
                        # 没有来源字段时仍保留知识，但只回链生成它的报告，
                        # 不覆盖已有的具体记录证据。
                        db.execute(
                            "UPDATE knowledge SET content=?, type=?, source_report_id=?, vector_json=? WHERE id=?",
                            (content, ktype, report_id, vec_json, existing["id"]),
                        )
                    knowledge_extracted += 1
                elif evidence:
                    # 已审核条目不改内容/状态，但补齐最近一次可核验来源。
                    db.execute(
                        "UPDATE knowledge SET entry_id=?, source_report_id=?, evidence_json=? WHERE id=?",
                        (evidence[0]["entry_id"], report_id,
                         json.dumps(evidence, ensure_ascii=False), existing["id"]),
                    )
                # accepted/rejected 用户已审，内容和状态保持不动
                continue
            entry_id = evidence[0]["entry_id"] if evidence else None
            evidence_json = json.dumps(evidence, ensure_ascii=False)
            db.execute(
                "INSERT INTO knowledge(id, entry_id, source_report_id, evidence_json, type, title, content, status, dedupe_key, "
                "vector_json, created_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)",
                (db.new_id(), entry_id, report_id, evidence_json, ktype, title, content,
                 db.knowledge_dedupe_key(title), vec_json, db.now_iso()),
            )
            knowledge_extracted += 1

    # 同周期报告重新生成 = 覆盖而非新增（id 不变）
    created_at = db.now_iso()
    analysis_json = json.dumps({**analysis, "_knowledge_extracted": knowledge_extracted},
                               ensure_ascii=False)
    if old is not None:
        db.execute(
            "UPDATE reports SET analysis_json=?, model=?, prompt_version=?, source_hash=?, "
            "entry_count=?, created_at=? WHERE id=?",
            (analysis_json, model, PROMPT_VERSION, source_hash, len(rows), created_at, report_id),
        )
    else:
        db.execute(
            "INSERT INTO reports(id, type, period_start, period_end, analysis_json, model, "
            "prompt_version, source_hash, entry_count, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (report_id, rtype, start.isoformat(), end.isoformat(),
             analysis_json, model, PROMPT_VERSION, source_hash, len(rows), created_at),
        )
    return {
        "id": report_id,
        "type": rtype,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "created_at": created_at,
        "model": model,
        "entry_count": len(rows),
        "analysis": analysis,
        "knowledge_extracted": knowledge_extracted,
        "new_highlights": new_highlights,
    }


def report_detail(row) -> dict:
    try:
        stored = json.loads(row["analysis_json"] or "{}")
    except (TypeError, ValueError):
        stored = {}
    # 兼容早期/手工导入的非对象 JSON（例如 [] 或 null），避免详情页 500。
    if not isinstance(stored, dict):
        stored = {}
    knowledge_extracted = stored.pop("_knowledge_extracted", 0) if isinstance(stored, dict) else 0
    analysis = {
        "executive_summary": stored.get("executive_summary", ""),
        "accomplishments": stored.get("accomplishments", []),
        "challenges": stored.get("challenges", []),
        "learnings": stored.get("learnings", []),
        "suggestions": stored.get("suggestions", []),
        "evidence": stored.get("evidence", []),
        "timeline_facts": stored.get("timeline_facts", []),
        "context_notes": stored.get("context_notes"),
        "mindmap_mermaid": stored.get("mindmap_mermaid"),
        "recurring": stored.get("recurring", []),
    }
    return {
        "id": row["id"],
        "type": row["type"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "created_at": row["created_at"],
        "model": row["model"],
        "entry_count": row["entry_count"],
        "analysis": analysis,
        "knowledge_extracted": knowledge_extracted,
    }


# ---------------- 成长路线 ----------------

ROADMAP_SYSTEM = """你是一位务实的职业规划师，帮助一位应届毕业生制定成长路线。
根据用户提供的信息，只输出一个 JSON 对象（不要输出其他文字）：
{"nodes": [{"title": "节点标题", "description": "具体要做什么", "first_step": "本周第一步", "done_when": "可检查的完成标准", "horizon_days": 30, "evidence_entry_ids": ["来源记录id"]}]}
要求：
1. horizon_days 只能是 30、90、180、365 之一，分别代表未来 1 个月 / 3 个月 / 半年 / 一年的目标节点。
2. 每个时间跨度给出 1-3 个节点，总计不超过 8 个。
3. 每个节点必须给出本周第一步和可检查的完成标准；没有证据支撑的方向不要编造。
4. evidence_entry_ids 只能引用提供的记录 id，可为空但要说明“记录不足，先观察”。
5. 语气务实、鼓励；内容具体可执行，避免空话。"""


def generate_roadmap(direction: str) -> None:
    knowledge = db.q(
        "SELECT title FROM knowledge WHERE status='accepted' ORDER BY created_at DESC LIMIT 30"
    )
    recent_rows = db.q(
        "SELECT id, title, summary, content, occurred_at FROM entries WHERE deleted_at IS NULL "
        "AND exclude_from_ai=0 ORDER BY occurred_at DESC LIMIT 20"
    )
    recent = len(recent_rows)
    recent_text = "\n".join(
        f"[entry_id={r['id']} date={r['occurred_at'][:10]}] "
        f"{(r['title'] or r['summary'] or r['content'] or '')[:160]}"
        for r in recent_rows
    )

    user_text = (
        f"目标方向：{direction}\n"
        f"已沉淀的经验知识：{('、'.join(k['title'] for k in knowledge)) or '暂无'}\n"
        f"最近可用记录（共 {recent} 条，引用时只能使用下面的 entry_id）：\n{recent_text}\n"
        "请输出成长路线 JSON。"
    )
    user_text += _circle_inject("路线生成")
    data = _chat_json([
        {"role": "system", "content": ROADMAP_SYSTEM},
        {"role": "user", "content": user_text},
    ], slot="strong")
    nodes = data.get("nodes") if isinstance(data, dict) else None
    if not isinstance(nodes, list):
        raise AIError("AI 未能生成有效的路线节点，请重试")

    valid = []
    valid_ids = {r["id"] for r in recent_rows}
    for item in nodes[:8]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        try:
            horizon = int(item.get("horizon_days"))
        except (TypeError, ValueError):
            horizon = 30
        horizon = min(HORIZONS, key=lambda h: abs(h - horizon))
        evidence = [str(x) for x in (item.get("evidence_entry_ids") or []) if str(x) in valid_ids][:3]
        valid.append((title, str(item.get("description") or "").strip(), horizon,
                      str(item.get("first_step") or "").strip()[:120],
                      str(item.get("done_when") or "").strip()[:160], evidence))
    if not valid:
        raise AIError("AI 未能生成有效的路线节点，请重试")

    with db.locked() as c:
        # 只删除仍处 suggested 状态的旧节点，保留用户已处理（accepted/rejected/done）的
        c.execute("DELETE FROM roadmap_nodes WHERE status='suggested'")
        for i, (title, desc, horizon, first_step, done_when, evidence) in enumerate(valid):
            c.execute(
                "INSERT INTO roadmap_nodes(id, title, description, horizon_days, status, "
                "sort_order, created_at, evidence_json, first_step, done_when) VALUES(?,?,?,?,'suggested',?,?,?,?,?)",
                (db.new_id(), title, desc, horizon, i, db.now_iso(),
                 json.dumps(evidence, ensure_ascii=False), first_step, done_when),
            )
        c.commit()


# ---------------- 情景推演 ----------------

FORECAST_SYSTEM = """你是一位数据解读顾问。程序已经算好了用户成长记录的数值趋势，你的任务只是解读这些数字，并把每个情景变成可观察、可行动的 90 天实验。
只输出一个 JSON 对象（不要输出其他文字）：
{"scenarios": [{"name": "基准", "horizon_days": 90, "narrative": "情景描述", "assumptions": ["成立前提"], "confidence": "中", "invalidators": ["会使情景失效的信号"], "next_actions": ["下一步可执行动作"], "watch_signals": ["每周观察的信号"], "review_by": "YYYY-MM-DD"}]}
要求：
1. scenarios 恰好三个，name 依次为 基准、积极、保守，horizon_days 都是 90。
2. confidence 只能是 低、中、高。
3. 基准 = 按当前斜率延续；积极 = 趋势向好且能够保持；保守 = 趋势回落的风险。
4. 严禁预测疾病、绩效评级、升职、离职等确定性结果，只描述记录习惯与成长投入的趋势。
5. 数值趋势以用户提供的数据为准，不要自己编造数字。
6. 每个情景必须给 1-3 个 next_actions（本周或本月能做的动作）、1-3 个 watch_signals（可在记录中观察的信号），不要写空泛口号。
7. review_by 必须是未来约 90 天的复盘日期；无法确定时留空。"""

SCENARIO_NAMES = ("基准", "积极", "保守")


def generate_forecast() -> dict:
    try:
        inputs = stats_mod.forecast_inputs()
    except ValueError as e:
        raise InsufficientDataError(int(e.args[0]))

    s = dict(inputs["stats"])
    counts = [int(w.get("count") or 0) for w in s.get("weekly_counts", [])]
    if counts:
        mean = sum(counts) / len(counts)
        variance = sum((x - mean) ** 2 for x in counts) / max(len(counts), 1)
        std = math.sqrt(variance)
        weeks = 13
        baseline = max(0, round((s.get("moving_avg_4") or mean) * weeks))
        margin = max(1, round(1.96 * std * math.sqrt(weeks)))
        s["projection_90d"] = {
            "baseline_entries": baseline,
            "low_entries": max(0, baseline - margin),
            "high_entries": baseline + margin,
            "sample_weeks": len(counts),
            "method": "近4周均值 × 13周；区间为历史周波动的程序估计",
        }
    weekly_str = "、".join(f"{w['week']}:{w['count']}条" for w in s["weekly_counts"])
    user_text = (
        "以下是程序统计出的记录趋势数据（单位：ISO 周）：\n"
        f"- 逐周记录数：{weekly_str}\n"
        f"- 最小二乘斜率：{s['slope']:+.3f} 条/周（正数表示上升趋势）\n"
        f"- 最近 4 周移动平均：{s['moving_avg_4']} 条/周\n"
        f"- 累计记录：{s['total_entries']} 条，约 {s['total_words']} 字\n"
        "请基于这些数据输出三个 90 天情景（基准/积极/保守）的 JSON。"
        "不要输出单点确定预测；请在 narrative 中明确这是记录投入的趋势，不是对现实结果的承诺。"
    )
    user_text += _circle_inject("情景推演")
    data = _chat_json([
        {"role": "system", "content": FORECAST_SYSTEM},
        {"role": "user", "content": user_text},
    ], slot="strong")
    raw_scenarios = data.get("scenarios") if isinstance(data, dict) else None
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise AIError("AI 未能生成有效的情景推演，请重试")

    scenarios = []
    review_default = (date.today() + timedelta(days=90)).isoformat()
    for i, item in enumerate(raw_scenarios[:3]):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name not in SCENARIO_NAMES:
            name = SCENARIO_NAMES[min(i, 2)]
        confidence = str(item.get("confidence") or "").strip()
        if confidence not in ("低", "中", "高"):
            confidence = "中"
        review_by = str(item.get("review_by") or "").strip()[:10]
        try:
            review_date = date.fromisoformat(review_by)
            if review_date < date.today() + timedelta(days=30):
                review_by = review_default
        except ValueError:
            review_by = review_default
        scenarios.append({
            "name": name,
            "horizon_days": 90,
            "narrative": str(item.get("narrative") or "").strip(),
            "assumptions": _str_list(item.get("assumptions")),
            "confidence": confidence,
            "invalidators": _str_list(item.get("invalidators")),
            "next_actions": _str_list(item.get("next_actions"))[:3],
            "watch_signals": _str_list(item.get("watch_signals"))[:3],
            "review_by": review_by,
        })
    if not scenarios:
        raise AIError("AI 未能生成有效的情景推演，请重试")

    forecast_id = db.new_id()
    generated_at = db.now_iso()
    db.execute(
        "INSERT INTO forecasts(id, generated_at, data_weeks, stats_json, scenarios_json) "
        "VALUES(?,?,?,?,?)",
        (forecast_id, generated_at, inputs["data_weeks"],
         json.dumps(s, ensure_ascii=False), json.dumps(scenarios, ensure_ascii=False)),
    )
    return {
        "id": forecast_id,
        "generated_at": generated_at,
        "data_weeks": inputs["data_weeks"],
        "stats": s,
        "scenarios": scenarios,
    }


def forecast_detail(row) -> dict:
    try:
        stats_data = json.loads(row["stats_json"] or "{}")
    except (TypeError, ValueError):
        stats_data = {}
    if not isinstance(stats_data, dict):
        stats_data = {}
    try:
        scenarios = json.loads(row["scenarios_json"] or "[]")
    except (TypeError, ValueError):
        scenarios = []
    if not isinstance(scenarios, list):
        scenarios = []
    return {
        "id": row["id"],
        "generated_at": row["generated_at"],
        "data_weeks": row["data_weeks"],
        "stats": stats_data,
        "scenarios": scenarios,
    }


# ---------------- 成长轨迹总结（现状为主） ----------------

SUMMARY_SYSTEM = """你是一位温暖、真诚的成长导师（mentor）。用户把你当作记录每日工作日志的伙伴，
现在请你总结他/她【当下已经获得的成长】，而不是未来规划。

只输出一个 JSON 对象（不要输出任何其他文字，不要用 markdown 代码块包裹）：
{
  "narrative": "一段温暖真诚的现状总结，150-250字，像一位了解他/她的 mentor 的口吻",
  "milestones": [{"when": "YYYY-MM-DD 或 YYYY年M月", "title": "里程碑标题", "detail": "具体发生了什么、体现了什么成长", "evidence_entry_ids": ["来源记录id"], "evidence_quote": "来源记录中的原文短句"}],
  "strengths": ["已经显现的优势短语"]
}
要求：
1. milestones 5-10 条，按时间先后排列，必须基于记录里的真实事件与真实日期，不得编造、不得把不同时间的事揉成一件；detail 要具体。每条必须给出 1-2 个 evidence_entry_ids 和一段能在原文中逐字找到的 evidence_quote；找不到原文就不要写该里程碑。
2. 若记录里出现环境变化信号（入职、离职、换岗、换项目、搬家等），milestones 要按变化前后自然分段，narrative 里点明这个转折，不要把两段经历混为一谈。
3. strengths 3-6 条短语。
4. 不要写未来建议，只总结已经发生的成长。
5. 任何“能力/性格/进步”判断都必须能回指到 evidence_quote；证据不足时写“记录不足，待核对”，不要用常识补全。"""


def generate_growth_summary() -> dict:
    cutoff = (date.today() - timedelta(days=60)).isoformat()
    rows = db.q(
        "SELECT * FROM entries WHERE deleted_at IS NULL AND exclude_from_ai=0 "
        "AND substr(occurred_at,1,10) >= ? ORDER BY occurred_at",
        (cutoff,),
    )
    if len(rows) < 3:
        # 最近 60 天不足则放宽到全部记录
        rows = db.q(
            "SELECT * FROM entries WHERE deleted_at IS NULL AND exclude_from_ai=0 "
            "ORDER BY occurred_at"
        )
    if len(rows) < 3:
        raise NoDataError("记录还太少，先写几天日志再来吧")

    knowledge = db.q(
        "SELECT title FROM knowledge WHERE status='accepted' ORDER BY created_at DESC LIMIT 30"
    )
    user_text = (
        f"以下是他/她的 {len(rows)} 条日志记录（最近的记录），"
        f"请按要求输出成长现状总结 JSON：\n\n{_entries_text(rows)}"
    )
    if knowledge:
        user_text += "\n\n已沉淀的经验知识：" + "、".join(k["title"] for k in knowledge)
    user_text += _circle_inject("成长总结")
    source_hash = hashlib.sha256(user_text.encode("utf-8")).hexdigest()

    source_by_id = {r["id"]: _entry_verifiable_text(r) for r in rows}
    data = _chat_json([
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content": user_text},
    ], slot="strong")
    if not isinstance(data, dict):
        raise AIError("AI 返回的内容结构不正确，请重试")

    narrative = str(data.get("narrative") or "").strip()
    milestones = []
    for item in data.get("milestones") or []:
        if isinstance(item, dict):
            title = str(item.get("title") or "").strip()
            detail = str(item.get("detail") or "").strip()
            when = str(item.get("when") or "").strip()
            ids = [str(x) for x in (item.get("evidence_entry_ids") or []) if str(x) in source_by_id][:2]
            quote = str(item.get("evidence_quote") or "").strip()[:120]
            valid_quote = bool(quote and any(quote in source_by_id[eid] for eid in ids))
            if (title or detail) and ids and valid_quote:
                milestones.append({"when": when, "title": title, "detail": detail,
                                   "evidence_entry_ids": ids, "evidence_quote": quote})
    milestones = milestones[:10]
    strengths = _str_list(data.get("strengths"))[:6]
    if not narrative or not milestones:
        raise AIError("AI 未能生成有效的成长总结，请重试")

    summary_id = db.new_id()
    created_at = db.now_iso()
    db.execute(
        "INSERT INTO growth_summaries(id, created_at, analysis_json, entry_count, source_hash) "
        "VALUES(?,?,?,?,?)",
        (summary_id, created_at,
         json.dumps({"narrative": narrative, "milestones": milestones, "strengths": strengths},
                    ensure_ascii=False),
         len(rows), source_hash),
    )
    return {"summary": {
        "id": summary_id,
        "created_at": created_at,
        "narrative": narrative,
        "milestones": milestones,
        "strengths": strengths,
        "entry_count": len(rows),
    }}


def summary_detail(row) -> dict:
    try:
        stored = json.loads(row["analysis_json"] or "{}")
    except (TypeError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "narrative": stored.get("narrative", ""),
        "milestones": stored.get("milestones", []),
        "strengths": stored.get("strengths", []),
        "entry_count": row["entry_count"],
    }


# ---------------- 手写笔记拍照识别（多页） ----------------

SCAN_NOTE_SYSTEM = """你是手写笔记识别助手。用户拍了一组纸质手写笔记的照片（可能多页），请识别其中的内容。
只输出一个 JSON 对象（不要输出任何其他文字，不要用 markdown 代码块包裹）：
{
  "occurred_date": "YYYY-MM-DD 或 null（笔记上写的日期，没有就 null）",
  "weather_text": "笔记上写的天气，没有就 null",
  "title": "笔记标题，不超过15字，没有就 null",
  "content": "完整转写正文，保留原有小节结构如【工作内容】，无法辨认的字用□，不要编造",
  "figures": [{"label": "区域说明", "box": [x1, y1, x2, y2], "page": 1}],
  "is_work": true 或 false 或 null
}
figures 用于圈出笔记中手绘图、表格、示意图所在区域，坐标为 0~1 归一化的 [左, 上, 右, 下]，
page 为该图所在的页码（从 1 开始，与图片上传顺序一致），没有就空数组。
is_work 判定：内容与实习/工作/求职/职业技能直接相关→true，其余→false，拿不准→null。"""

SCAN_FAIL_HINT = "（若是模型不支持图片输入，可在设置页关闭/更换视觉模型）"


def scan_note_image(images: list[bytes]) -> dict:
    """识别多页手写笔记照片（1~9 张），一次 chat 调用带全部图片。失败抛 AIError（中文）。"""
    _require_settings()
    n = len(images)
    if n > 1:
        user_text = (f"以下是同一本手写笔记按上传顺序的第 1 页到第 {n} 页，"
                     "请把多页内容按顺序合并转写为一条日志，按要求输出 JSON。")
    else:
        user_text = "识别这张手写/纸质笔记照片，按要求输出 JSON。"

    content_parts: list = [{"type": "text", "text": user_text}]
    try:
        from PIL import Image
        for img_bytes in images:
            with Image.open(io.BytesIO(img_bytes)) as im:
                im.thumbnail((1600, 1600))
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "JPEG", quality=85)
            b64 = base64.b64encode(buf.getvalue()).decode()
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })
    except Exception:
        raise AIError("图片处理失败，请换一张清晰的照片")

    messages = [
        {"role": "system", "content": SCAN_NOTE_SYSTEM},
        {"role": "user", "content": content_parts},
    ]
    try:
        data = _chat_json(messages, slot="strong")
        if not isinstance(data, dict):
            raise AIError("AI 返回的内容结构不正确，请重试")
        return _validate_scan(data, n)
    except AIError as e:
        raise AIError(f"识别失败：{e}{SCAN_FAIL_HINT}")


def _validate_scan(data: dict, page_count: int) -> dict:
    occurred_date = None
    od = data.get("occurred_date")
    if isinstance(od, str) and od.strip():
        try:
            occurred_date = date.fromisoformat(od.strip()).isoformat()
        except ValueError:
            occurred_date = None

    weather_text = data.get("weather_text")
    weather_text = str(weather_text).strip()[:50] if weather_text else None
    if not weather_text:
        weather_text = None

    title = data.get("title")
    title = title.strip()[:15] if isinstance(title, str) and title.strip() else None

    content = str(data.get("content") or "").strip()

    raw_is_work = data.get("is_work")
    is_work = raw_is_work if isinstance(raw_is_work, bool) else None

    figures = []
    raw_figures = data.get("figures")
    if isinstance(raw_figures, list):
        for fig in raw_figures:
            if not isinstance(fig, dict):
                continue
            box = fig.get("box")
            if not isinstance(box, (list, tuple)) or len(box) != 4:
                continue
            try:
                coords = [float(v) for v in box]
            except (TypeError, ValueError):
                continue
            try:
                page = int(fig.get("page", 1))
            except (TypeError, ValueError):
                continue
            if not 1 <= page <= page_count:
                continue
            figures.append({"label": str(fig.get("label") or "").strip(),
                            "box": coords, "page": page})

    return {
        "occurred_date": occurred_date,
        "weather_text": weather_text,
        "title": title,
        "content": content,
        "figures": figures,
        "is_work": is_work,
    }


# ---------------- 嵌入模型槽位与语义检索 ----------------

def _embed_settings() -> tuple[str, str, str] | None:
    """embed_model 必填；url/key 空时回落 strong 的 url/key。"""
    model = db.get_setting("embed_model").strip()
    if not model:
        return None
    base = db.get_setting("embed_base_url").strip() or db.get_setting("ai_base_url").strip()
    key = db.get_setting("embed_key").strip() or db.get_setting("ai_key").strip()
    if not (base and key):
        return None
    return base, key, model


def embed_configured() -> bool:
    return _embed_settings() is not None


def _embed_url(base: str) -> str:
    """宽容处理嵌入接口地址：已是完整端点就用，/v1 结尾补 /embeddings，否则补 /v1/embeddings。"""
    b = base.rstrip("/")
    if b.endswith("/embeddings"):
        return b
    if b.endswith("/embedding"):  # 常见的单数笔误
        return b + "s"
    return b + "/embeddings" if "/v1" in b else b + "/v1/embeddings"


def embed_text(text: str) -> list[float] | None:
    """调用 /embeddings。30s 超时，网络错误重试 2 次；任何失败静默返回 None。"""
    cfg = _embed_settings()
    if not cfg or not text or not text.strip():
        return None
    base, key, model = cfg
    url = _embed_url(base)
    payload = {"model": model, "input": text[:8000]}
    for attempt in range(3):  # 1 次正式 + 网络错误重试 2 次
        try:
            with httpx.Client(timeout=30.0) as c:
                r = c.post(url, json=payload, headers={"Authorization": f"Bearer {key}"})
            if r.status_code != 200:
                return None
            vec = r.json()["data"][0]["embedding"]
            return [float(x) for x in vec]
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError):
            continue
        except Exception:
            return None
    return None


def _entry_embed_text(row) -> str:
    """入库文本 = title + summary + content[:1000] + 附件文件名 + 附件内容描述（caption）。"""
    atts = db.q("SELECT filename, caption FROM attachments WHERE entry_id=?", (row["id"],))
    parts = [row["title"], row["summary"], (row["content"] or "")[:1000]]
    parts += [a["filename"] for a in atts]
    # caption 让"照片里拍了什么"也能被语义搜到（视觉关闭/老附件没有 caption 就跳过）
    parts += [a["caption"] for a in atts if a["caption"]]
    return "\n".join(p for p in parts if p)


def embed_entry(entry_id: str) -> bool:
    """为单条记录生成/更新向量。未配置或失败静默返回 False。"""
    cfg = _embed_settings()
    if not cfg:
        return False
    row = db.q1("SELECT * FROM entries WHERE id=? AND deleted_at IS NULL AND exclude_from_ai=0", (entry_id,))
    if row is None:
        return False
    vec = embed_text(_entry_embed_text(row))
    if vec is None:
        return False
    db.execute(
        "INSERT INTO entry_embeddings(entry_id, model, vector_json, updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(entry_id) DO UPDATE SET model=excluded.model, "
        "vector_json=excluded.vector_json, updated_at=excluded.updated_at",
        (entry_id, cfg[2], json.dumps(vec), db.now_iso()),
    )
    return True


def embed_knowledge_item(kid: str) -> bool:
    """为单条知识生成/更新向量（供语义判重）。未配置或失败静默返回 False。"""
    if not embed_configured():
        return False
    row = db.q1("SELECT * FROM knowledge WHERE id=?", (kid,))
    if row is None:
        return False
    vec = embed_text(str(row["title"] or "") + "\n" + str(row["content"] or "")[:200])
    if vec is None:
        return False
    db.execute("UPDATE knowledge SET vector_json=? WHERE id=?", (json.dumps(vec), kid))
    return True



def embed_entity(eid: str) -> bool:
    """为圈子实体生成向量（语义查重用，文本=实体名）。失败静默返回 False。"""
    if not embed_configured():
        return False
    row = db.q1("SELECT id, name FROM entities WHERE id=?", (eid,))
    if row is None:
        return False
    vec = embed_text(row["name"])
    if vec is None:
        return False
    db.execute("UPDATE entities SET vector_json=? WHERE id=?",
               (json.dumps(vec), eid))
    return True

def cosine(a: list, b: list) -> float:
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def semantic_topk(query_vec: list, k: int = 8, exclude_id: str | None = None) -> list[tuple[str, float]]:
    """纯 Python 余弦相似度 top-k（排除已删除 / exclude_from_ai）。"""
    rows = db.q(
        "SELECT e.entry_id, e.vector_json FROM entry_embeddings e "
        "JOIN entries en ON e.entry_id = en.id "
        "WHERE en.deleted_at IS NULL AND en.exclude_from_ai = 0"
    )
    scored = []
    for r in rows:
        if exclude_id and r["entry_id"] == exclude_id:
            continue
        try:
            vec = json.loads(r["vector_json"])
        except ValueError:
            continue
        scored.append((r["entry_id"], cosine(query_vec, vec)))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:k]


# ---------------- 问小满 ----------------

ASK_SYSTEM = """你是用户的好朋友，非常了解他/她的成长记录。请根据给出的记录和（如有）历史事实快照回答用户的问题。
只输出一个 JSON 对象（不要输出任何其他文字，不要用 markdown 代码块包裹）：
{"answer": "回答：温暖、具体，像一位了解TA的朋友；不确定就直说不确定", "entry_ids": ["回答依据的记录id"]}
要求：
1. 只依据给出的记录回答，不要编造记录里没有的事；entry_ids 必须来自给出的记录。
2. 涉及"先后/因果/是不是同一时间"的问题，只能按记录上的日期如实回答；记录里看不出的关系，就明说"从记录里看不出来"，不要脑补。
3. 如果提供了“历史事实快照”，它只代表截至指定日期的状态，优先于当前状态；来源缺失或标注“仅作线索”的内容只能谨慎表述，不能当作确定事实。
4. 不得把今天的关系、身份或状态倒推到过去；没有足够证据时明确说“记录里无法确认”。"""


def ask(question: str, hits: list, temporal_context: str = "") -> tuple[str, list[str]]:
    """基于命中记录回答。返回 (answer, entry_ids)；失败抛 AIError。"""
    blocks = []
    for r in hits:
        blocks.append(
            f"[记录 entry_id={r['id']}]\n"
            f"日期：{r['occurred_at'][:10]}\n"
            f"标题：{r['title'] or '（无标题）'}\n"
            f"正文：{(r['content'] or '')[:600]}"
        )
    user_text = f"用户的问题：{question}\n\n相关记录：\n" + "\n---\n".join(blocks)
    if temporal_context:
        user_text += "\n\n" + temporal_context
    user_text += _circle_inject("问小满", [r["id"] for r in hits])
    data = _chat_json([
        {"role": "system", "content": ASK_SYSTEM},
        {"role": "user", "content": user_text},
    ], slot="strong")
    if not isinstance(data, dict):
        raise AIError("AI 返回的内容结构不正确，请重试")
    answer = str(data.get("answer") or "").strip()
    if not answer:
        raise AIError("AI 未能给出有效回答，请重试")
    valid = {r["id"] for r in hits}
    entry_ids = []
    for x in data.get("entry_ids") or []:
        x = str(x).strip()
        if x in valid and x not in entry_ids:
            entry_ids.append(x)
    return answer, entry_ids


# ---------------- 问小满：时间解析（fast 前置） ----------------

TIME_SCOPE_SYSTEM = """你在解析用户问题里的时间范围。只输出一个 JSON 对象：
{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}；问题里没有可确定的时间范围就输出 null。
能认的：今天/昨天/前天、本周/上周、本月/上个月、N月/N月初/N月中/N月底、最近N天/周/月、今年/去年、具体日期。
今天是 {today}。拿不准就输出 null，不要猜。"""


def parse_time_scope(question: str, today: date) -> tuple[str, str] | None:
    """fast 把「上个月/8月初/上周」解析成 (from, to)；解析不出返回 None。任何失败静默。"""
    try:
        data = _chat_json([
            {"role": "system", "content": TIME_SCOPE_SYSTEM.replace("{today}", today.isoformat())},
            {"role": "user", "content": question[:300]},
        ], slot="fast")
    except AIError:
        return None
    if not isinstance(data, dict):
        return None
    f, t = str(data.get("from") or "").strip(), str(data.get("to") or "").strip()
    try:
        df, dt = date.fromisoformat(f), date.fromisoformat(t)
    except ValueError:
        return None
    # 只接受合理的回顾范围：避免模型把模糊问题误解析成几十年跨度或未来日期。
    if (df > dt or dt > today + timedelta(days=366)
            or df < today - timedelta(days=3650)
            or (dt - df).days > 366):
        return None
    return df.isoformat(), dt.isoformat()


def parse_explicit_time_scope(question: str, today: date) -> tuple[str, str] | None:
    """无需调用模型的安全日期解析兜底。

    只接受含义明确的日期/月份和常见相对词；模糊表达交给
    :func:`parse_time_scope`，避免把问题硬套进错误的时间窗口。
    """
    text = str(question or "").strip().lower()
    if not text:
        return None

    def month_range(year: int, month: int) -> tuple[str, str] | None:
        try:
            first = date(year, month, 1)
            next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
            return first.isoformat(), (next_month - timedelta(days=1)).isoformat()
        except ValueError:
            return None

    # 完整日期：2026-08-15 / 2026年8月15日 / 2026/8/15
    m = re.search(r"(?<!\d)(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*(?:日)?", text)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d.isoformat(), d.isoformat()
        except ValueError:
            return None

    # 明确年月：2025-08 / 2025年8月
    m = re.search(r"(?<!\d)(20\d{2})\s*[年./-]\s*(\d{1,2})\s*(?:月)?(?!\d)", text)
    if m:
        return month_range(int(m.group(1)), int(m.group(2)))

    if "前天" in text:
        d = today - timedelta(days=2)
        return d.isoformat(), d.isoformat()
    if "昨天" in text:
        d = today - timedelta(days=1)
        return d.isoformat(), d.isoformat()
    if "今天" in text:
        return today.isoformat(), today.isoformat()

    if "上个月" in text:
        y, mth = today.year, today.month - 1
        if mth == 0:
            y, mth = y - 1, 12
        return month_range(y, mth)
    if "本月" in text or "这个月" in text:
        return month_range(today.year, today.month)
    if "去年" in text:
        return date(today.year - 1, 1, 1).isoformat(), date(today.year - 1, 12, 31).isoformat()
    if "今年" in text:
        return date(today.year, 1, 1).isoformat(), date(today.year, 12, 31).isoformat()

    # 月份表达：默认取当前年份；若月份明显在当前月份之后，则按上一年处理，
    # 这样“12月复盘”在年末前不会意外指向未来。初/中/底收窄到对应十天段。
    m = re.search(r"(?<!\d)(\d{1,2})\s*月(初|中|底)?", text)
    if m:
        month = int(m.group(1))
        year = today.year - 1 if month > today.month else today.year
        whole = month_range(year, month)
        if not whole:
            return None
        suffix = m.group(2) or ""
        if not suffix:
            return whole
        first = date.fromisoformat(whole[0])
        last = date.fromisoformat(whole[1])
        if suffix == "初":
            return first.isoformat(), min(first.replace(day=10), last).isoformat()
        if suffix == "中":
            return max(first.replace(day=11), first).isoformat(), min(first.replace(day=20), last).isoformat()
        return max(first.replace(day=21), first).isoformat(), last.isoformat()
    return None


# ---------------- 晨间意图 ↔ 晚间复盘对照 ----------------

COMPARE_INTENT_SYSTEM = """用户在早上写下了今天的打算，晚上把实际记录给你对照。
只输出一句话（不超过40字），温柔、不评判；做到了就轻轻肯定，没做到也绝不指责，
对不上就如实温和地说"看来计划有变化"。不要提问、不要说教。"""


def compare_intent(intent: str, digest: str) -> str | None:
    """fast 生成一句「昨天你说要……实际上……」式对照。失败返回 None（静默不出）。"""
    if not intent.strip() or not digest.strip():
        return None
    try:
        text = chat([
            {"role": "system", "content": COMPARE_INTENT_SYSTEM},
            {"role": "user", "content": f"早上的打算：{intent[:100]}\n\n实际记录：\n{digest[:1200]}"},
        ], timeout=30.0, slot="fast", attempts=1)
    except AIError:
        return None
    text = text.strip().strip('"').split("\n")[0].strip()
    return text[:60] or None


# ---------------- 年度故事卡 ----------------

YEAR_CARDS_SYSTEM = """你在为用户做年度回顾故事卡（Wrapped 风）。程序已经算好全年的真实统计数据，并附了记录原文候选。
只输出一个 JSON 对象（不要输出任何其他文字，不要用 markdown 代码块包裹）：
{
  "cards": [{"kind": "card", "big": "超大主视觉（一个大数字或短词）", "title": "小标题，不超过10字", "text": "一句判词，不超过40字，温暖具体"}],
  "quote": {"text": "从候选原文里选一句用户写过的原话（一字不改）", "date": "它所在的日期 YYYY-MM-DD"}
}
规则：
1. cards 6-8 张，一卡只讲一个事实；数字必须与给定统计一致，不得编造。
2. 进行中的年份用「至今」口径说话（如"今年至今"）。
3. quote 必须逐字来自候选原文；没有合适候选就输出 null。
4. 语气：像一位老朋友在年末陪他/她翻相册，克制而温暖，不煽情。"""


def generate_year_cards(stats: dict, quote_candidates: list) -> dict:
    """strong 生成年度故事卡。返回 {"cards": [...], "quote": {...}|None}；失败抛 AIError。"""
    import json as _json
    stats_text = _json.dumps(stats, ensure_ascii=False, indent=1)
    quotes_text = "\n".join(
        f"- [{q['date']}] {q['text']}" for q in quote_candidates[:30]
    ) or "（无候选）"
    data = _chat_json([
        {"role": "system", "content": YEAR_CARDS_SYSTEM},
        {"role": "user", "content": f"全年统计：\n{stats_text}\n\n记录原文候选：\n{quotes_text}"},
    ], slot="strong")
    if not isinstance(data, dict):
        raise AIError("AI 返回的内容结构不正确，请重试")
    cards = []
    for c in data.get("cards") or []:
        if not isinstance(c, dict):
            continue
        big = str(c.get("big") or "").strip()[:20]
        title = str(c.get("title") or "").strip()[:12]
        text = str(c.get("text") or "").strip()[:60]
        if big or text:
            cards.append({"kind": "card", "big": big, "title": title, "text": text})
    if not cards:
        raise AIError("AI 未能生成有效的故事卡，请重试")
    quote = None
    q = data.get("quote")
    if isinstance(q, dict):
        qtext = str(q.get("text") or "").strip()[:120]
        qdate = str(q.get("date") or "").strip()[:10]
        # 防伪：金句必须逐字出现在候选里
        if qtext and any(qtext in c["text"] or c["text"] in qtext for c in quote_candidates):
            quote = {"text": qtext, "date": qdate}
    return {"cards": cards[:8], "quote": quote}


# ---------------- 每日一句 ----------------

MUSE_SYSTEM = """你是用户的记录伙伴。根据TA最近几天的记录摘要，出一个温柔、具体的引导问题，
帮助TA今天继续记录。20字以内，只输出问题本身，不要解释、不要编号。"""


def generate_muse(recent_digest: str) -> str | None:
    """fast 模型生成一句个性化引导问题；失败返回 None。5s 超时。"""
    try:
        text = chat([
            {"role": "system", "content": MUSE_SYSTEM},
            {"role": "user", "content": f"TA 最近的记录：\n{recent_digest}"},
        ], timeout=5.0, slot="fast")
        text = text.strip().strip('"').split("\n")[0].strip()
        return text[:50] if text else None
    except AIError:
        return None


# ---------------- 知识语义级判重 ----------------

def _bigrams(s: str) -> set:
    s = re.sub(r"\s+", "", (s or "").lower())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _knowledge_dedupe_match(ktype: str, title: str, content: str):
    """语义级判重。返回 (匹配的已有行 or None, 本条向量 or None)。

    依次：a) dedupe_key 精确命中；b) embed 余弦 ≥0.85；c) 无嵌入时
    bigram Jaccard ≥0.6（仅 type 相同或一方为 skill）。只防新增，不动存量。
    """
    key = db.knowledge_dedupe_key(title)
    if key:
        row = db.q1(
            "SELECT * FROM knowledge WHERE dedupe_key=? "
            "AND status IN ('pending','accepted','rejected')",
            (key,),
        )
        if row is not None:
            return row, None

    vec = None
    if embed_configured():
        vec = embed_text(title + "\n" + content[:200])
    if vec is not None:
        rows = db.q(
            "SELECT * FROM knowledge WHERE status IN ('pending','accepted','rejected') "
            "AND vector_json IS NOT NULL AND vector_json != ''"
        )
        for r in rows:
            try:
                other = json.loads(r["vector_json"])
            except ValueError:
                continue
            if cosine(vec, other) >= 0.82:
                return r, vec
        return None, vec

    # 无嵌入兜底：bigram Jaccard
    cand = _bigrams(title + content)
    if cand:
        rows = db.q(
            "SELECT * FROM knowledge WHERE status IN ('pending','accepted','rejected')"
        )
        for r in rows:
            if not (r["type"] == ktype or ktype == "skill" or r["type"] == "skill"):
                continue
            if _jaccard(cand, _bigrams((r["title"] or "") + (r["content"] or ""))) >= 0.6:
                return r, None
    return None, None


# ---------------- 附件 AI 自动命名 ----------------

NAME_ATT_SYSTEM = """你是文件命名助手。根据记录正文和附件原文件名，给这个附件起一个描述性的显示名，并写一句内容描述。
只输出一个 JSON 对象（不要输出其他文字）：
{"name": "不超过20字，描述性，不带扩展名，朴实", "caption": "一句话描述附件里是什么，不超过30字"}
不要编造正文里没有的内容。"""


def name_attachment(content_excerpt: str, filename: str,
                    image_path=None, vision: bool = False) -> dict | None:
    """fast 模型为附件起名并写一句内容描述（caption 入嵌入索引）。
    返回 {"name", "caption"}；失败抛 AIError。"""
    user_text = f"记录正文（截选）：{(content_excerpt or '')[:300]}\n原文件名：{filename}"
    user_content = user_text
    if image_path is not None and vision:
        try:
            from PIL import Image
            with Image.open(image_path) as im:
                im.thumbnail((768, 768))
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "JPEG", quality=70)
            b64 = base64.b64encode(buf.getvalue()).decode()
            user_content = [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]
        except Exception:
            user_content = user_text  # 图片处理失败降级纯文本
    data = _chat_json([
        {"role": "system", "content": NAME_ATT_SYSTEM},
        {"role": "user", "content": user_content},
    ], slot="fast")
    if not isinstance(data, dict):
        raise AIError("AI 返回的内容结构不正确，请重试")
    name = str(data.get("name") or "").strip()
    if not name:
        return None
    caption = str(data.get("caption") or "").strip()[:30]
    return {"name": name, "caption": caption}


# ---------------- 成长方向发现 ----------------

DIRECTIONS_SYSTEM = """你是一位了解用户的成长导师。根据用户的成长总结/近期记录、已沉淀的经验和统计，
为他/她发现 2-3 个可行的成长方向（求职/职业能力导向，不要教案、写书这类方向）。
只输出一个 JSON 对象（不要输出任何其他文字，不要用 markdown 代码块包裹）：
{"directions": [{"title": "方向名，不超过12字", "rationale": "为什么适合TA，不超过60字，基于记录说人话", "experiment": "未来14天可完成的小实验", "success_signal": "实验有效时能观察到的信号", "evidence_entry_ids": ["来源记录id"], "evidence_quote": "原文短句"}]}
要求：
1. 每个方向都必须有记录里的真实依据，不要空泛；evidence_quote 必须逐字来自提供的记录。
2. 方向是“值得验证的假设”，不是对用户的定论。experiment 必须低成本、14 天内可完成，success_signal 必须可观察、可核对。
3. 不得用一次偶然事件推断长期能力；证据不足的方向不要输出。"""


def generate_directions() -> list:
    total = db.q1(
        "SELECT COUNT(*) AS n FROM entries WHERE deleted_at IS NULL AND exclude_from_ai=0"
    )["n"]
    if total < 3:
        raise NoDataError("记录还太少，先写几天日志再来吧")

    summary = db.q1("SELECT analysis_json FROM growth_summaries ORDER BY created_at DESC LIMIT 1")
    if summary:
        try:
            stored = json.loads(summary["analysis_json"] or "{}")
        except ValueError:
            stored = {}
        miles = "；".join(m.get("title", "") for m in stored.get("milestones", [])[:6]
                        if isinstance(m, dict))
        context = f"成长现状总结：{stored.get('narrative', '')}\n里程碑：{miles}"
    else:
        rows = db.q(
            "SELECT title, summary, content FROM entries WHERE deleted_at IS NULL "
            "AND exclude_from_ai=0 ORDER BY occurred_at DESC LIMIT 10"
        )
        digest = "\n".join(
            "- " + (r["summary"] or r["title"] or (r["content"] or "")[:60]).strip()
            for r in rows
        )
        context = f"近期记录摘要：\n{digest}"

    knowledge = db.q(
        "SELECT title FROM knowledge WHERE status='accepted' ORDER BY created_at DESC LIMIT 30"
    )
    stats90 = db.q1(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT substr(occurred_at,1,10)) AS days "
        "FROM entries WHERE deleted_at IS NULL AND exclude_from_ai=0 "
        "AND substr(occurred_at,1,10) >= ?",
        ((date.today() - timedelta(days=90)).isoformat(),),
    )
    evidence_rows = db.q(
        "SELECT id, occurred_at, title, summary, content FROM entries WHERE deleted_at IS NULL "
        "AND exclude_from_ai=0 ORDER BY occurred_at DESC LIMIT 16"
    )
    evidence_text = "\n".join(
        f"[entry_id={r['id']} date={r['occurred_at'][:10]}] "
        f"{(r['title'] or r['summary'] or r['content'] or '')[:180]}"
        for r in evidence_rows
    )
    user_text = (
        f"{context}\n"
        f"已采纳的经验知识：{('、'.join(k['title'] for k in knowledge)) or '暂无'}\n"
        f"近 90 天：{stats90['n']} 条记录、{stats90['days']} 天有记录\n"
        f"可引用的近期记录：\n{evidence_text}\n"
        "请输出成长方向 JSON。"
    )
    user_text += _circle_inject("方向发现")
    data = _chat_json([
        {"role": "system", "content": DIRECTIONS_SYSTEM},
        {"role": "user", "content": user_text},
    ], slot="strong")
    if not isinstance(data, dict):
        raise AIError("AI 返回的内容结构不正确，请重试")

    source_by_id = {r["id"]: _entry_verifiable_text(r) for r in evidence_rows}
    directions = []
    for item in data.get("directions") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:12]
        rationale = str(item.get("rationale") or "").strip()[:60]
        experiment = str(item.get("experiment") or "").strip()[:100]
        success_signal = str(item.get("success_signal") or "").strip()[:100]
        ids = [str(x) for x in (item.get("evidence_entry_ids") or []) if str(x) in source_by_id][:2]
        quote = str(item.get("evidence_quote") or "").strip()[:120]
        if (title and rationale and experiment and success_signal and ids and quote
                and any(quote in source_by_id[eid] for eid in ids)):
            directions.append({"title": title, "rationale": rationale,
                               "experiment": experiment, "success_signal": success_signal,
                               "evidence_entry_ids": ids, "evidence_quote": quote})
    directions = directions[:3]
    if not directions:
        raise AIError("AI 未能发现有效的成长方向，请重试")
    return directions


# ---------------- 对话式记录（小满） ----------------

CHECKIN_SYSTEM = """你是"小满"，用户温柔的记录伙伴，陪他/她把今天的经历聊出来、记下来。
规则：
1. 只输出一个 JSON 对象（不要输出任何其他文字，不要用 markdown 代码块包裹）：
{"reply": "你说的话", "done": false, "entry_content": null}
2. 用户还没说出什么实质内容、或信息还单薄且对话未满 4 轮时：继续温柔地追问一个具体的问题，done=false。
3. 信息足够了：温柔收尾（感谢、肯定、一句鼓励），done=true，并把对话整理成 entry_content：
   格式为 "【小满陪我聊的】\\n问：…\\n答：…"（逐轮整理，保留要点，不要编造）。
4. reply 口语化、简短（不超过60字），像朋友聊天，不要说教。
5. entry_content 只在 done=true 时给出，否则为 null。"""


def _checkin_context() -> str:
    rows = db.q(
        "SELECT title, content FROM entries WHERE deleted_at IS NULL AND exclude_from_ai=0 "
        "AND substr(occurred_at,1,10) >= ? ORDER BY occurred_at DESC LIMIT 10",
        ((date.today() - timedelta(days=3)).isoformat(),),
    )
    if not rows:
        return "（近 3 天没有记录）"
    return "\n".join(
        "- " + ((r["title"] + "：" if r["title"] else "") + (r["content"] or "")[:200]).strip()
        for r in rows
    )


def checkin_reply(history: list) -> dict:
    """小满对聊。返回 {reply, done, entry_content}；失败抛 AIError。"""
    messages = [{"role": "system",
                 "content": CHECKIN_SYSTEM + "\n\nTA 最近 3 天的记录摘要：\n" + _checkin_context()
                            + _circle_inject("小满对聊")}]
    clean_history = []
    for m in history[:20]:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("assistant", "user"):
            continue
        clean_history.append({"role": role, "content": str(m.get("content") or "")[:1000]})
    if clean_history:
        messages += clean_history
    else:
        messages.append({"role": "user",
                         "content": "（我还没说话）请基于我的近况，生成一句具体的开场追问，不超过40字。"})

    data = _chat_json(messages, slot="fast")
    if not isinstance(data, dict):
        raise AIError("AI 返回的内容结构不正确，请重试")
    reply = str(data.get("reply") or "").strip()
    if not reply:
        raise AIError("AI 没有给出回复，请重试")
    done = bool(data.get("done"))
    entry_content = None
    if done:
        entry_content = str(data.get("entry_content") or "").strip()
        if not entry_content:
            # AI 没整理就自己动手：按对话轮次拼接
            lines = ["【小满陪我聊的】"]
            for m in clean_history:
                lines.append(("问：" if m["role"] == "assistant" else "答：") + m["content"])
            entry_content = "\n".join(lines)
    return {"reply": reply, "done": done, "entry_content": entry_content}


# ---------------- 心情推断 ----------------

MOOD_SYSTEM = """你是情绪观察助手。根据一条个人日志，判断记录者当时的心情。
只输出 JSON：{"score": 1到5的整数, "label": "一个简短心情词，如 平静/开心/疲惫/焦虑/充实"}
确实看不出来就输出 {"score": null, "label": null}。不要过度解读。"""


def infer_mood(title: str, content: str) -> tuple[int | None, str | None]:
    """fast 推断心情。返回 (score, label)，看不出或失败返回 (None, None)。"""
    try:
        data = _chat_json([
            {"role": "system", "content": MOOD_SYSTEM},
            {"role": "user", "content": f"标题：{title or '（无）'}\n正文：{(content or '')[:1000]}"},
        ], slot="fast")
    except AIError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    score = data.get("score")
    try:
        score = int(score)
        if not 1 <= score <= 5:
            score = None
    except (TypeError, ValueError):
        score = None
    if score is None:
        return None, None
    label = str(data.get("label") or "").strip()[:8] or None
    return score, label


# ---------------- 明日接力 ----------------

INTENTS_SYSTEM = """你从用户的日志里提取他/她"明天或接下来打算做的事"。
只输出 JSON：{"items": ["短语，每条不超过15字"]}，最多 5 条；没有就输出 {"items": []}。

严格规则：
1. 只提取"计划/打算/要做"的事——即用户明确说接下来会去做的。
2. **排除一切否定和搁置**："暂缓/推迟/不做了/先不做/取消/算了"等表述的事一律不要提取
   （例如"暂缓制作报销单"不是计划，不要提取）。
3. 措辞要自然 actionable，像给明天的自己留的便签：动词开头、去掉"近期/打算/需要"等冗词
   （如"近期驾驶观光车"→"开观光车"）。
4. 只提取日志里明确提到的打算，不要编造。"""


def extract_intents(digest: str) -> list[str]:
    """fast 提取明日打算。失败抛 AIError。"""
    data = _chat_json([
        {"role": "system", "content": INTENTS_SYSTEM},
        {"role": "user", "content": f"昨天的日志：\n{digest}"},
    ], slot="fast")
    if not isinstance(data, dict):
        raise AIError("AI 返回的内容结构不正确，请重试")
    return [t[:15] for t in _str_list(data.get("items"))[:5]]


# ---------------- 把今天变成故事 ----------------

STORY_SYSTEM = """你是一位温暖的散文作者。把用户的一条个人日志润色成一篇 300-600 字的小文章。
要求：
1. 保留全部事实，不改变因果关系；第一人称；温暖散文风；有头有尾。
2. 只输出一个 JSON 对象（不要输出任何其他文字，不要用 markdown 代码块包裹）：
{"title": "文章标题，不超过15字", "content": "文章正文"}
3. 不要编造记录里没有的事实。"""


def generate_story(row) -> dict:
    """把一条记录润色成小文章。返回 {title, content}；失败抛 AIError。"""
    lines = [f"日期：{row['occurred_at'][:16].replace('T', ' ')}"]
    if row["location_name"]:
        lines.append(f"地点：{row['location_name']}")
    try:
        w = json.loads(row["weather_json"]) if row["weather_json"] else None
    except ValueError:
        w = None
    wtext = _fmt_weather(w)
    if wtext:
        lines.append(f"天气：{wtext}")
    if row["title"]:
        lines.append(f"原标题：{row['title']}")
    lines.append(f"正文：\n{(row['content'] or '')[:4000]}")
    atts = db.q("SELECT filename, kind FROM attachments WHERE entry_id=?", (row["id"],))
    if atts:
        lines.append("附件：" + "；".join(f"{a['filename']}({a['kind']})" for a in atts))

    rewrite_model = db.get_setting("ai_rewrite_model").strip() or None
    data = _chat_json([
        {"role": "system", "content": STORY_SYSTEM},
        {"role": "user", "content": "请把这条日志润色成小文章：\n\n" + "\n".join(lines)
                                   + _circle_inject("写成故事", [row["id"]])},
    ], slot="strong", model_override=rewrite_model)
    if not isinstance(data, dict):
        raise AIError("AI 返回的内容结构不正确，请重试")
    content = str(data.get("content") or "").strip()
    if not content:
        raise AIError("AI 没有生成文章正文，请重试")
    title = str(data.get("title") or "").strip()[:15] or "今日故事"
    return {"title": title, "content": content}


# ---------------- 圈子：提及提取与档案综合 ----------------

CIRCLE_EXTRACT_SYSTEM = """从用户日志中提取明确出现的人物、地点、事件名词。
只输出一个 JSON 对象（不要输出任何其他文字，不要用 markdown 代码块包裹）：
{"mentions": [{"name": "名称", "type": "person|place|event", "snippet": "原文中相关片段，不超过50字",
               "certainty": "explicit|inferred|ambiguous",
               "known_as": "（可选）花名册里的名字", "unsure": false}]}
要求：
1. 最多 8 个，只提明确出现的；"公司""家里"这类泛指不算。
2. **只提取有持续意义的具体对象**：具体的人、具体的地点、具体的事件。
   天气、三餐、快递、日常琐事这类一闪而过的内容一律不提取（它们不构成"档案"）。
3. 人物用记录里的称呼（如"张工""老王"），地点/事件用记录里的叫法。
4. snippet 必须是原文片段，不要改写。
5. 系统会附上一份已知档案花名册：遇到称呼优先归入花名册里的同类型对象；确信时在该项
   给出 known_as（值为花名册里的名字）；觉得可能但拿不准时，给 known_as 并标 "unsure": true。
6. certainty 分级：
   - explicit：用户原文明写的事实（如"今天大雨""张工是我直属领导""8月6日喷灌作业"）；
   - inferred：你根据上下文合理推断的；
   - ambiguous：称呼指向拿不准、可能指多人/多个地方的。
   拿不准不要逞强，标 ambiguous。"""


def _person_roster(limit: int = 40) -> str:
    """启用中（active/confirmed）的全类型档案花名册：人物/地点/事件，名字+别名+关系，供消歧。"""
    from . import db as _db
    rows = _db.q(
        "SELECT name, type, aliases_json, relation_to_user FROM entities "
        "WHERE status IN ('active','confirmed') "
        "ORDER BY CASE status WHEN 'confirmed' THEN 0 ELSE 1 END, last_seen DESC LIMIT ?",
        (limit,),
    )
    type_label = {"person": "人物", "place": "地点", "event": "事件"}
    lines = []
    for r in rows:
        try:
            aliases = json.loads(r["aliases_json"] or "[]")
        except ValueError:
            aliases = []
        parts = r["name"] + f"〔{type_label.get(r['type'], r['type'])}〕"
        if aliases:
            parts += "（别名：" + "、".join(aliases) + "）"
        if r["relation_to_user"]:
            parts += f"｜{r['relation_to_user']}"
        lines.append("- " + parts)
    return "\n".join(lines)
    return "\n".join(lines)


def extract_mentions(title: str, content: str) -> list:
    """fast 提取人物/地点/事件提及（带花名册消歧）。失败抛 AIError。"""
    roster = _person_roster()
    system = CIRCLE_EXTRACT_SYSTEM
    if roster:
        system += ("\n\n已知档案花名册（人物/地点/事件都有）：\n" + roster +
                   "\n遇到称呼时，优先归入同类型的已知档案并给出 known_as；类型对不上或拿不准就不要强行归入。")
    data = _chat_json([
        {"role": "system", "content": system},
        {"role": "user", "content": f"标题：{title or '（无）'}\n正文：\n{(content or '')[:3000]}"},
    ], slot="fast")
    if not isinstance(data, dict):
        raise AIError("AI 返回的内容结构不正确，请重试")
    mentions = []
    for item in data.get("mentions") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:30]
        mtype = str(item.get("type") or "").strip()
        snippet = str(item.get("snippet") or "").strip()[:80]
        if not (name and mtype in ("person", "place", "event") and snippet):
            continue
        known_as = str(item.get("known_as") or "").strip()[:30] or None
        certainty = str(item.get("certainty") or "").strip()
        if certainty not in ("explicit", "inferred", "ambiguous"):
            certainty = "ambiguous" if item.get("unsure") else "inferred"
        mentions.append({"name": name, "type": mtype, "snippet": snippet,
                         "known_as": known_as, "unsure": bool(item.get("unsure")),
                         "certainty": certainty})
    return mentions[:8]


RESOLVE_SYSTEM = """你在判断记录里的一个称呼到底指谁。只输出 JSON：{"answer": "候选名单里的名字 或 null", "confident": true 或 false}
规则：只有上下文能看出明确指向时才给答案并标 confident=true；看不出来就 answer=null。
宁可说不知道，也不许猜。"""


def resolve_mention_identity(name: str, options: list[str], context: str) -> str | None:
    """拿称呼+候选+上下文给 fast 模型自查：能确定返回候选名，不能返回 None。"""
    if not options:
        return None
    user_text = (f"称呼：「{name}」\n候选：{'、'.join(options)}\n\n"
                 f"相关上下文（用户的记录）：\n{context[:3000]}")
    data = _chat_json([
        {"role": "system", "content": RESOLVE_SYSTEM},
        {"role": "user", "content": user_text},
    ], slot="fast")
    if not isinstance(data, dict):
        return None
    ans = str(data.get("answer") or "").strip()
    if data.get("confident") and ans in options:
        return ans
    return None


SUGGEST_OPTION_SYSTEM = """你在猜记录里的一个称呼最可能指谁。只输出 JSON：{"index": 候选下标（从0开始）或 null}
这是给用户的预选提示，不要求有把握：有倾向就给下标，完全没有线索才给 null。"""


def suggest_question_option(name: str, options: list[str], context: str) -> int | None:
    """fast 为提问卡预选一个最可能的答案下标（猜的，仅高亮用）。失败/无线索返回 None。"""
    if not options:
        return None
    try:
        data = _chat_json([
            {"role": "system", "content": SUGGEST_OPTION_SYSTEM},
            {"role": "user", "content": f"称呼：「{name}」\n候选：{'、'.join(options)}\n\n上下文：\n{context[:1500]}"},
        ], slot="fast")
    except AIError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        idx = int(data.get("index"))
    except (TypeError, ValueError):
        return None
    return idx if 0 <= idx < len(options) else None


# 合并提案：判断两份档案是否同一实体
MERGE_JUDGE_SYSTEM = """你在判断两份档案是否指的是同一个人/地点/事件。
只输出一个 JSON 对象（不要输出任何其他文字）：
{"same": true 或 false, "confidence": 0到1的小数, "reason": "判断依据，不超过40字"}
不确定就输出 false。称呼不同但指向一致（如"老王"和"王师傅"）才算 same。"""


def judge_same_entity(a: dict, b: dict) -> dict:
    """fast 判断两个实体是否同一个。a/b: {name, aliases, profile, relation_to_user, snippets[]}。"""
    def side(x):
        lines = [f"名字：{x['name']}"]
        if x.get("aliases"):
            lines.append("别名：" + "、".join(x["aliases"]))
        if x.get("relation_to_user"):
            lines.append(f"与用户关系：{x['relation_to_user']}")
        if x.get("profile"):
            lines.append(f"档案：{x['profile']}")
        for sn in (x.get("snippets") or [])[:3]:
            lines.append(f"片段：{sn}")
        return "\n".join(lines)

    data = _chat_json([
        {"role": "system", "content": MERGE_JUDGE_SYSTEM},
        {"role": "user", "content": f"档案A：\n{side(a)}\n\n档案B：\n{side(b)}"},
    ], slot="fast")
    if not isinstance(data, dict):
        raise AIError("AI 返回的内容结构不正确，请重试")
    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "same": bool(data.get("same")),
        "confidence": min(max(confidence, 0.0), 1.0),
        "reason": str(data.get("reason") or "").strip()[:40],
    }


RETYPE_SYSTEM = """你在核对一份名单里每个名字的类型：人物 person / 地点 place / 事件 event。
只输出一个 JSON 对象（不要输出任何其他文字）：
{"results": [{"index": 0, "type": "person|place|event|unknown"}]}
规则：
- 人物必须是具体的人（称呼、姓名、角色）；机构/部门/班组/公司 → place。
- 事件 = 具体的活动/仪式/会议/事项（如"喷灌作业""培训"）。
- 植物/动物/物品/抽象概念，或拿不准 → unknown（程序不会动 unknown）。
- 名单每一项都要给出结果，index 与输入顺序一致。"""


def judge_entity_types(items: list) -> dict:
    """fast 批量核对实体类型。items: [{"name", "profile"}]；返回 {index: "person|place|event"}
    （unknown/拿不准的不在结果里）。失败抛 AIError。"""
    lines = []
    for i, it in enumerate(items):
        line = f"{i}. {it.get('name') or ''}"
        prof = (it.get("profile") or "")[:60]
        if prof:
            line += f"（{prof}）"
        lines.append(line)
    data = _chat_json([
        {"role": "system", "content": RETYPE_SYSTEM},
        {"role": "user", "content": "名单：\n" + "\n".join(lines)},
    ], slot="fast")
    out = {}
    if not isinstance(data, dict):
        return out
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        t = str(item.get("type") or "").strip()
        if 0 <= idx < len(items) and t in ("person", "place", "event"):
            out[idx] = t
    return out


CIRCLE_SYNTH_SYSTEM = """你在整理用户的人际/地点/事件档案。根据现有档案、用户备注和最新记录片段，更新档案。
只输出一个 JSON 对象（不要输出任何其他文字，不要用 markdown 代码块包裹）：
{
  "profile": "客观描述，不超过120字",
  "relation_to_user": "与用户的关系，不超过20字；人物类必填，其他类型留空字符串",
  "confidence": 0到1的小数,
  "aliases": ["这个实体的其他叫法"],
  "relations": [{"target_name": "相关实体名", "target_type": "person|place|event", "label": "关系，不超过10字",
                  "snippet": "依据片段", "entry_id": "来源记录ID或空", "certainty": "explicit|inferred|ambiguous",
                  "state": "active|ended|uncertain", "valid_from": "YYYY-MM-DD 或 null", "valid_to": "YYYY-MM-DD 或 null"}],
  "conflict": {"has": false, "detail": ""},
  "suggest_type": "person|place|event；仅当你认为该档案的类型标错了才给，否则为 null",
  "better_name": "更正式/更完整的名字，没有则 null",
  "facts": [{"predicate": "事实谓语，如 任职于/居住在/状态/角色", "object": "事实值",
             "certainty": "explicit|inferred|ambiguous", "valid_from": "YYYY-MM-DD 或 null"}]
}
facts 规则：只写记录片段里有明确依据的事实（如"老王是我同事"→任职于/同事关系）；
发现"离职/搬家/分手/不再"类变化时，facts 里给出该谓语的新值（如 {predicate:"状态", object:"离职"}），
程序会自动把旧值封存进历史，不要改写历史。没有可靠事实就输出空数组。
规则：
1. 用户备注（user_note）是最高事实：只可补充细节，绝不可推翻或改写它。
1b. 如果最新片段与现有档案或用户备注明显矛盾（如档案写"在职"而片段说"离职了"），
    输出 conflict: {"has": true, "detail": "矛盾点，不超过60字"}；没有矛盾就 {"has": false, "detail": ""}。
1c. 类型自查：人物=人；地点=地方/机构/项目地址；事件=活动/仪式/会议/事项。
    若现有类型明显标错（比如一个项目被标成人物），在 suggest_type 里给出正确类型。
2. 证据不足就降低 confidence，不要硬写；没有新信息时 profile 可以基本不变。
3. relations 只写有明确依据的；target_name 用记录里的称呼，target_type 只在片段能判断时填写。
   entry_id 必须来自输入片段中的 entry_id。默认 state=active；只有原文明确说"不再/已经离开/分手/终止合作"
   等关系结束时才用 state=ended，并给出 valid_to（无法确定日期就填 null）。关系未被本轮提及不等于结束，不能凭空封存。
   certainty=ambiguous 时不要改变当前关系，只供用户复核。valid_from 仅填写原文明确或可由记录日期确定的日期。
4. 事件是用户记录里明确发生的事，证据清楚时可以给较高 confidence。
5. better_name：当证据给出了更正式/更完整的名字时给出（如先叫"老王"、后文出现全名"王建国"
   → "王建国"；"赵总" → "赵红"）；不确定或与现名相同就输出 null。
6. 若片段与现有档案明显不是同一个人（同名不同人，如两个"张伟"），输出
   conflict: {"has": true, "detail": "疑似重名：新片段说的是另一位X"}。"""


def synthesize_entity_profile(row, mention_rows) -> dict:
    """strong 综合一个实体的档案。

    mention_rows 支持 [(日期, 片段)]（兼容旧调用）或 [(日期, 片段, entry_id)]；
    给模型 entry_id 能让关系来源回链到具体记录，避免把另一条记录当作证据。
    """
    mention_lines = "\n".join(
        f"- [{item[0]}] [entry_id={item[2]}] {item[1]}" if len(item) >= 3 and item[2]
        else f"- [{item[0]}] {item[1]}"
        for item in mention_rows
    )
    user_text = (
        f"实体：{row['name']}（类型 {row['type']}）\n"
        f"现有档案：{row['profile'] or '（空）'}\n"
        f"与用户的关系（现有）：{row['relation_to_user'] or '（未知）'}\n"
        f"用户备注（最高事实，只可补充不可推翻）：{row['user_note'] or '（无）'}\n"
        f"最近提及片段：\n{mention_lines or '（无）'}"
    )
    data = _chat_json([
        {"role": "system", "content": CIRCLE_SYNTH_SYSTEM},
        {"role": "user", "content": user_text},
    ], slot="strong")
    if not isinstance(data, dict):
        raise AIError("AI 返回的内容结构不正确，请重试")

    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.4
    confidence = min(max(confidence, 0.0), 1.0)

    relations = []
    for item in data.get("relations") or []:
        if not isinstance(item, dict):
            continue
        tname = str(item.get("target_name") or "").strip()[:30]
        ttype = str(item.get("target_type") or "").strip()
        if ttype not in ("person", "place", "event"):
            ttype = ""
        label = str(item.get("label") or "").strip()[:10]
        snippet = str(item.get("snippet") or "").strip()[:80]
        entry_id = str(item.get("entry_id") or "").strip()[:80]
        certainty = str(item.get("certainty") or "").strip()
        if certainty not in ("explicit", "inferred", "ambiguous"):
            certainty = "inferred"
        state = str(item.get("state") or "").strip()
        if state not in ("active", "ended", "uncertain"):
            state = "active"
        valid_from = str(item.get("valid_from") or "").strip()[:10] or None
        valid_to = str(item.get("valid_to") or "").strip()[:10] or None
        if tname and label:
            relations.append({"target_name": tname, "target_type": ttype,
                              "label": label, "snippet": snippet, "entry_id": entry_id,
                              "certainty": certainty, "state": state,
                              "valid_from": valid_from, "valid_to": valid_to})

    conflict = data.get("conflict")
    if not isinstance(conflict, dict):
        conflict = {}
    conflict_has = bool(conflict.get("has"))
    conflict_detail = str(conflict.get("detail") or "").strip()[:60]

    suggest_type = str(data.get("suggest_type") or "").strip()
    if suggest_type not in ("person", "place", "event"):
        suggest_type = ""

    facts = []
    for item in data.get("facts") or []:
        if not isinstance(item, dict):
            continue
        predicate = str(item.get("predicate") or "").strip()[:20]
        obj = str(item.get("object") or item.get("object_text") or "").strip()[:60]
        if not (predicate and obj):
            continue
        fcertainty = str(item.get("certainty") or "").strip()
        if fcertainty not in ("explicit", "inferred", "ambiguous"):
            fcertainty = "inferred"
        valid_from = str(item.get("valid_from") or "").strip()[:10] or None
        facts.append({"predicate": predicate, "object": obj,
                      "certainty": fcertainty, "valid_from": valid_from})

    return {
        "profile": str(data.get("profile") or "").strip()[:200],
        "relation_to_user": str(data.get("relation_to_user") or "").strip()[:30],
        "confidence": confidence,
        "aliases": [a[:30] for a in _str_list(data.get("aliases"))[:8]],
        "relations": relations[:8],
        "conflict": {"has": conflict_has, "detail": conflict_detail},
        "suggest_type": suggest_type,
        "better_name": (str(data.get("better_name") or "").strip()[:30] or None),
        "facts": facts[:8],
    }


# ---------------- 圈子档案注入 ----------------

def _circle_inject(label: str, entry_ids: list | None = None) -> str:
    """生成'背景档案'段落并记一行日志；无档案返回空串。lazy import 避免循环依赖。
    传了 entry_ids（本期报告/命中记录）就按范围精确挑选相关档案，否则取最近活跃的。"""
    try:
        from . import circle as circle_mod, logx
        if entry_ids:
            text, names = circle_mod.circle_context_for_entries(entry_ids)
            if not text:  # 范围内没有相关档案时，退回最近活跃
                text, names = circle_mod.circle_context_with_names()
        else:
            text, names = circle_mod.circle_context_with_names()
        if not text:
            return ""
        logx.log(label, "参考档案：" + "".join(f"《{n}》" for n in names))
        return "\n\n背景档案（由以往记录整理，供理解人物关系）：\n" + text
    except Exception:
        return ""
