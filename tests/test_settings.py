import os
import stat
from types import SimpleNamespace

import pytest

from bian.curl_parse import CurlParseError, parse_credentials
from bian.rules import RuleConfig
from bian.settings import PRESETS, Settings, SettingsError, mask, public_view

# 虚构的测试值（格式与真实凭证一致）
P20T = "web.1000000001.ABCDEF0123456789ABCDEF0123456789"
CSRF = "abcdef0123456789abcdef0123456789"

BASH_CURL = f"""curl 'https://www.binance.com/bapi/fe/messagecenter/v2/group/list' \\
  -H 'accept: */*' \\
  -H 'clienttype: web' \\
  -b 'bnc-uuid=00000000; lang=zh-CN; cr00=FAKE; d1og=web.1.FAKE; logined=y; p20t={P20T}; theme=dark' \\
  -H 'csrftoken: {CSRF}' \\
  -H 'device-info: eyJ4IjoxfQ=='"""

CMD_CURL = f'''curl ^"https://www.binance.com/bapi/x^" ^
  -H ^"cookie: lang=zh-CN; p20t={P20T}; theme=dark^" ^
  -H ^"csrftoken: {CSRF}^"'''


def test_parse_bash_and_cmd_curl():
    assert parse_credentials(BASH_CURL) == (P20T, CSRF)
    assert parse_credentials(CMD_CURL) == (P20T, CSRF)
    assert parse_credentials(f"-H 'cookie: a=1; p20t={P20T}'\n-H 'csrftoken: {CSRF}'") == (P20T, CSRF)


def test_parse_errors_explain_what_is_missing():
    with pytest.raises(CurlParseError, match="Service Worker"):
        parse_credentials(f"-H 'csrftoken: {CSRF}'")
    with pytest.raises(CurlParseError, match="csrftoken"):
        parse_credentials(f"-b 'p20t={P20T}'")
    with pytest.raises(CurlParseError):
        parse_credentials("hello")


def test_mask():
    assert mask(P20T) == "web.****789"
    assert mask("") == "" and mask("abc") == "****"


@pytest.fixture
def st(tmp_path):
    return Settings(str(tmp_path / "b.db"), str(tmp_path / "secrets.json"))


def test_defaults_presets_and_validation(st):
    cfg = st.get()
    assert cfg["preset"] == "balanced" and cfg["rules"]["loss_tiers"] == [50, 100, 150]
    st.apply_preset("conservative")
    assert st.get()["rules"]["loss_tiers"] == PRESETS["conservative"]["loss_tiers"]
    assert st.rules_config().min_alert_u == 1.0
    with pytest.raises(SettingsError, match="档位"):
        st.update({"rules": {"loss_tiers": [-5]}})
    with pytest.raises(SettingsError, match="回撤"):
        st.update({"rules": {"account_dd_warn": 0.3, "account_dd_severe": 0.2}})
    with pytest.raises(SettingsError, match="轮询"):
        st.update({"monitor": {"poll_seconds": 3}})
    new, changed = st.update({"rules": {"loss_tiers": [150, 40]}, "preset": "custom"})
    assert new["rules"]["loss_tiers"] == [40.0, 150.0] and "rules.loss_tiers" in changed


def test_leader_override_and_version(st):
    v0 = st.version()
    st.update({"leader_rules": {"L1": {"loss_tiers": [20, 40], "loss_tiers_mode": "price"}}})
    assert st.version() > v0
    rc = st.rules_config("L1")
    assert rc.loss_tiers == (20.0, 40.0) and rc.loss_tiers_mode == "price"
    assert st.rules_config("other").loss_tiers == (50.0, 100.0, 150.0)
    st.update({"leader_rules": {}})  # 整体替换，可删除覆盖
    assert st.get()["leader_rules"] == {}


def test_secrets_file_permissions_and_public_view(st):
    st.set_secrets(binance_p20t=P20T, binance_csrftoken=CSRF, smtp_pass="auth#code")
    mode = stat.S_IMODE(os.stat(st.secrets_path).st_mode)
    assert mode == 0o600
    view = public_view(st)
    flat = repr(view)
    assert P20T not in flat and CSRF not in flat and "auth#code" not in flat
    assert view["credentials"]["p20t_masked"] == "web.****789" and view["smtp_pass_set"]
    st.update({"notify": {"smtp_user": "me@163.com"}})
    smtp = st.smtp_config()
    assert smtp.password == "auth#code" and smtp.security == "tls" and smtp.to == ["me@163.com"]


def test_import_legacy_once(st):
    app_cfg = SimpleNamespace(rules=RuleConfig(loss_tiers=(30.0, 60.0)), leaders=[], poll_seconds=20,
                              loss_budget_u=50, email_min_severity="urgent", email_max_per_hour=6)
    env = {"BINANCE_P20T": P20T, "BINANCE_CSRFTOKEN": CSRF, "SMTP_HOST": "smtp.163.com",
           "SMTP_USER": "a@163.com", "SMTP_PASS": "x"}
    imported = st.import_legacy(app_cfg, env)
    assert set(imported) == {"风控参数", "邮箱", "币安凭证"}
    cfg = st.get()
    assert cfg["rules"]["loss_tiers"] == [30.0, 60.0] and cfg["preset"] == "custom"
    assert cfg["notify"]["email_min_severity"] == "urgent" and st.binance_credentials() == (P20T, CSRF)
    assert st.import_legacy(app_cfg, env) == []  # 只导入一次
