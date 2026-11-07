"""交易所账户管理：添加（先测试连接、强制只读）、改名、更新密钥、启用 / 停用、移除（默认保留数据）。

- 密钥只存 secrets.json（权限 600），数据库里只有打码后的提示；接口和日志都不返回明文。
- 只接受只读 key：有交易或提现权限的一律拒绝（系统永远不下单，用不到这些权限，泄露了也不会被拿去下单 / 提币）。
"""
from __future__ import annotations

import json
import re
import secrets as _secrets
import time

from . import leader_data
from .exchanges import EXCHANGES, ExchangeError, classify_stops, make_adapter
from .settings import mask

ID_RE = re.compile(r"^x[0-9a-f]{6}$")
KEY_FIELDS = {"binance": ("api_key", "secret"), "okx": ("api_key", "secret", "passphrase")}


class AccountError(ValueError):
    """给用户看的错误（中文）。"""


def _rows(store, sql, args=()):
    cur = store.db.execute(sql, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def get(store, account_id: str) -> dict | None:
    rows = _rows(store, "SELECT * FROM exchange_accounts WHERE id=?", (account_id,))
    return rows[0] if rows else None


def list_accounts(store, include_removed: bool = True) -> list[dict]:
    sql = "SELECT * FROM exchange_accounts" + ("" if include_removed else " WHERE removed=0") + " ORDER BY created"
    out = []
    for a in _rows(store, sql):
        a["perm"] = json.loads(a["perm"]) if a.get("perm") else None
        a["exchange_name"] = EXCHANGES.get(a["exchange"], a["exchange"])
        out.append(a)
    return out


def active(store) -> list[dict]:
    return [a for a in list_accounts(store, False) if a["enabled"]]


def label(a: dict) -> str:
    return f"{EXCHANGES.get(a['exchange'], a['exchange'])} · {a['name']}"


def clean_keys(exchange: str, raw: dict) -> dict:
    if exchange not in KEY_FIELDS:
        raise AccountError("只支持币安和 OKX")
    keys = {k: str(raw.get(k) or "").strip() for k in KEY_FIELDS[exchange]}
    missing = [k for k, v in keys.items() if not v]
    if missing:
        names = {"api_key": "API Key", "secret": "Secret Key", "passphrase": "Passphrase（创建 OKX API 时自己设置的密码）"}
        raise AccountError("请填写 " + "、".join(names[k] for k in missing))
    return keys


def test_connection(exchange: str, keys: dict, account_id: str = "x000000", adapter_factory=None) -> dict:
    """依次检查：签名 / 时间 → 权限（必须只读）→ 权益 → 持仓 → 止损单。失败抛 ExchangeError / AccountError。"""
    ad = (adapter_factory or make_adapter)(exchange, account_id, keys)
    perm = ad.permissions()
    if perm.can_withdraw:
        raise AccountError("这个 key 开了提现 / 划转权限，太危险，不能保存。请在交易所新建一个只勾选“读取”的 key")
    if perm.can_trade:
        raise AccountError("这个 key 开了交易权限。本系统只读、永远不下单，请新建一个只勾选“读取”的 key（泄露了也无法被拿去交易）")
    if not perm.can_read:
        raise AccountError("这个 key 没有读取权限：创建 key 时勾选“读取”")
    info = ad.account()
    positions = ad.positions()
    stops = ad.stop_orders()
    covered = classify_stops(positions, stops)
    return {"permissions": {"read_only": True, "ip_restricted": perm.ip_restricted, "detail": perm.detail},
            "equity": info.equity, "available": info.available, "margin_ratio": info.margin_ratio,
            "positions": len(positions), "stops": len(stops),
            "unprotected": sum(1 for k, v in covered.items() if not v),
            "symbols": sorted({f"{p.symbol}:{p.side}" for p in positions})}


def add(store, settings, exchange: str, name: str, raw_keys: dict, tester=test_connection) -> dict:
    name = (name or "").strip()[:30] or "主账户"
    keys = clean_keys(exchange, raw_keys)
    for a in list_accounts(store):
        if a["exchange"] == exchange and a["key_hint"] == mask(keys["api_key"]) and not a["removed"]:
            raise AccountError(f"这个 API Key 已经添加过了（{label(a)}）")
    result = tester(exchange, keys)
    account_id = "x" + _secrets.token_hex(3)
    while get(store, account_id):
        account_id = "x" + _secrets.token_hex(3)
    settings.set_exchange_keys(account_id, keys)
    now = time.time()
    store.db.execute(
        "INSERT INTO exchange_accounts (id, exchange, name, enabled, removed, stop_check, created, last_ok, perm, "
        "key_hint) VALUES (?,?,?,1,0,1,?,?,?,?)",
        (account_id, exchange, name, now, now, json.dumps(result["permissions"], ensure_ascii=False),
         mask(keys["api_key"])))
    store.db.commit()
    store.log_event("account.added", f"添加交易所账户：{EXCHANGES[exchange]} · {name}（只读 key 验证通过）",
                    leader_id=account_id)
    return {"id": account_id, **result}


def update(store, settings, account_id: str, *, name=None, enabled=None, stop_check=None, raw_keys=None,
           tester=test_connection) -> dict:
    a = get(store, account_id)
    if not a or a["removed"]:
        raise AccountError("没有这个账户")
    changes = []
    if raw_keys:
        keys = clean_keys(a["exchange"], raw_keys)
        result = tester(a["exchange"], keys, account_id)
        settings.set_exchange_keys(account_id, keys)
        store.db.execute("UPDATE exchange_accounts SET key_hint=?, perm=?, last_error=NULL, last_ok=? WHERE id=?",
                         (mask(keys["api_key"]), json.dumps(result["permissions"], ensure_ascii=False), time.time(),
                          account_id))
        changes.append("更新密钥")
    if name is not None and name.strip():
        store.db.execute("UPDATE exchange_accounts SET name=? WHERE id=?", (name.strip()[:30], account_id))
        changes.append(f"改名为 {name.strip()[:30]}")
    if enabled is not None:
        store.db.execute("UPDATE exchange_accounts SET enabled=? WHERE id=?", (int(bool(enabled)), account_id))
        changes.append("启用" if enabled else "停用")
    if stop_check is not None:
        store.db.execute("UPDATE exchange_accounts SET stop_check=? WHERE id=?", (int(bool(stop_check)), account_id))
        changes.append("开启止损单检查" if stop_check else "关闭止损单检查")
    store.db.commit()
    if changes:
        store.log_event("account.updated", f"{label(get(store, account_id))}：" + "、".join(changes), leader_id=account_id)
    return get(store, account_id)


def remove(store, settings, account_id: str, delete_data: bool, backup_dir: str, confirm: str = "") -> dict:
    """移除账户：删除密钥、停止监控。delete_data=False 时历史数据保留（统计照常包含）；
    True 时需要输入账户名确认，删除前自动备份。"""
    a = get(store, account_id)
    if not a:
        raise AccountError("没有这个账户")
    settings.set_exchange_keys(account_id, None)
    if delete_data:
        if confirm.strip() != a["name"]:
            raise AccountError("请输入账户名确认删除")
        path, counts = leader_data.delete(store, settings, account_id, backup_dir, name=label(a), require_leader=False)
        return {"deleted": True, "backup": path, "counts": counts}
    store.db.execute("UPDATE exchange_accounts SET removed=1, enabled=0 WHERE id=?", (account_id,))
    store.db.commit()
    store.log_event("account.removed", f"移除交易所账户：{label(a)}（密钥已删除，历史数据保留）", leader_id=account_id)
    return {"deleted": False}


def adapter(settings, a: dict):
    keys = settings.exchange_keys(a["id"])
    if not keys:
        raise ExchangeError("auth", "没有保存这个账户的密钥")
    return make_adapter(a["exchange"], a["id"], keys)
