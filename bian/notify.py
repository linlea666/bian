"""通知渠道：控制台、Telegram、邮件（SMTP）。

投递模型：监控每轮把提醒放进发件箱，轮次结束时统一 deliver()。
- 控制台 / Telegram：逐条发送。
- 邮件：同一轮的多条提醒合并成一封；按级别过滤；每小时封数上限；失败 5 分钟后重试。
每条提醒的投递结果都如实返回（不把“已提交/排队中”当成“已送达”）。
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

ICON = {"info": "ℹ️", "warn": "⚠️", "urgent": "🚨"}
LEVEL = {"info": 0, "warn": 1, "urgent": 2}


@dataclass
class Outgoing:
    row_id: int  # alerts 表的行 ID，用于回写投递结果
    text: str
    severity: str = "info"
    created: float = field(default_factory=time.time)


Result = tuple[Outgoing, bool, str]  # (提醒, 是否送达, 说明/错误)


class Notifier:
    name = "base"
    counts_as_delivery = True  # 控制台输出不算“送达”（只要配置了真实渠道）

    def send(self, text: str, severity: str = "info") -> tuple[bool, str]:
        raise NotImplementedError

    def deliver(self, items: list[Outgoing], now: float | None = None) -> list[Result]:
        out = []
        for it in items:
            ok, err = self.send(it.text, it.severity)
            out.append((it, ok, err))
        return out


def deliver_all(notifier, items: list[Outgoing], now: float | None = None) -> list[Result]:
    """兼容只实现了 send() 的通知器（例如测试替身）。"""
    if hasattr(notifier, "deliver"):
        return notifier.deliver(items, now)
    return [(it, *notifier.send(it.text, it.severity)) for it in items]


class StdoutNotifier(Notifier):
    name = "stdout"
    counts_as_delivery = False

    def send(self, text: str, severity: str = "info") -> tuple[bool, str]:
        print(f"{ICON.get(severity, '')} {text}\n{'-' * 40}", file=sys.stdout, flush=True)
        return True, ""


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, token: str, chat_id: str, retries: int = 3, timeout: float = 10):
        self.token, self.chat_id, self.retries, self.timeout = token, chat_id, retries, timeout

    @classmethod
    def from_env(cls) -> "TelegramNotifier | None":
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        return cls(token, chat) if token and chat else None

    def send(self, text: str, severity: str = "info") -> tuple[bool, str]:
        body = json.dumps({
            "chat_id": self.chat_id,
            "text": f"{ICON.get(severity, '')} {text}"[:4000],
            "disable_web_page_preview": True,
            "disable_notification": severity == "info",
        }).encode()
        err = ""
        for i in range(self.retries):
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data=body, headers={"content-type": "application/json"}, method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if json.loads(resp.read()).get("ok"):
                        return True, ""
                    err = "telegram 返回 ok=false"
            except urllib.error.HTTPError as e:
                err = f"telegram HTTP {e.code}"  # 不打印 URL（含 token）
                if e.code == 429:
                    time.sleep(5)
            except Exception as e:  # noqa: BLE001
                err = f"telegram 网络错误: {type(e).__name__}"
            time.sleep(2 ** i)
        return False, err


# ===================================================================== 邮件
@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    to: list[str]
    security: str  # tls / starttls
    sender: str = ""

    @classmethod
    def from_env(cls) -> "SmtpConfig | None":
        e = os.environ
        host, user, pw = e.get("SMTP_HOST", "").strip(), e.get("SMTP_USER", "").strip(), e.get("SMTP_PASS", "")
        if not (host and user and pw):
            return None
        port = int(e.get("SMTP_PORT") or 465)
        sec = (e.get("SMTP_SECURITY") or ("tls" if port == 465 else "starttls")).strip().lower()
        if sec not in ("tls", "starttls"):
            raise ValueError("SMTP_SECURITY 只能是 tls 或 starttls")
        to = [x.strip() for x in (e.get("SMTP_TO") or user).replace(";", ",").split(",") if x.strip()]
        return cls(host, port, user, pw, to, sec, e.get("SMTP_FROM", "").strip() or user)

    def describe(self) -> str:
        return f"{self.host}:{self.port} {self.security.upper()}，发件 {self.sender}，收件 {', '.join(self.to)}"


def smtp_send(cfg: SmtpConfig, subject: str, body: str, timeout: float = 20.0) -> None:
    """发送一封纯文本邮件。校验证书；STARTTLS 不可用时直接失败，不退回明文。"""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(cfg.to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg.sender.split("@")[-1] or None)
    msg.set_content(body, charset="utf-8")
    ctx = ssl.create_default_context()
    if cfg.security == "tls":
        server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=timeout, context=ctx)
    else:
        server = smtplib.SMTP(cfg.host, cfg.port, timeout=timeout)
    try:
        if cfg.security == "starttls":
            server.ehlo()
            if not server.has_extn("starttls"):
                raise smtplib.SMTPNotSupportedError("服务器不支持 STARTTLS（不会退回明文发送）")
            server.starttls(context=ctx)
            server.ehlo()
        server.login(cfg.user, cfg.password)
        refused = server.send_message(msg)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001
            pass


def describe_smtp_error(e: Exception) -> str:
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return "SMTP 认证失败：请核对发件账号和邮箱“授权码”（不是网页登录密码）"
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return "SMTP 拒绝收件地址：请核对 SMTP_TO"
    if isinstance(e, ssl.SSLError):
        return f"TLS 失败：请核对端口与连接方式（465=tls，587=starttls）以及系统时间（{type(e).__name__}）"
    if isinstance(e, smtplib.SMTPNotSupportedError):
        return str(e)
    if isinstance(e, (TimeoutError, OSError)):
        return f"连接邮件服务器失败或超时（{type(e).__name__}）；阿里云等云服务器通常封禁 25 端口，请用 465/587"
    return f"邮件发送失败：{type(e).__name__}: {e}"


class EmailNotifier(Notifier):
    """合并发送 + 级别过滤 + 每小时上限 + 失败重试。"""

    name = "email"

    def __init__(self, cfg: SmtpConfig, min_severity: str = "warn", max_per_hour: int = 12,
                 retry_seconds: float = 300, max_pending: int = 200, sender=smtp_send, clock=time.time):
        self.cfg, self.min_level = cfg, LEVEL.get(min_severity, 1)
        self.max_per_hour, self.retry_seconds, self.max_pending = max_per_hour, retry_seconds, max_pending
        self._send_mail, self.clock = sender, clock
        self.pending: list[Outgoing] = []
        self.retry_at = 0.0
        self.sent_times: list[float] = []
        self.dropped = 0

    def send(self, text: str, severity: str = "info") -> tuple[bool, str]:  # 单条（测试邮件等）
        try:
            self._send_mail(self.cfg, f"{ICON.get(severity, '')} 币安跟单风控", text)
            return True, ""
        except Exception as e:  # noqa: BLE001
            return False, describe_smtp_error(e)

    def deliver(self, items: list[Outgoing], now: float | None = None) -> list[Result]:
        now = self.clock() if now is None else now
        results: list[Result] = []
        for it in items:
            if LEVEL.get(it.severity, 0) < self.min_level:
                results.append((it, False, "email: 低于邮件通知级别，未发送"))
            else:
                self.pending.append(it)
        if len(self.pending) > self.max_pending:  # 防止长期故障时无限堆积
            drop = len(self.pending) - self.max_pending
            for it in self.pending[:drop]:
                results.append((it, False, "email: 积压过多已丢弃"))
            self.dropped += drop
            self.pending = self.pending[drop:]
        if not self.pending:
            return results
        if now < self.retry_at:
            return results + [(it, False, "email: 上次发送失败，等待重试") for it in items if it in self.pending]
        self.sent_times = [t for t in self.sent_times if now - t < 3600]
        if len(self.sent_times) >= self.max_per_hour:
            return results + [(it, False, "email: 达到每小时上限，稍后合并补发") for it in items if it in self.pending]

        batch, self.pending = self.pending, []
        subject, body = self.compose(batch, now)
        try:
            self._send_mail(self.cfg, subject, body)
        except Exception as e:  # noqa: BLE001
            err = "email: " + describe_smtp_error(e)
            self.pending = batch + self.pending
            self.retry_at = now + self.retry_seconds
            return results + [(it, False, err) for it in batch]
        self.sent_times.append(now)
        self.retry_at = 0.0
        self.dropped = 0
        return results + [(it, True, "") for it in batch]

    def compose(self, batch: list[Outgoing], now: float) -> tuple[str, str]:
        urgent = sum(1 for b in batch if b.severity == "urgent")
        warn = sum(1 for b in batch if b.severity == "warn")
        info = len(batch) - urgent - warn
        parts = [f"{n} 条{label}" for n, label in ((urgent, "紧急"), (warn, "提醒"), (info, "信息")) if n]
        icon = ICON["urgent"] if urgent else ICON["warn"] if warn else ICON["info"]
        subject = f"{icon} 币安跟单风控：{' / '.join(parts)}"
        order = sorted(batch, key=lambda b: (-LEVEL.get(b.severity, 0), b.created))
        lines = []
        late = [b for b in batch if now - b.created > 120]
        if late:
            lines.append(f"（其中 {len(late)} 条因限流或发送失败延后送达，请以邮件中的数据时间为准）\n")
        if self.dropped:
            lines.append(f"（之前积压过多，已丢弃 {self.dropped} 条较早的提醒）\n")
        for b in order:
            lines.append(f"{ICON.get(b.severity, '')} {b.text}")
            lines.append("=" * 40)
        lines.append("本邮件由 bian 币安跟单风控助手自动发送；系统只做提醒，不会自动下单或止损。")
        return subject, "\n".join(lines)


class MultiNotifier(Notifier):
    name = "multi"

    def __init__(self, notifiers: list[Notifier]):
        self.notifiers = notifiers

    def send(self, text: str, severity: str = "info") -> tuple[bool, str]:
        oks, errs = [], []
        for n in self.notifiers:
            ok, err = n.send(text, severity)
            oks.append(ok)
            if err:
                errs.append(f"{n.name}: {err}")
        return any(oks), "; ".join(errs)

    def deliver(self, items: list[Outgoing], now: float | None = None) -> list[Result]:
        """有真实渠道（邮件/Telegram）时，“送达”只看真实渠道；只有控制台时控制台成功即送达。"""
        real = any(getattr(n, "counts_as_delivery", True) for n in self.notifiers)
        merged: dict[int, list] = {}
        order: list[Outgoing] = []
        for n in self.notifiers:
            counts = getattr(n, "counts_as_delivery", True) or not real
            for it, ok, err in deliver_all(n, items, now):
                if id(it) not in merged:
                    merged[id(it)] = [it, False, []]
                    order.append(it)
                m = merged[id(it)]
                m[1] = m[1] or (ok and counts)
                if err:
                    m[2].append(err if err.startswith(n.name) else f"{n.name}: {err}")
        return [(it, merged[id(it)][1], "; ".join(merged[id(it)][2])) for it in order]
