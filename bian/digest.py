"""每日摘要邮件：昨天的结果 + 现在要做什么。复用作战室的总览 / 待办，以及统计和离场判定。"""
from __future__ import annotations

import time

from . import exits, stats
from .exchanges import EXCHANGES
from .notify import EVENT_GROUPS, SKIPPED, event_group
from .web import views


def _recorded_only(store, start: float, end: float) -> list[tuple[str, int]]:
    """按设置不发邮件、只在后台记录的提醒，按事件分组计数（每日摘要里提一句，避免漏看）。"""
    names = {k: label for k, label, _, _ in EVENT_GROUPS}
    counts: dict[str, int] = {}
    for (rule_id,) in store.db.execute("SELECT rule_id FROM alerts WHERE ts >= ? AND ts < ? AND error LIKE ?",
                                       (start, end, f"%{SKIPPED}%")):
        g = names.get(event_group(rule_id or ""), "其他")
        counts[g] = counts.get(g, 0) + 1
    return sorted(counts.items(), key=lambda x: -x[1])


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
    for a in store.db.execute("SELECT id, exchange, name FROM exchange_accounts WHERE removed=0 ORDER BY created"):
        r = stats.daily_realized(store, y_start, y_end, a[0])
        if r:
            n = sum(x["net"] for x in r.values())
            fu = sum(x["funding"] for x in r.values())
            rows.append(f"  · {EXCHANGES.get(a[1], a[1])} · {a[2]}（交易所账户）：{n:+.2f}U"
                        + (f"（含资金费 {fu:+.2f}U）" if fu else ""))
    if rows:
        lines.append("各领航员 / 账户昨日：")
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
    quiet = _recorded_only(store, y_start, y_end)
    if quiet:
        lines.append("昨日只在后台记录、没有发邮件的提醒：" + "，".join(f"{k} {n} 条" for k, n in quiet)
                     + "（可在后台“记录 → 提醒记录”查看；在“设置 → 邮件提醒”里可以改成发邮件）")
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
