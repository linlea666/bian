"""作战室数据：总览、今日待办、领航员卡片、持仓。全部从 runtime / 数据库读取，不直接请求币安。"""
from __future__ import annotations

import time

from ..lifecycle import episodes

LEAD_URL = "https://www.binance.com/zh-CN/copy-trading/lead-details/{}"
COPY_MGMT_URL = "https://www.binance.com/zh-CN/copy-trading"


def day_start(now: float) -> float:
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def month_start(now: float) -> float:
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))


def pnl_since(store, start: float, accounts: list[dict]) -> float | None:
    """区间盈亏 = (现在权益 − 净投入) − (起点权益 − 起点净投入)；扣掉追加 / 赎回投入的影响。
    起点取区间开始前最后一个权益点；没有则取区间内第一个点（服务刚装好时）。"""
    if not accounts:
        return None
    base = store.equity_at_or_before(start)
    after = store.equity_first_after(start)
    total, have = 0.0, False
    for a in accounts:
        b = base.get(a["lead_id"]) or after.get(a["lead_id"])
        if not b:
            continue
        have = True
        total += (a["equity"] - a["invested"]) - (b[0] - b[1])
    return total if have else None


def light_for_account(a: dict, rules: dict, positions: list[dict]) -> str:
    mine = [p for p in positions if p["leader_id"] == a["lead_id"]]
    tier = max((p.get("tier") or 0 for p in mine), default=0)
    if a["drawdown"] >= rules["account_dd_severe"] or tier >= 2:
        return "red"
    if a["drawdown"] >= rules["account_dd_warn"] or tier >= 1 or not a.get("native_stop_u"):
        return "yellow"
    return "green"


def build_todos(ctx: dict) -> list[dict]:
    """今日待办：按紧急程度排序。key 在情况变化时会变（例如档位升级），已处理过的会重新出现。"""
    todos: list[dict] = []
    cred, poll, settings = ctx["cred"], ctx["poll"], ctx["settings"]

    def add(key, level, title, desc, action=None, link=None):
        todos.append({"key": key, "level": level, "title": title, "desc": desc, "action": action, "link": link})

    if cred.get("state") == "missing":
        add("cred:missing", "red", "设置币安凭证", "没有凭证就无法读取你的跟单仓位和总止损设置。",
            "去设置", "#/settings/credentials")
    elif cred.get("state") == "expired":
        add(f"cred:expired:{int(cred.get('expired_at', 0))}", "red", "币安凭证已失效，监控已暂停",
            "重新登录币安网页，复制 cURL 粘贴到设置页即可恢复。", "去更新", "#/settings/credentials")
    elif cred.get("expected_life_days") and cred.get("age_days", 0) >= 0.8 * cred["expected_life_days"]:
        add(f"cred:aging:{int(cred.get('updated_at', 0))}", "yellow", "币安凭证可能快过期了",
            f"当前凭证已使用 {cred['age_days']:.1f} 天，以往平均 {cred['expected_life_days']:.1f} 天失效。可以提前更新。",
            "去更新", "#/settings/credentials")
    if poll.get("stale"):
        add("poll:stale", "red", "监控数据没有更新", f"最近一次成功轮询在 {poll.get('age_text')}前。请检查服务状态。")
    if not ctx["smtp_ok"]:
        add("email:missing", "yellow", "设置邮件提醒", "没有邮箱时，提醒只能在后台看到，手机上收不到。",
            "去设置", "#/settings/email")
    for p in ctx["positions"]:
        if p.get("tier", 0) >= 1:
            side = {"LONG": "多", "SHORT": "空", "HEDGE": "多空对冲"}.get(p["side"], p["side"])
            u = f"{p['unrealized_pnl']:+.2f}U" if p.get("source") == "copy" else ""
            add(f"pos:{p['key']}:tier{p['tier']}", "red" if p["tier"] >= 2 else "yellow",
                f"{p['leader_name']} · {p['symbol']} {side} 亏损到第 {p['tier']} 档",
                f"当前 ROI {p['roi'] * 100:+.1f}% {u}。去币安给这个跟单仓位设置止损或手动减仓。",
                "去币安", LEAD_URL.format(p["leader_id"]))
    rules = settings["rules"]
    for a in ctx["accounts"]:
        if a["drawdown"] >= rules["account_dd_warn"]:
            lvl = "red" if a["drawdown"] >= rules["account_dd_severe"] else "yellow"
            add(f"acct:dd:{a['lead_id']}:{lvl}", lvl, f"{a['name']} 跟单权益回撤 {a['drawdown'] * 100:.1f}%",
                f"权益 {a['equity']:.2f}U（最高 {a['peak_equity']:.2f}U）。考虑暂停这个跟单或减仓。",
                "去币安", LEAD_URL.format(a["lead_id"]))
        if not a.get("native_stop_u"):
            add(f"acct:nostop:{a['lead_id']}", "yellow", f"给 {a['name']} 设置跟单总止损",
                f"净投入 {a['invested']:.0f}U，建议总止损约 {a['recommended_stop_u']:.0f}U。这是币安原生功能，由交易所自动执行。",
                "去币安", COPY_MGMT_URL)
    for lr in poll.get("leaders", []):
        if lr.get("blocked"):
            add(f"leader:blocked:{lr['id']}", "yellow", f"{lr['name']} 无法监控", lr["blocked"])
    done = ctx["done"]
    order = {"red": 0, "yellow": 1, "green": 2}
    out = [t for t in todos if t["key"] not in done]
    out.sort(key=lambda t: order[t["level"]])
    return out


def overview(store, settings, now: float | None = None) -> dict:
    now = now or time.time()
    cfg = settings.get()
    cred, _ = store.get_runtime("credentials", {})
    cred = cred or {"state": "unknown"}
    if not settings.credential_meta()["has_credentials"]:
        cred["state"] = "missing"
    poll, poll_upd = store.get_runtime("poll", {})
    poll = poll or {}
    age = now - poll.get("ts", 0) if poll.get("ts") else None
    limit = cfg["monitor"]["poll_seconds"] * 3 + 30
    poll["age"] = age
    poll["stale"] = age is None or age > limit
    poll["age_text"] = "从未" if age is None else (f"{int(age)} 秒" if age < 120 else f"{int(age / 60)} 分钟")
    accounts, _ = store.get_runtime("accounts", [])
    accounts = accounts or []
    positions, _ = store.get_runtime("positions", [])
    positions = positions or []
    smtp_ok = settings.smtp_config(cfg) is not None and cfg["notify"]["email_enabled"]
    # 只把真正的发送错误算作失败：按级别有意不发、达到每小时上限稍后补发，都不是故障
    last_mail_fail = store.db.execute(
        "SELECT ts, error FROM alerts WHERE error LIKE '%email:%' AND delivered = 0 AND ts > ? "
        "AND error NOT LIKE '%低于邮件通知级别%' AND error NOT LIKE '%达到每小时上限%' ORDER BY id DESC LIMIT 1",
        (now - 3600,)).fetchone()
    rules = cfg["rules"]
    for a in accounts:
        a["light"] = light_for_account(a, rules, positions)
        a["link"] = LEAD_URL.format(a["lead_id"])
        a["pnl"] = a["equity"] - a["invested"]
    today = pnl_since(store, day_start(now), accounts)
    month = pnl_since(store, month_start(now), accounts)
    worst = min((p for p in positions if p.get("source") == "copy"), key=lambda p: p["unrealized_pnl"], default=None)
    alerts_today = store.count_alerts_since(day_start(now))
    ctx = {"cred": cred, "poll": poll, "settings": cfg, "positions": positions, "accounts": accounts,
           "smtp_ok": smtp_ok, "done": store.todos_done()}
    return {
        "now": now,
        "credentials": {**cred, **{k: v for k, v in settings.credential_meta().items() if k != "lifetimes_days"}},
        "poll": poll,
        "email": {"configured": smtp_ok, "last_failure": last_mail_fail and {"ts": last_mail_fail[0], "error": last_mail_fail[1]}},
        "totals": {
            "equity": sum(a["equity"] for a in accounts),
            "invested": sum(a["invested"] for a in accounts),
            "unrealized": sum(p["unrealized_pnl"] for p in positions if p.get("source") == "copy"),
            "today_pnl": today, "month_pnl": month,
            "positions": len(positions),
            "worst_position": worst and {k: worst[k] for k in ("leader_name", "symbol", "side", "unrealized_pnl", "roi")},
            "alerts_today": alerts_today,
        },
        "accounts": accounts,
        "todos": build_todos(ctx),
        "preset": cfg["preset"],
        "onboarding_done": cfg["onboarding_done"],
    }


def positions_view(store) -> dict:
    positions, upd = store.get_runtime("positions", [])
    open_eps = {e["pos_key"]: e for e in episodes(store, status="open", limit=500)}
    for p in positions or []:
        keys = [p["key"]] if p["side"] != "HEDGE" else [f"{p['leader_id']}:{p['symbol']}:LONG", f"{p['leader_id']}:{p['symbol']}:SHORT"]
        eps = [open_eps[k] for k in keys if k in open_eps]
        p["episodes"] = [{k: e[k] for k in ("id", "opened_ts", "mae_roi", "mae_u", "mfe_roi", "mfe_u", "adds", "reduces", "max_tier")} for e in eps]
        p["link"] = LEAD_URL.format(p["leader_id"])
    return {"updated": upd, "positions": positions or []}


def equity_curve(store, days: int = 30, now: float | None = None) -> list[dict]:
    """全部跟单合计的权益 / 盈亏曲线：同一轮写入的多个跟单按分钟合并成一个点，最多 600 个点。"""
    now = now or time.time()
    rows = store.equity_series(now - days * 86400)
    latest: dict[str, tuple] = {}
    out: list[dict] = []
    for ts, lid, eq, inv in rows:
        latest[lid] = (eq, inv)
        point = {"ts": ts, "equity": sum(v[0] for v in latest.values()),
                 "pnl": sum(v[0] - v[1] for v in latest.values())}
        if out and ts - out[-1]["ts"] < 60:
            out[-1] = {**point, "ts": out[-1]["ts"]}
        else:
            out.append(point)
    step = max(1, len(out) // 600)
    return out[::step]
