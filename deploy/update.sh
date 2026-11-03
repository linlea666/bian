#!/usr/bin/env bash
# 更新 bian：拉取代码并重启服务（几秒钟完成，不影响同机的其他项目）。
#   sudo bash /opt/bian/deploy/update.sh
set -euo pipefail
HOME_DIR="${BIAN_HOME:-/opt/bian}"
[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行：sudo bash $0" >&2; exit 1; }
# 复用安装脚本：它会快进更新代码、刷新服务文件，并保留 .env / config.toml / 数据库
exec bash "$HOME_DIR/deploy/install.sh"
