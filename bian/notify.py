"""通知渠道：控制台、Telegram。发送失败会重试，并把结果如实返回（不把“已提交”当“已送达”）。"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

ICON = {"info": "ℹ️", "warn": "⚠️", "urgent": "🚨"}


class Notifier:
    name = "base"

    def send(self, text: str, severity: str = "info") -> tuple[bool, str]:
        raise NotImplementedError


class StdoutNotifier(Notifier):
    name = "stdout"

    def send(self, text: str, severity: str = "info") -> tuple[bool, str]:
        print(f"{ICON.get(severity, '')} {text}\n{'-' * 40}", file=sys.stdout, flush=True)
        return True, ""


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, token: str, chat_id: str, retries: int = 3, timeout: float = 10):
        self.token, self.chat_id, self.retries, self.timeout = token, chat_id, retries, timeout

    @classmethod
    def from_env(cls) -> "TelegramNotifier | None":
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        return cls(token, chat) if token and chat else None

    def send(self, text: str, severity: str = "info") -> tuple[bool, str]:
        body = json.dumps({
            "chat_id": self.chat_id,
            "text": f"{ICON.get(severity, '')} {text}"[:4000],
            "disable_web_page_preview": True,
            "disable_notification": severity == "info",
        }).encode()
        err = ""
        for i in range(self.retries):
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data=body, headers={"content-type": "application/json"}, method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if json.loads(resp.read()).get("ok"):
                        return True, ""
                    err = "telegram 返回 ok=false"
            except urllib.error.HTTPError as e:
                err = f"telegram HTTP {e.code}"  # 不打印 URL（含 token）
                if e.code == 429:
                    time.sleep(5)
            except Exception as e:  # noqa: BLE001
                err = f"telegram 网络错误: {type(e).__name__}"
            time.sleep(2 ** i)
        return False, err


class MultiNotifier(Notifier):
    name = "multi"

    def __init__(self, notifiers: list[Notifier]):
        self.notifiers = notifiers

    def send(self, text: str, severity: str = "info") -> tuple[bool, str]:
        oks, errs = [], []
        for n in self.notifiers:
            ok, err = n.send(text, severity)
            oks.append(ok)
            if err:
                errs.append(f"{n.name}: {err}")
        return any(oks), "; ".join(errs)
