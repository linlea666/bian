"""bian serve：监控线程 + 凭证守护 + 网页后台 + 看门狗，单进程运行。"""
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

    def start_background(self) -> None:
        for fn in (self.monitor_loop, self.watchdog_loop):
            threading.Thread(target=fn, daemon=True, name=fn.__name__).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
