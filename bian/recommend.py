"""按领航员给出止损档位 / 跟单总止损 / 分批止盈阶梯的推荐（第三期）。

依据（页面上逐条展示，推荐只是参考，点“采用”才写入该领航员的单独设置，绝不自动生效）：
1. 亏损档位：他历史上“最后赚钱”的单子，持仓过程中最深浮亏的 P75 / P90 / P97（向上取整到 10%）。
   含义：大多数最终盈利的单子不会跌到这个深度，提醒响了说明这一单比他通常扛的更深。
2. 硬止损参考：回测“只设止损”里累计收益最好的那一档，附误杀的盈利单数。
3. 跟单总止损比例：他近 90 天最大回撤 × 1.2（10%–50%），正常波动不触发、失控时兜底。
4. 分批止盈阶梯：只有在“样本外”也比不止盈好时才推荐（见 backtest.ladder_search）。
5. 我的实盘：在他身上提前离场 ≥5 次时，给一句“白止损多 / 挽回多”的说明（不参与计算）。
样本少于 30 笔（或算出浮亏 / 浮盈的少于 30 笔）只展示数据，不给推荐。
"""
from __future__ import annotations

import math

from . import exits, jobs
from .profile import _q

MIN_N = 30
TIER_MIN, TIER_MAX = 20, 400


def _ceil10(pct: float) -> int:
    return int(min(TIER_MAX, max(TIER_MIN, math.ceil(pct / 10) * 10)))


def _tiers(depths: list[float]) -> list[int]:
    out: list[int] = []
    for q in (0.75, 0.90, 0.97):
        t = _ceil10(_q(depths, q))
        if out and t <= out[-1]:
            t = min(TIER_MAX, out[-1] + 10)  # 保证逐档加深
        if not out or t > out[-1]:
            out.append(t)
    return out


def recommend(store, settings, lead_id: str) -> dict:
    prof = jobs.get_profile(store, lead_id)
    rows = store.db.execute("SELECT mae, pnl FROM leader_history WHERE lead_id=? AND status='All Closed' "
                            "AND mae IS NOT NULL", (lead_id,)).fetchall()
    winners = [-m * 100 for m, p in rows if (p or 0) > 0]
    losers = [-m * 100 for m, p in rows if (p or 0) < 0]
    cur = settings.rules_config(lead_id)
    out: dict = {"lead_id": lead_id, "n": prof["n"] if prof else 0, "n_paths": len(rows), "ok": False,
                 "outdated": bool(prof) and jobs.is_outdated(prof),  # 旧版本分析结果：后台会自动重新分析
                 "current": {"loss_tiers": list(cur.loss_tiers), "loss_tiers_mode": cur.loss_tiers_mode,
                             "recommend_total_stop_pct": cur.recommend_total_stop_pct,
                             "tp_ladder": [list(x) for x in cur.tp_ladder]}}
    if not prof:
        out["reason"] = "还没有分析结果，等后台分析完成后再看"
        return out
    if len(rows) < MIN_N or len(winners) < 10:
        out["reason"] = f"样本不足：算出浮亏的仓位只有 {len(rows)} 笔（盈利单 {len(winners)} 笔），至少需要 {MIN_N} 笔"
        return out
    tiers = _tiers(winners)
    out["loss_tiers"] = tiers
    out["tier_notes"] = [{
        "tier": t,
        "winners_deeper": sum(1 for d in winners if d >= t) / len(winners),  # 会提醒到的盈利单比例（“误报”）
        "losers_deeper": (sum(1 for d in losers if d >= t) / len(losers)) if losers else None,
    } for t in tiers]
    bt = prof.get("backtest") or {}
    stops = [x for x in (bt.get("stop_only") or []) if x]
    if stops and bt.get("baseline"):
        best = max(stops, key=lambda x: x["total"])
        out["hard_stop"] = {**best, "baseline_total": bt["baseline"]["total"], "better": best["total"] > bt["baseline"]["total"]}
    mdd = prof.get("mdd_90d")
    if mdd:
        out["mdd_90d"] = mdd
        out["total_stop_pct"] = round(min(0.5, max(0.1, mdd * 1.2 / 100)), 2)
    lad = prof.get("ladder") or {}
    out["ladder"] = lad.get("recommended")
    out["ladder_candidates"] = lad.get("candidates") or []
    ex = exits.summary(store, 0, lead_id)
    if ex["early"] >= 5:
        if ex["wasted_u"] + ex["missed_u"] > ex["saved_u"] + ex["avoid_giveback_u"]:
            out["my_note"] = (f"你在他身上提前离场 {ex['early']} 次，白止损 / 少赚合计多于挽回（净 {ex['net_u']:+.2f}U）："
                              "他常常能扛回来，档位可以放宽一些，或只在更深的档位处理")
        else:
            out["my_note"] = (f"你在他身上提前离场 {ex['early']} 次，挽回多于白止损（净 {ex['net_u']:+.2f}U）："
                              "提前离场对这个领航员是有效的，可以保持或收紧档位")
    out["ok"] = True
    return out


def adopt(store, settings, lead_id: str, what: str) -> dict:
    """采用推荐：写入该领航员的单独设置（复用 leader_rules 覆盖机制和校验）。what：stops / ladder / clear_ladder。"""
    rec = recommend(store, settings, lead_id)
    cfg = settings.get()
    rules = dict(cfg["leader_rules"].get(lead_id) or {})
    if what == "stops":
        if not rec["ok"]:
            raise ValueError(rec.get("reason") or "没有可采用的推荐")
        rules.update(loss_tiers=rec["loss_tiers"], loss_tiers_mode="roi")
        if rec.get("total_stop_pct"):
            rules["recommend_total_stop_pct"] = rec["total_stop_pct"]
        desc = f"亏损档位 {', '.join(f'-{t}%' for t in rec['loss_tiers'])}" + (
            f"，总止损建议 = 净投入 × {rec['total_stop_pct']:.0%}" if rec.get("total_stop_pct") else "")
    elif what == "ladder":
        if not rec.get("ladder"):
            raise ValueError("这个领航员没有通过样本外检验的止盈阶梯，暂不推荐")
        rules["tp_ladder"] = rec["ladder"]["levels"]
        desc = "分批止盈阶梯 " + rec["ladder"]["label"]
    elif what == "clear_ladder":
        rules.pop("tp_ladder", None)
        desc = "关闭分批止盈提醒"
    else:
        raise ValueError("未知的采用类型")
    leader_rules = {**cfg["leader_rules"], lead_id: rules}
    if not rules:
        leader_rules.pop(lead_id)
    settings.update({"leader_rules": leader_rules})
    store.log_event("settings.adopt", f"采用推荐：{desc}", leader_id=lead_id, what=what)
    return {"ok": True, "message": f"已采用：{desc}（只对这个领航员生效，可在设置 → 风控参数里修改）"}
