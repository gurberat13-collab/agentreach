# -*- coding: utf-8 -*-
"""Twitter/X — check if bird CLI (@steipete/bird) is available."""

import shutil
import subprocess
from .base import Channel


class TwitterChannel(Channel):
    name = "twitter"
    description = "Twitter/X 推文"
    backends = ["bird CLI"]
    tier = 1

    def can_handle(self, url: str) -> bool:
        from urllib.parse import urlparse
        d = urlparse(url).netloc.lower()
        return "x.com" in d or "twitter.com" in d

    def check(self, config=None):
        bird = shutil.which("bird") or shutil.which("birdx")
        if not bird:
            return "warn", (
                "bird CLI 未安装。搜索可通过 Exa 替代。安装：\n"
                "  npm install -g @steipete/bird"
            )

        try:
            r = subprocess.run(
                [bird, "check"], capture_output=True,
                encoding="utf-8", errors="replace", timeout=10
            )
            output = (r.stdout or "") + (r.stderr or "")
            if r.returncode == 0:
                return "ok", "完整可用（读取、搜索推文，含长文/X Article）"
            # bird check returns 1 when auth is missing
            if "Missing credentials" in output or "missing" in output.lower():
                return "warn", (
                    "bird CLI 已安装但未配置认证。设置环境变量：\n"
                    "  export AUTH_TOKEN=\"xxx\"\n"
                    "  export CT0=\"yyy\"\n"
                    "或运行：\n"
                    "  agent-reach configure twitter-cookies \"auth_token=xxx; ct0=yyy\""
                )
            return "warn", (
                "bird CLI 已安装但认证检查失败。运行：\n"
                "  agent-reach configure twitter-cookies \"auth_token=xxx; ct0=yyy\""
            )
        except Exception:
            return "warn", "bird CLI 已安装但连接失败"
