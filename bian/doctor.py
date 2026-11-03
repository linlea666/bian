"""部署自检：逐项检查运行环境、网络、凭证、邮件和配置。输出不含凭证。"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
import urllib.request

from .api import copy_account, leaders as leaders_api
from .api.market import mark_prices
from .client import AuthExpired, BinanceError, Client

OK, WARN, FAIL = "✅", "⚠️", "❌"


def run_doctor(client: Client, config_path: str | None = None, test_email: bool = False) -> tuple[list[str], int]:
    lines: list[str] = []
    fails = 0

    def item(mark: str, name: str, note: str = "") -> None:
        nonlocal fails
        if mark == FAIL:
            fails += 1
        lines.append(f"{mark} {name}" + (f"：{note}" if note else ""))

    v = sys.version_info
    item(OK if v >= (3, 11) else FAIL, "Python 版本", f"{v.major}.{v.minor}.{v.micro}（需要 ≥ 3.11）")

    try:
        total, _ = leaders_api.leaderboard(client, page_size=1)
        item(OK, "币安公共接口 www.binance.com/bapi", f"可访问（领航员 {total} 位）")
    except BinanceError as e:
        item(FAIL, "币安公共接口 www.binance.com/bapi", f"{e}（服务器所在地区可能被币安限制）")

    try:
        n = len(mark_prices(client))
        item(OK, "行情 www.binance.com/fapi", f"{n} 个合约标记价")
    except BinanceError as e:
        item(FAIL, "行情 www.binance.com/fapi", str(e))
    try:
        urllib.request.urlopen("https://fapi.binance.com/fapi/v1/time", timeout=10).read()
        item(OK, "fapi.binance.com（仅供参考，本工具不依赖）", "可访问")
    except Exception as e:  # noqa: BLE001
        item(WARN, "fapi.binance.com（仅供参考，本工具不依赖）", f"不可访问：{getattr(e, 'code', type(e).__name__)}")

    settings = None
    if config_path and os.path.exists(config_path):
        try:
            from .config import load_config
            from .settings import Settings

            cfg0 = load_config(config_path)
            db = cfg0.db_path if os.path.isabs(cfg0.db_path) else os.path.join(os.path.dirname(os.path.abspath(config_path)), cfg0.db_path)
            if os.path.exists(db):
                settings = Settings(db, os.path.join(os.path.dirname(db), "secrets.json"))
        except Exception:  # noqa: BLE001
            settings = None
    if settings and not client.has_credentials:
        client.set_credentials(*settings.binance_credentials())

    if client.has_credentials:
        try:
            if copy_account.check_auth(client):
                rels = copy_account.copy_relations(client)
                item(OK, "币安登录凭证", f"有效；进行中的跟单 {len(rels)} 个")
            else:
                item(FAIL, "币安登录凭证", "无效")
        except AuthExpired:
            item(FAIL, "币安登录凭证", "已过期或无效：登录后台 → 设置 → 币安凭证，粘贴新的 cURL")
        except BinanceError as e:
            item(FAIL, "币安登录凭证", str(e))
    else:
        item(WARN, "币安登录凭证", "未设置：登录后台 → 设置 → 币安凭证（或首次引导）粘贴 cURL")

    from .notify import SmtpConfig, describe_smtp_error, smtp_send

    try:
        smtp = settings.smtp_config() if settings else SmtpConfig.from_env()
    except ValueError as e:
        smtp = None
        item(FAIL, "邮件配置", str(e))
    else:
        if not smtp:
            item(WARN, "邮件配置", "未设置：登录后台 → 设置 → 邮件提醒")
        elif test_email:
            try:
                smtp_send(smtp, "✅ 币安跟单风控：测试邮件",
                          f"这是一封测试邮件，发送时间 {time.strftime('%Y-%m-%d %H:%M:%S')}。\n"
                          "收到说明邮件提醒配置正确；如果在垃圾箱里找到，请把发件地址加入白名单。")
                item(OK, "邮件发送", f"已交给邮件服务器（{smtp.describe()}），请检查收件箱/垃圾箱")
            except Exception as e:  # noqa: BLE001
                item(FAIL, "邮件发送", describe_smtp_error(e))
        else:
            item(OK, "邮件配置", f"{smtp.describe()}（加 --test-email 发送测试邮件）")

    if config_path:
        if os.path.exists(config_path):
            try:
                from .config import load_config

                cfg = load_config(config_path)
                tiers = settings.rules_config() if settings else cfg.rules
                item(OK, "配置文件", f"{config_path}：后台端口 {cfg.web_port}，自动发现跟单={'开' if cfg.auto_discover else '关'}，"
                     f"亏损档位 {list(tiers.loss_tiers)}（{tiers.loss_tiers_mode}）")
                try:
                    db = sqlite3.connect(cfg.db_path)
                    db.execute("CREATE TABLE IF NOT EXISTS _doctor (x)")
                    db.execute("DROP TABLE _doctor")
                    db.close()
                    item(OK, "数据库", f"{cfg.db_path} 可写")
                except sqlite3.Error as e:
                    item(FAIL, "数据库", f"{cfg.db_path} 不可写：{e}")
            except Exception as e:  # noqa: BLE001
                item(FAIL, "配置文件", f"{config_path} 解析失败：{e}")
        else:
            item(WARN, "配置文件", f"{config_path} 不存在，monitor 需要它（可从 config.example.toml 复制）")
    lines.append("")
    lines.append("全部通过" if not fails else f"{fails} 项失败")
    return lines, fails
