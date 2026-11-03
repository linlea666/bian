"""每日摘要邮件：昨天的结果 + 现在要做什么。复用作战室的总览 / 待办，以及统计和离场判定。"""
from __future__ import annotations

import time

from . import exits, stats
from .web import views


def build(store, settings, now: float | None = None) -> tuple[str, str]:
    now = now or time.time()
    st = stats.period_starts(now)
    y_start, y_end = st["yesterday"], st["today"]
    day = time.strftime("%m-%d", time.localtime(y_start))
    ov = views.overview(store, settings, now)
    real = stats.daily_realized(store, y_start, y_end)
    net = sum(x["net"] for x in real.values())
    fee = sum(x["fee"] for x in real.values())
    eq = sum(stats.daily_equity_change(store, y_start, y_end).values())
    lines = [f"【{day} 跟单日报】", ""]
    lines.append(f"昨日已实现净盈亏：{net:+.2f}U（其中手续费 {fee:+.2f}U）")
    lines.append(f"昨日权益变化（含浮动盈亏，扣除追加 / 赎回）：{eq:+.2f}U")
    t = ov["totals"]
    lines.append(f"当前总权益 {t['equity']:.2f}U，净投入 {t['invested']:.2f}U，浮动盈亏 {t['unrealized']:+.2f}U，持仓 {t['positions']} 个")
    lines.append("")
    rows = []
    for lid, name in store.db.execute("SELECT lead_id, name FROM leaders WHERE kind='copy' ORDER BY added"):
        r = stats.daily_realized(store, y_start, y_end, lid)
        n = sum(x["net"] for x in r.values())
        trades = sum(x["trades"] for x in r.values())
        if trades:
            rows.append(f"  · {name}：{n:+.2f}U（平仓 {trades} 笔）")
    if rows:
        lines.append("各领航员昨日：")
        lines += rows
        lines.append("")
    ex = exits.summary(store, y_start)
    if ex["early"] or ex["follow"]:
        lines.append(f"昨日离场：跟随平仓 {ex['follow']} 次，提前离场 {ex['early']} 次 —— "
                     f"止损挽回 {ex['saved_n']} 次 +{ex['saved_u']:.2f}U，白止损 {ex['wasted_n']} 次 -{ex['wasted_u']:.2f}U，"
                     f"避免回吐 {ex['avoid_giveback_n']} 次，少赚 {ex['missed_n']} 次；净效果 {ex['net_u']:+.2f}U")
        if ex["waiting"]:
            lines.append(f"另有 {ex['waiting']} 笔提前离场在等领航员平仓后结算")
        lines.append("")
    positions, _ = store.get_runtime("positions", [])
    mine = sorted((p for p in positions or [] if p.get("source") == "copy"), key=lambda p: p.get("unrealized_pnl") or 0)[:3]
    if mine:
        lines.append("当前风险最高的仓位：")
        side = {"LONG": "多", "SHORT": "空", "HEDGE": "多空对冲"}
        for p in mine:
            lines.append(f"  · {p['leader_name']} {p['symbol']} {side.get(p['side'], p['side'])}："
                         f"{p['unrealized_pnl']:+.2f}U（ROI {p['roi'] * 100:+.1f}%）")
        lines.append("")
    if ov["todos"]:
        lines.append("今日待办：")
        for td in ov["todos"][:8]:
            lines.append(f"  {'🔴' if td['level'] == 'red' else '🟡'} {td['title']}")
        lines.append("")
    cred = ov["credentials"]
    state = {"valid": "有效", "expired": "已失效（监控已暂停，请尽快更新）", "missing": "未设置", "unknown": "验证中"}
    lines.append(f"币安凭证：{state.get(cred.get('state'), cred.get('state'))}"
                 + (f"，已使用 {cred['age_days']:.1f} 天" if cred.get("age_days") else ""))
    lines.append("")
    lines.append("本邮件由 bian 币安跟单风控助手每天自动发送（可在后台 设置 → 账号与系统 里关闭或修改时间）。"
                 "只做提醒和统计，不构成投资建议。")
    subject = f"📊 跟单日报 {day}：已实现 {net:+.2f}U" + (f"，{len(ov['todos'])} 项待办" if ov["todos"] else "")
    return subject, "\n".join(lines)
