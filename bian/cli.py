"""命令行入口：bian probe | leaders | profile | backtest | copies | monitor | alerts"""
from __future__ import annotations

import argparse
import os
import sys

from .client import AuthExpired, BinanceError, Client


def _client() -> Client:
    return Client.from_env()


def _write(text: str, out: str | None) -> None:
    print(text)
    if out:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w") as f:
            f.write(text + "\n")
        print(f"\n已写入 {out}")


def cmd_probe(a) -> int:
    from .probe import run_probe

    _write("\n".join(run_probe(_client(), a.pid)), a.out)
    return 0


def cmd_leaders(a) -> int:
    from .api.leaders import leaderboard

    total, rows = leaderboard(_client(), a.range, a.sort, a.page, a.size)
    print(f"共 {total} 位（{a.range}，按 {a.sort} 排序）")
    print(f"{'portfolioId':<22}{'ROI%':>10}{'MDD%':>8}{'胜率%':>8}{'跟单者盈亏U':>14}{'人数':>10}  昵称")
    for x in rows:
        print(f"{x.portfolio_id:<22}{x.roi:>10.1f}{x.mdd:>8.1f}{x.win_rate:>8.1f}{x.copier_pnl:>14.0f}"
              f"{f'{x.current_copy_count}/{x.max_copy_count}':>10}  {x.nickname}")
    return 0


def _progress(i: int, n: int) -> None:
    if i == n or i % 10 == 0:
        print(f"  K 线路径 {i}/{n}", file=sys.stderr)


def cmd_profile(a) -> int:
    from .analysis import analyze, format_profile

    res = analyze(_client(), a.pid, a.days, with_paths=not a.fast, progress=_progress)
    _write(format_profile(res["name"], a.pid, res["profile"]), a.out)
    return 0


def cmd_backtest(a) -> int:
    from .analysis import analyze, format_backtest, format_profile
    from .backtest import Costs

    costs = Costs(fee_rate=a.fee, stop_slippage=a.slippage)
    res = analyze(_client(), a.pid, a.days, costs=costs, progress=_progress)
    if "grid" not in res:
        print("没有可回测的历史仓位")
        return 1
    text = format_profile(res["name"], a.pid, res["profile"]) + "\n\n" + format_backtest(
        res["name"], res["grid"], costs, a.top, len(res["paths"]), res["profile"].get("adds_share", 0))
    if res["errors"]:
        text += f"\n\n（{len(res['errors'])} 笔因 K 线缺失跳过）"
    _write(text, a.out)
    return 0


def cmd_copies(a) -> int:
    from .api.copy_account import copy_positions, copy_relations, relation_summary
    from .rules import AccountSnapshot

    c = _client()
    if a.diff:
        from .diagnostics import deviation, format_deviation

        _write(format_deviation(deviation(c)), a.out)
        return 0
    for r in copy_relations(c):
        s = relation_summary(r)
        print(f"{s['nickname']}（{s['leadPortfolioId']}）权益 {s['marginBalance']:.2f}U 净投入 {s['netCopyAmount']:.2f}U "
              f"浮盈亏 {s['unrealizedPnl']:+.2f}U 已实现 {s['realizedPnl']:+.2f}U 模式 {s['copyMode']}"
              f"{' 已暂停' if s['isPaused'] else ''}")
        acct = AccountSnapshot.from_relation(r)
        print("   总止损：" + (f"{acct.native_stop_u:.0f}U（权益跌到约 {acct.native_stop_trigger_equity:.2f}U 触发，{acct.disposal}）"
                              if acct.has_native_stop else "未设置"))
        for p in copy_positions(c, str(r.get("copyPortfolioId")), str(r.get("leadPortfolioId"))):
            liq = f"{p.liquidation_price:.6g}" if p.liquidation_valid() else "-"
            print(f"   {p.symbol} {p.side} {p.leverage:g}x 数量 {p.qty:g} 开仓 {p.entry_price:.6g} 现价 {p.mark_price:.6g}"
                  f" 保证金 {p.margin:.2f}U ROI {p.roi * 100:+.1f}% 浮盈亏 {p.unrealized_pnl:+.2f}U 强平 {liq}")
    return 0


def cmd_costs(a) -> int:
    from .diagnostics import costs, format_costs

    _write(format_costs(costs(_client())), a.out)
    return 0


def build_notifier(cfg, stdout_only: bool = False):
    from .notify import EmailNotifier, MultiNotifier, SmtpConfig, StdoutNotifier, TelegramNotifier

    notifiers = []
    if cfg.stdout or stdout_only:
        notifiers.append(StdoutNotifier())
    if not stdout_only:
        if cfg.email:
            smtp = SmtpConfig.from_env()
            if smtp:
                notifiers.append(EmailNotifier(smtp, cfg.email_min_severity, cfg.email_max_per_hour))
                print(f"邮件通知：{smtp.describe()}（级别 ≥ {cfg.email_min_severity}，每小时最多 {cfg.email_max_per_hour} 封）")
            else:
                print("未设置 SMTP_HOST / SMTP_USER / SMTP_PASS，不发邮件")
        if cfg.telegram:
            tg = TelegramNotifier.from_env()
            if tg:
                notifiers.append(tg)
    if not notifiers:
        notifiers.append(StdoutNotifier())
    return MultiNotifier(notifiers)


def cmd_monitor(a) -> int:
    from .config import load_config
    from .monitor import Monitor
    from .store import Store

    cfg = load_config(a.config)
    notifier = build_notifier(cfg, a.notify == "stdout")
    mon = Monitor(cfg, _client(), notifier, Store(cfg.db_path))
    if a.once:
        mon.setup()
        mon.poll_once()
        return 0
    try:
        mon.run()
    except KeyboardInterrupt:
        print("已停止")
    return 0


def _load_cfg_or_default(path: str):
    import os

    from .config import AppConfig, load_config

    return load_config(path) if os.path.exists(path) else AppConfig()


def cmd_serve(a) -> int:
    import signal

    from .app import App
    from .store import Store
    from .web import auth
    from .web.server import make_server

    cfg = _load_cfg_or_default(a.config)
    if a.port:
        cfg.web_port = a.port
    if a.host:
        cfg.web_host = a.host
    app = App(cfg, data_dir=".")
    store = Store(app.db_path)
    if not auth.has_user(store):
        pw = auth.random_password()
        auth.set_password(store, "admin", pw, must_change=True)
        print(f"已创建后台账号 admin，初始密码：{pw}（登录后请修改；也可以用 bian passwd 重置）", flush=True)
    srv = make_server(app, cfg.web_host, cfg.web_port)
    import threading

    def _term(*_):
        # shutdown() 会等待 serve_forever 退出，必须在另一个线程里调用，否则主线程自己等自己
        app.stop()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _term)
    app.start_background()
    print(f"风控后台已启动：http://{cfg.web_host}:{cfg.web_port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        app.store.log_event("service.stop", "风控服务已停止")
    return 0


def cmd_passwd(a) -> int:
    import getpass
    import os

    from .app import App  # noqa: F401  确保表结构存在
    from .store import Store
    from .web import auth

    cfg = _load_cfg_or_default(a.config)
    db = cfg.db_path if os.path.isabs(cfg.db_path) else os.path.join(".", cfg.db_path)
    store = Store(db)
    if a.if_missing and auth.has_user(store):
        print("后台账号已存在，未修改")
        return 0
    if a.random or a.if_missing:
        pw = auth.random_password()
    else:
        pw = getpass.getpass("新密码：")
        if pw != getpass.getpass("再输入一次："):
            print("两次输入不一致", file=sys.stderr)
            return 1
        problem = auth.password_problem(pw)
        if problem:
            print(problem, file=sys.stderr)
            return 1
    auth.set_password(store, a.username, pw, must_change=bool(a.random or a.if_missing))
    store.log_event("auth.password_reset", f"命令行重置后台密码（{a.username}）")
    if a.random or a.if_missing:
        print(f"后台账号：{a.username}\n密码：{pw}\n（首次登录后会要求修改密码）")
    else:
        print("密码已修改")
    return 0


def cmd_backup(a) -> int:
    import os

    from .backup import backup

    cfg = _load_cfg_or_default(a.config)
    db = cfg.db_path if os.path.isabs(cfg.db_path) else os.path.join(".", cfg.db_path)
    path = backup(db, os.path.join(os.path.dirname(os.path.abspath(db)), "backups"),
                  os.path.join(os.path.dirname(os.path.abspath(db)), "secrets.json"), a.keep)
    print(f"已备份到 {path}")
    return 0


def cmd_doctor(a) -> int:
    from .doctor import run_doctor

    lines, fails = run_doctor(_client(), a.config, a.test_email)
    print("\n".join(lines))
    return 1 if fails else 0


def cmd_alerts(a) -> int:
    import time

    from .config import load_config
    from .store import Store

    for ts, sev, rule, key, title, ok, err in Store(load_config(a.config).db_path).recent_alerts(a.limit):
        note = ""
        if not ok:
            note = f"  [未送达：{err}]"
        elif err and "低于邮件通知级别" not in err:
            note = f"  [部分渠道：{err}]"
        print(f"{time.strftime('%m-%d %H:%M:%S', time.localtime(ts))} {sev:<6} {rule:<4} {key} {title}{note}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bian", description="币安合约跟单风控助手（只读，不下单）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="探测接口可用性")
    p.add_argument("--pid", help="用于测试的领航员 portfolioId（默认自动找一个公开持仓的）")
    p.add_argument("--out", help="把结果写入 Markdown 文件")
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("leaders", help="领航员榜单")
    p.add_argument("--range", default="30D", choices=["7D", "30D", "90D", "180D"])
    p.add_argument("--sort", default="ROI", help="ROI / PNL / MDD / WIN_RATE / AUM / COPIER_PNL")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--size", type=int, default=20)
    p.set_defaults(fn=cmd_leaders)

    for name, fn, helptext in (("profile", cmd_profile, "领航员画像"), ("backtest", cmd_backtest, "画像 + 止损规则回测")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("pid", help="领航员 portfolioId（领航员主页 URL 里的数字）")
        p.add_argument("--days", type=int, help="只看最近 N 天")
        p.add_argument("--out", help="报告写入文件（Markdown）")
        if name == "profile":
            p.add_argument("--fast", action="store_true", help="不拉 K 线（不算 MAE/MFE）")
        else:
            p.add_argument("--fee", type=float, default=0.0005, help="单边手续费率，默认 0.0005")
            p.add_argument("--slippage", type=float, default=0.0005, help="规则出场滑点，默认 0.0005")
            p.add_argument("--top", type=int, default=10)
        p.set_defaults(fn=fn)

    p = sub.add_parser("copies", help="我的跟单关系与仓位（需凭证）")
    p.add_argument("--diff", action="store_true", help="跟单偏差报告：超配/低配/漏单/残留、实际杠杆对比")
    p.add_argument("--out", help="报告写入文件（Markdown）")
    p.set_defaults(fn=cmd_copies)

    p = sub.add_parser("costs", help="跟单损耗报告：手续费、分润、相对领航员的滑点（需凭证）")
    p.add_argument("--out", help="报告写入文件（Markdown）")
    p.set_defaults(fn=cmd_costs)

    p = sub.add_parser("monitor", help="实时监控与提醒")
    p.add_argument("-c", "--config", default="config.toml")
    p.add_argument("--notify", choices=["all", "stdout"], default="all")
    p.add_argument("--once", action="store_true", help="只轮询一次（测试用）")
    p.set_defaults(fn=cmd_monitor)

    p = sub.add_parser("serve", help="启动风控后台（网页 + 监控 + 凭证守护）")
    p.add_argument("-c", "--config", default="config.toml")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("passwd", help="设置 / 重置后台登录密码")
    p.add_argument("-c", "--config", default="config.toml")
    p.add_argument("--username", default="admin")
    p.add_argument("--random", action="store_true", help="生成随机密码并打印（首次登录需修改）")
    p.add_argument("--if-missing", action="store_true", help="仅在还没有账号时创建（安装脚本使用）")
    p.set_defaults(fn=cmd_passwd)

    p = sub.add_parser("backup", help="备份数据库和设置（保留最近 N 份）")
    p.add_argument("-c", "--config", default="config.toml")
    p.add_argument("--keep", type=int, default=7)
    p.set_defaults(fn=cmd_backup)

    p = sub.add_parser("doctor", help="部署自检：网络、凭证、邮件、配置")
    p.add_argument("-c", "--config", default="config.toml")
    p.add_argument("--test-email", action="store_true", help="发送一封测试邮件")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("alerts", help="查看最近提醒记录")
    p.add_argument("-c", "--config", default="config.toml")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=cmd_alerts)

    a = ap.parse_args(argv)
    from .envfile import load_env_file

    load_env_file()
    try:
        return a.fn(a)
    except AuthExpired as e:
        print(f"凭证错误：{e}。请设置/更新环境变量 BINANCE_P20T 和 BINANCE_CSRFTOKEN", file=sys.stderr)
        return 2
    except BinanceError as e:
        print(f"币安接口错误：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
