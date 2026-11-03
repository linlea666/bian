"""离场定性 + 双向反事实（后台定期执行）。

每条离场记录（lifecycle 在我的跟单仓位减仓 / 平仓时写入）：
1. 用我的逐笔成交（fills）确定精确的离场时间、价格、实际已实现盈亏和手续费；
2. 找领航员对应的仓位（同币种同方向、开仓时间相近；我开始跟单时领航员已持有的，取当时仍未平的那一笔）；
3. 定性：
   - 领航员在我离场 ±窗口内平仓 / 减仓 → 跟随平仓；
   - 领航员更晚才平（或还拿着）→ 提前离场 / 提前部分离场（统一叫法，不细分手动 / 止损单）；
   - 领航员早就平了、我后来才平 → 滞后平仓（残留仓位）；
   - 领航员还整仓拿着（他的已平仓历史里查不到这个仓位）：离场后他的历史又成功同步过一次仍查不到，
     就认定他还在持仓 → 提前离场，等他平仓后结算；
   - 30 天内找不到领航员的对应仓位 → 未知。
实时记录器看不到的平仓（凭证失效 / 停机期间、一个轮询周期内开平）由 backfill() 用我的历史仓位补出。
4. 领航员最终平仓后结算反事实：
   - 亏损离场：止损挽回 X U / 白止损 X U；盈利离场：避免回吐 X U / 少赚 X U；
   - 假设结果 =（领航员平仓均价 − 我的开仓价）× 离场数量 × 方向 − 同等手续费。
备注（不改变类型）：疑似强平、疑似总止损、响应提醒。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .diagnostics import match_one

EARLY_TYPES = ("提前离场", "提前部分离场", "跟单结束")
FILL_SLACK_S = 90
FALLBACK_AFTER_S = 2 * 3600  # 2 小时还没同步到成交，就用回合里的估算值
MAX_PENDING_S = 30 * 86400
FEE_RATE = 0.0005
HOLD_CONFIRM_S = 300  # 离场 5 分钟之后的成功同步仍查不到领航员平仓 → 认定他还拿着
BACKFILL_MATCH_S = 600  # 回补时：开仓 ~ 平仓后 10 分钟内已有离场记录的，视为已记录
AUTO_TYPES = ("待判定", "提前离场", "提前部分离场")  # 自动判定、结算前允许随新数据重新定性的类型


@dataclass
class LeaderPos:
    position_id: str
    symbol: str
    side: str
    opened: int  # ms（match_one 需要这个字段名）
    closed_ms: int | None
    update_ms: int | None
    avg_close: float
    status: str


def _rows(store, sql, args=()):
    cur = store.db.execute(sql, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def closing_fills(store, ex: dict, poll_s: float) -> dict | None:
    """离场窗口内我的平仓成交汇总：数量、均价、已实现盈亏、手续费、最后成交时间。"""
    close_side = "SELL" if ex["side"] == "LONG" else "BUY"
    start = (ex["ts"] - poll_s - FILL_SLACK_S) * 1000
    end = ((ex.get("created") or ex["ts"]) + FILL_SLACK_S) * 1000
    rows = store.db.execute(
        "SELECT qty, price, realized, fee, ts_ms FROM fills WHERE lead_id=? AND symbol=? AND side=? "
        "AND position_side IN (?, 'BOTH') AND ts_ms BETWEEN ? AND ?",
        (ex["lead_id"], ex["symbol"], close_side, ex["side"], start, end)).fetchall()
    if not rows:
        return None
    qty = sum(r[0] for r in rows)
    return {"qty": qty, "price": sum(r[0] * r[1] for r in rows) / qty if qty else 0.0,
            "realized": sum(r[2] for r in rows), "fee": sum(r[3] for r in rows), "last_ms": max(r[4] for r in rows)}


def leader_match(store, lead_id: str, symbol: str, side: str, ep_open_s: float) -> LeaderPos | None:
    cands = [LeaderPos(r["position_id"], r["symbol"], r["side"], r["opened_ms"], r["closed_ms"], r["update_ms"],
                       r["avg_close"] or 0.0, r["status"])
             for r in _rows(store, "SELECT * FROM leader_history WHERE lead_id=? AND symbol=? AND side=?",
                            (lead_id, symbol, side))]
    open_ms = int(ep_open_s * 1000)
    hit = match_one(symbol, side, open_ms, cands)
    if hit:
        return hit
    # 我开始跟单时领航员已经持有：取在我开仓时仍未平的、开得最晚的那一笔
    live = [c for c in cands if c.opened <= open_ms + 15 * 60_000 and (c.closed_ms is None or c.closed_ms >= open_ms)]
    return max(live, key=lambda c: c.opened) if live else None


def counterfactual(side: str, entry: float, qty: float, actual_u: float, fee: float, leader_close: float) -> dict:
    d = 1 if side == "LONG" else -1
    hypo = (leader_close - entry) * qty * d - abs(fee)
    delta = actual_u - hypo
    if actual_u < 0:
        verdict = "止损挽回" if delta >= 0 else "白止损"
    else:
        verdict = "避免回吐" if delta >= 0 else "少赚"
    return {"hypo_u": hypo, "delta_u": delta, "verdict": verdict}


def _remarks(store, ex: dict, ep: dict, actual_u: float | None, exit_s: float) -> list[str]:
    out = []
    lev = (ep or {}).get("leverage") or 1
    margin = (ex["entry"] or 0) * ex["qty"] / max(lev, 1)
    if actual_u is not None and margin > 0 and actual_u <= -0.9 * margin:
        out.append("疑似强平")
    others = store.db.execute(
        "SELECT COUNT(*) FROM exits WHERE lead_id=? AND id<>? AND ABS(ts-?)<=60 AND partial=0",
        (ex["lead_id"], ex["id"], ex["ts"])).fetchone()[0]
    if others >= 1 and not ex["partial"]:
        out.append("疑似总止损")
    hit = store.db.execute(
        "SELECT COUNT(*) FROM todos_done WHERE key LIKE ? AND ts BETWEEN ? AND ?",
        (f"pos:{ex['lead_id']}:{ex['symbol']}:%", exit_s - 7200, exit_s + 60)).fetchone()[0]
    if hit:
        out.append("响应提醒")
    return out


def _remark_text(store, ex: dict, ep: dict | None) -> str:
    keep = ["历史回补"] if "历史回补" in (ex.get("remarks") or "") else []
    return "、".join(keep + _remarks(store, ex, ep, ex["actual_u"], ex["ts"]))


def resolve(store, poll_s: float = 20, now: float | None = None) -> list[dict]:
    """处理所有待判定 / 等领航员结果的离场记录。返回本次给出结论的记录（用于事件和摘要）。"""
    now = now or time.time()
    window_ms = max(120.0, 2 * poll_s) * 1000
    done = []
    for ex in _rows(store, "SELECT * FROM exits WHERE status IN ('pending', 'waiting') ORDER BY id"):
        ep = (_rows(store, "SELECT * FROM episodes WHERE id=?", (ex["episode_id"],)) or [None])[0]
        upd: dict = {}
        # 1) 实际结果：优先用成交记录
        if ex["actual_u"] is None:
            f = closing_fills(store, ex, poll_s)
            if f:
                upd.update(actual_u=f["realized"] + f["fee"], fee=f["fee"], price=f["price"], ts=f["last_ms"] / 1000)
            elif now - (ex["created"] or ex["ts"]) > FALLBACK_AFTER_S and ep:
                lev = ep.get("leverage") or 1
                est_margin = (ex["entry"] or 0) * ex["qty"] / max(lev, 1)
                fee = -(ex["price"] or ex["entry"] or 0) * ex["qty"] * FEE_RATE
                upd.update(actual_u=(ex["roi"] or 0) * est_margin + fee, fee=fee)
            else:
                continue  # 等成交记录同步
        ex = {**ex, **upd}
        exit_ms = ex["ts"] * 1000
        # 2) 领航员对应仓位
        opened = (ep or {}).get("opened_ts") or ex.get("opened_ts") or ex["ts"]
        lp = leader_match(store, ex["lead_id"], ex["symbol"], ex["side"], opened)
        manual = bool(ex["manual"])
        etype = ex["exit_type"]
        status = ex["status"]
        if lp is None:
            if now - (ex["created"] or ex["ts"]) > MAX_PENDING_S:
                upd.update(status="done", verdict="未知", verdict_ts=now)
                if not manual and etype in AUTO_TYPES:
                    upd["exit_type"] = "未知"
            elif not manual and etype in AUTO_TYPES and _synced_after(store, ex["lead_id"], ex["ts"] + HOLD_CONFIRM_S):
                # 他的已平仓历史只有平过的仓位：离场后成功同步过仍查不到 → 他还整仓拿着
                upd.update(exit_type="提前部分离场" if ex["partial"] else "提前离场", status="waiting")
                if status == "pending":
                    upd["remarks"] = _remark_text(store, ex, ep)
            _save(store, ex["id"], upd)
            if upd.get("status") == "done":
                row = {**ex, **upd}
                done.append(row)
                _log_verdict(store, row)
            continue
        upd["leader_position_id"] = lp.position_id
        closed = lp.status == "All Closed" and lp.closed_ms
        follow = (closed and abs(lp.closed_ms - exit_ms) <= window_ms) or \
                 (not closed and lp.update_ms and abs(lp.update_ms - exit_ms) <= window_ms and ex["partial"])
        if not manual and etype in AUTO_TYPES:  # 结算前按最新数据重新定性（例如之前以为他还拿着）
            if follow:
                etype = "跟随平仓"
            elif closed and lp.closed_ms < exit_ms - window_ms:
                etype = "滞后平仓"
            else:
                etype = "提前部分离场" if ex["partial"] else "提前离场"
            upd["exit_type"] = etype
        if etype == "跟随平仓":
            upd.update(status="done", verdict="跟随", verdict_ts=now)
        elif closed:
            cf = counterfactual(ex["side"], ex["entry"] or 0, ex["qty"], ex["actual_u"], ex["fee"] or 0, lp.avg_close)
            if etype == "滞后平仓":
                cf["verdict"] = "比领航员多赚" if cf["delta_u"] >= 0 else "比领航员多亏"
            upd.update(status="done", leader_close_ms=lp.closed_ms, leader_close_price=lp.avg_close,
                       verdict_ts=now, **cf)
        else:
            upd["status"] = "waiting"  # 领航员还拿着：等他平仓后结算
        if upd.get("status") == "done" or status == "pending":
            upd["remarks"] = _remark_text(store, ex, ep)
        _save(store, ex["id"], upd)
        if upd.get("status") == "done":
            row = {**ex, **upd}
            done.append(row)
            _log_verdict(store, row)
    return done


def _synced_after(store, lead_id: str, ts: float) -> bool:
    row = store.db.execute("SELECT history_ok FROM leaders WHERE lead_id=?", (lead_id,)).fetchone()
    return bool(row and row[0] and row[0] >= ts)


def backfill(store, poll_s: float = 20, now: float | None = None) -> int:
    """用我的历史仓位补出实时记录器漏掉的离场（凭证失效 / 停机期间平仓、第二期上线前的平仓、
    一个轮询周期内开平）。只读本地数据库、不请求币安；只看近 30 天全部平仓的仓位。返回补了几条。"""
    now = now or time.time()
    since_ms = int((now - MAX_PENDING_S) * 1000)
    added = 0
    for m in _rows(store, "SELECT m.*, COALESCE(l.name, m.lead_id) AS leader_name FROM my_positions m "
                          "LEFT JOIN leaders l ON l.lead_id=m.lead_id "
                          "WHERE m.status='All Closed' AND m.closed_ms >= ? ORDER BY m.closed_ms", (since_ms,)):
        if m["side"] not in ("LONG", "SHORT") or not m["opened_ms"]:
            continue
        opened_s, closed_s = m["opened_ms"] / 1000, m["closed_ms"] / 1000
        known = store.db.execute(
            "SELECT 1 FROM exits WHERE lead_id=? AND symbol=? AND side=? AND ts BETWEEN ? AND ? LIMIT 1",
            (m["lead_id"], m["symbol"], m["side"], opened_s - 60, closed_s + BACKFILL_MATCH_S)).fetchone()
        if known:
            continue
        ep = store.db.execute(
            "SELECT id FROM episodes WHERE source='copy' AND leader_id=? AND symbol=? AND side=? "
            "AND ABS(opened_ts-?) <= 900 ORDER BY ABS(opened_ts-?) LIMIT 1",
            (m["lead_id"], m["symbol"], m["side"], opened_s, opened_s)).fetchone()
        qty = m["closed_qty"] or m["max_qty"] or 0
        if qty <= 0:
            continue
        ex = {"lead_id": m["lead_id"], "symbol": m["symbol"], "side": m["side"], "ts": closed_s, "created": closed_s}
        f = closing_fills(store, ex, poll_s)  # 成交先于历史仓位同步，正常都能找到
        actual, fee, price = (f["realized"] + f["fee"], f["fee"], f["price"]) if f else (m["pnl"], None, m["avg_close"])
        store.db.execute(
            "INSERT INTO exits (episode_id, lead_id, leader_name, symbol, side, ts, qty, entry, price, partial, roi, "
            "actual_u, fee, exit_type, remarks, status, created, opened_ts) VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?)",
            (ep[0] if ep else None, m["lead_id"], m["leader_name"], m["symbol"], m["side"], closed_s, qty,
             m["avg_cost"], price, m["roi"], actual, fee, "待判定", "历史回补", "pending", now, opened_s))
        added += 1
    if added:
        store.db.commit()
        store.log_event("exit.backfill", f"从历史仓位补出 {added} 条离场记录（实时监控没看到的平仓）", n=added)
    return added


def _save(store, exit_id: int, upd: dict) -> None:
    if not upd:
        return
    keys = list(upd)
    store.db.execute(f"UPDATE exits SET {', '.join(k + '=?' for k in keys)} WHERE id=?",
                     [upd[k] for k in keys] + [exit_id])
    store.db.commit()


def _log_verdict(store, ex: dict) -> None:
    side = "多" if ex["side"] == "LONG" else "空"
    if ex["verdict"] in ("跟随", "未知"):
        title = f"[{ex['leader_name']}] {ex['symbol']} {side} 离场判定：{ex['exit_type']}"
    else:
        title = (f"[{ex['leader_name']}] {ex['symbol']} {side} {ex['exit_type']}：{ex['verdict']} "
                 f"{abs(ex['delta_u']):.2f}U（实际 {ex['actual_u']:+.2f}U，跟到底 {ex['hypo_u']:+.2f}U）")
    level = "warn" if ex["verdict"] in ("白止损", "少赚", "比领航员多亏") else "info"
    store.log_event("exit.verdict", title, level, leader_id=ex["lead_id"], episode_id=ex["episode_id"],
                    ref=f"exit:{ex['id']}", **{k: ex.get(k) for k in (
                        "exit_type", "verdict", "actual_u", "hypo_u", "delta_u", "qty", "entry", "price",
                        "leader_close_price", "remarks")})


def set_manual(store, exit_id: int, exit_type: str) -> None:
    """手动纠正离场类型（只用于纠错）。跟随平仓不再算反事实；改成提前类型会重新等待结算。"""
    allowed = ("跟随平仓", "提前离场", "提前部分离场", "滞后平仓", "跟单结束")
    if exit_type not in allowed:
        raise ValueError("离场类型只能是：" + "、".join(allowed))
    status = "done" if exit_type == "跟随平仓" else "pending"
    store.db.execute(
        "UPDATE exits SET exit_type=?, manual=1, status=?, verdict=CASE WHEN ?='跟随平仓' THEN '跟随' ELSE NULL END, "
        "hypo_u=NULL, delta_u=NULL WHERE id=?", (exit_type, status, exit_type, exit_id))
    store.db.commit()
    store.log_event("exit.manual", f"手动纠正离场类型为：{exit_type}", ref=f"exit:{exit_id}")


def residuals(store, poll_s: float = 20, now: float | None = None) -> dict:
    """残留仓位：领航员已平仓，我的跟单仓位过了 2 个轮询周期仍在。返回 {pos_key: 信息}。"""
    now = now or time.time()
    out = {}
    for ep in _rows(store, "SELECT * FROM episodes WHERE status='open' AND source='copy'"):
        lp = leader_match(store, ep["leader_id"], ep["symbol"], ep["side"], ep["opened_ts"])
        if lp and lp.status == "All Closed" and lp.closed_ms and lp.closed_ms / 1000 < now - max(120.0, 2 * poll_s) \
                and lp.closed_ms / 1000 >= ep["opened_ts"] - 60:
            out[ep["pos_key"]] = {"episode_id": ep["id"], "leader_close_ms": lp.closed_ms,
                                  "leader_close_price": lp.avg_close, "symbol": ep["symbol"], "side": ep["side"]}
    return out


def summary(store, since: float = 0.0, lead_id: str | None = None) -> dict:
    """止损效果汇总（统计页 / 领航员页 / 每日摘要用）。"""
    sql = "SELECT exit_type, verdict, delta_u, actual_u FROM exits WHERE status='done' AND ts>=?"
    args: list = [since]
    if lead_id:
        sql += " AND lead_id=?"
        args.append(lead_id)
    s = {"follow": 0, "early": 0, "saved_n": 0, "saved_u": 0.0, "wasted_n": 0, "wasted_u": 0.0,
         "avoid_giveback_n": 0, "avoid_giveback_u": 0.0, "missed_n": 0, "missed_u": 0.0, "lag": 0}
    for etype, verdict, delta, actual in store.db.execute(sql, args):
        if etype == "跟随平仓":
            s["follow"] += 1
            continue
        if etype == "滞后平仓":
            s["lag"] += 1
            continue
        if delta is None:
            continue
        s["early"] += 1
        key = {"止损挽回": "saved", "白止损": "wasted", "避免回吐": "avoid_giveback", "少赚": "missed"}.get(verdict)
        if key:
            s[f"{key}_n"] += 1
            s[f"{key}_u"] += abs(delta)
    s["net_u"] = s["saved_u"] + s["avoid_giveback_u"] - s["wasted_u"] - s["missed_u"]
    s["waiting"] = store.db.execute("SELECT COUNT(*) FROM exits WHERE status='waiting'" +
                                    (" AND lead_id=?" if lead_id else ""), ([lead_id] if lead_id else [])).fetchone()[0]
    return s


def exits_list(store, limit: int = 200, lead_id: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM exits WHERE 1=1", []
    if lead_id:
        sql += " AND lead_id=?"
        args.append(lead_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(min(int(limit), 1000))
    return _rows(store, sql, args)

