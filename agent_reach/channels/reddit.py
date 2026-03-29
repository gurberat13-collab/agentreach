# -*- coding: utf-8 -*-
"""Reddit — check connectivity and proxy configuration."""

import os
import urllib.request
from .base import Channel

_UA = "agent-reach/1.0"
_TIMEOUT = 10


def _reddit_reachable() -> bool:
    """Return True if Reddit JSON API responds with 200 (带 User-Agent)."""
    url = "https://www.reddit.com/r/linux.json?limit=1"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.status == 200
    except Exception:
        return False


class RedditChannel(Channel):
    name = "reddit"
    description = "Reddit 帖子和评论"
    backends = ["JSON API", "Exa"]
    tier = 1

    def can_handle(self, url: str) -> bool:
        from urllib.parse import urlparse
        d = urlparse(url).netloc.lower()
        return "reddit.com" in d or "redd.it" in d

    def check(self, config=None):
        proxy = (config.get("reddit_proxy") if config else None) or os.environ.get("REDDIT_PROXY")
        if proxy:
            return "ok", "代理已配置，可读取帖子。搜索走 Exa"
        # 实际探测连通性（带 User-Agent，符合 Reddit API 要求）
        if _reddit_reachable():
            return "ok", "直连可用（JSON API 响应正常）。搜索走 Exa"
        return "warn", (
            "无代理且 Reddit JSON API 无响应。服务器 IP 可能被封锁。配置代理：\n"
            "  agent-reach configure proxy http://user:pass@ip:port"
        )
