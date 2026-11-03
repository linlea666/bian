"""领航员分析任务（后台、可断点续传、限速）。

一个任务 = 一个领航员的“历史 → 逐笔 K 线算 MAE/MFE → 画像 + 回测”。
- 进度存在 jobs 表；每笔仓位的 MAE/MFE 写回 leader_history，重启后已算过的不再请求 K 线。
- K 线请求走单独的客户端，每 2 秒最多 1 个（每分钟 30 个），不和监控抢配额。
- 每次 step() 最多运行 budget_s 秒就返回，后台线程可以穿插做别的事。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .analysis import KlineCache
from .api.market import INTERVAL_MS, pick_interval
from .api import leaders as leaders_api
from .backtest import Costs, Summary, build_path, grid_search, ladder_search
from .client import BinanceError, Client
from .models import ClosedPosition
from .profile import profile
from .sync import sync_leader_history

KLINE_INTERVAL_S = 2.0
RETRY_AFTER_S = 600  # 临时失败（网络 / 限流）后等 10 分钟再试，不反复请求


@dataclass
class StoredPath:
    """只用已存的 MAE/MFE 做画像（不需要重新拉 K 线）。"""

    cp: ClosedPosition
    mae: float
    mfe: float

    @property
    def final_roi(self) -> float:
        return self.cp.price_roi


def _closed_positions(store, lead_id: str) -> list[tuple[ClosedPosition, dict]]:
    cur = store.db.execute(
        "SELECT * FROM leader_history WHERE lead_id=? AND status='All Closed' AND closed_ms IS NOT NULL "
        "ORDER BY opened_ms", (lead_id,))
    cols = [c[0] for c in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        cp = ClosedPosition(lead_id, d["position_id"], d["symbol"], d["side"], d["opened_ms"], d["closed_ms"],
                            d["avg_cost"] or 0.0, d["avg_close"] or 0.0, d["max_qty"] or 0.0, d["closed_qty"] or 0.0,
                            d["leverage"] or 1.0, d["pnl"] or 0.0, d["roi"] or 0.0)
        out.append((cp, d))
    return out


def enqueue(store, lead_id: str, reason: str = "") -> bool:
    """排队一个分析任务；同一领航员已有未完成任务时不重复排队。"""
    if store.db.execute("SELECT 1 FROM jobs WHERE lead_id=? AND state IN ('queued','running')", (lead_id,)).fetchone():
        return False
    now = time.time()
    store.db.execute("INSERT INTO jobs (lead_id, kind, state, progress, total, message, created, updated) "
                     "VALUES (?, 'profile', 'queued', 0, 0, ?, ?, ?)", (lead_id, reason, now, now))
    store.db.commit()
    store.log_event("job.queued", f"排队分析领航员历史（{reason or '手动'}）", leader_id=lead_id)
    return True


def _update(store, job_id: int, **kw) -> None:
    kw["updated"] = time.time()
    store.db.execute(f"UPDATE jobs SET {', '.join(k + '=?' for k in kw)} WHERE id=?", [*kw.values(), job_id])
    store.db.commit()


def current_job(store, lead_id: str) -> dict | None:
    cur = store.db.execute("SELECT * FROM jobs WHERE lead_id=? ORDER BY id DESC LIMIT 1", (lead_id,))
    row = cur.fetchone()
    return dict(zip([c[0] for c in cur.description], row)) if row else None


def step(store, public: Client, kline_client: Client, budget_s: float = 20.0, cache_dir: str = ".cache/klines",
         clock=time.monotonic) -> bool:
    """推进最早的一个任务。返回是否还有任务待处理。"""
    cur = store.db.execute(
        "SELECT id, lead_id, state FROM jobs WHERE state IN ('queued','running') "
        "AND NOT (message LIKE '暂时失败%' AND updated > ?) ORDER BY id LIMIT 1", (time.time() - RETRY_AFTER_S,))
    row = cur.fetchone()
    if not row:
        return False
    job_id, lead_id, state = row
    t0 = clock()
    try:
        if state == "queued":
            _update(store, job_id, state="running", message="同步历史")
            full = store.db.execute("SELECT COUNT(*) FROM leader_history WHERE lead_id=?", (lead_id,)).fetchone()[0] == 0
            sync_leader_history(public, store, lead_id, full=full)
        todo = [(cp, d) for cp, d in _closed_positions(store, lead_id) if d["path_interval"] is None]
        total = store.db.execute(
            "SELECT COUNT(*) FROM leader_history WHERE lead_id=? AND status='All Closed' AND closed_ms IS NOT NULL",
            (lead_id,)).fetchone()[0]
        cache = KlineCache(kline_client, cache_dir)
        processed = 0
        for cp, d in todo:
            if clock() - t0 > budget_s:
                _update(store, job_id, progress=total - len(todo) + processed, total=total, message="逐笔计算浮亏 / 浮盈")
                return True
            processed += 1
            if cp.avg_cost <= 0 or cp.closed <= cp.opened:
                _set_path(store, lead_id, cp.position_id, None, None, "skip")
                continue
            iv = pick_interval(cp.opened, cp.closed)
            try:
                bars = cache.get(cp.symbol, iv, cp.opened, cp.closed)
            except BinanceError:
                _set_path(store, lead_id, cp.position_id, None, None, "error")  # 下架币种等：跳过
                continue
            path = build_path(cp, bars, INTERVAL_MS[iv], iv)
            _set_path(store, lead_id, cp.position_id, path.mae, path.mfe, iv)
        _update(store, job_id, progress=total, total=total, message="计算画像和回测")
        compute_profile(store, lead_id, kline_client, cache_dir, mdd=leader_mdd(public, lead_id))
        _update(store, job_id, state="done", message="完成")
        store.log_event("job.done", f"领航员历史分析完成（{total} 笔）", leader_id=lead_id)
    except BinanceError as e:
        if e.code == "11012030":  # 领航员已关闭带单（或 ID 不对）：不再重试
            _update(store, job_id, state="error", message="领航员已关闭带单，或 ID 不正确")
            store.log_event("job.error", "领航员已关闭带单或 ID 不正确，停止分析", "warn", leader_id=lead_id)
            return True
        _update(store, job_id, message=f"暂时失败，10 分钟后重试：{e}")
        return True
    except Exception as e:  # noqa: BLE001
        _update(store, job_id, state="error", message=f"{type(e).__name__}: {e}")
        store.log_event("job.error", f"领航员历史分析出错：{type(e).__name__}", "warn", leader_id=lead_id, error=str(e))
    return True


def _set_path(store, lead_id: str, pid: str, mae, mfe, interval: str) -> None:
    store.db.execute("UPDATE leader_history SET mae=?, mfe=?, path_interval=? WHERE lead_id=? AND position_id=?",
                     (mae, mfe, interval, lead_id, pid))
    store.db.commit()


def _summary_json(s: Summary | None) -> dict | None:
    if s is None:
        return None
    return {"label": s.params.label(), "n": s.n, "total": s.total, "win_rate": s.win_rate,
            "profit_factor": None if s.profit_factor == float("inf") else s.profit_factor, "worst": s.worst,
            "max_dd": s.max_dd, "rule_exits": s.rule_exits, "winners_cut": s.winners_cut,
            "profit_given_up": s.profit_given_up, "loss_saved": s.loss_saved}


def leader_mdd(public: Client, lead_id: str) -> float | None:
    """领航员近 90 天最大回撤（%）：详情接口没有，按昵称在榜单里查（只读、2 个请求）。查不到返回 None。"""
    try:
        name = leaders_api.detail(public, lead_id).get("nickname")
        if not name:
            return None
        _, found = leaders_api.leaderboard(public, time_range="90D", nickname=name, page_size=20)
        hit = next((x for x in found if x.portfolio_id == lead_id), None)
        return hit.mdd if hit and hit.mdd > 0 else None
    except Exception:  # noqa: BLE001  只是补充信息：查不到不能让整个分析任务失败
        return None


def compute_profile(store, lead_id: str, kline_client: Client, cache_dir: str = ".cache/klines",
                    mdd: float | None = None) -> dict:
    """画像用已存的 MAE/MFE；回测需要逐根 K 线路径，从本地缓存读取（缓存被清掉的跳过）。
    mdd：领航员近 90 天最大回撤（%），用于推荐跟单总止损比例。"""
    rows = _closed_positions(store, lead_id)
    closed = [cp for cp, _ in rows]
    stored = [StoredPath(cp, d["mae"], d["mfe"]) for cp, d in rows if d["mae"] is not None]
    prof = profile(closed, stored or None)
    cache = KlineCache(kline_client, cache_dir)
    paths = []
    for cp, d in rows:
        iv = d["path_interval"]
        if iv not in INTERVAL_MS:
            continue
        try:
            bars = cache.get(cp.symbol, iv, cp.opened, cp.closed)
        except BinanceError:
            continue
        paths.append(build_path(cp, bars, INTERVAL_MS[iv], iv))
    bt = None
    if len(paths) >= 10:
        rep = grid_search(paths, Costs())
        bt = {"baseline": _summary_json(rep.baseline), "stop_only": [_summary_json(x) for x in rep.stop_only],
              "ranked": [_summary_json(x) for x in rep.ranked[:10]],
              "oos": {"train_best": _summary_json(rep.train_best), "test_baseline": _summary_json(rep.test_baseline),
                      "test_best": _summary_json(rep.test_best), "n_train": rep.n_train, "n_test": rep.n_test}}
    ladder = ladder_search(paths, prof.get("winner_mfe_p50") or 0.0, prof.get("winner_mfe_p75") or 0.0) \
        if prof.get("path_n") else None
    data = {"profile": prof, "backtest": bt, "ladder": ladder, "mdd_90d": mdd,
            "paths_n": len(paths), "stored_n": len(stored)}
    store.db.execute("INSERT OR REPLACE INTO leader_profiles (lead_id, computed_at, n, data) VALUES (?,?,?,?)",
                     (lead_id, time.time(), len(closed), json.dumps(data, ensure_ascii=False, default=str)))
    store.db.commit()
    return data


def get_profile(store, lead_id: str) -> dict | None:
    row = store.db.execute("SELECT computed_at, n, data FROM leader_profiles WHERE lead_id=?", (lead_id,)).fetchone()
    if not row:
        return None
    return {"computed_at": row[0], "n": row[1], **json.loads(row[2])}


def needs_refresh(store, lead_id: str) -> bool:
    """有新的已平仓记录（比画像多出 5 笔以上，或画像超过 3 天）时需要重新分析；
    旧版本的分析结果（还没有止盈阶梯回测 / 最大回撤）也重新分析一次（K 线有缓存，很快）。"""
    p = store.db.execute("SELECT computed_at, n, data FROM leader_profiles WHERE lead_id=?", (lead_id,)).fetchone()
    n = store.db.execute("SELECT COUNT(*) FROM leader_history WHERE lead_id=? AND status='All Closed'",
                         (lead_id,)).fetchone()[0]
    if not p:
        return True
    if is_outdated(json.loads(p[2] or "{}")):
        return True
    return n - (p[1] or 0) >= 5 or (time.time() - p[0] > 3 * 86400 and n != p[1])


def is_outdated(data: dict) -> bool:
    return "ladder" not in data or "mdd_90d" not in data
