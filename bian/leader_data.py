"""单个领航员的数据：导出（zip：每张表一个 CSV + 完整 JSON）和删除（删除前自动导出一份到备份目录）。

停止跟单后默认保留全部数据；只有“已停止跟单”或“观察中”的领航员可以删除（跟单中的删了下次同步又会重建）。
"""
from __future__ import annotations

import io
import json
import os
import time
import zipfile

from .csvutil import to_csv

# (表, 条件, 参数个数)：该领航员的全部数据。条件里的 ? 都填领航员 ID（LIKE 的会拼上通配符）。
TABLES: list[tuple[str, str, int]] = [
    ("leaders", "lead_id=?", 1),
    ("fills", "lead_id=?", 1),
    ("my_positions", "lead_id=?", 1),
    ("exits", "lead_id=?", 1),
    ("episodes", "leader_id=?", 1),
    ("episode_points", "episode_id IN (SELECT id FROM episodes WHERE leader_id=?)", 1),
    ("events", "leader_id=?", 1),
    ("alerts", "key LIKE ? OR key=? OR key=?", 3),
    ("equity", "leader_id=?", 1),
    ("snapshots", "leader_id=?", 1),
    ("state", "key LIKE ? OR key=?", 2),
    ("todos_done", "key LIKE ? OR key LIKE ? OR key=? OR key=? OR key LIKE ?", 5),
    ("leader_history", "lead_id=?", 1),
    ("leader_profiles", "lead_id=?", 1),
    ("jobs", "lead_id=?", 1),
    # 交易所账户（ID 是 x 开头，和数字的领航员 ID 不会冲突）
    ("account_ledger", "account_id=?", 1),
    ("exchange_accounts", "id=?", 1),
]


def _params(table: str, lid: str) -> tuple:
    return {
        "alerts": (f"{lid}:%", f"acct:{lid}", f"leader:{lid}"),
        "state": (f"{lid}:%", f"acct:{lid}"),
        "todos_done": (f"pos:{lid}:%", f"acct:dd:{lid}:%", f"acct:nostop:{lid}", f"leader:blocked:{lid}",
                       f"acct:principal:{lid}:%"),
    }.get(table, (lid,))


def _select(store, table: str, where: str, lid: str) -> tuple[list[str], list[tuple]]:
    cur = store.db.execute(f"SELECT * FROM {table} WHERE {where}", _params(table, lid))
    return [c[0] for c in cur.description], cur.fetchall()


def leader_row(store, lid: str) -> dict | None:
    cur = store.db.execute("SELECT * FROM leaders WHERE lead_id=?", (lid,))
    row = cur.fetchone()
    return dict(zip([c[0] for c in cur.description], row)) if row else None


def export_zip(store, settings, lid: str) -> bytes:
    buf = io.BytesIO()
    full: dict = {"lead_id": lid, "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "leader_rules": settings.get()["leader_rules"].get(lid)}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for table, where, _ in TABLES:
            cols, rows = _select(store, table, where, lid)
            full[table] = [dict(zip(cols, r)) for r in rows]
            if rows:
                z.writestr(f"{table}.csv", to_csv(cols, rows))
        z.writestr("all.json", json.dumps(full, ensure_ascii=False, indent=1, default=str))
        z.writestr("说明.txt", "bian 领航员数据导出：每张表一个 CSV（Excel 可直接打开），all.json 是完整数据。\n"
                              "时间字段：ts / *_ts 为秒，*_ms 为毫秒（Unix 时间戳）。\n")
    return buf.getvalue()


class DeleteRefused(ValueError):
    pass


def delete(store, settings, lid: str, backup_dir: str, name: str | None = None,
           require_leader: bool = True) -> tuple[str, dict]:
    """删除前先导出到 backup_dir（权限 600），再在一个事务里删掉该领航员（或交易所账户）的全部数据。
    返回 (备份路径, 各表删除条数)。交易所账户由 accounts.remove 调用（require_leader=False）。"""
    row = leader_row(store, lid)
    if require_leader:
        if not row:
            raise DeleteRefused("没有这个领航员的数据")
        if row["kind"] == "copy":
            raise DeleteRefused("正在跟单的领航员不能删除（下次同步会重新生成）。请先在币安停止跟单")
    name = name or (row and row["name"]) or lid
    os.makedirs(backup_dir, exist_ok=True)
    prefix = "leader" if require_leader else "account"
    path = os.path.join(backup_dir, f"{prefix}-{lid}-{time.strftime('%Y%m%d-%H%M%S')}.zip")
    data = export_zip(store, settings, lid)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    counts = {}
    with store.db:  # 一个事务：要么全删，要么都不删
        for table, where, _ in reversed(TABLES):  # 先删 episode_points 再删 episodes
            cur = store.db.execute(f"DELETE FROM {table} WHERE {where}", _params(table, lid))
            if cur.rowcount:
                counts[table] = cur.rowcount
    rules = dict(settings.get()["leader_rules"])
    if rules.pop(lid, None) is not None:
        settings.update({"leader_rules": rules})
    store.log_event("leader.deleted", f"已删除{'领航员' if require_leader else '交易所账户'} {name} 的全部数据"
                    f"（删除前已备份到 {os.path.basename(path)}）",
                    deleted=counts)  # 这条事件不带 leader_id，不会被一起删
    return path, counts
