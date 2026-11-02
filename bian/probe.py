"""接口探测：验证每个接口是否可用、返回哪些字段。输出不含凭证与个人 ID。"""
from __future__ import annotations

import time

from .api import copy_account, leaders as leaders_api
from .api.market import klines, mark_prices
from .client import AuthExpired, BinanceError, Client


def _check(name: str, fn):
    t0 = time.monotonic()
    try:
        res = fn()
        return {"name": name, "ok": True, "ms": int((time.monotonic() - t0) * 1000), "result": res, "error": ""}
    except AuthExpired as e:
        return {"name": name, "ok": False, "ms": 0, "result": None, "error": f"凭证无效/缺失：{e}"}
    except BinanceError as e:
        return {"name": name, "ok": False, "ms": 0, "result": None, "error": str(e)}


def _keys(obj) -> str:
    if isinstance(obj, list):
        obj = obj[0] if obj else {}
    if hasattr(obj, "__dataclass_fields__"):
        return ", ".join(obj.__dataclass_fields__)
    if isinstance(obj, dict):
        return ", ".join(sorted(obj.keys()))
    return ""


def run_probe(client: Client, pid: str | None = None) -> list[str]:
    lines = ["| 接口 | 结果 | 耗时 | 说明 |", "|---|---|---|---|"]

    def row(r, note):
        lines.append(f"| {r['name']} | {'✅' if r['ok'] else '❌'} | {r['ms']}ms | {note if r['ok'] else r['error']} |")

    r = _check("榜单 home-page/query-list", lambda: leaders_api.leaderboard(client, page_size=50, sort_by="AUM"))
    row(r, f"总数 {r['result'][0]}" if r["ok"] else "")
    if not pid and r["ok"]:
        for ld in r["result"][1]:
            try:
                if leaders_api.detail(client, ld.portfolio_id).get("positionShow"):
                    pid = ld.portfolio_id
                    break
            except BinanceError:
                continue
    if not pid:
        lines.append("\n没有可用的领航员 ID，后续检查跳过。")
        return lines

    r = _check("领航员详情 lead-portfolio/detail", lambda: leaders_api.detail(client, pid))
    row(r, f"positionShow={r['result'].get('positionShow')}" if r["ok"] else "")
    r = _check("公开持仓 lead-data/positions", lambda: leaders_api.public_positions(client, pid))
    row(r, f"非零持仓 {len(r['result'])} 个；字段：{_keys(r['result'])}" if r["ok"] else "")
    r = _check("已平仓历史 position-history", lambda: leaders_api.position_history(client, pid, max_pages=1))
    row(r, f"首页 {len(r['result'])} 笔；字段：{_keys(r['result'])}" if r["ok"] else "")
    r = _check("标记价 fapi/v1/premiumIndex", lambda: mark_prices(client))
    row(r, f"{len(r['result'])} 个合约" if r["ok"] else "")
    now = int(time.time() * 1000)
    r = _check("K线 fapi/v1/klines", lambda: klines(client, "BTCUSDT", "1m", now - 3_600_000, now))
    row(r, f"{len(r['result'])} 根 1m K 线" if r["ok"] else "")

    lines.append("")
    if not client.has_credentials:
        lines.append("未设置 BINANCE_P20T / BINANCE_CSRFTOKEN：私有接口未测试。")
        return lines

    lines += ["### 私有接口（需登录凭证）", "", "| 接口 | 结果 | 耗时 | 说明 |", "|---|---|---|---|"]
    r = _check("凭证校验 get-user-base-info", lambda: copy_account.check_auth(client))
    row(r, f"有效={r['result']}" if r["ok"] else "")
    r = _check("我的跟单关系 copy-portfolio/detail-list", lambda: copy_account.copy_relations(client))
    row(r, f"{len(r['result'])} 个进行中的跟单；字段：{_keys(r['result'])}" if r["ok"] else "")
    rels = r["result"] or []
    r2 = _check("详情带凭证（copyPortfolioId）", lambda: leaders_api.detail(client, pid, private=True))
    row(r2, f"hasCopy={r2['result'].get('hasCopy')}" if r2["ok"] else "")

    for rel in rels[:5]:
        lead, cpid, nick = str(rel.get("leadPortfolioId")), str(rel.get("copyPortfolioId")), rel.get("nickname")
        rp = _check(f"我的跟单仓位 user-position（{nick}）", lambda: copy_account.copy_positions(client, cpid, lead))
        note = ""
        if rp["ok"]:
            mine = rp["result"]
            note = f"{len(mine)} 个仓位；字段含强平价={any(p.liquidation_price > 0 for p in mine)}"
            try:
                pub = {p.key: p for p in leaders_api.public_positions(client, lead)}
                ratios = [f"{p.symbol} {p.qty / pub[p.key].qty:.4g}" for p in mine if p.key in pub and pub[p.key].qty]
                if ratios:
                    note += "；我的/领航员数量比：" + "，".join(ratios[:5])
                elif pub:
                    note += "；与公开持仓无交集"
            except BinanceError:
                pass
        row(rp, note)
        rh = _check(f"我的跟单成交 trade-history（{nick}）", lambda: copy_account.copy_trade_history(client, cpid))
        row(rh, f"字段：{_keys((rh['result'] or {}).get('list') or [])}" if rh["ok"] else "")
    return lines
