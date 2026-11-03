"""bian serve：监控线程 + 凭证守护 + 后台工作线程（同步 / 离场判定 / 分析 / 摘要 / 备份）+ 网页后台 + 看门狗。"""
from __future__ import annotations

import os
import threading
import time

from .client import Client
from .config import AppConfig
from .credentials import CredentialGuard
from .lifecycle import EpisodeRecorder
from .monitor import Monitor
from .notify import EmailNotifier, MultiNotifier, StdoutNotifier, describe_smtp_error, smtp_send
from .rules import Alert
from .settings import Settings
from .store import LocalStore, Store


class App:
    def __init__(self, cfg: AppConfig, data_dir: str = ".", clock=time.time):
        self.cfg, self.clock = cfg, clock
        self.data_dir = os.path.abspath(data_dir)
        self.db_path = cfg.db_path if os.path.isabs(cfg.db_path) else os.path.join(self.data_dir, cfg.db_path)
        self.settings = Settings(self.db_path, os.path.join(self.data_dir, "secrets.json"))
        self.store = LocalStore(self.db_path)
        imported = self.settings.import_legacy(cfg)
        if imported:
            self.store.log_event("settings.imported", "已导入旧版配置：" + "、".join(imported))
        self.client = Client()
        self.guard = CredentialGuard(self.client, self.settings, self.store, self.send_urgent, clock,
                                     panel_url=cfg.panel_url)
        self.heartbeat = clock()
        self.worker_heartbeat = clock()
        self.started = clock()
        self.monitor: Monitor | None = None
        self._notifier_key = None
        self._notifier = None
        self._settings_version = -1.0
        self._stop = threading.Event()
        self._wake = threading.Event()

    # ------------------------------------------------------------ 通知
    def notifier(self, cfg: dict | None = None):
        """按网页配置构建通知器；配置没变时复用（保留邮件合并 / 重试状态）。"""
        cfg = cfg or self.settings.get()
        n = cfg["notify"]
        smtp = self.settings.smtp_config(cfg) if n["email_enabled"] else None
        key = (smtp and (smtp.host, smtp.port, smtp.user, smtp.password, tuple(smtp.to), smtp.security),
               n["email_min_severity"], n["email_max_per_hour"])
        if key != self._notifier_key:
            parts = [StdoutNotifier()]
            if smtp:
                parts.append(EmailNotifier(smtp, n["email_min_severity"], n["email_max_per_hour"]))
            self._notifier, self._notifier_key = MultiNotifier(parts), key
        return self._notifier

    def send_urgent(self, title: str, body: str, severity: str = "urgent") -> tuple[bool, str]:
        """凭证失效 / 恢复等系统通知：立即发送，不受邮件级别过滤和每小时上限限制。"""
        a = Alert("AUTH", severity, "credentials", 0, title, body, ts=self.clock())
        text = f"{title}\n{body}"
        print(text, flush=True)
        row_id = self.store.add_alert(a, text, False, "发送中")
        smtp = self.settings.smtp_config()
        if not smtp or not self.settings.get()["notify"]["email_enabled"]:
            self.store.update_delivery(row_id, False, "未配置邮箱，只记录在后台")
            return False, "未配置邮箱"
        try:
            smtp_send(smtp, ("🚨 " if severity == "urgent" else "ℹ️ ") + title,
                      body + "\n\n本邮件由 bian 币安跟单风控助手自动发送；系统只做提醒，不会自动下单或止损。")
            self.store.update_delivery(row_id, True, "")
            return True, ""
        except Exception as e:  # noqa: BLE001
            err = describe_smtp_error(e)
            self.store.update_delivery(row_id, False, "email: " + err)
            self.store.log_event("notify.failed", f"紧急邮件未送达：{err}", "warn", ref=f"alert:{row_id}")
            return False, err

    # ------------------------------------------------------------ 监控线程
    def apply_settings(self, force: bool = False) -> dict | None:
        v = self.settings.version()
        if not force and v == self._settings_version:
            return None
        self._settings_version = v
        cfg = self.settings.get()
        self.cfg.poll_seconds = float(cfg["monitor"]["poll_seconds"])
        self.cfg.loss_budget_u = float(cfg["monitor"]["loss_budget_u"])
        if self.monitor:
            self.monitor.notifier = self.notifier(cfg)
            self.monitor.refresh_rules()
        return cfg

    def wake(self) -> None:
        """网页保存配置 / 更新凭证后，立即开始下一轮，不等轮询间隔。"""
        self._wake.set()

    def monitor_loop(self) -> None:
        store = Store(self.db_path)  # 监控线程独占的连接
        cfg = self.apply_settings(force=True) or self.settings.get()
        self.monitor = Monitor(self.cfg, self.client, self.notifier(cfg), store, self.clock, guard=self.guard,
                               recorder=EpisodeRecorder(store, self.clock),
                               rules_provider=lambda pid: self.settings.rules_config(pid))
        store.log_event("service.start", "风控服务已启动")
        self.guard.tick(force=True)
        try:
            self.monitor.setup()
        except Exception as e:  # noqa: BLE001
            print(f"初始化异常：{type(e).__name__}: {e}", flush=True)
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.apply_settings()
                was_paused = self.guard.paused
                self.guard.tick()
                if was_paused and not self.guard.paused:
                    self.monitor.setup()  # 凭证恢复：重新识别跟单关系
                self.monitor.poll_once()
            except Exception as e:  # noqa: BLE001  单轮异常不能让监控退出
                print(f"轮询异常：{type(e).__name__}: {e}", flush=True)
                store.log_event("service.error", f"轮询异常：{type(e).__name__}: {e}", "warn")
            self.heartbeat = self.clock()
            wait = max(1.0, self.cfg.poll_seconds - (time.monotonic() - t0))
            self._wake.wait(wait)
            self._wake.clear()

    def watchdog_loop(self, limit_s: float = 180) -> None:
        """轮询线程卡住超过 limit_s 秒：记录事件后退出进程，交给 systemd 重启。"""
        while not self._stop.wait(15):
            stuck = self.clock() - self.heartbeat
            if stuck > max(limit_s, self.cfg.poll_seconds * 5):
                self.store.log_event("service.watchdog", f"轮询线程 {int(stuck)} 秒无响应，进程退出等待自动重启", "urgent")
                print(f"看门狗：轮询线程 {int(stuck)} 秒无响应，退出", flush=True)
                os._exit(3)

    # ------------------------------------------------------------ 后台工作线程（第二期）
    COPY_SYNC_S = 1800
    COPY_FULL_S = 86400
    HOT_LEADER_S = 600
    LEADER_DAILY_S = 86400

    def worker_loop(self) -> None:
        """与监控线程分开：独立数据库连接、独立请求客户端（限速），任何一步出错都不影响监控。"""
        from . import exits, jobs, sync
        from .api import copy_account
        from .diagnostics import deviation

        store = Store(self.db_path)
        public = Client(min_interval=1.0)
        private = Client(min_interval=1.0)
        klines = Client(min_interval=jobs.KLINE_INTERVAL_S)
        cache_dir = os.path.join(self.data_dir, ".cache", "klines")
        last = {"copy": 0.0, "copy_full": 0.0, "hot": 0.0, "daily": 0.0, "deviation": 0.0}
        for lc in self.cfg.leaders:  # config.toml 里观察的公开领航员
            if sync.ensure_leader(store, lc.portfolio_id, lc.name or lc.portfolio_id, "watch"):
                jobs.enqueue(store, lc.portfolio_id, "观察中的领航员")
        errors: dict[str, float] = {}

        def guarded(name: str, fn) -> None:
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                now = self.clock()
                if now - errors.get(name, 0) > 3600:  # 同类错误一小时只记一次
                    errors[name] = now
                    store.log_event("worker.error", f"后台任务“{name}”出错：{type(e).__name__}: {e}", "warn")
                print(f"后台任务 {name} 出错：{type(e).__name__}: {e}", flush=True)

        def copy_sync() -> None:
            now = self.clock()
            if self.guard.status.state != "valid":
                return
            pending_fill = store.db.execute(
                "SELECT COUNT(*) FROM exits WHERE status='pending' AND actual_u IS NULL").fetchone()[0]
            due = now - last["copy"] >= self.COPY_SYNC_S or (pending_fill and now - last["copy"] >= 300)
            if not due:
                return
            private.set_credentials(self.client.p20t, self.client.csrftoken)
            full = now - last["copy_full"] >= self.COPY_FULL_S
            # 先记下尝试时间：接口持续出错时按正常间隔重试，不会每 30 秒请求一次私有接口
            last["copy"] = now
            if full:
                last["copy_full"] = now
            for rel in copy_account.copy_relations(private):
                lid = str(rel.get("leadPortfolioId"))
                if sync.ensure_leader(store, lid, str(rel.get("nickname") or lid), "copy"):
                    jobs.enqueue(store, lid, "新跟单的领航员")
                # 每个跟单关系单独处理：一个出错不影响其他关系
                guarded(f"同步跟单 {lid}", lambda rel=rel: sync.sync_copy_relation(private, store, rel, full=full))

        def one_leader(lid: str) -> None:
            # 每个领航员单独处理：一个出错（例如已关闭带单）不影响其他领航员
            guarded(f"同步领航员历史 {lid}", lambda: sync.sync_leader_history(public, store, lid))

        def leader_sync() -> None:
            now = self.clock()
            if now - last["hot"] >= self.HOT_LEADER_S:
                hot = {r[0] for r in store.db.execute(
                    "SELECT DISTINCT lead_id FROM exits WHERE status IN ('pending','waiting') "
                    "UNION SELECT DISTINCT leader_id FROM episodes WHERE status='open' AND source='copy'")}
                for lid in hot:
                    one_leader(lid)
                last["hot"] = now
            for lid, synced in store.db.execute("SELECT lead_id, history_synced FROM leaders").fetchall():
                if not synced or now - synced >= self.LEADER_DAILY_S:
                    one_leader(lid)
                    # 失败时也记下时间，避免每轮重试同一个出错的领航员
                    store.db.execute("UPDATE leaders SET history_synced=? WHERE lead_id=?", (now, lid))
                    store.db.commit()
                    if jobs.needs_refresh(store, lid):
                        jobs.enqueue(store, lid, "有新的历史仓位")

        def judge() -> None:
            now = self.clock()
            poll = self.cfg.poll_seconds
            exits.resolve(store, poll, now)  # 结论写入 exits 表和事件日志，进统计页和每日摘要
            store.set_runtime("residuals", exits.residuals(store, poll, now))

        def daily_deviation() -> None:
            now = self.clock()
            if self.guard.status.state != "valid" or now - last["deviation"] < 86400:
                return
            private.set_credentials(self.client.p20t, self.client.csrftoken)
            last["deviation"] = now  # 先记下尝试时间，出错也不会频繁重试
            out = {}
            for d in deviation(private):
                bad = [x for x in d.legs if x.kind != "ok"]
                out[d.lead_id] = {"over": sum(1 for x in bad if x.kind == "over"),
                                  "under": sum(1 for x in bad if x.kind == "under"),
                                  "missing": sum(1 for x in bad if x.kind == "missing"),
                                  "residual": sum(1 for x in bad if x.kind == "residual"),
                                  "my_lev": d.my_eff_leverage, "leader_lev": d.leader_eff_leverage,
                                  "comparable": bool(d.position_show)}
            store.set_runtime("deviation", out)

        while not self._stop.is_set():
            guarded("同步我的跟单", copy_sync)
            guarded("同步领航员历史", leader_sync)
            guarded("离场判定", judge)
            guarded("跟单偏差", daily_deviation)
            guarded("每日维护", lambda: self.maintenance(store))
            pending = [False]

            def analyze() -> None:
                pending[0] = jobs.step(store, public, klines, 20.0, cache_dir)

            guarded("领航员分析", analyze)
            self.worker_heartbeat = self.clock()
            self._stop.wait(3 if pending[0] else 30)

    def maintenance(self, store, now: float | None = None) -> list[str]:
        """每天到点执行一次：自动备份、每日摘要。到点时服务没开，当天启动后补做。返回本次执行了什么。"""
        from .backup import backup
        from .digest import build

        now = now or self.clock()
        cfg = self.settings.get()["maintenance"]
        done, _ = store.get_runtime("maintenance", {})
        done = done or {}
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        hhmm = time.strftime("%H:%M", time.localtime(now))
        ran = []
        if cfg["backup_enabled"] and hhmm >= cfg["backup_time"] and done.get("backup") != today:
            done["backup"] = today  # 先记下：失败（如磁盘满）也不在当天反复重试，也不耽误后面的摘要
            try:
                path = backup(self.db_path, os.path.join(self.data_dir, "backups"), self.settings.secrets_path)
                store.log_event("maintenance.backup", f"自动备份完成：{os.path.basename(path)}")
            except Exception as e:  # noqa: BLE001
                store.log_event("maintenance.backup_failed", f"自动备份失败：{type(e).__name__}: {e}", "warn")
            ran.append("backup")
        if cfg["digest_enabled"] and hhmm >= cfg["digest_time"] and done.get("digest") != today:
            done["digest"] = today  # 先记下，发送失败也不在当天反复重发
            smtp = self.settings.smtp_config()
            if smtp and self.settings.get()["notify"]["email_enabled"]:
                subject, body = build(store, self.settings, now)
                try:
                    smtp_send(smtp, subject, body)
                    store.log_event("maintenance.digest", "每日摘要邮件已发送")
                except Exception as e:  # noqa: BLE001
                    store.log_event("maintenance.digest_failed", f"每日摘要发送失败：{describe_smtp_error(e)}", "warn")
            ran.append("digest")
        if ran:
            store.set_runtime("maintenance", done)
        return ran

    def start_background(self) -> None:
        for fn in (self.monitor_loop, self.watchdog_loop, self.worker_loop):
            threading.Thread(target=fn, daemon=True, name=fn.__name__).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
