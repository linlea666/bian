"""把历史 + K 线拼成回测输入，并生成画像/回测报告文本。"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from .api import leaders as leaders_api
from .api.market import INTERVAL_MS, klines, pick_interval
from .backtest import Costs, GridReport, Path, Summary, build_path, grid_search
from .client import BinanceError, Client
from .models import ClosedPosition
from .profile import profile


class KlineCache:
    def __init__(self, client: Client, cache_dir: str = ".cache/klines"):
        self.client, self.dir = client, cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def get(self, symbol: str, interval: str, start: int, end: int):
        path = os.path.join(self.dir, f"{symbol}_{interval}_{start}_{end}.json")
        if os.path.exists(path):
            with open(path) as f:
                return [tuple(x) for x in json.load(f)]
        bars = klines(self.client, symbol, interval, start, end)
        with open(path, "w") as f:
            json.dump(bars, f)
        return bars


def load_history(client: Client, pid: str, days: int | None = None) -> list[ClosedPosition]:
    hist = leaders_api.position_history(client, pid)
    if days:
        cutoff = (time.time() - days * 86400) * 1000
        hist = [c for c in hist if c.closed >= cutoff]
    return hist


def build_paths(client: Client, closed: list[ClosedPosition], progress=None) -> tuple[list[Path], list[str]]:
    cache = KlineCache(client)
    paths, errors = [], []
    for i, cp in enumerate(closed):
        if cp.avg_cost <= 0 or cp.closed <= cp.opened:
            continue
        iv = pick_interval(cp.opened, cp.closed)
        try:
            bars = cache.get(cp.symbol, iv, cp.opened, cp.closed)
        except BinanceError as e:  # 下架币种等
            errors.append(f"{cp.symbol}: {e}")
            continue
        paths.append(build_path(cp, bars, INTERVAL_MS[iv], iv))
        if progress:
            progress(i + 1, len(closed))
    return paths, errors


def _ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def format_profile(name: str, pid: str, p: dict) -> str:
    if not p.get("n"):
        return f"# {name}（{pid}）\n\n没有已平仓记录。"
    L = [
        f"# 领航员画像：{name}（{pid}）",
        "",
        f"样本：{p['n']} 笔已平仓（{_ts(p['first'])} ~ {_ts(p['last'])}）"
        + ("" if p["sample_ok"] else "  ⚠️ 样本少于 30 笔，结论仅供参考"),
        f"标签：{'、'.join(p['tags']) or '无明显特征'}",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 胜率 | {p['win_rate'] * 100:.1f}% |",
        f"| 平均盈利 / 平均亏损 ROI | {_pct(p['avg_win_roi'])} / {_pct(p['avg_loss_roi'])} |",
        f"| 盈亏比（payoff） | {p['payoff']:.2f} |",
        f"| 利润因子 | {p['profit_factor']:.2f} |",
        f"| 总已实现盈亏 | {p['total_pnl']:.2f} U |",
        f"| 最差 / 最好单笔 ROI | {_pct(p['worst_roi'])} / {_pct(p['best_roi'])} |",
        f"| 最大连亏 | {p['max_loss_streak']} 笔 |",
        f"| 前 5 大亏损占全部亏损 | {p['top5_loss_share'] * 100:.0f}% |",
        f"| 持仓时长 中位 / P90 | {p['duration_median_h']:.1f}h / {p['duration_p90_h']:.1f}h |",
        f"| 杠杆 中位 / 最高 | {p['leverage_median']:g}x / {p['leverage_max']:g}x |",
        f"| 做多占比 | {p['long_share'] * 100:.0f}% |",
        f"| 最常交易 | {p['top_symbol']}（{p['top_symbol_share'] * 100:.0f}%） |",
        f"| 可识别的加减仓单子 | {p['adds_share'] * 100:.0f}% |",
    ]
    if "mae_median" in p:
        L += [
            f"| 途中最大浮亏 MAE 中位 / 最差10% | {_pct(p['mae_median'])} / {_pct(p['mae_p10'])} |",
            f"| 途中最大浮盈 MFE 中位 | {_pct(p['mfe_median'])} |",
            f"| 扛单率（盈利单途中浮亏曾 ≤-30%） | {p['carry_rate'] * 100:.0f}% |",
            f"| 利润回吐率（浮盈≥20% 后回吐过半） | {p['giveback_rate'] * 100:.0f}% |",
            f"| 盈转亏率（浮盈≥20% 最终亏损） | {p['profit_to_loss_rate'] * 100:.0f}% |",
            "",
            "> MAE/MFE 由 K 线近似重建：无逐笔加仓数据，按均价一次性建仓估算；数据质量等级 C/D。",
        ]
    return "\n".join(L)


def _row(s: Summary, base: Summary | None = None) -> str:
    delta = f"{(s.total - base.total):+.2f}" if base else ""
    return (
        f"| {s.params.label()} | {s.total:+.2f} | {delta} | {s.max_dd:.2f} | {s.worst * 100:+.0f}% | "
        f"{s.win_rate * 100:.0f}% | {s.rule_exits} | {s.winners_cut} | {s.profit_given_up:.2f} | {s.loss_saved:.2f} |"
    )


HEADER = (
    "| 方案 | 累计收益 | 相对原始 | 最大回撤 | 最差单笔 | 胜率 | 规则出场 | 误杀盈利单 | 少赚 | 少亏 |\n"
    "|---|---|---|---|---|---|---|---|---|---|"
)


def format_backtest(name: str, rep: GridReport, costs: Costs, top: int = 10, n_paths: int = 0,
                    adds_share: float = 0.0) -> str:
    b = rep.baseline
    L = [
        f"# 规则回测：{name}",
        "",
        f"样本 {b.n} 笔；收益单位 = “每笔投入 1 份保证金”的倍数（例如 +3.5 表示累计赚了 3.5 份单笔保证金）。",
        f"成本假设：单边手续费 {costs.fee_rate * 100:.3f}%，规则出场额外滑点 {costs.stop_slippage * 100:.3f}%（名义价值）。",
        "",
        "## 结论",
        "",
    ]
    L += conclusions(rep)
    L += [
        "",
        "## 方案一：只设硬止损（币安跟单设置里就能配置）",
        "",
        HEADER,
        _row(b),
    ]
    L += [_row(s, b) for s in rep.stop_only]
    L += [
        "",
        f"## 方案二：全部组合排名前 {top}（score = 累计收益 − 0.5 × 最大回撤；保本/回撤止盈需按提醒手动操作）",
        "",
        HEADER,
        _row(b),
    ]
    L += [_row(s, b) for s in rep.ranked[:top]]
    L += ["", "## 样本外验证（按时间：前 70% 选参数，后 30% 检验）", ""]
    if rep.train_best and rep.test_best and rep.test_baseline:
        if rep.train_best.params.is_baseline:
            L.append(f"训练 {rep.n_train} 笔里，没有任何规则组合比原始跟单更好。")
        else:
            L += [
                f"训练 {rep.n_train} 笔选出：**{rep.train_best.params.label()}**，在后 {rep.n_test} 笔上的表现：",
                "",
                HEADER,
                _row(rep.test_baseline),
                _row(rep.test_best, rep.test_baseline),
            ]
    else:
        L.append("样本不足（训练 ≥20 笔且检验 ≥10 笔才做样本外验证）。")
    L += [
        "",
        "## 说明",
        "",
        "- 误杀盈利单：原本最终盈利，但被规则提前出场并少赚的单数；少赚/少亏为相对原始跟单的合计差额。",
        "- 硬止损在币安跟单里只对设置后的新仓生效，领航员加仓后需复查。",
        "- 保本/回撤止盈：币安原生跟单没有这类自动功能，由 `bian monitor` 提醒你手动上移止损或部分止盈。",
        "- 路径由 K 线近似重建（无逐笔加仓），K 线内先按不利方向处理；历史表现不代表未来，优先选样本外也稳定的简单方案。",
    ]
    return "\n".join(L)


def conclusions(rep: GridReport) -> list[str]:
    b = rep.baseline
    out = []
    if b.n < 30:
        out.append(f"- ❗ 只有 {b.n} 笔已平仓，以下结论基本没有统计意义，仅供参考。")
    better_dd = [s for s in rep.stop_only if s.max_dd < b.max_dd * 0.8]
    keeps = [s for s in better_dd if s.total >= b.total * 0.9] if b.total > 0 else [s for s in better_dd if s.total >= b.total]
    if keeps:
        best = max(keeps, key=lambda s: s.score())
        out.append(f"- ✅ 硬止损有效：**{best.params.label()}** 回撤 {b.max_dd:.2f} → {best.max_dd:.2f}，"
                   f"收益 {b.total:+.2f} → {best.total:+.2f}。")
    else:
        best = max(rep.stop_only, key=lambda s: s.score(), default=None)
        if best:
            out.append(f"- ⚠️ 单纯硬止损效果有限：最好的 **{best.params.label()}** 收益 {b.total:+.2f} → {best.total:+.2f}，"
                       f"回撤 {b.max_dd:.2f} → {best.max_dd:.2f}，误杀盈利单 {best.winners_cut} 笔。"
                       "该领航员的利润主要来自少数大赚单或“扛回来”的单，止损会把它们提前砍掉。")
    top = rep.ranked[0] if rep.ranked else None
    if top and top.score() > b.score():
        out.append(f"- 全样本得分最高：**{top.params.label()}**（收益 {top.total:+.2f}，回撤 {top.max_dd:.2f}）。")
    else:
        out.append("- 全样本里没有规则组合的“收益−回撤”得分超过原始跟单：对该领航员，规则主要是降低波动，代价是少赚。")
    if rep.train_best and rep.test_best and rep.test_baseline and not rep.train_best.params.is_baseline:
        tb, tt = rep.test_baseline, rep.test_best
        verdict = "✅ 样本外依然有效" if tt.score() > tb.score() else "❌ 样本外没有优于原始跟单（可能过拟合）"
        out.append(f"- 样本外：{verdict}（{tt.params.label()}：收益 {tb.total:+.2f} → {tt.total:+.2f}，回撤 {tb.max_dd:.2f} → {tt.max_dd:.2f}）。")
    if b.total <= 0:
        out.append("- ❗ 该领航员按原始跟单在样本期内本身就不赚钱，止损只能少亏，更该考虑是否继续跟。")
    return out


def analyze(client: Client, pid: str, days: int | None = None, with_paths: bool = True,
            costs: Costs | None = None, progress=None) -> dict:
    d = leaders_api.detail(client, pid)
    name = d.get("nickname") or pid
    closed = load_history(client, pid, days)
    paths, errors = build_paths(client, closed, progress) if with_paths else ([], [])
    prof = profile(closed, paths or None)
    out = {"name": name, "detail": d, "closed": closed, "paths": paths, "errors": errors, "profile": prof}
    if paths:
        costs = costs or Costs()
        out["grid"] = grid_search(paths, costs)
        out["costs"] = costs
    return out
