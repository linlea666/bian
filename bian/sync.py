"""历史数据同步（后台线程调用）：我的逐笔成交、我的历史仓位、领航员公开已平仓历史。

- 首次运行回补全部历史（跟单开始以来），之后增量：翻页时遇到整页都已同步过就停止。
- 只读；私有接口在凭证失效时由调用方跳过。
- 时间单位与币安接口一致：毫秒。
"""
from __future__ import annotations

import time

from .api import copy_account, leaders as leaders_api
from .client import Client
from .models import fnum


def fill_uid(lead_id: str, x: dict) -> str:
    # 成交记录没有成交 ID：用时间 + 币种 + 方向 + 数量 + 价格 + 金额 + 手续费 组合去重
    return ":".join(str(x.get(k, "")) for k in ("time", "symbol", "side", "positionSide", "qty", "price", "quantity", "fee")) \
        + f"@{lead_id}"


def upsert_fills(store, lead_id: str, rows: list[dict]) -> int:
    new = 0
    for x in rows:
        cur = store.db.execute(
            "INSERT OR IGNORE INTO fills (uid, lead_id, ts_ms, symbol, side, position_side, price, qty, quote_qty, fee, realized) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (fill_uid(lead_id, x), lead_id, int(fnum(x.get("time"))), x.get("symbol"), x.get("side"),
             x.get("positionSide"), fnum(x.get("price")), fnum(x.get("qty")), fnum(x.get("quantity")),
             fnum(x.get("fee")), fnum(x.get("realizedProfit"))))
        new += cur.rowcount
    store.db.commit()
    return new


def _pos_row(x: dict) -> tuple:
    closed = x.get("closed")
    return (str(x.get("symbol")), str(x.get("side") or "").upper(), int(fnum(x.get("opened"))),
            int(fnum(closed)) if closed else None, fnum(x.get("avgCost")), fnum(x.get("avgClosePrice")),
            fnum(x.get("maxOpenInterest")), fnum(x.get("closedVolume")), fnum(x.get("leverage"), 1.0),
            fnum(x.get("closingPnl")), fnum(x.get("roi")), str(x.get("status") or ""),
            int(fnum(x.get("updateTime"))) or None)


def upsert_my_positions(store, lead_id: str, rows: list[dict]) -> int:
    """返回新增或有变化（例如部分平仓继续推进）的条数。"""
    changed = 0
    now = time.time()
    for x in rows:
        pid = str(x.get("positionId") or x.get("id"))
        uid = f"{lead_id}:{pid}"
        vals = _pos_row(x)
        old = store.db.execute("SELECT closed_qty, status, closed_ms FROM my_positions WHERE uid=?", (uid,)).fetchone()
        if old and (old[0], old[1], old[2]) == (vals[7], vals[11], vals[3]):
            continue
        store.db.execute(
            "INSERT OR REPLACE INTO my_positions (uid, lead_id, position_id, symbol, side, opened_ms, closed_ms, avg_cost, "
            "avg_close, max_qty, closed_qty, leverage, pnl, roi, status, update_ms, updated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, lead_id, pid, *vals, now))
        changed += 1
    store.db.commit()
    return changed


def upsert_leader_history(store, lead_id: str, rows: list[dict]) -> int:
    """写入领航员历史；保留已经算好的 MAE/MFE（仓位继续部分平仓、数据变化时清空重算）。"""
    changed = 0
    now = time.time()
    for x in rows:
        pid = str(x.get("positionId") or x.get("id"))
        vals = _pos_row(x)
        old = store.db.execute("SELECT closed_qty, status, closed_ms FROM leader_history WHERE lead_id=? AND position_id=?",
                               (lead_id, pid)).fetchone()
        if old and (old[0], old[1], old[2]) == (vals[7], vals[11], vals[3]):
            continue
        store.db.execute(
            "INSERT OR REPLACE INTO leader_history (lead_id, position_id, symbol, side, opened_ms, closed_ms, avg_cost, avg_close, "
            "max_qty, closed_qty, leverage, pnl, roi, status, update_ms, mae, mfe, path_interval, updated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?)", (lead_id, pid, *vals, now))
        changed += 1
    store.db.commit()
    return changed


def ensure_leader(store, lead_id: str, name: str, kind: str) -> bool:
    """登记关注的领航员；返回是否新加入（新加入的会触发历史分析任务）。
    kind：copy 跟单中 / watch 观察。跟单中的不会被降为观察；已停止跟单（ended）的：重新跟单 → copy，
    手动分析 / 转为观察 → watch。"""
    row = store.db.execute("SELECT kind FROM leaders WHERE lead_id=?", (lead_id,)).fetchone()
    now = time.time()
    if row:
        new_kind = "copy" if kind == "copy" else ("watch" if row[0] == "ended" else row[0])
        store.db.execute("UPDATE leaders SET name=?, kind=?, ended_ts=CASE WHEN ?='ended' THEN ended_ts ELSE NULL END, "
                         "updated=? WHERE lead_id=?", (name, new_kind, new_kind, now, lead_id))
        store.db.commit()
        return False
    store.db.execute("INSERT INTO leaders (lead_id, name, kind, added, history_synced, updated) VALUES (?,?,?,?,NULL,?)",
                     (lead_id, name, kind, now, now))
    store.db.commit()
    return True


def mark_copy_set(store, live_ids, now: float | None = None) -> list[str]:
    """按最新的跟单列表更新状态：不在列表里的“跟单中”标为“已停止跟单”（包括服务停机期间停止的），
    重新出现的恢复为“跟单中”。只改已登记的领航员；新出现的由同步线程登记（并排队分析）。返回新标记为停止的 ID。"""
    now = now or time.time()
    live = {str(x) for x in live_ids}
    ended = [lid for (lid,) in store.db.execute("SELECT lead_id FROM leaders WHERE kind='copy'") if lid not in live]
    for lid in ended:
        store.db.execute("UPDATE leaders SET kind='ended', ended_ts=?, updated=? WHERE lead_id=?", (now, now, lid))
    for lid in live:
        store.db.execute("UPDATE leaders SET kind='copy', ended_ts=NULL, updated=? WHERE lead_id=? AND kind<>'copy'",
                         (now, lid))
    store.db.commit()
    return ended


def sync_copy_relation(client: Client, store, rel: dict, max_pages: int = 40, full: bool = False) -> dict:
    """同步一个跟单关系的逐笔成交和历史仓位。默认增量（遇到整页已同步就停）；
    full=True 时历史仓位翻完所有页（部分平仓的仓位会继续变化，可能不在第一页），每天做一次兜底。"""
    lead, cpid = str(rel.get("leadPortfolioId")), str(rel.get("copyPortfolioId") or "")
    out = {"fills": 0, "positions": 0}
    if not cpid:
        return out
    for rows in copy_account.iter_pages(client, "/copy-portfolio/trade-history", cpid, 50, max_pages):
        n = upsert_fills(store, lead, rows)
        out["fills"] += n
        if rows and n == 0:
            break  # 整页都同步过：之后更早的也都有了
    for rows in copy_account.iter_pages(client, "/copy-portfolio/position-history", cpid, 50, max_pages):
        n = upsert_my_positions(store, lead, rows)
        out["positions"] += n
        if rows and n == 0 and not full:
            break
    return out


def sync_leader_history(client: Client, store, lead_id: str, max_pages: int = 30, full: bool = False) -> int:
    total = 0
    for rows in leaders_api.iter_position_history_pages(client, lead_id, 100, max_pages):
        n = upsert_leader_history(store, lead_id, rows)
        total += n
        if rows and n == 0 and not full:
            break
    now = time.time()  # 走到这里说明全部页都成功拿到了
    store.db.execute("UPDATE leaders SET history_synced=?, history_ok=? WHERE lead_id=?", (now, now, lead_id))
    store.db.commit()
    return total
