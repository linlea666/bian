#!/bin/sh
# /usr/local/bin/bian（由 start.sh 生成）—— 以 bian 用户身份运行命令行工具
# 例：bian doctor ｜ bian copies --diff ｜ bian backtest <领航员ID>
BIAN_HOME="@PROJECT_DIR@"
cd "$BIAN_HOME/data" 2>/dev/null || { echo "无法进入 $BIAN_HOME/data：请用 root（或 sudo）执行" >&2; exit 1; }
if [ "$(id -u)" = 0 ]; then
  exec runuser -u bian -- env PYTHONPATH="$BIAN_HOME" python3 -m bian.cli "$@"
fi
exec env PYTHONPATH="$BIAN_HOME" python3 -m bian.cli "$@"
