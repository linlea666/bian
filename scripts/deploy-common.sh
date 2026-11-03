#!/usr/bin/env bash
# install.sh / start.sh 共用的函数。所有路径都以脚本所在的仓库为根。
# shellcheck disable=SC2034
REPO_RE='^(https://github\.com/linlea666/bian(\.git)?|git@github\.com:linlea666/bian(\.git)?|ssh://git@github\.com/linlea666/bian(\.git)?)$'
SVC_USER=bian
SERVICE=bian.service

fail() { printf '\033[1;31m错误：\033[0m%s\n' "$*" >&2; exit 1; }
say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m注意：\033[0m%s\n' "$*"; }

need_root() { [[ "$(id -u)" == 0 ]] || fail "请用 root 运行：sudo $0 $*"; }

check_repo() {
    [[ "$(git -C "$PROJECT_DIR" rev-parse --show-toplevel)" == "$PROJECT_DIR" ]] || fail '目标不是独立的 bian 仓库'
    local remote
    remote=$(git -C "$PROJECT_DIR" remote get-url origin)
    [[ "$remote" =~ $REPO_RE ]] || fail "origin 必须是 linlea666/bian（当前：$remote）；不会自动修改仓库来源"
    [[ -z "$(git -C "$PROJECT_DIR" status --porcelain --untracked-files=no)" ]] \
        || fail '仓库里有被修改过的文件；请先处理（git -C '"$PROJECT_DIR"' status），脚本不会自动覆盖'
}

lock_deployment() {
    command -v flock >/dev/null || fail '需要 flock（Ubuntu：apt install util-linux）'
    exec 9>"$PROJECT_DIR/.git/bian-deploy.lock"
    flock -n 9 || fail '另一个安装 / 更新正在进行'
}

data_dir() { printf '%s/data' "$PROJECT_DIR"; }

web_port() {
    python3 - "$(data_dir)/config.toml" <<'PY' 2>/dev/null || echo 3100
import sys, tomllib
try:
    print(int(tomllib.load(open(sys.argv[1], "rb")).get("web", {}).get("port", 3100)))
except FileNotFoundError:
    print(3100)
PY
}

port_in_use() {
    python3 - "$1" <<'PY'
import socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # 只在真的有程序监听时才算占用（忽略 TIME_WAIT）
try:
    s.bind(("0.0.0.0", int(sys.argv[1])))
except OSError:
    sys.exit(0)
sys.exit(1)
PY
}

bian_cli() { (cd "$(data_dir)" && runuser -u "$SVC_USER" -- env PYTHONPATH="$PROJECT_DIR" python3 -m bian.cli "$@"); }

install_unit() {
    local tz
    tz=$(timedatectl show -p Timezone --value 2>/dev/null || true)
    [[ -n "$tz" && "$tz" != "Etc/UTC" && "$tz" != "UTC" ]] || tz=Asia/Shanghai
    sed -e "s#@PROJECT_DIR@#$PROJECT_DIR#g" -e "s#@TZ@#$tz#g" "$PROJECT_DIR/deploy/bian.service" > /etc/systemd/system/$SERVICE
    sed -e "s#@PROJECT_DIR@#$PROJECT_DIR#g" "$PROJECT_DIR/deploy/bian-cli.sh" > /usr/local/bin/bian
    chmod 755 /usr/local/bin/bian
    systemctl daemon-reload
    systemctl enable -q $SERVICE
}

verify_service() {
    local port i
    port=$(web_port)
    for i in $(seq 1 30); do
        if curl -fsS "http://127.0.0.1:$port/api/health" >/dev/null 2>&1; then
            say "健康检查通过（http://127.0.0.1:$port/api/health）"
            return 0
        fi
        sleep 1
    done
    systemctl --no-pager status $SERVICE | tail -20 || true
    fail "服务 30 秒内没有通过健康检查；查看日志：$PROJECT_DIR/start.sh logs"
}

print_access() {
    local port ip
    port=$(web_port)
    ip=$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')
    say "后台地址：http://${ip:-服务器IP}:$port"
    warn "请在云服务器安全组里放行 TCP $port，建议只允许你自己的 IP 访问。"
}
