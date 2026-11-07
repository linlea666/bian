"""统计：盈亏日历、区间汇总、盈亏构成、亏损分析、领航员排行、利润回吐。

口径（页面上同样注明）：
- 已实现净盈亏 = 已实现盈亏 + 手续费（手续费为负数）+ 资金费：跟单来自我的逐笔成交（含跟单开始以来的历史回补，
  没有资金费数据）；交易所账户来自资金流水（account_ledger，近 3 个月回补，含资金费）；
- source 筛选：all 全部 / copy 只看跟单 / 交易所账户 ID（x 开头）只看这个账户；
- 权益变化 =（权益 − 净投入）的变化，扣除追加 / 赎回，来自 5 分钟一次的权益快照（安装后才有）；
- 分润 = 跟单关系里的累计已分润，按区间首尾做差；资金费接口没有数据，不含。
日期按服务器时区（systemd 里设置的 TZ，默认 Asia/Shanghai）划分。
"""
from __future__ import annotations

import calendar as _cal
from datetime import date as _date
import json
import time

from . import exits
from .lifecycle import episodes, labels
from .models import is_mine


def _local_midnight(ts: float) -> float:
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def period_starts(now: float) -> dict[str, float]:
    today = _local_midnight(now)
    lt = time.localtime(now)
    week = today - lt.tm_wday * 86400
    month = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    return {"today": today, "yesterday": today - 86400, "week": week, "month": month,
            "d30": today - 29 * 86400, "all": 0.0}


def is_account(source: str | None) -> bool:
    return bool(source) and source[0] == "x"


def daily_realized(store, start: float, end: float, lead_id: str | None = None,
                   source: str = "all") -> dict[str, dict]:
    """按天汇总我的已实现盈亏、手续费、资金费：{'2026-11-03': {realized, fee, funding, net, trades}}。
    lead_id：某个领航员（跟单）或交易所账户；source：all / copy / 账户 ID。"""
    rng = [int(start * 1000), int(end * 1000)]
    out: dict[str, dict] = {}

    def add(d, r, f, fu, n):
        x = out.setdefault(d, {"realized": 0.0, "fee": 0.0, "funding": 0.0, "net": 0.0, "trades": 0})
        x["realized"] += r or 0.0
        x["fee"] += f or 0.0
        x["funding"] += fu or 0.0
        x["trades"] += n or 0
        x["net"] = x["realized"] + x["fee"] + x["funding"]

    target = lead_id or (source if is_account(source) else None)
    if not is_account(target) and source in ("all", "copy"):
        sql = ("SELECT date(ts_ms/1000, 'unixepoch', 'localtime') AS d, SUM(realized), SUM(fee), "
               "SUM(CASE WHEN realized <> 0 THEN 1 ELSE 0 END) FROM fills WHERE ts_ms >= ? AND ts_ms < ?")
        args = list(rng)
        if target:
            sql += " AND lead_id=?"
            args.append(target)
        for d, r, f, n in store.db.execute(sql + " GROUP BY d", args):
            add(d, r, f, 0.0, n)
    if source != "copy" and (target is None or is_account(target)):
        sql = ("SELECT date(ts_ms/1000, 'unixepoch', 'localtime') AS d, "
               "SUM(CASE WHEN kind='realized' THEN amount ELSE 0 END), SUM(CASE WHEN kind='fee' THEN amount ELSE 0 END), "
               "SUM(CASE WHEN kind='funding' THEN amount ELSE 0 END), "
               "SUM(CASE WHEN kind='realized' AND amount <> 0 THEN 1 ELSE 0 END) "
               "FROM account_ledger WHERE ts_ms >= ? AND ts_ms < ?")
        args = list(rng)
        if target:
            sql += " AND account_id=?"
            args.append(target)
        for d, r, f, fu, n in store.db.execute(sql + " GROUP BY d", args):
            add(d, r, f, fu, n)
    return out


def _equity_filter(source: str) -> tuple[str, list]:
    if source == "copy":
        return " AND leader_id NOT LIKE 'x%'", []
    if is_account(source):
        return " AND leader_id = ?", [source]
    return "", []


def daily_equity_change(store, start: float, end: float, source: str = "all") -> dict[str, float]:
    """按天的（权益 − 净投入）变化（跟单和交易所账户合计，可按来源筛选）：每天最后一个点减前一天最后一个点。
    一次顺序扫描，避免相关子查询在快照变多后变慢。"""
    last: dict[str, dict[str, float]] = {}
    where, extra = _equity_filter(source)
    for ts, lid, v in store.db.execute(
            "SELECT ts, leader_id, equity - invested FROM equity WHERE ts >= ? AND ts < ?" + where + " ORDER BY ts",
            (start - 86400, end, *extra)):
        last.setdefault(time.strftime("%Y-%m-%d", time.localtime(ts)), {})[lid] = v
    out, prev = {}, None
    for d in sorted(last):
        if prev is not None:
            out[d] = sum(v - prev.get(lid, v) for lid, v in last[d].items())
        prev = {**(prev or {}), **last[d]}
    first_day = time.strftime("%Y-%m-%d", time.localtime(start))
    return {d: v for d, v in out.items() if d >= first_day}


def calendar_month(store, year: int, month: int, source: str = "all") -> dict:
    start = time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1))
    days_in = _cal.monthrange(year, month)[1]
    end = start + days_in * 86400 + 3600  # 跨夏令时也覆盖到月底
    realized = daily_realized(store, start, end, source=source)
    eq = daily_equity_change(store, start, end, source)
    days = []
    for d in range(1, days_in + 1):
        key = f"{year:04d}-{month:02d}-{d:02d}"
        r = realized.get(key)
        days.append({"date": key, "net": r["net"] if r else None, "fee": r["fee"] if r else None,
                     "trades": r["trades"] if r else 0, "equity_change": eq.get(key)})
    nets = [x["net"] for x in days if x["net"] is not None]
    return {"year": year, "month": month, "first_weekday": _cal.monthrange(year, month)[0], "days": days,
            "total_net": sum(nets), "win_days": sum(1 for n in nets if n > 0), "loss_days": sum(1 for n in nets if n < 0)}


def _profit_shared_change(store, start: float) -> float:
    """区间内新增分润。start=0 表示全部（= 当前累计分润）；否则用当前值减去区间开始前最后一个值，
    区间开始前没有数据（安装晚于区间开始）时，从安装后的第一个值算起，不猜测之前的金额。"""
    total = 0.0
    for (lid,) in store.db.execute("SELECT DISTINCT leader_id FROM equity WHERE profit_shared IS NOT NULL"):
        q = "SELECT profit_shared FROM equity WHERE leader_id=? AND profit_shared IS NOT NULL"
        last = store.db.execute(q + " ORDER BY ts DESC LIMIT 1", (lid,)).fetchone()[0] or 0.0
        if start <= 0:
            total += last
            continue
        base = store.db.execute(q + " AND ts < ? ORDER BY ts DESC LIMIT 1", (lid, start)).fetchone() \
            or store.db.execute(q + " ORDER BY ts LIMIT 1", (lid,)).fetchone()
        total += last - (base[0] or 0.0)
    return total


def periods(store, now: float | None = None, source: str = "all") -> dict:
    now = now or time.time()
    out = {}
    starts = period_starts(now)
    for name, start in starts.items():
        end = starts["today"] if name == "yesterday" else now + 1
        d = daily_realized(store, start, end, source=source)
        out[name] = {k: sum(x[k] for x in d.values()) for k in ("net", "realized", "fee", "funding", "trades")}
    return out


def _source_match(p: dict, source: str) -> bool:
    if source == "copy":
        return p.get("source") == "copy"
    if is_account(source):
        return p.get("leader_id") == source
    return is_mine(p.get("source") or "")


def composition(store, now: float | None = None, source: str = "all") -> dict:
    now = now or time.time()
    month = period_starts(now)["month"]
    d_all = daily_realized(store, 0, now + 1, source=source)
    d_month = daily_realized(store, month, now + 1, source=source)
    positions, _ = store.get_runtime("positions", [])
    unreal = sum(p.get("unrealized_pnl") or 0 for p in positions or [] if _source_match(p, source))
    shared = not is_account(source)  # 分润只有跟单有

    def part(d, since):
        return {"realized": sum(x["realized"] for x in d.values()), "fee": sum(x["fee"] for x in d.values()),
                "funding": sum(x["funding"] for x in d.values()),
                "profit_shared": -_profit_shared_change(store, since) if shared else 0.0}
    return {"all": part(d_all, 0.0), "month": part(d_month, month), "unrealized": unreal}


def _closed_trades(store, source: str) -> list[dict]:
    """已平仓的单笔结果：跟单用币安的历史仓位（精确的平仓盈亏）；交易所账户用仓位回合（平仓前最后的浮动盈亏，近似）。"""
    out = []
    if not is_account(source):
        for r in store.db.execute(
                "SELECT m.lead_id, COALESCE(l.name, m.lead_id), m.symbol, m.side, m.opened_ms, m.closed_ms, m.pnl, m.roi, "
                "m.leverage FROM my_positions m LEFT JOIN leaders l ON l.lead_id=m.lead_id WHERE m.status='All Closed'"):
            out.append({"lead_id": r[0], "leader_name": r[1], "symbol": r[2], "side": r[3], "opened_ms": r[4],
                        "closed_ms": r[5], "pnl": r[6] or 0.0, "roi": r[7], "leverage": r[8]})
    if source != "copy":
        sql = ("SELECT leader_id, leader_name, symbol, side, opened_ts, closed_ts, last_u, last_roi, leverage FROM episodes "
               "WHERE source='exchange' AND status='closed' AND last_u IS NOT NULL")
        args = []
        if is_account(source):
            sql += " AND leader_id=?"
            args.append(source)
        for r in store.db.execute(sql, args):
            out.append({"lead_id": r[0], "leader_name": r[1], "symbol": r[2], "side": r[3],
                        "opened_ms": int((r[4] or 0) * 1000), "closed_ms": int((r[5] or 0) * 1000), "pnl": r[6],
                        "roi": r[7], "leverage": r[8], "approx": True})
    return out


def losses(store, limit: int = 10, source: str = "all") -> dict:
    trades = _closed_trades(store, source)
    worst = sorted((t for t in trades if t["pnl"] < 0), key=lambda t: t["pnl"])[:limit]
    buckets = [(-1e18, -50), (-50, -20), (-20, -10), (-10, -5), (-5, -1), (-1, 0), (0, 1), (1, 5), (5, 20), (20, 1e18)]
    names = ["≤-50", "-50~-20", "-20~-10", "-10~-5", "-5~-1", "-1~0", "0~1", "1~5", "5~20", "≥20"]
    pnls = [t["pnl"] for t in trades]
    dist = [{"range": n, "count": sum(1 for p in pnls if lo <= (p or 0) < hi)} for n, (lo, hi) in zip(names, buckets)]
    days = daily_realized(store, 0, time.time() + 1, source=source)
    streak = best = 0
    prev = None
    for d in sorted(days):
        cur = _date.fromisoformat(d)
        if prev is None or (cur - prev).days != 1:
            streak = 0  # 中间隔了没有交易的日子：连续中断（按日历天算“连续”）
        streak = streak + 1 if days[d]["net"] < 0 else 0
        best, prev = max(best, streak), cur
    return {"worst": worst, "distribution": dist, "max_loss_streak_days": best, "closed_n": len(pnls)}


def giveback(store) -> list[dict]:
    """按领航员统计我的回合里的利润回吐 / 盈转亏和少拿的利润。"""
    by: dict[str, dict] = {}
    for ep in episodes(store, status="closed", limit=500):
        if ep["source"] != "copy":
            continue
        tags = labels(ep)
        b = by.setdefault(ep["leader_id"], {"lead_id": ep["leader_id"], "name": ep["leader_name"], "closed": 0,
                                            "giveback": 0, "profit_to_loss": 0, "no_partial_tp": 0, "lost_u": 0.0})
        b["closed"] += 1
        if "利润回吐" in tags or "盈转亏" in tags:
            b["giveback" if "利润回吐" in tags else "profit_to_loss"] += 1
            if ep.get("mfe_u") is not None and ep.get("last_u") is not None:
                b["lost_u"] += max(0.0, ep["mfe_u"] - ep["last_u"])
        if "未分批止盈" in tags:
            b["no_partial_tp"] += 1
    return sorted(by.values(), key=lambda x: -x["lost_u"])


def _my_trades(store, lead_id: str) -> dict:
    rows = [r[0] or 0.0 for r in store.db.execute(
        "SELECT pnl FROM my_positions WHERE lead_id=? AND status='All Closed'", (lead_id,))]
    wins = [p for p in rows if p > 0]
    loss = [p for p in rows if p < 0]
    return {"closed": len(rows), "win_rate": len(wins) / len(rows) if rows else None,
            "payoff": (sum(wins) / len(wins)) / (-sum(loss) / len(loss)) if wins and loss else None}


def ranking(store, now: float | None = None) -> list[dict]:
    """领航员排行：我的结果 + 领航员风格 + 止损效果 + 提醒次数。"""
    now = now or time.time()
    month = period_starts(now)["month"]
    accounts, _ = store.get_runtime("accounts", [])
    acc = {a["lead_id"]: a for a in accounts or []}
    deviation, _ = store.get_runtime("deviation", {})
    out = []
    for lid, name, kind in store.db.execute("SELECT lead_id, name, kind FROM leaders ORDER BY added"):
        a = acc.get(lid)
        real_m = daily_realized(store, month, now + 1, lid)
        real_all = daily_realized(store, 0, now + 1, lid)
        prof = store.db.execute("SELECT data, n, computed_at FROM leader_profiles WHERE lead_id=?", (lid,)).fetchone()
        p = json.loads(prof[0])["profile"] if prof else {}
        job = store.db.execute("SELECT state, progress, total FROM jobs WHERE lead_id=? ORDER BY id DESC LIMIT 1",
                               (lid,)).fetchone()
        ex = exits.summary(store, 0, lid)
        out.append({
            "lead_id": lid, "name": name, "kind": kind,
            "equity": a["equity"] if a else None, "pnl": (a["equity"] - a["invested"]) if a else None,
            "drawdown": a["drawdown"] if a else None, "native_stop_u": a.get("native_stop_u") if a else None,
            "month_net": sum(x["net"] for x in real_m.values()), "all_net": sum(x["net"] for x in real_all.values()),
            **_my_trades(store, lid),
            "alerts": store.db.execute("SELECT COUNT(*) FROM alerts WHERE key LIKE ? OR key = ?",
                                       (f"{lid}:%", f"acct:{lid}")).fetchone()[0],
            "tags": p.get("tags", []), "carry_rate": p.get("carry_rate"), "giveback_rate": p.get("giveback_rate"),
            "leader_win_rate": p.get("win_rate"), "leader_n": prof[1] if prof else 0,
            "exits_net_u": ex["net_u"], "early_n": ex["early"],
            "deviation": (deviation or {}).get(lid),
            "job": {"state": job[0], "progress": job[1], "total": job[2]} if job else None,
        })
    return out
