"""SQLite 存储：提醒审计、持仓快照、规则状态、事件日志、仓位回合、权益快照、运行状态、登录会话。

每个线程使用自己的 Store 实例（独立连接）；数据库开启 WAL，监控线程和网页线程可以同时读写。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, rule_id TEXT, severity TEXT, key TEXT, version INTEGER,
  title TEXT, detail TEXT, suggestion TEXT, message TEXT,
  delivered INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, leader_id TEXT, source TEXT, n_positions INTEGER, digest TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS state (
  key TEXT PRIMARY KEY, payload TEXT, updated REAL
);
-- 事件日志：只追加，用于复盘与排查
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, type TEXT, level TEXT, leader_id TEXT, episode_id INTEGER, ref TEXT,
  title TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_episode ON events(episode_id);
-- 仓位回合：我的一个仓位从出现到消失
CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pos_key TEXT, leader_id TEXT, leader_name TEXT, symbol TEXT, side TEXT, source TEXT,
  status TEXT, opened_ts REAL, closed_ts REAL, first_entry REAL, last_entry REAL,
  first_qty REAL, max_qty REAL, last_qty REAL, leverage REAL, max_margin_u REAL,
  mae_roi REAL, mae_u REAL, mae_ts REAL, mfe_roi REAL, mfe_u REAL, mfe_ts REAL,
  last_roi REAL, last_u REAL, last_mark REAL, adds INTEGER, reduces INTEGER, reduces_in_profit INTEGER,
  max_tier INTEGER, alerts INTEGER, exit_type TEXT, note TEXT
);
CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);
-- 跟单权益快照（约 5 分钟一个点）
CREATE TABLE IF NOT EXISTS equity (
  ts REAL, leader_id TEXT, equity REAL, invested REAL, unrealized REAL, realized REAL
);
CREATE INDEX IF NOT EXISTS idx_equity ON equity(leader_id, ts);
-- 运行状态（最新持仓、跟单账户、轮询心跳），供网页读取
CREATE TABLE IF NOT EXISTS runtime (
  key TEXT PRIMARY KEY, value TEXT, updated REAL
);
CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY, pw_hash TEXT, created REAL, must_change INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, username TEXT, csrf TEXT, created REAL, expires REAL
);
-- 用户在“今日待办”里点了“已处理”的条目（key 变化后会重新出现）
CREATE TABLE IF NOT EXISTS todos_done (
  key TEXT PRIMARY KEY, ts REAL
);
"""


class Store:
    def __init__(self, path: str = "bian.db"):
        self.path = path
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.db.execute("PRAGMA busy_timeout=10000")
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._last_digest: dict[str, str] = {}

    def add_alert(self, a, message: str, delivered: bool, error: str) -> int:
        cur = self.db.execute(
            "INSERT INTO alerts (ts, rule_id, severity, key, version, title, detail, suggestion, message, delivered, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (a.ts or time.time(), a.rule_id, a.severity, a.key, a.version, a.title, a.detail,
             a.suggestion, message, int(delivered), error),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def update_delivery(self, row_id: int, delivered: bool, error: str) -> None:
        """回写投递结果；一旦送达（任一渠道）就保持已送达。"""
        self.db.execute(
            "UPDATE alerts SET delivered = MAX(delivered, ?), error = ? WHERE id = ?",
            (int(delivered), error, row_id),
        )
        self.db.commit()

    def add_snapshot(self, leader_id: str, source: str, positions: list[dict]) -> None:
        payload = json.dumps(positions, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha1(
            json.dumps([(p["symbol"], p["side"], p["qty"], p["entry_price"]) for p in positions]).encode()
        ).hexdigest()
        if self._last_digest.get(leader_id) == digest:
            return  # 只在仓位结构变化时落快照，避免数据库膨胀
        self._last_digest[leader_id] = digest
        self.db.execute(
            "INSERT INTO snapshots (ts, leader_id, source, n_positions, digest, payload) VALUES (?,?,?,?,?,?)",
            (time.time(), leader_id, source, len(positions), digest, payload),
        )
        self.db.commit()

    def save_state(self, key: str, payload: dict) -> None:
        self.db.execute(
            "INSERT INTO state (key, payload, updated) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, updated=excluded.updated",
            (key, json.dumps(payload, ensure_ascii=False), time.time()),
        )
        self.db.commit()

    def delete_state(self, key: str) -> None:
        self.db.execute("DELETE FROM state WHERE key=?", (key,))
        self.db.commit()

    def load_states(self) -> dict[str, dict]:
        return {k: json.loads(v) for k, v in self.db.execute("SELECT key, payload FROM state")}

    def recent_alerts(self, limit: int = 50) -> list[tuple]:
        return list(self.db.execute(
            "SELECT ts, severity, rule_id, key, title, delivered, error FROM alerts ORDER BY id DESC LIMIT ?",
            (limit,),
        ))

    # ------------------------------------------------------------ 事件日志
    def log_event(self, type_: str, title: str, level: str = "info", leader_id: str | None = None,
                  episode_id: int | None = None, ref: str | None = None, ts: float | None = None,
                  **detail) -> int:
        cur = self.db.execute(
            "INSERT INTO events (ts, type, level, leader_id, episode_id, ref, title, detail) VALUES (?,?,?,?,?,?,?,?)",
            (ts or time.time(), type_, level, leader_id, episode_id, ref, title,
             json.dumps(detail, ensure_ascii=False, default=str) if detail else ""),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def events(self, limit: int = 100, before_id: int | None = None, type_prefix: str | None = None,
               leader_id: str | None = None, episode_id: int | None = None, level: str | None = None,
               q: str | None = None) -> list[dict]:
        sql, args = "SELECT id, ts, type, level, leader_id, episode_id, ref, title, detail FROM events WHERE 1=1", []
        if before_id:
            sql += " AND id < ?"
            args.append(before_id)
        if type_prefix:
            sql += " AND type LIKE ?"
            args.append(type_prefix + "%")
        if leader_id:
            sql += " AND leader_id = ?"
            args.append(leader_id)
        if episode_id:
            sql += " AND episode_id = ?"
            args.append(episode_id)
        if level:
            sql += " AND level = ?"
            args.append(level)
        if q:
            sql += " AND (title LIKE ? OR detail LIKE ?)"
            args += [f"%{q}%", f"%{q}%"]
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(min(int(limit), 1000))
        cols = ("id", "ts", "type", "level", "leader_id", "episode_id", "ref", "title", "detail")
        out = []
        for row in self.db.execute(sql, args):
            d = dict(zip(cols, row))
            d["detail"] = json.loads(d["detail"]) if d["detail"] else {}
            out.append(d)
        return out

    # ------------------------------------------------------------ 运行状态
    def set_runtime(self, key: str, value) -> None:
        self.db.execute(
            "INSERT INTO runtime (key, value, updated) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (key, json.dumps(value, ensure_ascii=False, default=str), time.time()),
        )
        self.db.commit()

    def get_runtime(self, key: str, default=None):
        row = self.db.execute("SELECT value, updated FROM runtime WHERE key=?", (key,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else (default, 0.0)

    # ------------------------------------------------------------ 权益快照
    def add_equity(self, ts: float, leader_id: str, equity: float, invested: float,
                   unrealized: float, realized: float) -> None:
        self.db.execute("INSERT INTO equity VALUES (?,?,?,?,?,?)",
                        (ts, leader_id, equity, invested, unrealized, realized))
        self.db.commit()

    def equity_series(self, since: float, leader_id: str | None = None) -> list[tuple]:
        if leader_id:
            return list(self.db.execute(
                "SELECT ts, leader_id, equity, invested FROM equity WHERE ts >= ? AND leader_id = ? ORDER BY ts",
                (since, leader_id)))
        return list(self.db.execute(
            "SELECT ts, leader_id, equity, invested FROM equity WHERE ts >= ? ORDER BY ts", (since,)))

    def equity_at_or_before(self, ts: float) -> dict[str, float]:
        """每个跟单在 ts 之前最后一个权益点（用于计算今日/本月盈亏的起点）。"""
        rows = self.db.execute(
            "SELECT leader_id, equity, invested FROM equity e WHERE ts = "
            "(SELECT MAX(ts) FROM equity WHERE leader_id = e.leader_id AND ts <= ?)", (ts,))
        return {r[0]: (r[1], r[2]) for r in rows}

    def equity_first_after(self, ts: float) -> dict[str, tuple]:
        rows = self.db.execute(
            "SELECT leader_id, equity, invested FROM equity e WHERE ts = "
            "(SELECT MIN(ts) FROM equity WHERE leader_id = e.leader_id AND ts >= ?)", (ts,))
        return {r[0]: (r[1], r[2]) for r in rows}

    # ------------------------------------------------------------ 待办
    def todos_done(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT key FROM todos_done")}

    def mark_todo_done(self, key: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO todos_done (key, ts) VALUES (?, ?)", (key, time.time()))
        self.db.commit()

    def alerts_page(self, limit: int = 50, before_id: int | None = None, severity: str | None = None) -> list[dict]:
        sql, args = ("SELECT id, ts, rule_id, severity, key, title, detail, suggestion, message, delivered, error "
                     "FROM alerts WHERE 1=1"), []
        if before_id:
            sql += " AND id < ?"
            args.append(before_id)
        if severity:
            sql += " AND severity = ?"
            args.append(severity)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(min(int(limit), 500))
        cols = ("id", "ts", "rule_id", "severity", "key", "title", "detail", "suggestion", "message", "delivered", "error")
        return [dict(zip(cols, r)) for r in self.db.execute(sql, args)]

    def count_alerts_since(self, ts: float) -> dict[str, int]:
        return {r[0]: r[1] for r in self.db.execute(
            "SELECT severity, COUNT(*) FROM alerts WHERE ts >= ? GROUP BY severity", (ts,))}


class LocalStore:
    """每个线程自动使用自己的 Store 连接（网页请求线程、凭证守护共用这个对象）。"""

    def __init__(self, path: str):
        import threading

        self.path = path
        self._local = threading.local()

    def _get(self) -> Store:
        s = getattr(self._local, "store", None)
        if s is None:
            s = self._local.store = Store(self.path)
        return s

    def __getattr__(self, name):
        return getattr(self._get(), name)
