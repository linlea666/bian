#!/usr/bin/env bash
# bian 风控作战室 一键安装（Ubuntu 22.04 / 24.04）。可重复运行，不会删除已有数据。
#   curl -fsSL https://raw.githubusercontent.com/linlea666/bian/main/install.sh -o /tmp/bian-install.sh
#   sudo bash /tmp/bian-install.sh            # 默认装到 /opt/bian
# 与同机其他项目（如 /opt/nofx 的 joz）完全隔离：独立目录、独立系统用户、独立 systemd 服务，不用 Docker。
set -euo pipefail
umask 022
fail() { printf '安装失败：%s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || fail '请使用 sudo bash install.sh [安装目录]'
[[ -r /etc/os-release ]] || fail '无法识别操作系统'
# shellcheck disable=SC1091
source /etc/os-release
[[ "${ID:-}" == ubuntu ]] || fail "目前只支持 Ubuntu 22.04 / 24.04（当前：${PRETTY_NAME:-未知}）"
TARGET=${1:-/opt/bian}
BRANCH=${BIAN_BRANCH:-main}
[[ "$TARGET" == /* && "$TARGET" != / ]] || fail '安装目录必须是绝对路径，且不能是根目录'
mkdir -p "$(dirname "$TARGET")"
command -v flock >/dev/null || fail '缺少 flock（apt install util-linux）'
exec 8>"$(dirname "$TARGET")/.bian-install.lock"
flock -n 8 || fail '另一个安装正在进行'
available=$(df -Pk "$(dirname "$TARGET")" | awk 'END {print $4}')
(( available >= 1024 * 1024 )) || fail '至少需要 1 GiB 可用磁盘'
memory=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
(( memory >= 512 * 1024 )) || fail '至少需要 512 MiB 内存'
if [[ -e "$TARGET" && ! -d "$TARGET/.git" ]]; then
    [[ -d "$TARGET" && -z "$(find "$TARGET" -mindepth 1 -maxdepth 1 -print -quit)" ]] || fail "$TARGET 已有其他内容，不覆盖"
fi
need=()
for c in git curl python3; do command -v "$c" >/dev/null || need+=("$c"); done
if (( ${#need[@]} )); then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ca-certificates "${need[@]}"
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || fail "需要 Python 3.11+（当前 $(python3 -V 2>&1)）"
if [[ ! -d "$TARGET/.git" ]]; then
    git clone -q --branch "$BRANCH" --single-branch https://github.com/linlea666/bian.git "$TARGET"
fi
PROJECT_DIR=$(cd "$TARGET" && pwd -P)
# 校验来源后才执行目录里的任何脚本
case "$(git -C "$PROJECT_DIR" remote get-url origin)" in
    https://github.com/linlea666/bian|https://github.com/linlea666/bian.git|git@github.com:linlea666/bian.git|ssh://git@github.com/linlea666/bian.git) ;;
    *) fail '目标目录不是 linlea666/bian 仓库' ;;
esac
# 已安装的目录只做配置和服务刷新；更新代码请用 ./start.sh update
exec bash "$PROJECT_DIR/start.sh" install
