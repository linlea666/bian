"""邮件通知：合并、级别过滤、每小时上限、失败重试、SMTP 安全约束（全部用模拟，不发真实邮件）。"""
import smtplib
import ssl

import pytest

from bian import notify
from bian.notify import EmailNotifier, MultiNotifier, Outgoing, SmtpConfig, StdoutNotifier, describe_smtp_error

CFG = SmtpConfig("smtp.163.com", 465, "me@163.com", "authcode", ["me@163.com"], "tls", "me@163.com")


class Recorder:
    def __init__(self, fail=None):
        self.mails, self.fail = [], fail

    def __call__(self, cfg, subject, body):
        if self.fail:
            raise self.fail
        self.mails.append((subject, body))


def items(*sevs):
    return [Outgoing(i, f"消息{i}", s, created=0) for i, s in enumerate(sevs)]


def test_batch_into_one_mail_and_filter_info():
    rec = Recorder()
    em = EmailNotifier(CFG, "warn", sender=rec)
    res = em.deliver(items("urgent", "warn", "info", "urgent"), now=10)
    assert len(rec.mails) == 1
    subject, body = rec.mails[0]
    assert "2 条紧急" in subject and "1 条提醒" in subject and subject.startswith("🚨")
    assert body.index("消息0") < body.index("消息1")  # 紧急在前
    assert "消息2" not in body
    status = {it.row_id: (ok, err) for it, ok, err in res}
    assert status[2] == (False, "email: 按设置不发邮件（只在后台记录）")
    assert status[0][0] and status[1][0] and status[3][0]


def test_hourly_cap_defers_then_merges():
    rec = Recorder()
    em = EmailNotifier(CFG, "warn", max_per_hour=2, sender=rec)
    em.deliver(items("warn"), now=0)
    em.deliver(items("warn"), now=10)
    res = em.deliver(items("urgent"), now=20)
    assert len(rec.mails) == 2 and res[0][1] is False and "每小时上限" in res[0][2]
    res = em.deliver(items("warn"), now=3601)  # 第一封过了 1 小时
    assert len(rec.mails) == 3 and "1 条紧急 / 1 条提醒" in rec.mails[-1][0]
    assert len(res) == 2 and all(ok for _, ok, _ in res)
    assert "延后送达" in rec.mails[-1][1]


def test_failure_retries_after_delay():
    rec = Recorder(fail=smtplib.SMTPAuthenticationError(535, b"auth"))
    em = EmailNotifier(CFG, "warn", retry_seconds=300, sender=rec)
    res = em.deliver(items("urgent"), now=0)
    assert res[0][1] is False and "授权码" in res[0][2]
    rec.fail = None
    res = em.deliver([], now=100)  # 未到重试时间
    assert rec.mails == []
    res = em.deliver([], now=301)
    assert len(rec.mails) == 1 and res and res[0][1] is True


def test_stdout_does_not_count_as_delivery_when_email_configured():
    rec = Recorder(fail=OSError("timeout"))
    m = MultiNotifier([StdoutNotifier(), EmailNotifier(CFG, "warn", sender=rec)])
    (it, ok, err), = m.deliver(items("urgent"), now=0)
    assert not ok and "email" in err and "465/587" in err
    rec.fail = None
    res = m.deliver(items("urgent"), now=1000)  # 上次失败的一条 + 新的一条，合并成一封
    assert len(res) == 2 and all(ok for _, ok, _ in res) and len(rec.mails) == 1
    (it, ok, err), = MultiNotifier([StdoutNotifier()]).deliver(items("info"), now=0)
    assert ok  # 只有控制台时，输出即送达


def test_smtp_config_from_env(monkeypatch):
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "SMTP_PORT", "SMTP_TO", "SMTP_SECURITY", "SMTP_FROM"):
        monkeypatch.delenv(k, raising=False)
    assert SmtpConfig.from_env() is None
    monkeypatch.setenv("SMTP_HOST", "smtp.163.com")
    monkeypatch.setenv("SMTP_USER", "a@163.com")
    monkeypatch.setenv("SMTP_PASS", "x")
    c = SmtpConfig.from_env()
    assert c.port == 465 and c.security == "tls" and c.to == ["a@163.com"]
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_TO", "b@qq.com; c@qq.com")
    c = SmtpConfig.from_env()
    assert c.security == "starttls" and c.to == ["b@qq.com", "c@qq.com"]
    monkeypatch.setenv("SMTP_SECURITY", "none")
    with pytest.raises(ValueError):
        SmtpConfig.from_env()


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None, starttls=True):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls, self._starttls = [], starttls
        FakeSMTP.instances.append(self)

    def ehlo(self):
        self.calls.append("ehlo")

    def has_extn(self, name):
        return self._starttls

    def starttls(self, context=None):
        self.calls.append("starttls")
        assert context is not None

    def login(self, u, p):
        self.calls.append("login")

    def send_message(self, msg):
        self.calls.append(("send", msg["Subject"], msg["To"]))
        return {}

    def quit(self):
        self.calls.append("quit")


def test_smtp_send_tls_and_starttls(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", lambda h, p, timeout, context: FakeSMTP(h, p, timeout, context))
    notify.smtp_send(CFG, "主题", "正文")
    s = FakeSMTP.instances[-1]
    assert s.timeout == 20 and isinstance(s.context, ssl.SSLContext) and "login" in s.calls
    assert ("send", "主题", "me@163.com") in s.calls

    monkeypatch.setattr(notify.smtplib, "SMTP", lambda h, p, timeout: FakeSMTP(h, p, timeout, starttls=False))
    cfg = SmtpConfig("smtp.qq.com", 587, "u", "p", ["u@qq.com"], "starttls", "u@qq.com")
    with pytest.raises(smtplib.SMTPNotSupportedError):
        notify.smtp_send(cfg, "s", "b")  # 不支持 STARTTLS 时不退回明文
    assert "login" not in FakeSMTP.instances[-1].calls


def test_error_descriptions():
    assert "授权码" in describe_smtp_error(smtplib.SMTPAuthenticationError(535, b""))
    assert "SMTP_TO" in describe_smtp_error(smtplib.SMTPRecipientsRefused({}))
    assert "465" in describe_smtp_error(ssl.SSLError("x"))


# ---------------------------------------------------------------- 按事件发邮件 + 定时汇总（M8）
def ev(i, rule, sev="warn", created=0):
    return Outgoing(i, f"消息{i}", sev, created=created, rule_id=rule)


def test_event_filter_defaults():
    rec = Recorder()
    em = EmailNotifier(CFG, events=notify.DEFAULT_EVENTS, sender=rec)
    res = em.deliver([ev(1, "L1"), ev(2, "R05"), ev(3, "R07"), ev(4, "R07L"), ev(5, "AUTH", "urgent"),
                      ev(6, "A01b", "urgent"), ev(7, "R00", "info")], now=10)
    sent = {it.row_id for it, ok, _ in res if ok}
    assert sent == {1, 4, 5, 6}  # 浮盈回撤、普通加仓、开仓只在后台记录；凭证失效始终发
    assert len(rec.mails) == 1


def test_warn_waits_for_batch_and_urgent_flushes_everything():
    rec = Recorder()
    em = EmailNotifier(CFG, events=notify.DEFAULT_EVENTS, batch_minutes=60, sender=rec)
    res = em.deliver([ev(1, "L1", created=0)], now=0)
    assert rec.mails == [] and notify.is_soft(res[0][2]) and "等待合并" in res[0][2]
    em.deliver([ev(2, "R12", created=600)], now=600)
    assert rec.mails == []
    res = em.deliver([], now=3600)  # 最早一条满 60 分钟：两条汇总成一封
    assert len(rec.mails) == 1 and {it.row_id for it, ok, _ in res if ok} == {1, 2}
    em.deliver([ev(3, "L2", created=3700)], now=3700)
    res = em.deliver([ev(4, "R09", "urgent", created=3800)], now=3800)  # 紧急：立即发，捎上等待中的
    assert len(rec.mails) == 2 and {it.row_id for it, ok, _ in res if ok} == {3, 4}
    assert rec.mails[1][0].startswith("🚨")


def test_requeue_after_restart():
    rec = Recorder()
    em = EmailNotifier(CFG, events=notify.DEFAULT_EVENTS, batch_minutes=60, sender=rec)
    em.requeue([ev(9, "L1", created=0)])
    res = em.deliver([], now=3601)
    assert [it.row_id for it, ok, _ in res if ok] == [9]


def test_event_group_and_severity_migration():
    assert notify.event_group("L3") == "loss_tier" and notify.event_group("R07L") == "add_in_loss"
    assert notify.event_group("XYZ") == "info"
    assert notify.events_for_severity("urgent") == notify.URGENT_EVENTS
    assert set(notify.events_for_severity("info")) == set(notify.EVENT_KEYS)


def test_replay_screenshot_window_sends_at_most_three_mails():
    """06:10–07:28 的真实场景：原来 10 封（加仓、浮盈回撤、权益回撤 3 次、强平 2 次……）。"""
    from bian.models import Position
    from bian.rules import AccountSnapshot, RuleConfig, evaluate, evaluate_account

    cfg, t0 = RuleConfig(), 1_700_000_000.0
    alerts = []  # (分钟, 规则, 级别)

    def acct(eq):
        return AccountSnapshot("L", "BTC杰师傅", eq, 300, 0, 0, False, 0, 0, "")
    st, _ = evaluate_account(acct(300), None, cfg, t0)
    for minute, eq in ((17, 236), (47, 224), (77, 238)):  # 21.4% / 25.4% / 20.8%
        st, al = evaluate_account(acct(eq), st, cfg, t0 + minute * 60)
        alerts += [(minute, a.rule_id, a.severity) for a in al if a.rule_id != "A02"]
    liq = Position("L", "ETHUSDT", "SHORT", 1, 2000, 1905, 20, 95, "copy", liquidation_price=2000)
    ps, _ = evaluate(liq, None, cfg, t0 + 14 * 60)
    ps.fired.clear()
    for minute in (14, 59):  # 距强平 5.0% 两次
        ps, al = evaluate(liq, ps, cfg, t0 + minute * 60)
        alerts += [(minute, a.rule_id, a.severity) for a in al if a.rule_id == "R09"]
    alerts += [(0, "R07", "warn"), (15, "R05", "warn"), (41, "R05", "warn"), (0, "R00", "info"), (78, "R07", "warn")]

    rec = Recorder()
    em = EmailNotifier(CFG, events=notify.DEFAULT_EVENTS, batch_minutes=60, sender=rec)
    alerts.sort()
    for i, (minute, rule, sev) in enumerate(alerts):
        em.deliver([Outgoing(i, rule, sev, created=t0 + minute * 60, rule_id=rule)], now=t0 + minute * 60)
    for minute in range(80, 200):
        em.deliver([], now=t0 + minute * 60)
    assert [r for _, r, _ in alerts].count("A01b") == 1 and [r for _, r, _ in alerts].count("R09") == 1
    assert len(rec.mails) <= 3


def test_leader_action_switch_overrides_global():
    """交易动作（开仓 / 加仓 / 减仓 / 平仓）按领航员单独开关优先；风险提醒不受影响。"""
    rec = Recorder()
    em = EmailNotifier(CFG, events=notify.DEFAULT_EVENTS, sender=rec, leader_actions={"111": True, "222": False})
    items = [Outgoing(1, "a", "info", 0, "R00", "111"), Outgoing(2, "b", "info", 0, "R11", "111"),
             Outgoing(3, "c", "info", 0, "R00", "333"),  # 没设置：跟随全局（默认不发）
             Outgoing(4, "d", "warn", 0, "R07", "222"), Outgoing(5, "e", "warn", 0, "R07L", "222"),  # 风险提醒照常
             Outgoing(6, "f", "warn", 0, "L1", "222")]
    sent = {it.row_id for it, ok, _ in em.deliver(items, now=10) if ok}
    assert sent == {1, 2, 5, 6}
    em2 = EmailNotifier(CFG, events=notify.EVENT_KEYS, sender=Recorder(), leader_actions={"222": False})
    sent = {it.row_id for it, ok, _ in em2.deliver([Outgoing(7, "g", "info", 0, "R07b", "222"),
                                                   Outgoing(8, "h", "info", 0, "R07b", "333")], now=10) if ok}
    assert sent == {8}  # 全局都发，单独关闭的领航员不发
