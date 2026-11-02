"""币安 www.binance.com web 接口客户端（只读）。

- 公共接口（bapi .../friendly/...、fapi 行情）无需凭证。
- 私有接口（bapi .../private/...）需要登录 cookie `p20t` + header `csrftoken`，
  只从环境变量 BINANCE_P20T / BINANCE_CSRFTOKEN 读取，绝不写日志或落盘。
- 行情统一走 www.binance.com/fapi：fapi.binance.com 在部分地区返回 451。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE_URL = "https://www.binance.com"
CODE_SUCCESS = "000000"
AUTH_ERROR_CODES = {"100001005", "100002002"}  # 未登录 / 鉴权失败
CODE_PORTFOLIO_CLOSED = "11012030"  # 领航员已关闭带单，或把 copyPortfolioId 当 leadPortfolioId 用

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


class BinanceError(Exception):
    def __init__(self, message: str, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class AuthExpired(BinanceError):
    """p20t/csrftoken 缺失、过期或无效。"""


class RateLimited(BinanceError):
    def __init__(self, message: str, retry_after: float, status: int = 429):
        super().__init__(message, status=status)
        self.retry_after = retry_after


class Client:
    def __init__(
        self,
        p20t: str | None = None,
        csrftoken: str | None = None,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
        min_interval: float = 0.25,
        max_retries: int = 3,
    ):
        self.p20t = (p20t or "").strip() or None
        self.csrftoken = (csrftoken or "").strip() or None
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._last_request = 0.0

    @classmethod
    def from_env(cls, **kw: Any) -> "Client":
        return cls(os.environ.get("BINANCE_P20T"), os.environ.get("BINANCE_CSRFTOKEN"), **kw)

    @property
    def has_credentials(self) -> bool:
        return bool(self.p20t and self.csrftoken)

    # ------------------------------------------------------------------ http
    def get(self, path: str, params: dict | None = None, private: bool = False) -> Any:
        return self._request("GET", path, params=params, private=private)

    def post(self, path: str, body: dict | None = None, private: bool = False) -> Any:
        return self._request("POST", path, body=body or {}, private=private)

    def _request(self, method: str, path: str, params=None, body=None, private=False) -> Any:
        if private and not self.has_credentials:
            raise AuthExpired("私有接口需要环境变量 BINANCE_P20T 和 BINANCE_CSRFTOKEN")
        url = path if path.startswith("http") else self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "accept": "*/*",
            "clienttype": "web",
            "lang": "zh-CN",
            "user-agent": USER_AGENT,
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["content-type"] = "application/json"
        if private:
            headers["csrftoken"] = self.csrftoken  # type: ignore[assignment]
            headers["cookie"] = f"p20t={self.p20t}"

        attempt = 0
        while True:
            attempt += 1
            self._throttle()
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    status = resp.status
            except urllib.error.HTTPError as e:
                raw, status = e.read(), e.code
                if status in (401, 403):
                    raise AuthExpired(f"HTTP {status}", status=status) from None
                if status in (418, 429):
                    retry_after = _parse_retry_after(e.headers.get("Retry-After"))
                    if attempt <= self.max_retries and retry_after <= 60:
                        time.sleep(retry_after)
                        continue
                    raise RateLimited(f"HTTP {status} 限流", retry_after, status) from None
                if status >= 500 and attempt <= self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise BinanceError(f"HTTP {status}: {_short(raw)}", status=status) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt <= self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise BinanceError(f"网络错误: {e}") from None
            return _unwrap(raw, status)

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()


def _unwrap(raw: bytes, status: int) -> Any:
    try:
        payload = json.loads(raw)
    except ValueError:
        raise BinanceError(f"非 JSON 响应: {_short(raw)}", status=status) from None
    # bapi 统一包了一层 {code, message, data, success}；fapi 直接返回数据
    if isinstance(payload, dict) and "code" in payload and "data" in payload:
        code = str(payload.get("code"))
        if code in AUTH_ERROR_CODES:
            raise AuthExpired(f"凭证失效 code={code}", code=code, status=status)
        if code != CODE_SUCCESS:
            raise BinanceError(
                f"code={code} msg={payload.get('message')}", code=code, status=status
            )
        return payload["data"]
    return payload


def _parse_retry_after(value: str | None) -> float:
    try:
        return max(1.0, float(value)) if value else 5.0
    except ValueError:
        return 5.0


def _short(raw: bytes, n: int = 200) -> str:
    s = raw.decode(errors="replace")
    return s if len(s) <= n else s[:n] + "..."
