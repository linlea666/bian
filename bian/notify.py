"""通知渠道：控制台、Telegram、邮件（SMTP）。

投递模型：监控每轮把提醒放进发件箱，轮次结束时统一 deliver()。
- 控制台 / Telegram：逐条发送。
- 邮件：只发设置里勾选的事件（email_events）；紧急的立即发，并捎上等待中的提醒；
  非紧急的攒满 batch_minutes 后汇总成一封；每小时封数上限；失败 5 分钟后重试。
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


# 邮件事件分组：(键, 名称, 规则 ID, 默认是否发邮件)。L1/L2/... 亏损分档统一归入 loss_tier。
EVENT_GROUPS: list[tuple[str, str, tuple[str, ...], bool]] = [
    ("loss_tier", "仓位亏损到档位", ("L*",), True),
    ("account_dd", "跟单权益回撤", ("A01", "A01b"), True),
    ("liquidation", "接近强平", ("R09",), True),
    ("profit_to_loss", "盈利单跌回保本以下", ("R10",), True),
    ("native_stop", "接近跟单总止损", ("A03",), True),
    ("portfolio", "组合浮亏超预算", ("R06",), True),
    ("residual", "残留仓位（领航员已平、你还在）", ("R12",), True),
    ("add_in_loss", "亏损中还在加仓（摊平扛单）", ("R07L",), True),
    ("system", "无法监控 / 数据中断", ("R08",), True),
    ("trail", "浮盈回撤", ("R05",), False),
    ("add", "领航员加仓", ("R07",), False),
    ("tp_ladder", "分批止盈到位", ("R13",), False),
    ("breakeven", "浮盈达标建议保本", ("R04",), False),
    ("info", "开仓 / 减仓 / 未设总止损 / 暂停等信息", ("R00", "R07b", "A02", "A04"), False),
]
EVENT_KEYS = [g[0] for g in EVENT_GROUPS]
DEFAULT_EVENTS = [g[0] for g in EVENT_GROUPS if g[3]]
URGENT_EVENTS = ["loss_tier", "account_dd", "liquidation", "profit_to_loss", "native_stop", "portfolio"]
ALWAYS_EMAIL = ("AUTH",)  # 凭证失效：始终发，不可关闭
ACTION_RULES = ("R00", "R07", "R07b", "R11")  # 交易动作：开仓 / 加仓 / 减仓 / 平仓（可按领航员单独开关）


def event_group(rule_id: str) -> str:
    if len(rule_id) >= 2 and rule_id[0] == "L" and rule_id[1:].isdigit():
        return "loss_tier"
    for key, _, ids, _ in EVENT_GROUPS:
        if rule_id in ids:
            return key
    return "info"


def events_for_severity(min_severity: str) -> list[str]:
    """旧版“按级别发邮件”的设置换算成事件清单（升级迁移用）。"""
    return {"urgent": list(URGENT_EVENTS), "info": list(EVENT_KEYS)}.get(min_severity, list(DEFAULT_EVENTS))


# 投递说明里的“非故障”状态：有意不发 / 排队等待，不算发送失败（作战室邮件状态、未送达日志都据此判断）
SKIPPED = "按设置不发邮件（只在后台记录）"
HELD = "等待合并"
SOFT_MARKERS = (SKIPPED, HELD, "达到每小时上限", "上次发送失败，等待重试", "服务重启",
                "低于邮件通知级别")  # 最后一个是旧版文案，兼容历史记录


def is_soft(err: str) -> bool:
    return any(m in (err or "") for m in SOFT_MARKERS)


@dataclass
class Outgoing:
    row_id: int  # alerts 表的行 ID，用于回写投递结果
    text: str
    severity: str = "info"
    created: float = field(default_factory=time.time)
    rule_id: str = ""  # 决定是否发邮件（按事件分组）
    leader_id: str = ""  # 交易动作按领航员单独开关时用


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
    """事件过滤 + 合并发送 + 每小时上限 + 失败重试。

    events：发邮件的事件分组（None 表示按 min_severity 级别过滤，命令行旧配置用）。
    batch_minutes：非紧急提醒攒多久汇总一封（0 = 每轮立即发）；有紧急提醒时立即发，并捎上等待中的。"""

    name = "email"

    def __init__(self, cfg: SmtpConfig, min_severity: str = "warn", max_per_hour: int = 6,
                 retry_seconds: float = 300, max_pending: int = 200, sender=smtp_send, clock=time.time,
                 events=None, batch_minutes: float = 0, leader_actions: dict | None = None):
        self.cfg, self.min_level = cfg, LEVEL.get(min_severity, 1)
        self.events = set(events) if events is not None else None
        self.leader_actions = dict(leader_actions or {})  # 领航员 ID -> 是否发交易动作邮件（优先于全局）
        self.batch_s = max(0.0, float(batch_minutes)) * 60
        self.max_per_hour, self.retry_seconds, self.max_pending = max_per_hour, retry_seconds, max_pending
        self._send_mail, self.clock = sender, clock
        self.pending: list[Outgoing] = []
        self.retry_at = 0.0
        self.sent_times: list[float] = []
        self.dropped = 0

    def allows(self, it: Outgoing) -> bool:
        if it.rule_id in ALWAYS_EMAIL:
            return True
        if it.rule_id in ACTION_RULES and it.leader_id in self.leader_actions:
            return bool(self.leader_actions[it.leader_id])  # 按领航员单独设置的交易动作开关优先
        if self.events is not None:
            return event_group(it.rule_id) in self.events
        return LEVEL.get(it.severity, 0) >= self.min_level

    def requeue(self, items: list[Outgoing]) -> None:
        """服务重启后，把还在等待合并的提醒放回队列（由调用方从 alerts 表恢复）。"""
        self.pending.extend(items)

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
            if not self.allows(it):
                results.append((it, False, "email: " + SKIPPED))
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
        urgent = any(p.severity == "urgent" for p in self.pending)
        oldest = min(p.created for p in self.pending)
        if not urgent and self.batch_s > 0 and now - oldest < self.batch_s:
            at = time.strftime("%H:%M", time.localtime(oldest + self.batch_s))
            return results + [(it, False, f"email: {HELD}，约 {at} 汇总发送") for it in items if it in self.pending]
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
            lines.append(f"（其中 {len(late)} 条是汇总或延后送达的，请以每条里的数据时间为准）\n")
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
