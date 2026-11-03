"""配置加载（TOML）。凭证不放配置文件，只走环境变量。"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field

from .rules import RuleConfig


@dataclass
class LeaderConfig:
    portfolio_id: str
    name: str = ""
    source: str = "auto"  # auto / copy（我的跟单仓位，需凭证）/ public（领航员公开持仓）
    position_margin_u: float = 0.0  # public 模式下，我每笔跟单大约投入多少 U 保证金（用于估算 U 亏损）
    recommended_total_stop_u: float = 0.0  # 建议的跟单总止损额（U）；0 = 按 净投入 × recommend_total_stop_pct
    rules: dict = field(default_factory=dict)


@dataclass
class AppConfig:
    poll_seconds: float = 20
    stale_seconds: float = 120
    empty_confirm_polls: int = 2
    auto_discover: bool = True  # 有凭证时自动监控我所有的跟单关系
    db_path: str = "bian.db"
    loss_budget_u: float = 0.0
    rules: RuleConfig = field(default_factory=RuleConfig)
    leaders: list[LeaderConfig] = field(default_factory=list)
    stdout: bool = True
    telegram: bool = True
    email: bool = True
    email_min_severity: str = "warn"  # info / warn / urgent：低于该级别的提醒不发邮件
    email_max_per_hour: int = 12
    web_host: str = "0.0.0.0"
    web_port: int = 3100
    panel_url: str = ""  # 后台访问地址（写进凭证失效邮件），例如 http://1.2.3.4:3100


def load_config(path: str) -> AppConfig:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    mon = raw.get("monitor", {})
    notify = raw.get("notify", {})
    web = raw.get("web", {})
    cfg = AppConfig(
        poll_seconds=float(mon.get("poll_seconds", 20)),
        stale_seconds=float(mon.get("stale_seconds", 120)),
        empty_confirm_polls=int(mon.get("empty_confirm_polls", 2)),
        auto_discover=bool(mon.get("auto_discover", True)),
        db_path=str(mon.get("db_path", "bian.db")),
        loss_budget_u=float(raw.get("portfolio", {}).get("loss_budget_u", 0)),
        rules=RuleConfig().merged(raw.get("rules")),
        stdout=bool(notify.get("stdout", True)),
        telegram=bool(notify.get("telegram", True)),
        email=bool(notify.get("email", True)),
        email_min_severity=str(notify.get("email_min_severity", "warn")),
        email_max_per_hour=int(notify.get("email_max_per_hour", 12)),
        web_host=str(web.get("host", "0.0.0.0")),
        web_port=int(web.get("port", 3100)),
        panel_url=str(web.get("panel_url", "")),
    )
    for item in raw.get("leaders", []):
        cfg.leaders.append(LeaderConfig(
            portfolio_id=str(item["portfolio_id"]),
            name=str(item.get("name", "")),
            source=str(item.get("source", "auto")),
            position_margin_u=float(item.get("position_margin_u", 0)),
            recommended_total_stop_u=float(item.get("recommended_total_stop_u", 0)),
            rules=dict(item.get("rules", {})),
        ))
    if cfg.email_min_severity not in ("info", "warn", "urgent"):
        raise ValueError("email_min_severity 只能是 info / warn / urgent")
    if cfg.poll_seconds < 5:
        raise ValueError("poll_seconds 不能小于 5 秒（避免被限流）")
    return cfg
