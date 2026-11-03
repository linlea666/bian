"""仓位回合（episode）记录：一个仓位从首次出现到消失的全过程。

每轮轮询更新：最大浮亏 / 最大浮盈（ROI、U、发生时间）、加仓 / 减仓（是否在盈利时减仓）、
到达的最高亏损档位、提醒次数。平仓时给出“利润回吐 / 盈转亏 / 是否分批止盈”等标注。
这些数据是第二、三期统计与止损 / 止盈推荐的基础，所以第一期就开始记录。
"""
from __future__ import annotations

import time

from .models import Position

COLS = ("id", "pos_key", "leader_id", "leader_name", "symbol", "side", "source", "status", "opened_ts",
        "closed_ts", "first_entry", "last_entry", "first_qty", "max_qty", "last_qty", "leverage", "max_margin_u",
        "mae_roi", "mae_u", "mae_ts", "mfe_roi", "mfe_u", "mfe_ts", "last_roi", "last_u", "last_mark", "adds",
        "reduces", "reduces_in_profit", "max_tier", "alerts", "exit_type", "note", "last_seen")

GIVEBACK_MIN_MFE = 0.30  # 最大浮盈至少 +30% ROI 才评估“利润回吐 / 盈转亏”
QTY_EPS = 0.01
POINT_EVERY_S = 300  # 时间线采样间隔


def ensure_schema(db) -> None:
    cols = {r[1] for r in db.execute("PRAGMA table_info(episodes)")}
    if "last_seen" not in cols:
        db.execute("ALTER TABLE episodes ADD COLUMN last_seen REAL")
        db.commit()


def labels(ep: dict) -> list[str]:
    """平仓后的标注（也用于页面展示）。"""
    out = []
    mfe, last = ep.get("mfe_roi") or 0.0, ep.get("last_roi") or 0.0
    if mfe >= GIVEBACK_MIN_MFE:
        if last < 0:
            out.append("盈转亏")
        elif last < mfe / 2:
            out.append("利润回吐")
        out.append("分批止盈" if (ep.get("reduces_in_profit") or 0) > 0 else "未分批止盈")
    if (ep.get("mae_roi") or 0) <= -1.0:
        out.append("浮亏超过保证金")
    if (ep.get("adds") or 0) >= 2:
        out.append("多次加仓")
    return out


class EpisodeRecorder:
    def __init__(self, store, clock=time.time, downtime_s: float = 600):
        self.store, self.clock, self.downtime_s = store, clock, downtime_s
        ensure_schema(store.db)
        self.open: dict[str, dict] = {}
        for row in store.db.execute(f"SELECT {', '.join(COLS)} FROM episodes WHERE status='open'"):
            ep = dict(zip(COLS, row))
            self.open[ep["pos_key"]] = ep
        self.last_point: dict[int, float] = {
            r[0]: r[1] for r in store.db.execute("SELECT episode_id, MAX(ts) FROM episode_points GROUP BY episode_id")}

    # ------------------------------------------------------------ 写库
    def _insert(self, ep: dict) -> int:
        keys = [k for k in COLS if k != "id"]
        cur = self.store.db.execute(
            f"INSERT INTO episodes ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
            [ep.get(k) for k in keys])
        self.store.db.commit()
        return int(cur.lastrowid)

    def _save(self, ep: dict) -> None:
        keys = [k for k in COLS if k != "id"]
        self.store.db.execute(
            f"UPDATE episodes SET {', '.join(k + '=?' for k in keys)} WHERE id=?",
            [ep.get(k) for k in keys] + [ep["id"]])

    # ------------------------------------------------------------ 观察
    def observe(self, leader_id: str, leader_name: str, positions: list[Position], now: float | None = None) -> None:
        now = now or self.clock()
        cur = {p.key: p for p in positions if p.side in ("LONG", "SHORT")}
        for key, p in cur.items():
            ep = self.open.get(key)
            if ep and now - (ep.get("last_seen") or now) > self.downtime_s and ep["last_entry"] and \
                    abs(p.entry_price / ep["last_entry"] - 1) > 1e-6 and p.qty <= (ep["last_qty"] or 0) * (1 + QTY_EPS):
                # 停机期间平仓又重新开（开仓价变了、数量没增加）：旧回合以“停机期间结束”收尾
                self._close(ep, now, "停机期间结束", leader_name)
                ep = None
            if ep is None:
                self._open(leader_id, leader_name, p, now)
            else:
                self._update(ep, p, now, leader_name)
        for key in [k for k, e in self.open.items() if e["leader_id"] == leader_id and k not in cur]:
            self._close(self.open[key], now, "已平仓", leader_name)
        self.store.db.commit()

    def _money(self, p: Position, roi: float) -> float | None:
        return roi * p.margin if p.source == "copy" and p.margin else None

    def _open(self, leader_id: str, leader_name: str, p: Position, now: float) -> None:
        roi = p.roi
        ep = {
            "pos_key": p.key, "leader_id": leader_id, "leader_name": leader_name, "symbol": p.symbol,
            "side": p.side, "source": p.source, "status": "open", "opened_ts": now, "closed_ts": None,
            "first_entry": p.entry_price, "last_entry": p.entry_price, "first_qty": p.qty, "max_qty": p.qty,
            "last_qty": p.qty, "leverage": p.leverage, "max_margin_u": p.margin,
            "mae_roi": min(roi, 0.0), "mae_u": self._money(p, min(roi, 0.0)), "mae_ts": now,
            "mfe_roi": max(roi, 0.0), "mfe_u": self._money(p, max(roi, 0.0)), "mfe_ts": now,
            "last_roi": roi, "last_u": p.unrealized_pnl if p.source == "copy" else None, "last_mark": p.mark_price,
            "adds": 0, "reduces": 0, "reduces_in_profit": 0, "max_tier": 0, "alerts": 0,
            "exit_type": None, "note": None, "last_seen": now,
        }
        ep["id"] = self._insert(ep)
        self.open[p.key] = ep
        self._point(ep, p, now, force=True)
        side = "多" if p.side == "LONG" else "空"
        self.store.log_event("position.open", f"[{leader_name}] {p.symbol} {side} 开仓", leader_id=leader_id,
                             episode_id=ep["id"], ts=now, entry=p.entry_price, qty=p.qty, leverage=p.leverage,
                             margin_u=round(p.margin, 4), roi=round(roi, 4))

    def _update(self, ep: dict, p: Position, now: float, leader_name: str) -> None:
        roi = p.roi
        side = "多" if p.side == "LONG" else "空"
        last_qty = ep["last_qty"] or 0
        if last_qty and p.qty > last_qty * (1 + QTY_EPS):
            ep["adds"] += 1
            self.store.log_event("position.add", f"[{leader_name}] {p.symbol} {side} 加仓 {last_qty:g} → {p.qty:g}",
                                 "warn", leader_id=ep["leader_id"], episode_id=ep["id"], ts=now,
                                 qty_from=last_qty, qty_to=p.qty, entry_from=ep["last_entry"], entry_to=p.entry_price,
                                 roi=round(roi, 4))
        elif last_qty and p.qty < last_qty * (1 - QTY_EPS):
            ep["reduces"] += 1
            in_profit = roi > 0
            if in_profit:
                ep["reduces_in_profit"] += 1
            self.store.log_event("position.reduce",
                                 f"[{leader_name}] {p.symbol} {side} 减仓 {last_qty:g} → {p.qty:g}"
                                 + ("（盈利中减仓）" if in_profit else ""),
                                 leader_id=ep["leader_id"], episode_id=ep["id"], ts=now, qty_from=last_qty,
                                 qty_to=p.qty, roi=round(roi, 4), in_profit=in_profit)
            self._record_exit(ep, now, last_qty - p.qty, partial=True, mark=p.mark_price, roi=roi,
                              exit_type="待判定")
        if roi < (ep["mae_roi"] or 0):
            ep["mae_roi"], ep["mae_u"], ep["mae_ts"] = roi, self._money(p, roi), now
        if roi > (ep["mfe_roi"] or 0):
            ep["mfe_roi"], ep["mfe_u"], ep["mfe_ts"] = roi, self._money(p, roi), now
        ep.update(last_entry=p.entry_price, last_qty=p.qty, max_qty=max(ep["max_qty"] or 0, p.qty),
                  leverage=p.leverage or ep["leverage"], max_margin_u=max(ep["max_margin_u"] or 0, p.margin),
                  last_roi=roi, last_u=p.unrealized_pnl if p.source == "copy" else None, last_mark=p.mark_price,
                  last_seen=now, leader_name=leader_name)
        self._save(ep)
        self._point(ep, p, now)

    def _close(self, ep: dict, now: float, exit_type: str, leader_name: str) -> None:
        if ep.get("last_qty"):
            # 停机期间结束的：实际平仓时间在 last_seen ~ now 之间，后台按成交记录确定
            ts = ep.get("last_seen") or now if exit_type == "停机期间结束" else now
            self._record_exit(ep, ts, ep["last_qty"], partial=False, mark=ep.get("last_mark"),
                              roi=ep.get("last_roi"), exit_type="跟单结束" if exit_type == "跟单结束" else "待判定",
                              created=now)
        ep.update(status="closed", closed_ts=now, exit_type=exit_type)
        tags = labels(ep)
        ep["note"] = "、".join(tags)
        self._save(ep)
        self.open.pop(ep["pos_key"], None)
        side = "多" if ep["side"] == "LONG" else "空"
        u = f"，最后浮盈亏 {ep['last_u']:+.2f}U" if ep.get("last_u") is not None else ""
        level = "warn" if ("盈转亏" in tags or "利润回吐" in tags) else "info"
        self.store.log_event(
            "position.close",
            f"[{leader_name}] {ep['symbol']} {side} {exit_type}：最后 ROI {ep['last_roi'] * 100:+.1f}%{u}"
            + (f"（{ep['note']}）" if ep["note"] else ""),
            level, leader_id=ep["leader_id"], episode_id=ep["id"], ts=now,
            hold_hours=round((now - ep["opened_ts"]) / 3600, 2), mae_roi=ep["mae_roi"], mae_u=ep["mae_u"],
            mfe_roi=ep["mfe_roi"], mfe_u=ep["mfe_u"], last_roi=ep["last_roi"], last_u=ep["last_u"],
            adds=ep["adds"], reduces=ep["reduces"], max_tier=ep["max_tier"], labels=tags)

    def _point(self, ep: dict, p: Position, now: float, force: bool = False) -> None:
        if not force and now - self.last_point.get(ep["id"], 0) < POINT_EVERY_S:
            return
        self.last_point[ep["id"]] = now
        self.store.db.execute("INSERT INTO episode_points (episode_id, ts, roi, u, mark) VALUES (?,?,?,?,?)",
                              (ep["id"], now, p.roi, p.unrealized_pnl if p.source == "copy" else None, p.mark_price))

    def _record_exit(self, ep: dict, now: float, qty: float, partial: bool, mark, roi, exit_type: str,
                     created: float | None = None) -> None:
        """我的跟单仓位减仓 / 平仓：写一条待判定的离场记录，由后台对比领航员后定性并做反事实。
        只针对我自己的跟单仓位（source=copy）；领航员公开持仓不是我的钱，不记。"""
        if ep.get("source") != "copy" or qty <= 0:
            return
        self.store.db.execute(
            "INSERT INTO exits (episode_id, lead_id, leader_name, symbol, side, ts, qty, entry, price, partial, roi, "
            "exit_type, remarks, status, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ep["id"], ep["leader_id"], ep["leader_name"], ep["symbol"], ep["side"], now, qty, ep.get("last_entry"),
             mark, int(partial), roi, exit_type, "", "pending", created or now))

    def close_leader(self, leader_id: str, leader_name: str, now: float | None = None) -> None:
        """跟单关系结束：该跟单下仍未结束的回合以“跟单结束”收尾。"""
        now = now or self.clock()
        for key in [k for k, e in self.open.items() if e["leader_id"] == leader_id]:
            self._close(self.open[key], now, "跟单结束", leader_name)
        self.store.db.commit()

    # ------------------------------------------------------------ 提醒关联
    def note_alert(self, leader_id: str, symbol: str, rule_id: str, alert_id: int | None = None) -> list[int]:
        ids = []
        for ep in self.open.values():
            if ep["leader_id"] == leader_id and ep["symbol"] == symbol:
                ep["alerts"] += 1
                if rule_id.startswith("L") and rule_id[1:].isdigit():
                    ep["max_tier"] = max(ep["max_tier"] or 0, int(rule_id[1:]))
                self._save(ep)
                ids.append(ep["id"])
        self.store.db.commit()
        return ids


def episodes(store, status: str | None = None, leader_id: str | None = None, limit: int = 100,
             before_id: int | None = None) -> list[dict]:
    ensure_schema(store.db)
    sql, args = f"SELECT {', '.join(COLS)} FROM episodes WHERE 1=1", []
    if status:
        sql += " AND status=?"
        args.append(status)
    if leader_id:
        sql += " AND leader_id=?"
        args.append(leader_id)
    if before_id:
        sql += " AND id<?"
        args.append(before_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(min(int(limit), 500))
    out = []
    for row in store.db.execute(sql, args):
        ep = dict(zip(COLS, row))
        ep["labels"] = labels(ep) if ep["status"] == "closed" else []
        out.append(ep)
    return out


def get_episode(store, episode_id: int) -> dict | None:
    ensure_schema(store.db)
    row = store.db.execute(f"SELECT {', '.join(COLS)} FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not row:
        return None
    ep = dict(zip(COLS, row))
    ep["labels"] = labels(ep) if ep["status"] == "closed" else []
    return ep
