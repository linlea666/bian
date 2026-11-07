"""网页可编辑的业务配置 + 密钥存储。

- 业务配置（阈值、邮件参数、每个领航员的覆盖值、引导状态）存在 SQLite 的 settings 表，网页修改后
  监控在下一轮轮询自动生效（按 version 判断是否变化），不用重启。
- 密钥（币安 p20t/csrftoken、SMTP 授权码）存在 data/secrets.json，权限 600；任何 API 都不返回明文。
- 首次启动时把旧版 config.toml / .env 里已有的值导入一次。
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import threading
import time
from dataclasses import asdict, fields

from .notify import DEFAULT_EVENTS, EVENT_GROUPS, EVENT_KEYS, URGENT_EVENTS, events_for_severity
from .rules import RuleConfig

RULE_KEYS = [f.name for f in fields(RuleConfig)]

PRESETS: dict[str, dict] = {
    "conservative": {
        "loss_tiers": [30, 60, 100], "min_alert_u": 1.0,
        "breakeven_trigger_roi": 0.15, "trail_trigger_roi": 0.15, "trail_gap_roi": 0.10,
        "account_dd_warn": 0.08, "account_dd_severe": 0.15, "recommend_total_stop_pct": 0.15,
    },
    "balanced": {
        "loss_tiers": [50, 100, 150], "min_alert_u": 2.0,
        "breakeven_trigger_roi": 0.20, "trail_trigger_roi": 0.20, "trail_gap_roi": 0.15,
        "account_dd_warn": 0.10, "account_dd_severe": 0.20, "recommend_total_stop_pct": 0.20,
    },
    "aggressive": {
        "loss_tiers": [80, 150, 250], "min_alert_u": 5.0,
        "breakeven_trigger_roi": 0.30, "trail_trigger_roi": 0.30, "trail_gap_roi": 0.20,
        "account_dd_warn": 0.15, "account_dd_severe": 0.30, "recommend_total_stop_pct": 0.30,
    },
}
PRESET_NAMES = {"conservative": "保守", "balanced": "均衡", "aggressive": "激进"}


def _rule_defaults() -> dict:
    d = asdict(RuleConfig())
    d["loss_tiers"] = list(d["loss_tiers"])
    d["tp_ladder"] = [list(x) for x in d["tp_ladder"]]
    d.update(PRESETS["balanced"])
    return d


DEFAULTS: dict = {
    "preset": "balanced",
    "rules": _rule_defaults(),
    "leader_rules": {},  # 领航员 ID -> 覆盖的规则字段
    "notify": {
        "email_enabled": True,
        "email_events": list(DEFAULT_EVENTS),  # 哪些事件发邮件（其余只在后台记录，并进每日摘要）
        "email_events_seen": list(EVENT_KEYS),  # 保存设置时已有的事件：之后新增的默认开启事件升级时自动加进来
        "email_batch_minutes": 60,  # 非紧急提醒攒多久汇总一封；0 = 立即发。紧急的始终立即发
        "email_max_per_hour": 6,
        "leader_actions": {},  # 领航员 ID -> 是否发开仓 / 加仓 / 减仓 / 平仓邮件；没设置的跟随全局
        "smtp_host": "smtp.163.com",
        "smtp_port": 465,
        "smtp_security": "",
        "smtp_user": "",
        "smtp_to": "",
    },
    "monitor": {"poll_seconds": 20, "loss_budget_u": 0.0},
    "maintenance": {
        "digest_enabled": True,  # 每日摘要邮件
        "digest_time": "08:30",
        "backup_enabled": True,  # 每天自动备份数据库和设置（保留 7 份）
        "backup_time": "04:00",
    },
    "onboarding_done": False,
}


def _valid_hhmm(v: str) -> bool:
    try:
        h, m = str(v).split(":")
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59 and len(m) == 2
    except ValueError:
        return False

SECRET_KEYS = ("binance_p20t", "binance_csrftoken", "smtp_pass")


def _deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("leader_rules", "leader_actions"):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def mask(secret: str | None) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "****"
    head = secret.split(".")[0] + "." if secret.startswith("web.") else secret[:3]
    return f"{head}****{secret[-3:]}"


def is_target_id(v) -> bool:
    """单独设置的对象：领航员（数字 ID）或交易所账户（x 开头的 6 位十六进制）。"""
    v = str(v)
    return v.isdigit() or (len(v) == 7 and v[0] == "x" and all(c in "0123456789abcdef" for c in v[1:]))


# 开始记录 email_events_seen 之前就有的事件（“利润翻倍取回本金”及之后新增的不在其中）
LEGACY_EVENT_KEYS = ("loss_tier", "account_dd", "liquidation", "profit_to_loss", "native_stop", "portfolio", "residual",
                     "add_in_loss", "system", "trail", "add", "tp_ladder", "breakeven", "info")


def upgrade(raw: dict) -> dict:
    """旧版保存的配置升级到当前格式（读取时执行，下次保存时落盘）：
    - 按级别发邮件（email_min_severity）→ 按事件勾选（email_events）；当时默认的每小时 12 封降为 6 封；
    - 去掉已不存在的风控参数（例如 urgent_repeat_minutes，改为“同一事件当天只发一次”）。"""
    n = raw.get("notify")
    if isinstance(n, dict) and "email_events" not in n:
        n["email_events"] = events_for_severity(n.get("email_min_severity", "warn"))
        if n.get("email_max_per_hour") == 12:
            n["email_max_per_hour"] = 6
    if isinstance(n, dict):
        n.pop("email_min_severity", None)
        # 新版本新增、默认要发邮件的事件：用户之前没见过（不是主动取消的），自动加进勾选列表
        seen = n.get("email_events_seen")
        seen = set(seen) if isinstance(seen, list) else set(LEGACY_EVENT_KEYS)
        if isinstance(n.get("email_events"), list):
            n["email_events"] = list(n["email_events"]) + [k for k in DEFAULT_EVENTS
                                                            if k not in seen and k not in n["email_events"]]
        n["email_events_seen"] = list(EVENT_KEYS)
    if isinstance(raw.get("rules"), dict):
        raw["rules"] = {k: v for k, v in raw["rules"].items() if k in RULE_KEYS}
    if isinstance(raw.get("leader_rules"), dict):
        raw["leader_rules"] = {pid: {k: v for k, v in (r or {}).items() if k in RULE_KEYS}
                               for pid, r in raw["leader_rules"].items()}
    return raw


class SettingsError(ValueError):
    """校验失败，message 直接展示给用户。"""


def validate(cfg: dict) -> dict:
    """校验并规范化整份配置；不合法抛 SettingsError（中文说明）。"""
    cfg = _deep_merge(DEFAULTS, cfg)
    try:
        RuleConfig().merged(cfg["rules"])
        for pid, r in cfg["leader_rules"].items():
            RuleConfig().merged({**cfg["rules"], **r})
    except (ValueError, TypeError) as e:
        raise SettingsError(f"风控参数不合法：{e}") from None
    r = cfg["rules"]
    checks = [
        (all(float(t) > 0 for t in r["loss_tiers"]) and len(r["loss_tiers"]) <= 6, "亏损档位必须是正数，最多 6 档"),
        (0 <= float(r["loss_cooldown_minutes"]) <= 1440, "冷却时间应在 0–1440 分钟之间"),
        (1 <= float(r["loss_max_per_day"]) <= 50, "每日上限应在 1–50 条之间"),
        (0 <= float(r["min_alert_u"]) <= 10000, "最小提醒金额应在 0–10000U 之间"),
        (0 < float(r["account_dd_warn"]) < float(r["account_dd_severe"]) < 1, "账户回撤：提醒线要小于紧急线，且都在 0–100% 之间"),
        (0 < float(r["recommend_total_stop_pct"]) < 1, "建议总止损比例应在 0–100% 之间"),
        (float(r["principal_out_multiple"]) == 0 or 1.2 <= float(r["principal_out_multiple"]) <= 100,
         "取回本金倍数应为 0（关闭）或 1.2–100 倍"),
    ]
    for ok, msg in checks:
        if not ok:
            raise SettingsError(msg)
    r["loss_tiers"] = sorted(float(t) for t in r["loss_tiers"])
    n = cfg["notify"]
    events = n.get("email_events")
    if not isinstance(events, list) or any(e not in EVENT_KEYS for e in events):
        raise SettingsError("发邮件的事件不合法")
    n["email_events"] = [k for k in EVENT_KEYS if k in events]  # 去重、固定顺序
    la = n.get("leader_actions")
    if not isinstance(la, dict) or any(not is_target_id(k) or not isinstance(v, bool) for k, v in la.items()):
        raise SettingsError("按领航员的交易动作邮件设置不合法")
    n["email_batch_minutes"] = int(float(n["email_batch_minutes"]))
    if not 0 <= n["email_batch_minutes"] <= 240:
        raise SettingsError("汇总间隔应在 0–240 分钟之间")
    n["smtp_port"] = int(n["smtp_port"])
    n["email_max_per_hour"] = int(n["email_max_per_hour"])
    if not 1 <= n["email_max_per_hour"] <= 120:
        raise SettingsError("每小时邮件上限应在 1–120 之间")
    if n["smtp_security"] not in ("", "tls", "starttls"):
        raise SettingsError("SMTP 加密方式只能是 tls 或 starttls")
    m = cfg["monitor"]
    m["poll_seconds"] = float(m["poll_seconds"])
    if not 10 <= m["poll_seconds"] <= 300:
        raise SettingsError("轮询间隔应在 10–300 秒之间")
    m["loss_budget_u"] = float(m.get("loss_budget_u") or 0)
    mt = cfg["maintenance"]
    for k in ("digest_time", "backup_time"):
        if not _valid_hhmm(mt[k]):
            raise SettingsError("时间格式应为 HH:MM，例如 08:30")
    mt["digest_enabled"], mt["backup_enabled"] = bool(mt["digest_enabled"]), bool(mt["backup_enabled"])
    if cfg["preset"] not in PRESETS and cfg["preset"] != "custom":
        raise SettingsError("未知的预设")
    return cfg


class Settings:
    """线程安全；每个调用使用独立的 SQLite 连接。"""

    def __init__(self, db_path: str, secrets_path: str):
        self.db_path, self.secrets_path = db_path, secrets_path
        self._lock = threading.Lock()
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated REAL)")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        return db

    # ------------------------------------------------------------ 配置
    def get(self) -> dict:
        with self._db() as db:
            row = db.execute("SELECT value FROM settings WHERE key='app'").fetchone()
        return _deep_merge(DEFAULTS, upgrade(json.loads(row[0])) if row else {})

    def version(self) -> float:
        with self._db() as db:
            row = db.execute("SELECT updated FROM settings WHERE key='app'").fetchone()
        return float(row[0]) if row else 0.0

    def is_initialized(self) -> bool:
        with self._db() as db:
            return db.execute("SELECT 1 FROM settings WHERE key='app'").fetchone() is not None

    def save(self, cfg: dict) -> dict:
        cfg = validate(cfg)
        with self._lock, self._db() as db:
            db.execute(
                "INSERT INTO settings (key, value, updated) VALUES ('app', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                (json.dumps(cfg, ensure_ascii=False), time.time()),
            )
        return cfg

    def update(self, patch: dict) -> tuple[dict, list[str]]:
        """合并保存，返回 (新配置, 改动字段列表)。"""
        old = self.get()
        new = self.save(_deep_merge(old, patch))
        return new, diff_keys(old, new)

    def apply_preset(self, name: str) -> dict:
        if name not in PRESETS:
            raise SettingsError("未知的预设")
        cfg = self.get()
        cfg["rules"] = {**cfg["rules"], **copy.deepcopy(PRESETS[name])}
        cfg["preset"] = name
        return self.save(cfg)

    def rules_config(self, leader_id: str | None = None, cfg: dict | None = None) -> RuleConfig:
        cfg = cfg or self.get()
        merged = dict(cfg["rules"])
        if leader_id and leader_id in cfg["leader_rules"]:
            merged.update(cfg["leader_rules"][leader_id])
        return RuleConfig().merged(merged)

    # ------------------------------------------------------------ 密钥
    def secrets(self) -> dict:
        try:
            with open(self.secrets_path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {}

    def set_secrets(self, **values) -> None:
        with self._lock:
            data = self.secrets()
            for k, v in values.items():
                if v is None:
                    data.pop(k, None)
                else:
                    data[k] = v
            self._write_secrets(data)

    def set_exchange_keys(self, account_id: str, keys: dict | None) -> None:
        """交易所账户的 API 密钥（存在 secrets.json 的 exchange_keys 下）；None 表示删除。整个读改写在锁内完成。"""
        with self._lock:
            data = self.secrets()
            ex = dict(data.get("exchange_keys") or {})
            if keys is None:
                ex.pop(account_id, None)
            else:
                ex[account_id] = dict(keys)
            data["exchange_keys"] = ex
            self._write_secrets(data)

    def exchange_keys(self, account_id: str) -> dict:
        return dict((self.secrets().get("exchange_keys") or {}).get(account_id) or {})

    def _write_secrets(self, data: dict) -> None:
        """原子写入 secrets.json（权限 600）。调用方持有 self._lock。"""
        d = os.path.dirname(os.path.abspath(self.secrets_path))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".secrets-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.secrets_path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def binance_credentials(self) -> tuple[str, str]:
        s = self.secrets()
        return s.get("binance_p20t", ""), s.get("binance_csrftoken", "")

    def credential_meta(self) -> dict:
        s = self.secrets()
        return {
            "has_credentials": bool(s.get("binance_p20t") and s.get("binance_csrftoken")),
            "p20t_masked": mask(s.get("binance_p20t")),
            "updated_at": s.get("binance_updated_at", 0),
            "lifetimes_days": s.get("binance_lifetimes_days", []),
        }

    def smtp_config(self, cfg: dict | None = None):
        from .notify import SmtpConfig

        n = (cfg or self.get())["notify"]
        pw = self.secrets().get("smtp_pass", "")
        if not (n["smtp_host"] and n["smtp_user"] and pw):
            return None
        port = int(n["smtp_port"] or 465)
        sec = n["smtp_security"] or ("tls" if port == 465 else "starttls")
        to = [x.strip() for x in (n["smtp_to"] or n["smtp_user"]).replace(";", ",").split(",") if x.strip()]
        return SmtpConfig(n["smtp_host"], port, n["smtp_user"], pw, to, sec, n["smtp_user"])

    # ------------------------------------------------------------ 旧版导入
    def import_legacy(self, app_cfg=None, env: dict | None = None) -> list[str]:
        """首次启动：导入旧版 config.toml 规则 / 领航员覆盖，以及 .env 里的凭证和 SMTP。"""
        if self.is_initialized():
            return []
        env = dict(os.environ if env is None else env)
        imported: list[str] = []
        cfg = copy.deepcopy(DEFAULTS)
        if app_cfg is not None:
            rc = asdict(app_cfg.rules)
            rc["loss_tiers"] = list(rc["loss_tiers"])
            rc["tp_ladder"] = [list(x) for x in rc["tp_ladder"]]
            if rc != _rule_defaults_from_ruleconfig():
                cfg["rules"].update(rc)
                cfg["preset"] = "custom"
                imported.append("风控参数")
            for lc in app_cfg.leaders:
                if lc.rules:
                    cfg["leader_rules"][lc.portfolio_id] = dict(lc.rules)
            cfg["monitor"]["poll_seconds"] = max(10.0, float(app_cfg.poll_seconds))
            cfg["monitor"]["loss_budget_u"] = float(app_cfg.loss_budget_u)
            cfg["notify"]["email_events"] = events_for_severity(app_cfg.email_min_severity)
            cfg["notify"]["email_max_per_hour"] = app_cfg.email_max_per_hour
        if env.get("SMTP_HOST") and env.get("SMTP_USER"):
            cfg["notify"].update(
                smtp_host=env["SMTP_HOST"], smtp_port=int(env.get("SMTP_PORT") or 465),
                smtp_user=env["SMTP_USER"], smtp_to=env.get("SMTP_TO", ""),
                smtp_security=(env.get("SMTP_SECURITY") or "").lower(),
            )
            imported.append("邮箱")
        secrets = {}
        if env.get("SMTP_PASS"):
            secrets["smtp_pass"] = env["SMTP_PASS"]
        if env.get("BINANCE_P20T") and env.get("BINANCE_CSRFTOKEN"):
            secrets.update(binance_p20t=env["BINANCE_P20T"], binance_csrftoken=env["BINANCE_CSRFTOKEN"],
                           binance_updated_at=time.time())
            imported.append("币安凭证")
        if secrets:
            self.set_secrets(**secrets)
        self.save(cfg)
        return imported


def _rule_defaults_from_ruleconfig() -> dict:
    d = asdict(RuleConfig())
    d["loss_tiers"] = list(d["loss_tiers"])
    d["tp_ladder"] = [list(x) for x in d["tp_ladder"]]
    return d


def diff_keys(old: dict, new: dict, prefix: str = "") -> list[str]:
    out = []
    for k in sorted(set(old) | set(new)):
        a, b = old.get(k), new.get(k)
        if isinstance(a, dict) and isinstance(b, dict):
            out += diff_keys(a, b, f"{prefix}{k}.")
        elif a != b:
            out.append(prefix + k)
    return out


def public_view(settings: Settings) -> dict:
    """给前端的配置视图（不含任何密钥明文）。"""
    cfg = settings.get()
    s = settings.secrets()
    return {
        **cfg,
        "credentials": settings.credential_meta(),
        "smtp_pass_set": bool(s.get("smtp_pass")),
        "presets": PRESETS,
        "preset_names": PRESET_NAMES,
        "email_event_groups": [{"key": k, "label": label, "default": d} for k, label, _, d in EVENT_GROUPS],
        "email_event_presets": {"key": DEFAULT_EVENTS, "urgent": URGENT_EVENTS, "all": EVENT_KEYS},
    }
