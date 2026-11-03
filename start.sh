#!/usr/bin/env bash
# bian 风控作战室 运维脚本（参考 joz 的 start.sh）
#   ./start.sh status | logs | restart | stop | start | update | doctor | password | backup
set -euo pipefail
PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
export PROJECT_DIR
# shellcheck source=scripts/deploy-common.sh
source "$PROJECT_DIR/scripts/deploy-common.sh"
cd "$PROJECT_DIR"

usage() {
    cat <<'TXT'
用法：./start.sh <命令>
  status     服务状态、健康检查、凭证与轮询情况
  logs       实时查看运行日志（Ctrl+C 退出）
  restart    重启服务
  stop       停止服务（数据保留）
  start      启动服务
  update     拉取最新代码并重启（只做快进合并，保留数据和设置，几秒钟完成）
  doctor     自检：网络、币安凭证、邮件、配置
  password   重置后台登录密码（打印新的随机密码）
  backup     立即备份数据库和设置（保留最近 7 份）
TXT
}

cmd=${1:-status}
case "$cmd" in
    help|-h|--help) usage; exit 0 ;;
esac
need_root "$@"

case "$cmd" in
    install)
        # 由 install.sh 调用：创建用户 / 目录 / 配置 / 服务，并启动
        lock_deployment
        check_repo
        if ! id "$SVC_USER" >/dev/null 2>&1; then
            say "创建系统用户 $SVC_USER"
            useradd --system --home-dir "$(data_dir)" --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
        fi
        install -d -m 750 -o "$SVC_USER" -g "$SVC_USER" "$(data_dir)"
        if [[ ! -f "$(data_dir)/config.toml" ]]; then
            install -m 640 -o "$SVC_USER" -g "$SVC_USER" "$PROJECT_DIR/config.example.toml" "$(data_dir)/config.toml"
        fi
        port=$(web_port)
        if ! systemctl is-active -q "$SERVICE" && port_in_use "$port"; then
            fail "端口 $port 已被其他程序占用；请修改 $(data_dir)/config.toml 里 [web] 的 port 后重试"
        fi
        pwinfo=$(bian_cli passwd --if-missing -c "$(data_dir)/config.toml")
        install_unit
        systemctl restart "$SERVICE"
        verify_service
        print_access
        if grep -q '密码：' <<<"$pwinfo"; then
            printf '\n%s\n\n' "$pwinfo"
            warn '请记下上面的初始密码，首次登录后会要求修改。忘记密码可执行：./start.sh password'
        fi
        say '打开后台，按引导完成：粘贴币安凭证 → 设置邮箱 → 选择风控预设'
        ;;
    update)
        lock_deployment
        check_repo
        branch=$(git branch --show-current)
        [[ -n "$branch" ]] || fail '当前不在任何分支上'
        say "拉取 origin/$branch"
        git fetch -q origin "$branch"
        git merge-base --is-ancestor HEAD "origin/$branch" || fail "本地 $branch 有 origin 上没有的提交；停止更新"
        if [[ "$(git rev-parse HEAD)" == "$(git rev-parse "origin/$branch")" ]]; then
            say '已经是最新版本'
        else
            git merge -q --ff-only "origin/$branch"
            say "已更新到 $(git log -1 --format='%h %s')"
        fi
        # 用更新后的脚本继续（FD 9 保留部署锁）
        exec bash "$PROJECT_DIR/start.sh" --continue-update "$(git rev-parse HEAD)"
        ;;
    --continue-update)
        [[ -e /dev/fd/9 ]] || fail '此入口只允许由 update 调用'
        [[ "$(git rev-parse HEAD)" == "${2:-}" ]] || fail '更新过程中版本发生变化'
        install_unit
        systemctl restart "$SERVICE"
        verify_service
        say '更新完成，数据和设置都已保留'
        ;;
    start|restart)
        [[ -f /etc/systemd/system/$SERVICE ]] || fail '尚未安装，请先运行 install.sh'
        systemctl "$cmd" "$SERVICE"
        verify_service
        ;;
    stop)
        systemctl stop "$SERVICE"
        say '已停止（数据保留）。再次启动：./start.sh start'
        ;;
    status)
        systemctl --no-pager status "$SERVICE" | head -12 || true
        port=$(web_port)
        if curl -fsS "http://127.0.0.1:$port/api/health" 2>/dev/null; then echo; else warn '健康检查未通过'; fi
        ;;
    logs)
        journalctl -u "$SERVICE" -n 200 -f
        ;;
    doctor)
        bian_cli doctor -c "$(data_dir)/config.toml"
        ;;
    password)
        bian_cli passwd --random -c "$(data_dir)/config.toml"
        ;;
    backup)
        bian_cli backup -c "$(data_dir)/config.toml"
        ;;
    *)
        usage
        fail "未知命令：$cmd"
        ;;
esac
