#!/bin/bash
# /tmp 运行环境自愈恢复（2026-10-15）
# 背景：macOS 会不定期清理 /tmp，导致看板服务/任务脚本/凭证被删，看板直接挂掉。
# 用法：restore_tmp.sh            → 仅恢复缺失的文件（不覆盖已存在的）
#       restore_tmp.sh --force    → 全部覆盖重写
# 被 tunnel_watchdog.sh 自动调用：发现 /tmp/xlb_web/server.py 缺失即恢复
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
FORCE="${1:-}"
PY=/Users/youww/.workbuddy/binaries/python/versions/3.13.12/bin/python3
[ -x "$PY" ] || PY=$(command -v python3)

copy() {  # copy <src> <dst>
  local s="$1" d="$2"
  [ -f "$s" ] || return 0
  if [ "$FORCE" = "--force" ] || [ ! -f "$d" ]; then
    mkdir -p "$(dirname "$d")"
    cp -f "$s" "$d" && echo "恢复 $d"
  fi
}

mkdir -p /tmp/xlb_web
# 看板前端与后端
copy "$DIR/server.py"        /tmp/xlb_web/server.py
copy "$DIR/index.html"       /tmp/xlb_web/index.html
copy "$DIR/tunnel_url.js"    /tmp/xlb_web/tunnel_url.js
copy "$DIR/xlb_drivers.json" /tmp/xlb_web/xlb_drivers.json
# 任务脚本（server.py 的 SCRIPTS_DIR=/tmp）
for f in task1.py task2.py task3_direct.py task5.py task7.py xlb_login_api.py; do
  copy "$DIR/$f" "/tmp/$f"
done

# GitHub token（推 Pages 快照用）：缺失则从仓库外持久位置补
if [ -f "$HOME/.xlb_gh_token" ] && [ ! -f /tmp/gh_token.txt ]; then
  cp -f "$HOME/.xlb_gh_token" /tmp/gh_token.txt && echo "恢复 /tmp/gh_token.txt"
fi

# 凭证：缺失则用密码登录重建（脚本内无明文密码，参数来自 ~/.xlb_login.json）
if [ ! -f /tmp/xlb_auth.json ] && [ -f /tmp/xlb_login_api.py ] && [ -f "$HOME/.xlb_login.json" ]; then
  echo "凭证缺失，执行密码登录重建…"
  XLB_AUTH=/tmp/xlb_auth.json "$PY" /tmp/xlb_login_api.py >> /tmp/xlb_login_restore.log 2>&1 \
    && echo "凭证已重建" || echo "凭证重建失败（详见 /tmp/xlb_login_restore.log）"
fi

# 数据快照：本地缺失时从 Pages 拉最近一次（保证看板有数据可显示，不必等凭证）
for t in 1 2 3; do
  if [ ! -s "/tmp/xlb_web/data_task$t.json" ]; then
    curl -s --max-time 20 "https://vanessa-you.github.io/xlb-dashboard/data_task$t.json" \
      -o "/tmp/xlb_web/data_task$t.json" && echo "已从 Pages 拉取 data_task$t.json"
  fi
done
echo "restore_tmp 完成"
