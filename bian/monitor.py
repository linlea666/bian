"""实时监控：轮询持仓 → 规则评估 → 提醒。只读，不下单。"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from .api import copy_account, leaders as leaders_api
from .client import AuthExpired, BinanceError, Client, RateLimited
from .config import AppConfig, LeaderConfig
from .exposure import describe_legs, net_by_symbol
from .models import Position
from .notify import Notifier, Outgoing, deliver_all, is_soft
from .sync import mark_copy_set
from .rules import (
    AccountSnapshot, AccountState, Alert, PortfolioState, PosState, RuleConfig,
    evaluate, evaluate_account, evaluate_portfolio,
)
from .store import Store

LEAD_URL = "https://www.binance.com/zh-CN/copy-trading/lead-details/{}"


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
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
    positions: dict[str, Position] = field(default_factory=dict)  # 原始单腿仓位
    blocked_reason: str = ""
    initialized: bool = False  # 首次成功拉取前的已有持仓合并成一条汇总，避免启动刷屏


class Monitor:
    EQUITY_EVERY_S = 300

    def __init__(self, cfg: AppConfig, client: Client, notifier: Notifier, store: Store, clock=time.time,
                 guard=None, recorder=None, rules_provider=None):
        """guard：CredentialGuard（凭证失效时暂停私有接口）；recorder：EpisodeRecorder（仓位回合）；
        rules_provider(leader_id) -> RuleConfig：网页配置的阈值（支持按领航员覆盖，热更新）。"""
        self.cfg, self.client, self.notifier, self.store, self.clock = cfg, client, notifier, store, clock
        self.guard, self.recorder = guard, recorder
        self.rules_provider = rules_provider or (lambda pid: cfg.rules.merged(
            next((lc.rules for lc in cfg.leaders if lc.portfolio_id == pid), None)))
        self.last_equity_ts: dict[str, float] = {}
        self.last_poll: dict = {}
        saved = store.load_states()
        self.states: dict[str, PosState] = {
            k: PosState.from_dict(v) for k, v in saved.items() if not k.startswith("acct:")
        }
        self.accounts: dict[str, AccountState] = {
            k: AccountState.from_dict(v) for k, v in saved.items() if k.startswith("acct:")
        }
        self.portfolio = PortfolioState()
        self.outbox: list[Outgoing] = []
        self.auth_alerted = False
        self.leaders = [LeaderRuntime(c, self.rules_provider(c.portfolio_id)) for c in cfg.leaders]

    def refresh_rules(self) -> None:
        """网页修改阈值后调用：所有领航员按最新配置重新取规则（下一轮生效）。"""
        for lr in self.leaders:
            lr.rules = self.rules_provider(lr.cfg.portfolio_id)

    @property
    def private_paused(self) -> bool:
        return bool(self.guard and self.guard.paused)

    # ------------------------------------------------------------- setup
    def setup(self) -> None:
        relations: dict[str, dict] = {}
        if self.client.has_credentials and not self.private_paused:
            try:
                relations = {str(r.get("leadPortfolioId")): r for r in copy_account.copy_relations(self.client)}
            except AuthExpired:
                self._auth_alert()
            except BinanceError as e:
                print(f"读取跟单关系失败：{e}")
        if self.cfg.auto_discover:
            known = {lr.cfg.portfolio_id for lr in self.leaders}
            for pid, rel in relations.items():
                if pid not in known:
                    c = LeaderConfig(portfolio_id=pid, name=str(rel.get("nickname") or ""), source="copy")
                    self.leaders.append(LeaderRuntime(c, self.rules_provider(pid)))
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
        self.flush()

    # -------------------------------------------------------------- poll
    def fetch(self, lr: LeaderRuntime) -> list[Position]:
        if lr.source == "copy":
            return copy_account.copy_positions(self.client, lr.copy_portfolio_id or "", lr.cfg.portfolio_id)
        return leaders_api.public_positions(self.client, lr.cfg.portfolio_id)

    def poll_once(self) -> list[Alert]:
        sent: list[Alert] = []
        now = self.clock()
        t0 = time.monotonic()
        errors: list[str] = []
        for lr in self.leaders:
            if lr.blocked_reason:
                continue
            if lr.source == "copy" and self.private_paused:
                continue  # 凭证失效：不拿失效凭证反复请求；由凭证守护负责提醒
            try:
                positions = self.fetch(lr)
            except AuthExpired as e:
                self._auth_alert(str(e))
                sent += self._check_stale(lr, now)
                continue
            except (RateLimited, BinanceError) as e:
                print(f"[{lr.name}] 拉取持仓失败：{e}")
                errors.append(f"{lr.name}: {e}")
                sent += self._check_stale(lr, now)
                continue
            sent += self._process(lr, positions, now)
        sent += self._accounts(now)
        sent += self._portfolio(now)
        self.flush(now)
        self._publish_runtime(now, time.monotonic() - t0, errors)
        return sent

    def _publish_runtime(self, now: float, duration: float, errors: list[str]) -> None:
        """把最新持仓、跟单账户和心跳写进 runtime 表，供网页读取。"""
        positions = []
        for lr in self.leaders:
            legs = list(lr.positions.values())
            for p in net_by_symbol(legs):
                st = self.states.get(p.key)
                d = asdict(p)
                d.update(key=p.key, leader_name=lr.name, roi=p.roi, margin=p.margin,
                         liq_valid=p.liquidation_valid(),
                         tier=max(st.loss.get("fired") or [-1]) + 1 if st else 0,
                         peak_roi=st.peak_roi if st else 0.0, trough_roi=st.trough_roi if st else 0.0,
                         first_seen=st.first_seen if st else now,
                         legs=[{**asdict(x), "roi": x.roi, "margin": x.margin} for x in legs
                               if x.symbol == p.symbol] if p.is_hedge else [])
                positions.append(d)
        self.store.set_runtime("positions", positions)
        self.last_poll = {
            "ts": now, "duration": round(duration, 2), "errors": errors,
            "paused_private": self.private_paused,
            "leaders": [{"id": lr.cfg.portfolio_id, "name": lr.name, "source": lr.source,
                         "blocked": lr.blocked_reason, "last_ok": lr.last_ok,
                         "positions": len(lr.positions)} for lr in self.leaders],
        }
        self.store.set_runtime("poll", self.last_poll)

    def _accounts(self, now: float) -> list[Alert]:
        """跟单账户级规则：权益回撤、原生总止损。每轮一次 detail-list 请求。"""
        if not self.client.has_credentials or self.private_paused:
            return []
        try:
            rels = copy_account.copy_relations(self.client)
        except AuthExpired as e:
            self._auth_alert(str(e))
            return []
        except BinanceError as e:
            print(f"读取跟单关系失败：{e}")
            return []
        if self.guard:
            self.guard.report_success(now, len(rels))
        self._sync_relations(rels, now)
        by_id = {lr.cfg.portfolio_id: lr for lr in self.leaders}
        out: list[Alert] = []
        accounts_view = []
        for r in rels:
            snap = AccountSnapshot.from_relation(r)
            lr = by_id.get(snap.lead_id)
            if lr is None:
                continue
            key = f"acct:{snap.lead_id}"
            st, alerts = evaluate_account(snap, self.accounts.get(key), lr.rules, now,
                                          lr.cfg.recommended_total_stop_u)
            self.accounts[key] = st
            self.store.save_state(key, st.to_dict())
            out += [self._send(a, lr) for a in alerts]
            if now - self.last_equity_ts.get(snap.lead_id, 0) >= self.EQUITY_EVERY_S:
                self.last_equity_ts[snap.lead_id] = now
                self.store.add_equity(now, snap.lead_id, snap.equity, snap.invested, snap.unrealized, snap.realized,
                                      _float(r.get("profitSharedAmount")))
            dd = (st.peak_equity - snap.equity) / st.peak_equity if st.peak_equity > 0 else 0.0
            accounts_view.append({
                **asdict(snap), "name": lr.name, "peak_equity": st.peak_equity, "drawdown": dd,
                "recommended_stop_u": lr.cfg.recommended_total_stop_u or round(snap.invested * lr.rules.recommend_total_stop_pct, 2),
                "copy_mode": r.get("copyMode"), "start_date": r.get("startDate"),
                "profit_shared": r.get("profitSharedAmount"), "positions": len(lr.positions),
            })
        self.store.set_runtime("accounts", accounts_view)
        return out

    def _sync_relations(self, rels: list[dict], now: float) -> None:
        """运行中发现新跟单 / 结束跟单：自动加入或移除监控（只针对自动发现的领航员）；
        并更新领航员的“跟单中 / 已停止跟单”状态（停止后数据默认保留，可在领航员页导出或删除）。"""
        live = {str(r.get("leadPortfolioId")): r for r in rels}
        ended = set(mark_copy_set(self.store, live, now))
        removed = self._discover(live, now) if self.cfg.auto_discover else set()
        for pid in sorted(ended - removed):  # 例如服务停机期间停止的跟单（监控里已经没有它）
            row = self.store.db.execute("SELECT name FROM leaders WHERE lead_id=?", (pid,)).fetchone()
            self.store.log_event("leader.ended", f"已停止跟单：{(row and row[0]) or pid}（数据继续保留，"
                                 "可在领航员页导出或删除）", leader_id=pid, ts=now)

    def _discover(self, live: dict, now: float) -> set:
        """自动加入新跟单、移除已结束的跟单。返回本次移除的领航员 ID。"""
        removed = set()
        configured = {lc.portfolio_id for lc in self.cfg.leaders}
        for pid, r in live.items():
            if any(lr.cfg.portfolio_id == pid for lr in self.leaders):
                continue
            name = str(r.get("nickname") or pid)
            c = LeaderConfig(portfolio_id=pid, name=name, source="copy")
            lr = LeaderRuntime(c, self.rules_provider(pid), source="copy",
                               copy_portfolio_id=str(r.get("copyPortfolioId") or ""), name=name)
            self.leaders.append(lr)
            self.store.log_event("leader.added", f"发现新的跟单：{name}，已加入监控", leader_id=pid, ts=now)
        for lr in list(self.leaders):
            pid = lr.cfg.portfolio_id
            if lr.source == "copy" and pid not in live and pid not in configured:
                self.leaders.remove(lr)
                if self.recorder:
                    self.recorder.close_leader(pid, lr.name, now)
                self.store.log_event("leader.removed", f"跟单已结束：{lr.name}，停止监控（数据继续保留，可在领航员页导出或删除）",
                                     leader_id=pid, ts=now)
                removed.add(pid)
        return removed

    def _process(self, lr: LeaderRuntime, positions: list[Position], now: float) -> list[Alert]:
        out: list[Alert] = []
        if lr.stale_alerted:
            lr.stale_alerted = False
            self.store.log_event("data.recovered", f"[{lr.name}] 数据已恢复", leader_id=lr.cfg.portfolio_id, ts=now)
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

        lr.positions = {p.key: p for p in positions}
        if self.recorder:
            self.recorder.observe(lr.cfg.portfolio_id, lr.name, positions, now)
        current = {p.key: p for p in net_by_symbol(positions)}  # 同币种多空合并后评估
        prefix = lr.cfg.portfolio_id + ":"
        prev_symbols = {k.split(":")[1] for k in self.states if k.startswith(prefix)}
        cur_symbols = {p.symbol for p in current.values()}
        summary: list[str] = []
        if not lr.initialized:
            self._drop_stale_states(current, now)
        residuals = (self.store.get_runtime("residuals", {})[0] or {}) if lr.source == "copy" else {}
        for key, pos in current.items():
            known = key in self.states
            st, alerts = evaluate(pos, self.states.get(key), lr.rules, now, self._margin_u(lr, pos))
            self.states[key] = st
            self.store.save_state(key, st.to_dict())
            # R12 残留仓位：领航员已经平仓，我的跟单仓位还在（后台对比领航员历史得出），每个回合只提醒一次
            legs = [pos.key] if not pos.is_hedge else [f"{prefix}{pos.symbol}:LONG", f"{prefix}{pos.symbol}:SHORT"]
            res = next((residuals[k] for k in legs if k in residuals), None)
            if res and st.fired.get("R12", {}).get("ref") != res["episode_id"]:
                st.fired["R12"] = {"version": st.version, "ts": now, "armed": False, "ref": res["episode_id"]}
                self.store.save_state(key, st.to_dict())
                alerts.append(Alert(
                    "R12", "warn", key, st.version, "残留仓位：领航员已经平仓，你的跟单仓位还在",
                    f"领航员平仓时间 {time.strftime('%m-%d %H:%M', time.localtime(res['leader_close_ms'] / 1000))}，"
                    f"平仓均价约 {res['leader_close_price']:.6g}",
                    "币安不会再替你平这个仓位，请在币安手动平仓（或确认你是有意继续持有）", now))
            if not known and pos.symbol in prev_symbols:
                # 同币种从单边变成对冲（或反过来）：不当作新开仓
                alerts = [a for a in alerts if a.rule_id != "R00"]
            if not lr.initialized and not known:
                # 启动时已存在的仓位：info 级提醒并入汇总，warn/urgent 仍单独发
                line = f"{pos.symbol} {self._side_cn(pos)} {pos.leverage:g}x ROI {pos.roi * 100:+.1f}%"
                if pos.source == "copy":
                    line += f"（{pos.unrealized_pnl:+.2f}U）"
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
        for key in [k for k in self.states if k.startswith(prefix) and k not in current]:
            st = self.states.pop(key)
            self.store.delete_state(key)
            if key.split(":")[1] in cur_symbols:
                continue  # 只是多空结构变化，币种仍在持仓
            out.append(self._send(Alert(
                "R11", "info", key, st.version, "仓位已平（或已不在列表中）",
                f"最后 ROI {st.last_roi * 100:+.1f}%，期间最高 {st.peak_roi * 100:+.1f}%、最低 {st.trough_roi * 100:+.1f}%",
                ts=now), lr))
        return out

    def _check_stale(self, lr: LeaderRuntime, now: float) -> list[Alert]:
        if lr.last_ok and not lr.stale_alerted and now - lr.last_ok > self.cfg.stale_seconds:
            lr.stale_alerted = True
            self.store.log_event("data.stale", f"[{lr.name}] 数据中断 {int(now - lr.last_ok)} 秒", "warn",
                                 leader_id=lr.cfg.portfolio_id, ts=now)
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
            for p in net_by_symbol(list(lr.positions.values())):
                if p.source == "copy":
                    est += p.unrealized_pnl
                elif lr.cfg.position_margin_u > 0:
                    est += p.roi * lr.cfg.position_margin_u
        self.portfolio, alert = evaluate_portfolio(est, self.cfg.loss_budget_u, self.portfolio, now)
        return [self._send(alert)] if alert else []

    STALE_STATE_SECONDS = 600

    def _drop_stale_states(self, current: dict[str, Position], now: float) -> None:
        """启动时：超过 10 分钟没观察到、且开仓价已经变了的状态视为旧仓位（停机期间已平仓又重新开），
        清掉后按新仓位处理，避免继承旧的“已通知”状态（参考 nofx-1 的 10 分钟过期）。"""
        for key, pos in current.items():
            st = self.states.get(key)
            if not st or now - st.last_seen <= self.STALE_STATE_SECONDS or pos.is_hedge:
                continue
            if st.entry_price > 0 and abs(pos.entry_price / st.entry_price - 1) > 1e-6:
                self.states.pop(key)
                self.store.delete_state(key)

    @staticmethod
    def _margin_u(lr: LeaderRuntime, pos: Position) -> float | None:
        """我在该仓位上的保证金（U），用于换算盈亏金额。"""
        if pos.source == "copy":
            return pos.margin or None
        return lr.cfg.position_margin_u or None

    @staticmethod
    def _side_cn(pos: Position) -> str:
        return {"LONG": "多", "SHORT": "空", "HEDGE": "多空对冲"}.get(pos.side, pos.side)

    def _auth_alert(self, reason: str = "") -> None:
        if self.guard:
            self.guard.report_failure(reason or "鉴权失败", self.clock())
            return
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
            head = f"[{lr.name}] {pos.symbol} {self._side_cn(pos)} {pos.leverage:g}x ｜{a.title}"
            lines.append(head)
            if pos.is_hedge:
                lines.append(f"{src}（多空合并）：现价 {pos.mark_price:.6g} 合计 ROI {pos.roi * 100:+.1f}%")
                lines.append("两条腿：" + describe_legs(list(lr.positions.values()), pos.symbol))
            else:
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
        """写入审计表并放进发件箱；本轮结束时由 flush() 统一投递（邮件会合并成一封）。"""
        msg = self.format(a, lr, pos)
        row_id = self.store.add_alert(a, msg, False, "待发送")
        self.outbox.append(Outgoing(row_id, msg, a.severity, a.ts or self.clock(), a.rule_id))
        leader_id = lr.cfg.portfolio_id if lr else None
        ep_ids = []
        if self.recorder and lr and pos:
            ep_ids = self.recorder.note_alert(leader_id, pos.symbol, a.rule_id, row_id)
        prefix = ""
        if lr and not a.title.startswith("["):
            prefix = f"[{lr.name}] " + (f"{pos.symbol} {self._side_cn(pos)} ｜" if pos else "")
        self.store.log_event(f"alert.{a.rule_id}", prefix + a.title,
                             a.severity, leader_id=leader_id, episode_id=ep_ids[0] if len(ep_ids) == 1 else None,
                             ref=f"alert:{row_id}", ts=a.ts or self.clock(), key=a.key,
                             suggestion=a.suggestion, episodes=ep_ids)
        return a

    def flush(self, now: float | None = None) -> None:
        items, self.outbox = self.outbox, []
        for it, ok, err in deliver_all(self.notifier, items, now if now is not None else self.clock()):
            self.store.update_delivery(it.row_id, ok, err)
            if not ok and not is_soft(err):  # 有意不发 / 等待合并不是故障
                print(f"通知未送达（已记录，可能稍后重试）：{err}")
                self.store.log_event("notify.failed", f"提醒未送达：{err[:120]}", "warn", ref=f"alert:{it.row_id}")

    def run(self) -> None:
        self.setup()
        while True:
            t0 = time.monotonic()
            try:
                self.poll_once()
            except Exception as e:  # noqa: BLE001  监控循环不能因单次异常退出
                print(f"轮询异常：{type(e).__name__}: {e}")
            time.sleep(max(1.0, self.cfg.poll_seconds - (time.monotonic() - t0)))
