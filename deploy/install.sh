#!/usr/bin/env bash
# bian 一键安装 / 更新（Ubuntu 22.04+ / 24.04）。可重复运行，不会覆盖已有的 .env、config.toml 和数据库。
#
#   curl -fsSL https://raw.githubusercontent.com/linlea666/bian/<分支>/deploy/install.sh -o /tmp/bian-install.sh
#   sudo bash /tmp/bian-install.sh
#
# 可选环境变量：BIAN_REPO（仓库地址）、BIAN_BRANCH（分支，默认仓库默认分支）、BIAN_HOME（默认 /opt/bian）
#
# 与同机其他项目（如 /opt/nofx 的 joz）完全隔离：独立目录、独立系统用户、独立 systemd 服务，
# 不使用 Docker，不监听任何端口，不需要修改安全组。
set -euo pipefail

REPO="${BIAN_REPO:-https://github.com/linlea666/bian.git}"
BRANCH="${BIAN_BRANCH:-}"
HOME_DIR="${BIAN_HOME:-/opt/bian}"
SVC_USER=bian
DATA="$HOME_DIR/data"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[警告]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo bash $0"
command -v systemctl >/dev/null || die "需要 systemd"

if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  [ "${ID:-}" = "ubuntu" ] || warn "未在 ${PRETTY_NAME:-该系统} 上验证过，仅保证 Ubuntu 22.04+/24.04"
fi

# ---- 依赖：只需要 python3 (>=3.11) 和 git，全部是标准库，不用 pip
NEED=()
command -v python3 >/dev/null || NEED+=(python3)
command -v git >/dev/null || NEED+=(git)
if [ "${#NEED[@]}" -gt 0 ]; then
  say "安装依赖：${NEED[*]}"
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${NEED[@]}"
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "需要 Python 3.11+，当前 $(python3 -V 2>&1)"

# ---- 代码：克隆或快进更新（只在本目录，不碰其他项目）
if [ -d "$HOME_DIR/.git" ]; then
  origin="$(git -C "$HOME_DIR" remote get-url origin 2>/dev/null || true)"
  case "$origin" in
    *linlea666/bian*|"$REPO") ;;
    *) die "$HOME_DIR 已存在但不是 bian 仓库（origin=$origin），为安全起见停止" ;;
  esac
  say "更新代码：$HOME_DIR"
  git -C "$HOME_DIR" fetch -q origin
  if [ -n "$BRANCH" ]; then
    git -C "$HOME_DIR" checkout -q "$BRANCH"
  fi
  git -C "$HOME_DIR" pull -q --ff-only || die "无法快进更新（本地有改动？）。请检查：git -C $HOME_DIR status"
elif [ -e "$HOME_DIR" ] && [ -n "$(ls -A "$HOME_DIR" 2>/dev/null)" ]; then
  die "$HOME_DIR 已存在且不是空目录，为安全起见停止"
else
  say "克隆代码到 $HOME_DIR"
  if [ -n "$BRANCH" ]; then
    git clone -q --depth 50 -b "$BRANCH" "$REPO" "$HOME_DIR"
  else
    git clone -q --depth 50 "$REPO" "$HOME_DIR"
  fi
fi

# ---- 系统用户与目录
if ! id "$SVC_USER" >/dev/null 2>&1; then
  say "创建系统用户 $SVC_USER"
  useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
fi
install -d -m 750 -o "$SVC_USER" -g "$SVC_USER" "$DATA"

if [ ! -f "$HOME_DIR/.env" ]; then
  say "生成 $HOME_DIR/.env（请填写凭证和邮箱）"
  install -m 600 -o "$SVC_USER" -g "$SVC_USER" "$HOME_DIR/deploy/env.example" "$HOME_DIR/.env"
  FIRST_ENV=1
else
  chown "$SVC_USER:$SVC_USER" "$HOME_DIR/.env" && chmod 600 "$HOME_DIR/.env"
fi
if [ ! -f "$DATA/config.toml" ]; then
  say "生成 $DATA/config.toml"
  install -m 640 -o "$SVC_USER" -g "$SVC_USER" "$HOME_DIR/config.example.toml" "$DATA/config.toml"
fi

# ---- 命令行包装与 systemd 服务
install -m 755 "$HOME_DIR/deploy/bian-cli.sh" /usr/local/bin/bian
install -m 644 "$HOME_DIR/deploy/bian.service" /etc/systemd/system/bian.service
systemctl daemon-reload
systemctl enable -q bian.service

say "自检（网络 / 凭证 / 邮件 / 配置）"
/usr/local/bin/bian doctor -c "$DATA/config.toml" || true

if [ "${FIRST_ENV:-0}" = 1 ] || ! grep -qE '^BINANCE_P20T=.+' "$HOME_DIR/.env"; then
  cat <<EOF

下一步：
  1. 编辑凭证与邮箱：   nano $HOME_DIR/.env
  2. 检查并发测试邮件： bian doctor --test-email
  3. 启动服务：         systemctl restart bian
  4. 查看运行日志：     journalctl -u bian -f
  （阈值等配置在 $DATA/config.toml，修改后 systemctl restart bian）
EOF
else
  systemctl restart bian.service
  say "已启动。查看日志：journalctl -u bian -f"
fi
