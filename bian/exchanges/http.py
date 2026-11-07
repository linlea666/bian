"""交易所签名请求的公共部分：GET JSON、超时、网络错误重试、服务器时间校准。

独立于 bian.client（那个是币安网页 cookie + bapi 接口，认证方式和错误码完全不同）。"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from . import ExchangeError

UA = "bian-risk-monitor/1.0 (read-only)"


class SignedHttp:
    TIME_SYNC_S = 600

    def __init__(self, base_url: str, timeout: float = 20.0, retries: int = 2, opener=None, clock=time.time):
        self.base = base_url.rstrip("/")
        self.timeout, self.retries = timeout, retries
        self._open = opener or urllib.request.urlopen
        self.clock = clock
        self.offset_ms = 0  # 交易所时间 − 本机时间
        self._synced = 0.0

    def now_ms(self) -> int:
        return int(self.clock() * 1000) + self.offset_ms

    def sync_time(self, fetch_server_ms) -> None:
        """每 10 分钟用交易所时间校准一次（失败不影响后续请求）。"""
        if self.clock() - self._synced < self.TIME_SYNC_S:
            return
        try:
            local = int(self.clock() * 1000)
            self.offset_ms = int(fetch_server_ms()) - local
            self._synced = self.clock()
        except ExchangeError:
            pass

    def get(self, url: str, headers: dict | None = None, classify=None):
        """返回 (HTTP 状态, 解析后的 JSON)。classify(status, body) 把交易所的错误转换成 ExchangeError。"""
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
            try:
                with self._open(req, timeout=self.timeout) as r:
                    status, raw = r.status, r.read()
            except urllib.error.HTTPError as e:
                status, raw = e.code, e.read()
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 4))
                    continue
                raise ExchangeError("network", f"{type(e).__name__}: {e}") from None
            try:
                body = json.loads(raw or b"null")
            except ValueError:
                body = {"raw": raw[:200].decode("utf-8", "replace")}
            if status in (451, 403) and not isinstance(body, dict):
                raise ExchangeError("region", f"HTTP {status}", status=status)
            if classify:
                err = classify(status, body)
                if err:
                    if err.kind in ("network", "maintenance") and attempt < self.retries:
                        last = err
                        time.sleep(min(2 ** attempt, 4))
                        continue
                    raise err
            if status >= 400:
                raise ExchangeError("region" if status in (451,) else "other", f"HTTP {status}", status=status)
            return status, body
        raise ExchangeError("network", str(last))
