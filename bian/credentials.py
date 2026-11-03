"""币安登录凭证守护：定时验证、失效判定、紧急邮件、更新与恢复。

状态：missing（未设置）/ unknown（尚未验证）/ valid / expired。
- 每 check_interval（默认 10 分钟）主动验证一次，即使没有持仓也会检查。
- 鉴权失败（错误码 100001005/100002002 或 HTTP 401/403）**连续 fail_threshold 次**才判定失效，避免网络抖动误报；
  网络错误、5xx 不计入失败。
- 判定失效：立即发紧急邮件（不受邮件级别过滤和每小时上限限制），之后每 repeat_hours 提醒一次，直到更新。
  失效期间监控暂停私有接口请求（不拿失效凭证反复请求，避免触发风控）。
- 网页更新凭证：先验证有效才保存；保存后立即生效；如果之前是失效状态，发一封“已恢复”邮件。
- 记录每份凭证的实际有效天数，用于提示“可能快过期了”。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .api import copy_account
from .client import AuthExpired, BinanceError, Client


@dataclass
class CredentialStatus:
    state: str = "unknown"  # missing / unknown / valid / expired
    checked_at: float = 0.0
    valid_at: float = 0.0  # 最近一次验证有效的时间
    expired_at: float = 0.0
    consecutive_failures: int = 0
    last_error: str = ""
    relations: int = 0
    notified_at: float = 0.0
    updated_at: float = 0.0  # 凭证保存时间
    age_days: float = 0.0
    expected_life_days: float = 0.0  # 历史平均有效天数（有记录时）
    history: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "history"}


def validate_credentials(p20t: str, csrftoken: str, base_client: Client | None = None) -> tuple[bool, str, int]:
    """用一个临时客户端验证（不影响正在运行的监控）。返回 (是否有效, 说明, 进行中的跟单数)。"""
    c = Client(p20t, csrftoken, base_url=base_client.base_url if base_client else "https://www.binance.com",
               min_interval=0.2, max_retries=1)
    try:
        if not copy_account.check_auth(c):
            return False, "凭证无效（未返回账号信息）", 0
        return True, "有效", len(copy_account.copy_relations(c))
    except AuthExpired:
        return False, "凭证无效或已过期：请重新登录币安网页后再复制", 0
    except BinanceError as e:
        return False, f"暂时无法验证（网络或币安接口异常）：{e}", 0


class CredentialGuard:
    def __init__(self, client: Client, settings, store, urgent_sender, clock=time.time,
                 check_interval: float = 600, fail_threshold: int = 2, repeat_hours: float = 6,
                 panel_url: str = ""):
        """urgent_sender(title, body, severity) -> None：绕过过滤和上限，立即发送（邮件 + 记录）。"""
        self.client, self.settings, self.store = client, settings, store
        self.send_urgent, self.clock = urgent_sender, clock
        self.check_interval, self.fail_threshold, self.repeat_s = check_interval, fail_threshold, repeat_hours * 3600
        self.panel_url = panel_url
        self.status = CredentialStatus()
        self._lock = threading.RLock()
        self._last_failure_ts = 0.0
        self.load()

    # ------------------------------------------------------------ 状态
    def load(self) -> None:
        p20t, csrf = self.settings.binance_credentials()
        meta = self.settings.credential_meta()
        self.client.set_credentials(p20t, csrf)
        with self._lock:
            self.status.updated_at = meta["updated_at"]
            hist = meta["lifetimes_days"]
            self.status.expected_life_days = sum(hist) / len(hist) if hist else 0.0
            if not (p20t and csrf):
                self.status.state = "missing"
        self._publish()

    @property
    def paused(self) -> bool:
        """失效或未设置时，监控应暂停私有接口请求。"""
        return self.status.state in ("expired", "missing")

    def _publish(self) -> None:
        s = self.status
        if s.updated_at:
            s.age_days = (self.clock() - s.updated_at) / 86400
        self.store.set_runtime("credentials", s.to_dict())

    # ------------------------------------------------------------ 定时检查
    def tick(self, force: bool = False) -> None:
        now = self.clock()
        with self._lock:
            if self.status.state == "missing":
                self._publish()
                return
            due = force or now - self.status.checked_at >= self.check_interval
            if self.status.state == "expired":
                # 失效后仍定时检查（也许是币安接口临时问题），并按间隔重复提醒
                if now - self.status.notified_at >= self.repeat_s:
                    self._notify_expired(now, repeat=True)
        if due:
            self.check(now)

    def check(self, now: float | None = None) -> None:
        now = now or self.clock()
        try:
            ok = copy_account.check_auth(self.client)
            rels = len(copy_account.copy_relations(self.client)) if ok else 0
        except AuthExpired as e:
            self.report_failure(str(e), now)
            return
        except BinanceError as e:
            with self._lock:
                self.status.checked_at = now
                self.status.last_error = f"暂时无法验证：{e}"
            self.store.log_event("credential.check_error", "凭证验证暂时失败（网络/接口），不计入失效", "warn",
                                 error=str(e))
            self._publish()
            return
        if ok:
            self.report_success(now, rels)
        else:
            self.report_failure("未返回账号信息", now)

    def report_success(self, now: float | None = None, relations: int | None = None) -> None:
        now = now or self.clock()
        with self._lock:
            was = self.status.state
            self.status.state = "valid"
            self.status.checked_at = now
            self.status.valid_at = now
            self.status.consecutive_failures = 0
            self.status.last_error = ""
            if relations is not None:
                self.status.relations = relations
        if was in ("unknown", "expired"):
            self.store.log_event("credential.valid", "币安凭证验证有效", relations=relations)
        self._publish()

    def report_failure(self, reason: str, now: float | None = None) -> None:
        """鉴权失败（来自定时检查，或监控请求时收到的 AuthExpired）。同一分钟内多次只计一次。"""
        now = now or self.clock()
        with self._lock:
            if self.status.state in ("expired", "missing"):
                self.status.checked_at = now
                self._publish()
                return
            if now - self._last_failure_ts < 60:
                return
            self._last_failure_ts = now
            self.status.consecutive_failures += 1
            self.status.checked_at = now
            self.status.last_error = reason
            n = self.status.consecutive_failures
        self.store.log_event("credential.auth_failed", f"币安凭证鉴权失败（第 {n} 次）", "warn", reason=reason)
        if n >= self.fail_threshold:
            with self._lock:
                self.status.state = "expired"
                self.status.expired_at = now
            if self.status.updated_at:
                life = (now - self.status.updated_at) / 86400
                hist = (self.settings.credential_meta()["lifetimes_days"] + [round(life, 2)])[-10:]
                self.settings.set_secrets(binance_lifetimes_days=hist)
                self.status.expected_life_days = sum(hist) / len(hist)
            self.store.log_event("credential.expired", "币安凭证已失效，已暂停读取跟单仓位", "urgent", reason=reason)
            self._notify_expired(now)
        else:
            # 再确认一次：下一轮尽快复查，而不是等 10 分钟
            with self._lock:
                self.status.checked_at = now - self.check_interval + 60
        self._publish()

    def _notify_expired(self, now: float, repeat: bool = False) -> None:
        self.status.notified_at = now
        url = f"\n后台地址：{self.panel_url}" if self.panel_url else ""
        title = "币安凭证已失效" + ("（仍未更新）" if repeat else "") + "：跟单仓位已无法监控"
        body = ("币安网页登录凭证（p20t）已失效，你的跟单仓位、亏损档位和总止损提醒都已暂停。\n\n"
                "更新方法：\n"
                "1. 用电脑浏览器登录 binance.com；\n"
                "2. 按 F12 → Network，刷新页面，在过滤框输入 bapi；\n"
                "3. 右键任意一个 bapi 请求 → Copy → Copy as cURL；\n"
                "4. 打开风控后台 → 设置 → 币安凭证，整段粘贴后点“验证并保存”。"
                f"{url}\n\n失效原因：{self.status.last_error or '鉴权失败'}"
                + (f"\n上一份凭证使用了约 {(now - self.status.updated_at) / 86400:.1f} 天。" if self.status.updated_at else ""))
        self.send_urgent(title, body, "urgent")
        self._publish()

    # ------------------------------------------------------------ 更新
    def update(self, p20t: str, csrftoken: str, validator=validate_credentials) -> tuple[bool, str, int]:
        ok, msg, rels = validator(p20t, csrftoken, self.client)
        if not ok:
            self.store.log_event("credential.update_rejected", "新凭证验证未通过，未保存", "warn", reason=msg)
            return ok, msg, rels
        now = self.clock()
        self.settings.set_secrets(binance_p20t=p20t, binance_csrftoken=csrftoken, binance_updated_at=now)
        self.client.set_credentials(p20t, csrftoken)
        with self._lock:
            was = self.status.state
            self.status.updated_at = now
            self._last_failure_ts = 0.0
        self.report_success(now, rels)
        self.store.log_event("credential.updated", "币安凭证已更新并验证有效", relations=rels)
        if was == "expired":
            self.send_urgent("币安凭证已恢复：监控已恢复",
                             f"新凭证验证有效，进行中的跟单 {rels} 个。跟单仓位监控和提醒已恢复。", "info")
        return ok, msg, rels
