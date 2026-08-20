# -*- coding: utf-8 -*-
# src/mcp_web.py
"""
MCP server exposing web-fetch tools to Claude Code subprocesses.
Bypasses Claude Code's built-in WebFetch pre-flight check (which requires
claude.ai to be reachable) by providing equivalent tools via MCP.
"""
import ipaddress
import re
import socket
from html import unescape
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("web-tools")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_MAX_CONTENT_CHARS = 40_000

_BLOCKED_NETS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_private(host: str) -> bool:
    """Resolve hostname and check all IPs against blocked private ranges."""
    try:
        for info in socket.getaddrinfo(host, None):
            addr = ipaddress.ip_address(info[4][0])
            if any(addr in net for net in _BLOCKED_NETS):
                return True
    except socket.gaierror:
        return True
    return False


def _check_url(url: str) -> str | None:
    """Return error message if URL is blocked, else None."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"Unsupported scheme: {parsed.scheme}"
    if not parsed.hostname or _is_private(parsed.hostname):
        return "Access to private/internal addresses is not allowed"
    return None


def _html_to_text(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@mcp.tool()
async def fetch_web(url: str, timeout: int = 15) -> dict:
    """Fetch a web page and return its text content with HTML stripped.

    Args:
        url: The full URL to fetch (e.g. "https://example.com/article").
        timeout: Request timeout in seconds (default 15).

    Returns:
        A dict with:
          - "url": the final URL after redirects
          - "status": HTTP status code, or "error" on failure
          - "content": plain-text page content (HTML tags stripped)
          - "error": error message if the request failed (omitted on success)
    """
    err = _check_url(url)
    if err:
        return {"url": url, "status": "error", "error": err}

    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            resp = await client.get(url)

            if resp.is_redirect:
                location = resp.headers.get("location", "")
                if location:
                    redir_err = _check_url(location)
                    if redir_err:
                        return {"url": url, "status": "error", "error": f"Redirect blocked: {redir_err}"}

                async with httpx.AsyncClient(
                    follow_redirects=True,
                    timeout=timeout,
                    headers={"User-Agent": _USER_AGENT},
                ) as client2:
                    resp = await client2.get(url)

            content = _html_to_text(resp.text)
            if len(content) > _MAX_CONTENT_CHARS:
                content = content[:_MAX_CONTENT_CHARS] + f"\n\n[截断，共 {len(content)} 字符]"
            return {"url": str(resp.url), "status": resp.status_code, "content": content}
    except httpx.HTTPStatusError as e:
        return {"url": url, "status": e.response.status_code, "error": str(e)}
    except Exception as e:
        return {"url": url, "status": "error", "error": str(e)}


if __name__ == "__main__":
    mcp.run()
