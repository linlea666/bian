"""SQLite 存储：提醒审计、持仓快照、规则状态（重启不重复轰炸）。"""
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
"""


class Store:
    def __init__(self, path: str = "bian.db"):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self._last_digest: dict[str, str] = {}

    def add_alert(self, a, message: str, delivered: bool, error: str) -> None:
        self.db.execute(
            "INSERT INTO alerts (ts, rule_id, severity, key, version, title, detail, suggestion, message, delivered, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (a.ts or time.time(), a.rule_id, a.severity, a.key, a.version, a.title, a.detail,
             a.suggestion, message, int(delivered), error),
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
