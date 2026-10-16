"""链接元信息抓取：标题/描述。仅 http/https，拒绝私网地址（SSRF 防护）。

- 解析主机名并用 ipaddress 校验**全部**解析结果，只放行公网地址（is_global 白名单）
- 分项超时（connect/read/write/pool）+ 整个抓取过程 8 秒总预算、最多读 2MB、最多 3 跳
- 手动跟随重定向（每跳重新校验），非法 scheme / 内网地址抛 LinkRejected（中文原因）
- 网络失败（含域名无法解析）返回 title=url 兜底，不阻断"离线保存带链接的记录"

已知限制（A4-29，本次未修）：校验用 socket.getaddrinfo，真正连接由 httpx 自己再解析一次
域名，两次解析之间 DNS 记录可改变（DNS 重绑定）。彻底消除需要把连接固定到校验通过的 IP
（自定义 transport resolver）。剩余风险面：攻击者需控制域名与 TTL，且只能取到页面标题/描述。
"""
from __future__ import annotations

import html
import ipaddress
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from . import logx

TIMEOUT = 5.0                 # 兼容旧常量（实际请求走下面的分项超时）
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 3.0
TOTAL_BUDGET = 8.0            # 含全部重定向在内的一次抓取总预算（A4-31）
MAX_REDIRECTS = 3
MAX_BYTES = 2 * 1024 * 1024
UA = {"User-Agent": "growth-vault/1.0 (+link preview)"}

# A4-31：旧写法把 5.0 当整体超时交给 httpx，最多 3 跳重定向时最坏阻塞约 15 秒。
# 改为分项超时 + 总预算，最坏约 8 秒。
_TIMEOUT = httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT,
                         write=READ_TIMEOUT, pool=CONNECT_TIMEOUT)


def _budget_timeout(remain: float) -> httpx.Timeout:
    """把"总预算剩余时间"压进分项超时：每一项都不超过剩余预算，
    connect 仍受 3 秒上限约束（不因为还有 8 秒预算就让一次连接挂满）。"""
    return httpx.Timeout(connect=min(CONNECT_TIMEOUT, remain), read=min(READ_TIMEOUT, remain),
                         write=min(READ_TIMEOUT, remain), pool=min(CONNECT_TIMEOUT, remain))


class LinkRejected(Exception):
    """链接本身不被允许（非 http/https、内网地址等），message 为中文。"""


def _validate_url(url: str) -> str:
    """校验 scheme/host 与解析出的全部地址，返回可安全请求的 URL。

    A4-28：不再用 scheme://netloc/path 重新拼 URL——那会丢掉 query 与 fragment，
    用户加 `?id=123` 的链接时抓到的却是另一个页面（B 站 ?bvid=、YouTube ?v= 全是常态
    失效）。scheme/host 的安全判定已在前面完成，不需要靠重建 URL 保证。

    A4-29 / A4-30：地址判定改为 is_global 白名单（旧代码用黑名单枚举，覆盖不到
    `::ffff:127.0.0.1` 这类 IPv4-mapped 变体）；单个地址无法解析时**拒绝**而不是
    `continue` 放行（旧代码若全部地址都走 continue，函数会带着未校验的 URL 通过）。
    域名解析失败仍按"抓取失败"处理（返回 url 兜底），见下方注释与模块文档的已知限制。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise LinkRejected("仅支持 http/https 链接")
    host = parsed.hostname
    if not host:
        raise LinkRejected("链接格式不正确，缺少主机名")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        # 域名无法解析：这是"抓取失败"而不是"安全判定为内网"。
        # 刻意**不**在这里抛 LinkRejected —— main.py 会在任何写库之前校验链接
        # （_fetch_link_meta_checked，A1-03/A1-14），抛异常会让"离线时保存带链接的
        # 记录"整条失败并返回 400。上层据此走 title=url 兜底，标为未抓取。
        # 残留的 DNS 重绑定窗口见模块文档的"已知限制"。
        return url
    if not infos:
        raise LinkRejected("链接格式不正确，缺少主机名")
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            # 取不到字面量地址（某些平台的简化写法）：无法判定即拒绝，不再 continue 放行
            raise LinkRejected("链接地址无法识别，已拒绝访问")
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped  # ::ffff:127.0.0.1 → 127.0.0.1，按 IPv4 判定
        # 白名单：只放行公网地址。黑名单枚举覆盖不到 IPv4-mapped 等变体（A4-30）
        if not ip.is_global:
            raise LinkRejected("不允许访问内网或保留地址")
    # 只去掉 fragment（纯客户端概念），query 必须保留，否则抓的是另一个页面
    return parsed._replace(fragment="").geturl()


class _HeadMetaParser(HTMLParser):
    """只收集 <title> 与 og:title / og:description / meta description（A4-33）。

    正则版会漏掉 `<meta content='x' property='og:title'/>`（自闭合、无空格）以及属性值
    里含 `>` 的写法（`[^>]+` 直接失配）。HTMLParser 是标准库、按属性解析，没有这些问题。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs) -> None:
        if tag == "title":
            self._in_title = True
            return
        if tag != "meta":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        key = (a.get("property") or a.get("name") or "").strip().lower()
        content = a.get("content") or ""
        if key and content and key not in self.meta:
            self.meta[key] = content

    def handle_endtag(self, tag) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data) -> None:
        if self._in_title:
            self.title_parts.append(data)


def _clean(value: str) -> str:
    """展示安全化（A4-32）：反解实体、去控制字符与尖括号、折叠空白。

    抓来的 title/description 会入库并出现在导出（Markdown/HTML）与前端，旧代码只做
    `html.unescape` + 空白折叠，`<script>`、引号等原样进入数据。这里做一次统一的
    "可安全展示"处理，渲染侧再转义即可，不再依赖单一前端出口。
    """
    text = html.unescape(value or "")
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = text.replace("<", " ").replace(">", " ")
    return re.sub(r"\s+", " ", text).strip()


def _meta(patterns: list[str], text: str) -> str:
    """正则兜底提取（解析器完全没抓到时的老页面畸形写法）。"""
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            value = _clean(m.group(1))
            if value:
                return value
    return ""


_TITLE_PATTERNS = [
    r'<meta[^>]+property=["\']og:title["\'][^>]*?content=["\']([^"\']*)["\']',
    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*?property=["\']og:title["\']',
    r"<title[^>]*>(.*?)</title>",
]
_DESC_PATTERNS = [
    r'<meta[^>]+property=["\']og:description["\'][^>]*?content=["\']([^"\']*)["\']',
    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*?property=["\']og:description["\']',
    r'<meta[^>]+name=["\']description["\'][^>]*?content=["\']([^"\']*)["\']',
    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*?name=["\']description["\']',
]


def _extract(head: str) -> tuple[str, str]:
    """从 HTML 提取 (title, description)：优先 og:，退回 <title>/meta description。"""
    parser = _HeadMetaParser()
    try:
        parser.feed(head)
    except Exception:
        # HTMLParser 对畸形输入一般不抛，这里兜底保证抓取不被解析器异常打断
        pass
    title = parser.meta.get("og:title") or "".join(parser.title_parts)
    description = parser.meta.get("og:description") or parser.meta.get("description") or ""
    title, description = _clean(title), _clean(description)
    if not title:
        title = _meta(_TITLE_PATTERNS, head)
    if not description:
        description = _meta(_DESC_PATTERNS, head)
    return title, description


def fetch_link_meta(url: str) -> dict:
    """抓取 <title>/og:title/og:description/meta description。

    返回 {url, title, description}；抓取失败时 title=url。
    """
    current = _validate_url(url)
    body = ""
    deadline = time.monotonic() + TOTAL_BUDGET
    try:
        with httpx.Client(timeout=_TIMEOUT, headers=UA, follow_redirects=False) as c:
            for _ in range(MAX_REDIRECTS):
                remain = deadline - time.monotonic()
                if remain <= 0:
                    break  # 总预算耗尽：走抓取失败兜底，不再发起新请求（A4-31）
                with c.stream("GET", current, timeout=_budget_timeout(remain)) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            break
                        current = _validate_url(urljoin(current, location))
                        continue
                    if resp.status_code >= 400:
                        raise ValueError(f"HTTP {resp.status_code}")
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes(65536):
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= MAX_BYTES:
                            break
                    body = b"".join(chunks).decode(resp.encoding or "utf-8", errors="ignore")
                    break
    except LinkRejected:
        raise
    except Exception as e:
        # 抓取失败仍按兜底保存（title=url），但不再完全无声：失败原因进本地日志，
        # 否则"标题和链接对不上/抓不到标题"在数据层没有任何诊断出口。
        logx.log("链接抓取失败", f"{current}：{type(e).__name__} {e}"[:200])
        return {"url": url, "title": url, "description": ""}

    title, description = _extract(body[:200000])
    return {
        "url": url,
        "title": (title or url)[:200],
        "description": description[:500],
    }
