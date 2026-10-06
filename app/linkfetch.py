"""链接元信息抓取：标题/描述。仅 http/https，拒绝私网地址（SSRF 防护）。

- 解析主机名并用 ipaddress 校验所有解析结果，拒绝私网/回环/链路本地等地址
- 5 秒超时、最多读 2MB、手动跟随重定向（每跳重新校验）
- 网络失败时返回 title=url 的兜底结果； scheme 非法/内网地址抛 LinkRejected
"""
from __future__ import annotations

import html
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx

TIMEOUT = 5.0
MAX_BYTES = 2 * 1024 * 1024
UA = {"User-Agent": "growth-vault/1.0 (+link preview)"}


class LinkRejected(Exception):
    """链接本身不被允许（非 http/https、内网地址等），message 为中文。"""


def _validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise LinkRejected("仅支持 http/https 链接")
    host = parsed.hostname
    if not host:
        raise LinkRejected("链接格式不正确，缺少主机名")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        # 域名无法解析属于"抓取失败"，交给网络层兜底（title=url 照常保存）
        return url
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise LinkRejected("不允许访问内网或保留地址")
    return parsed.scheme + "://" + (parsed.netloc or "") + (parsed.path or "")


def _meta(patterns: list[str], text: str) -> str:
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            value = html.unescape(m.group(1)).strip()
            if value:
                return re.sub(r"\s+", " ", value)
    return ""


def fetch_link_meta(url: str) -> dict:
    """抓取 <title>/og:title/og:description/meta description。

    返回 {url, title, description}；抓取失败时 title=url。
    """
    current = _validate_url(url)
    body = ""
    try:
        with httpx.Client(timeout=TIMEOUT, headers=UA, follow_redirects=False) as c:
            for _ in range(3):
                with c.stream("GET", current) as resp:
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
    except Exception:
        return {"url": url, "title": url, "description": ""}

    head = body[:200000]
    title = _meta([
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)["\']',
        r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']og:title["\']',
        r"<title[^>]*>(.*?)</title>",
    ], head)
    description = _meta([
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)["\']',
        r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']og:description["\']',
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']',
        r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']description["\']',
    ], head)
    return {
        "url": url,
        "title": (title or url)[:200],
        "description": description[:500],
    }
