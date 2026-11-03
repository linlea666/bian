"""从浏览器 “Copy as cURL” 的文本里提取币安凭证（p20t、csrftoken）。

支持：
- bash 格式：-b 'a=1; p20t=...' 或 -H 'cookie: ...'，-H 'csrftoken: ...'
- Windows cmd 格式：^" 转义、-H ^"csrftoken: ...^"
- 直接粘贴 Cookie 字符串 / 单独的 p20t 值（web. 开头）
提取后只返回这两个值，其余内容（设备指纹、其他 cookie）全部丢弃。
"""
from __future__ import annotations

import re


class CurlParseError(ValueError):
    pass


def _clean(text: str) -> str:
    t = text.replace("^\"", "\"").replace("^\\^\"", "\"").replace("^%", "%").replace("^&", "&")
    t = t.replace("\\\n", " ").replace("^\n", " ").replace("`\n", " ")
    return t


def parse_credentials(text: str) -> tuple[str, str]:
    """返回 (p20t, csrftoken)。缺任何一个都抛 CurlParseError（中文说明）。"""
    if not text or not text.strip():
        raise CurlParseError("内容为空")
    t = _clean(text)
    p20t = ""
    m = re.search(r"(?:^|[;\s'\"])p20t=([A-Za-z0-9._%-]+)", t)
    if m:
        p20t = m.group(1)
    else:
        m = re.search(r"\bweb\.\d+\.[A-F0-9]{16,}\b", t)
        if m:
            p20t = m.group(0)
    csrf = ""
    m = re.search(r"csrftoken\s*[:=]\s*['\"]?([A-Za-z0-9]{16,64})", t, re.IGNORECASE)
    if m:
        csrf = m.group(1)
    if not p20t and not csrf:
        raise CurlParseError("没有找到 p20t 和 csrftoken：请在币安网页按 F12 → Network，对一个 bapi 请求右键 Copy as cURL 后整段粘贴")
    if not p20t:
        raise CurlParseError("找到了 csrftoken，但没有 p20t：这个请求可能是 Service Worker 代发的（不带 Cookie），换一个 bapi 请求再复制")
    if not csrf:
        raise CurlParseError("找到了 p20t，但没有 csrftoken：请复制一个 bapi 请求（请求头里带 csrftoken）的 cURL")
    return p20t, csrf
