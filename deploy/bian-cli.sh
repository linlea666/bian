#!/bin/sh
# /usr/local/bin/bian —— 以 bian 用户身份运行命令，自动读取 /opt/bian/.env
# 用法：bian doctor --test-email ｜ bian copies ｜ bian alerts ｜ bian backtest <领航员ID> ...
BIAN_HOME=/opt/bian
cd "$BIAN_HOME/data" 2>/dev/null || { echo "无法进入 $BIAN_HOME/data：请先运行安装脚本，并用 root（或 sudo）执行 bian 命令" >&2; exit 1; }
if [ "$(id -u)" = 0 ]; then
  exec runuser -u bian -- env PYTHONPATH="$BIAN_HOME" BIAN_ENV_FILE="$BIAN_HOME/.env" \
    python3 -m bian.cli "$@"
fi
exec env PYTHONPATH="$BIAN_HOME" BIAN_ENV_FILE="$BIAN_HOME/.env" python3 -m bian.cli "$@"
