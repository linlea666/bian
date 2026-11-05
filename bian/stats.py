"""统计：盈亏日历、区间汇总、盈亏构成、亏损分析、领航员排行、利润回吐。

口径（页面上同样注明）：
- 已实现净盈亏 = 已实现盈亏 + 手续费（手续费为负数），来自我的逐笔成交，含跟单开始以来的历史回补；
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


def daily_realized(store, start: float, end: float, lead_id: str | None = None) -> dict[str, dict]:
    """按天汇总我的已实现盈亏和手续费：{'2026-11-03': {realized, fee, net, trades}}。"""
    sql = ("SELECT date(ts_ms/1000, 'unixepoch', 'localtime') AS d, SUM(realized), SUM(fee), "
           "SUM(CASE WHEN realized <> 0 THEN 1 ELSE 0 END) FROM fills WHERE ts_ms >= ? AND ts_ms < ?")
    args: list = [int(start * 1000), int(end * 1000)]
    if lead_id:
        sql += " AND lead_id=?"
        args.append(lead_id)
    sql += " GROUP BY d"
    return {d: {"realized": r or 0.0, "fee": f or 0.0, "net": (r or 0.0) + (f or 0.0), "trades": n or 0}
            for d, r, f, n in store.db.execute(sql, args)}


def daily_equity_change(store, start: float, end: float) -> dict[str, float]:
    """按天的（权益 − 净投入）变化，所有跟单合计：每天最后一个点减前一天最后一个点。
    一次顺序扫描，避免相关子查询在快照变多后变慢。"""
    last: dict[str, dict[str, float]] = {}
    for ts, lid, v in store.db.execute(
            "SELECT ts, leader_id, equity - invested FROM equity WHERE ts >= ? AND ts < ? ORDER BY ts",
            (start - 86400, end)):
        last.setdefault(time.strftime("%Y-%m-%d", time.localtime(ts)), {})[lid] = v
    out, prev = {}, None
    for d in sorted(last):
        if prev is not None:
            out[d] = sum(v - prev.get(lid, v) for lid, v in last[d].items())
        prev = {**(prev or {}), **last[d]}
    first_day = time.strftime("%Y-%m-%d", time.localtime(start))
    return {d: v for d, v in out.items() if d >= first_day}


def calendar_month(store, year: int, month: int) -> dict:
    start = time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1))
    days_in = _cal.monthrange(year, month)[1]
    end = start + days_in * 86400 + 3600  # 跨夏令时也覆盖到月底
    realized = daily_realized(store, start, end)
    eq = daily_equity_change(store, start, end)
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


def periods(store, now: float | None = None) -> dict:
    now = now or time.time()
    out = {}
    starts = period_starts(now)
    for name, start in starts.items():
        end = starts["today"] if name == "yesterday" else now + 1
        d = daily_realized(store, start, end)
        out[name] = {"net": sum(x["net"] for x in d.values()), "realized": sum(x["realized"] for x in d.values()),
                     "fee": sum(x["fee"] for x in d.values()), "trades": sum(x["trades"] for x in d.values())}
    return out


def composition(store, now: float | None = None) -> dict:
    now = now or time.time()
    month = period_starts(now)["month"]
    d_all = daily_realized(store, 0, now + 1)
    d_month = daily_realized(store, month, now + 1)
    positions, _ = store.get_runtime("positions", [])
    unreal = sum(p.get("unrealized_pnl") or 0 for p in positions or [] if p.get("source") == "copy")
    return {
        "all": {"realized": sum(x["realized"] for x in d_all.values()), "fee": sum(x["fee"] for x in d_all.values()),
                "profit_shared": -_profit_shared_change(store, 0.0)},
        "month": {"realized": sum(x["realized"] for x in d_month.values()),
                  "fee": sum(x["fee"] for x in d_month.values()),
                  "profit_shared": -_profit_shared_change(store, month)},
        "unrealized": unreal,
    }


def losses(store, limit: int = 10) -> dict:
    rows = store.db.execute(
        "SELECT m.lead_id, COALESCE(l.name, m.lead_id), m.symbol, m.side, m.opened_ms, m.closed_ms, m.pnl, m.roi, m.leverage "
        "FROM my_positions m LEFT JOIN leaders l ON l.lead_id=m.lead_id WHERE m.status='All Closed' ORDER BY m.pnl LIMIT ?",
        (limit,)).fetchall()
    worst = [{"lead_id": r[0], "leader_name": r[1], "symbol": r[2], "side": r[3], "opened_ms": r[4], "closed_ms": r[5],
              "pnl": r[6], "roi": r[7], "leverage": r[8]} for r in rows if (r[6] or 0) < 0]
    buckets = [(-1e18, -50), (-50, -20), (-20, -10), (-10, -5), (-5, -1), (-1, 0), (0, 1), (1, 5), (5, 20), (20, 1e18)]
    names = ["≤-50", "-50~-20", "-20~-10", "-10~-5", "-5~-1", "-1~0", "0~1", "1~5", "5~20", "≥20"]
    pnls = [r[0] for r in store.db.execute("SELECT pnl FROM my_positions WHERE status='All Closed'")]
    dist = [{"range": n, "count": sum(1 for p in pnls if lo <= (p or 0) < hi)} for n, (lo, hi) in zip(names, buckets)]
    days = daily_realized(store, 0, time.time() + 1)
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
