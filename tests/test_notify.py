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
    assert status[2] == (False, "email: 低于邮件通知级别，未发送")
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
