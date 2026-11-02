"""实时监控：轮询持仓 → 规则评估 → 提醒。只读，不下单。"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from .api import copy_account, leaders as leaders_api
from .client import AuthExpired, BinanceError, Client, RateLimited
from .config import AppConfig, LeaderConfig
from .models import Position
from .notify import Notifier
from .rules import Alert, PortfolioState, PosState, RuleConfig, evaluate, evaluate_portfolio
from .store import Store

LEAD_URL = "https://www.binance.com/zh-CN/copy-trading/lead-details/{}"
FOOTER = "（建议仅供参考：本系统不会自动下单/止损，请在币安页面自行操作）"


@dataclass
class LeaderRuntime:
    cfg: LeaderConfig
    rules: RuleConfig
    source: str = "public"
    copy_portfolio_id: str | None = None
    name: str = ""
    position_show: bool | None = None
    last_ok: float = 0.0
    stale_alerted: bool = False
    empty_polls: int = 0
    positions: dict[str, Position] = field(default_factory=dict)
    blocked_reason: str = ""
    initialized: bool = False  # 首次成功拉取前的已有持仓合并成一条汇总，避免启动刷屏


class Monitor:
    def __init__(self, cfg: AppConfig, client: Client, notifier: Notifier, store: Store, clock=time.time):
        self.cfg, self.client, self.notifier, self.store, self.clock = cfg, client, notifier, store, clock
        self.states: dict[str, PosState] = {
            k: PosState.from_dict(v) for k, v in store.load_states().items()
        }
        self.portfolio = PortfolioState()
        self.auth_alerted = False
        self.leaders = [LeaderRuntime(c, cfg.rules.merged(c.rules)) for c in cfg.leaders]

    # ------------------------------------------------------------- setup
    def setup(self) -> None:
        relations: dict[str, dict] = {}
        if self.client.has_credentials:
            try:
                relations = {str(r.get("leadPortfolioId")): r for r in copy_account.copy_relations(self.client)}
            except AuthExpired:
                self._auth_alert()
            except BinanceError as e:
                print(f"读取跟单关系失败：{e}")
        for lr in self.leaders:
            pid = lr.cfg.portfolio_id
            try:
                d = leaders_api.detail(self.client, pid)
                lr.name = lr.cfg.name or d.get("nickname") or pid
                lr.position_show = d.get("positionShow")
            except BinanceError as e:
                lr.name = lr.cfg.name or pid
                print(f"[{lr.name}] 读取领航员详情失败：{e}")
            rel = relations.get(pid)
            want = lr.cfg.source
            if want in ("auto", "copy") and rel and rel.get("copyPortfolioId"):
                lr.source, lr.copy_portfolio_id = "copy", str(rel["copyPortfolioId"])
            elif want == "copy":
                lr.source = "copy"
                lr.blocked_reason = "未配置凭证或未在币安跟单该领航员，无法读取我的跟单仓位"
            else:
                lr.source = "public"
                if lr.position_show is False:
                    lr.blocked_reason = "该领航员未公开持仓；请配置 BINANCE_P20T/BINANCE_CSRFTOKEN 并确保已跟单"
            if lr.blocked_reason:
                self._send(Alert("R08", "warn", f"leader:{pid}", 0, f"[{lr.name}] 无法监控持仓",
                                 lr.blocked_reason, ts=self.clock()))
            print(f"[{lr.name}] 数据源={lr.source}" + (f"（{lr.blocked_reason}）" if lr.blocked_reason else ""))

    # -------------------------------------------------------------- poll
    def fetch(self, lr: LeaderRuntime) -> list[Position]:
        if lr.source == "copy":
            return copy_account.copy_positions(self.client, lr.copy_portfolio_id or "", lr.cfg.portfolio_id)
        return leaders_api.public_positions(self.client, lr.cfg.portfolio_id)

    def poll_once(self) -> list[Alert]:
        sent: list[Alert] = []
        now = self.clock()
        for lr in self.leaders:
            if lr.blocked_reason:
                continue
            try:
                positions = self.fetch(lr)
            except AuthExpired:
                self._auth_alert()
                sent += self._check_stale(lr, now)
                continue
            except (RateLimited, BinanceError) as e:
                print(f"[{lr.name}] 拉取持仓失败：{e}")
                sent += self._check_stale(lr, now)
                continue
            sent += self._process(lr, positions, now)
        sent += self._portfolio(now)
        return sent

    def _process(self, lr: LeaderRuntime, positions: list[Position], now: float) -> list[Alert]:
        out: list[Alert] = []
        if lr.stale_alerted:
            lr.stale_alerted = False
            out.append(self._send(Alert("R08", "info", f"leader:{lr.cfg.portfolio_id}", 0,
                                        f"[{lr.name}] 数据已恢复", ts=now)))
        # 空快照二次确认：避免接口抖动被误判成“全部平仓”
        had = any(k.startswith(lr.cfg.portfolio_id + ":") for k in self.states)
        if not positions and had:
            lr.empty_polls += 1
            if lr.empty_polls < self.cfg.empty_confirm_polls:
                lr.last_ok = now
                return out
            if lr.source == "public":
                # 领航员可能刚关闭了“展示持仓”，此时空列表不代表平仓
                try:
                    if leaders_api.detail(self.client, lr.cfg.portfolio_id).get("positionShow") is False:
                        lr.blocked_reason = "领航员已关闭持仓展示，空列表不代表已平仓"
                        out.append(self._send(Alert("R08", "warn", f"leader:{lr.cfg.portfolio_id}", 0,
                                                    f"[{lr.name}] 无法继续监控持仓", lr.blocked_reason,
                                                    "配置凭证改用“我的跟单仓位”数据源", ts=now)))
                        return out
                except BinanceError:
                    return out
        lr.empty_polls = 0
        lr.last_ok = now
        self.auth_alerted = False
        self.store.add_snapshot(lr.cfg.portfolio_id, lr.source, [asdict(p) for p in positions])

        current = {p.key: p for p in positions}
        lr.positions = current
        summary: list[str] = []
        for key, pos in current.items():
            known = key in self.states
            st, alerts = evaluate(pos, self.states.get(key), lr.rules, now)
            self.states[key] = st
            self.store.save_state(key, st.to_dict())
            if not lr.initialized and not known:
                # 启动时已存在的仓位：info 级提醒并入汇总，warn/urgent 仍单独发
                line = f"{pos.symbol} {'多' if pos.side == 'LONG' else '空'} {pos.leverage:g}x ROI {pos.roi * 100:+.1f}%"
                if any(a.rule_id == "R04" for a in alerts):
                    be = pos.break_even_price if pos.break_even_price > 0 else pos.entry_price
                    line += f"（浮盈已达保本门槛，建议止损上移到约 {be:.6g}）"
                summary.append(line)
                alerts = [a for a in alerts if a.severity != "info"]
            for a in alerts:
                out.append(self._send(a, lr, pos))
        if not lr.initialized:
            lr.initialized = True
            if summary:
                out.append(self._send(Alert(
                    "R00", "info", f"leader:{lr.cfg.portfolio_id}", 0,
                    f"开始监控，当前 {len(summary)} 个持仓（来源：{'我的跟单仓位' if lr.source == 'copy' else '领航员公开持仓'}）",
                    "\n".join(summary), ts=now), lr))
        for key in [k for k in self.states if k.startswith(lr.cfg.portfolio_id + ":") and k not in current]:
            st = self.states.pop(key)
            self.store.delete_state(key)
            out.append(self._send(Alert(
                "R11", "info", key, st.version, "仓位已平（或已不在列表中）",
                f"最后 ROI {st.last_roi * 100:+.1f}%，期间最高 {st.peak_roi * 100:+.1f}%、最低 {st.trough_roi * 100:+.1f}%",
                ts=now), lr))
        return out

    def _check_stale(self, lr: LeaderRuntime, now: float) -> list[Alert]:
        if lr.last_ok and not lr.stale_alerted and now - lr.last_ok > self.cfg.stale_seconds:
            lr.stale_alerted = True
            return [self._send(Alert(
                "R08", "warn", f"leader:{lr.cfg.portfolio_id}", 0,
                f"[{lr.name}] 数据已 {int(now - lr.last_ok)} 秒未更新",
                "持仓/行情不是实时的，期间的风险判断不可靠", "请直接打开币安 App 查看仓位", ts=now))]
        return []

    def _portfolio(self, now: float) -> list[Alert]:
        if self.cfg.loss_budget_u <= 0:
            return []
        est = 0.0
        for lr in self.leaders:
            for p in lr.positions.values():
                if p.source == "copy":
                    est += p.unrealized_pnl
                elif lr.cfg.position_margin_u > 0:
                    est += p.roi * lr.cfg.position_margin_u
        self.portfolio, alert = evaluate_portfolio(est, self.cfg.loss_budget_u, self.portfolio, now)
        return [self._send(alert)] if alert else []

    def _auth_alert(self) -> None:
        if not self.auth_alerted:
            self.auth_alerted = True
            self._send(Alert("AUTH", "urgent", "auth", 0, "币安登录凭证失效",
                             "私有接口返回未登录/鉴权失败，隐藏领航员与我的跟单仓位无法监控",
                             "重新登录币安网页，更新环境变量 BINANCE_P20T / BINANCE_CSRFTOKEN 后重启",
                             ts=self.clock()))

    # -------------------------------------------------------------- send
    def format(self, a: Alert, lr: LeaderRuntime | None = None, pos: Position | None = None) -> str:
        lines = []
        head = a.title
        if lr and pos:
            src = "我的跟单仓位" if pos.source == "copy" else "领航员仓位"
            head = f"[{lr.name}] {pos.symbol} {'多' if pos.side == 'LONG' else '空'} {pos.leverage:g}x ｜{a.title}"
            lines.append(head)
            lines.append(f"{src}：开仓 {pos.entry_price:.6g} 现价 {pos.mark_price:.6g} ROI {pos.roi * 100:+.1f}%")
            if pos.source == "copy":
                lines.append(f"浮动盈亏 {pos.unrealized_pnl:+.2f}U")
            elif lr.cfg.position_margin_u > 0:
                lines.append(f"按每笔 {lr.cfg.position_margin_u:g}U 估算我的盈亏 {pos.roi * lr.cfg.position_margin_u:+.2f}U")
        elif lr:
            lines.append(f"[{lr.name}] {a.title}")
        else:
            lines.append(head)
        if a.detail:
            lines.append(a.detail)
        if a.suggestion:
            lines.append("建议：" + a.suggestion)
        lines.append(f"规则 {a.rule_id} · 数据时间 {time.strftime('%m-%d %H:%M:%S', time.localtime(a.ts or self.clock()))}")
        if lr:
            lines.append(LEAD_URL.format(lr.cfg.portfolio_id))
        if a.severity != "info":
            lines.append(FOOTER)
        return "\n".join(lines)

    def _send(self, a: Alert, lr: LeaderRuntime | None = None, pos: Position | None = None) -> Alert:
        msg = self.format(a, lr, pos)
        ok, err = self.notifier.send(msg, a.severity)
        self.store.add_alert(a, msg, ok, err)
        if not ok:
            print(f"通知发送失败（已记录）：{err}")
        return a

    def run(self) -> None:
        self.setup()
        while True:
            t0 = time.monotonic()
            try:
                self.poll_once()
            except Exception as e:  # noqa: BLE001  监控循环不能因单次异常退出
                print(f"轮询异常：{type(e).__name__}: {e}")
            time.sleep(max(1.0, self.cfg.poll_seconds - (time.monotonic() - t0)))
