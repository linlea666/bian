"""领航员风格画像。"""
from __future__ import annotations

import statistics
from collections import Counter

from .backtest import Path
from .models import ClosedPosition

MIN_SAMPLE = 30


def _q(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def profile(closed: list[ClosedPosition], paths: list[Path] | None = None) -> dict:
    n = len(closed)
    out: dict = {"n": n, "sample_ok": n >= MIN_SAMPLE}
    if not n:
        return out
    pnls = [c.pnl for c in closed]
    rois = [c.roi for c in closed]
    wins = [c for c in closed if c.pnl > 0]
    losses = [c for c in closed if c.pnl < 0]
    avg_win = statistics.mean([c.roi for c in wins]) if wins else 0.0
    avg_loss = statistics.mean([c.roi for c in losses]) if losses else 0.0
    streak = cur = 0
    for c in closed:
        cur = cur + 1 if c.pnl < 0 else 0
        streak = max(streak, cur)
    sym = Counter(c.symbol for c in closed)
    top_sym, top_cnt = sym.most_common(1)[0]
    levs = [c.leverage for c in closed]
    durs = [c.duration_hours for c in closed]
    out.update(
        first=min(c.opened for c in closed),
        last=max(c.closed for c in closed),
        win_rate=len(wins) / n,
        total_pnl=sum(pnls),
        avg_win_roi=avg_win,
        avg_loss_roi=avg_loss,
        payoff=(avg_win / -avg_loss) if avg_loss < 0 else float("inf"),
        profit_factor=(sum(c.pnl for c in wins) / -sum(c.pnl for c in losses)) if losses else float("inf"),
        worst_roi=min(rois),
        best_roi=max(rois),
        worst_pnl=min(pnls),
        max_loss_streak=streak,
        duration_median_h=statistics.median(durs),
        duration_p90_h=_q(durs, 0.9),
        leverage_median=statistics.median(levs),
        leverage_max=max(levs),
        top_symbol=top_sym,
        top_symbol_share=top_cnt / n,
        long_share=sum(1 for c in closed if c.side == "LONG") / n,
        adds_share=sum(1 for c in closed if c.had_adds) / n,
        # 大亏单占全部亏损的比例：越高说明“少数几笔大亏吃掉利润”
        top5_loss_share=(
            sum(sorted(c.pnl for c in losses)[:5]) / sum(c.pnl for c in losses) if losses else 0.0
        ),
    )
    if paths:
        maes = [p.mae for p in paths]
        mfes = [p.mfe for p in paths]
        win_paths = [p for p in paths if p.cp.pnl > 0]
        big_mfe = [p for p in paths if p.mfe >= 0.2]
        out.update(
            path_n=len(paths),
            mae_median=statistics.median(maes),
            mae_p10=_q(maes, 0.1),  # 最差 10% 的浮亏深度
            mfe_median=statistics.median(mfes),
            # 扛单率：最终盈利的单子里，途中浮亏曾超过 -30% ROI 的比例
            carry_rate=(sum(1 for p in win_paths if p.mae <= -0.3) / len(win_paths)) if win_paths else 0.0,
            # 盈利回吐：浮盈曾 ≥20% 的单子，最终没拿住、回吐超过一半的比例
            giveback_rate=(
                sum(1 for p in big_mfe if p.final_roi < p.mfe / 2) / len(big_mfe) if big_mfe else 0.0
            ),
            # 浮盈曾 ≥20% 最后却亏损出场的比例（“盈转亏”）
            profit_to_loss_rate=(
                sum(1 for p in big_mfe if p.final_roi < 0) / len(big_mfe) if big_mfe else 0.0
            ),
        )
    out["tags"] = tags(out)
    return out


def tags(p: dict) -> list[str]:
    if not p.get("n"):
        return []
    t = []
    if p["leverage_median"] >= 20:
        t.append("高杠杆")
    if p["duration_median_h"] < 1:
        t.append("短线高频")
    elif p["duration_median_h"] > 72:
        t.append("长线持仓")
    if p["top_symbol_share"] > 0.4:
        t.append(f"集中交易{p['top_symbol']}")
    if p["win_rate"] > 0.65 and p.get("payoff", 1) < 0.7:
        t.append("高胜率低盈亏比(可能扛单)")
    if p["top5_loss_share"] > 0.6 and p["n"] >= 15:
        t.append("少数大亏吞利润")
    if p.get("carry_rate", 0) > 0.3:
        t.append("扛单型")
    if p.get("mae_p10", 0) <= -0.8:
        t.append("极端浮亏")
    if p.get("giveback_rate", 0) > 0.5:
        t.append("利润回吐大")
    if p["adds_share"] > 0.3:
        t.append("频繁加减仓")
    if not p["sample_ok"]:
        t.append(f"样本不足{MIN_SAMPLE}笔(仅供参考)")
    return t
