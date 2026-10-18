#!/bin/bash
# 隧道看门狗（单次检查版，由 cron 每分钟调用）
# 1) 看护本机看板服务(8787)：无响应则拉起
# 2) 看护【命名隧道】xlb.hepinggh.cn（2026-10-19 起，固定地址不再换址）：
#    连续 3 次探测失败 → 重启命名隧道（地址不变，无需推送 Pages）
#    （旧快速隧道 trycloudflare.com 已废弃：免费临时地址 18分钟~4小时就被回收，
#     导致看板频繁「更新失败」；命名隧道基于用户域名 hepinggh.cn 永久有效）
# 日志 /tmp/xlb_watchdog.log；失败计数 /tmp/xlb_watchdog.fail；冷却时间戳 /tmp/xlb_watchdog.restart
DIR="$(cd "$(dirname "$0")" && pwd)"
CF="$DIR/cloudflared"
[ -x "$CF" ] || CF=/tmp/cloudflared
LOG=/tmp/xlb_watchdog.log
FAILFILE=/tmp/xlb_watchdog.fail
RESTARTFILE=/tmp/xlb_watchdog.restart
TUNLOG=/tmp/xlb_tunnel.log
MAXFAIL=3
COOLDOWN=300
TUNURL="https://xlb.hepinggh.cn"          # 固定隧道入口
TUNNAME="xlb-dashboard"                    # 命名隧道名（凭证 ~/.cloudflared/）
PY=/Users/youww/.workbuddy/binaries/python/versions/3.13.12/bin/python3
[ -x "$PY" ] || PY=$(command -v python3)
[ -x "$CF" ] || { echo "$(date '+%F %T') 找不到 cloudflared 二进制" >> "$LOG"; exit 1; }

# 暂停开关：存在 /tmp/xlb_watchdog.pause 时看门狗不动作（用户手动关闭看板时使用）
[ -f /tmp/xlb_watchdog.pause ] && exit 0

# 防止上一轮还没跑完又叠一轮
LOCK=/tmp/xlb_watchdog.lock
[ -f "$LOCK" ] && [ "$(($(date +%s) - $(stat -f %m "$LOCK")))" -lt 240 ] && exit 0
touch "$LOCK"
trap 'rm -f "$LOCK"' EXIT

# ---- 1) 看板服务 ----
# 【2026-10-15】/tmp 会被系统不定期清理 → 服务与任务脚本丢失导致看板整站挂掉。
# 检测到 /tmp/xlb_web/server.py 缺失时先自愈恢复（从 xlb_gh 备份拷回 + 必要时密码登录重建凭证）
if [ ! -f /tmp/xlb_web/server.py ]; then
  echo "$(date '+%F %T') 检测到 /tmp 运行环境缺失，执行自愈恢复" >> "$LOG"
  bash "$DIR/restore_tmp.sh" >> "$LOG" 2>&1
fi
if ! curl -s -o /dev/null --max-time 5 http://localhost:8787/; then
  echo "$(date '+%F %T') 看板服务无响应，尝试拉起" >> "$LOG"
  (XLB_BIND=0.0.0.0 XLB_GH_SYNC=1 nohup "$PY" /tmp/xlb_web/server.py >> /tmp/xlb_server.log 2>&1 &)
fi

# 【2026-10-17 用户要求】取消定时拉数：只有手动点看板按钮才更新数据
# （原每 5 分钟 GH Actions dispatch 已停用；ping_workflow.sh 也已置为空操作）
# if [ "$(( $(date +%M) % 5 ))" -eq 0 ]; then
#   (bash "$DIR/ping_workflow.sh" >> /tmp/xlb_ping.log 2>&1 &)
# fi

# 提取真实隧道地址：快速隧道主机名形如 xxx-yyy-zzz-www.trycloudflare.com（≥2 个连字符）
# 必须排除 api./update./region* 等非隧道主机（曾把日志里的 API 域名误推 Pages）
tunnel_url() {
  grep -oE 'https://[a-z0-9]+(-[a-z0-9]+){2,}\.trycloudflare\.com' "$TUNLOG" 2>/dev/null \
    | grep -vE 'https://(api|update|region[0-9]*)\.trycloudflare\.com' | tail -1
}

# 【2026-10-19 起命名隧道】固定地址 xlb.hepinggh.cn，无需再从日志提取/推送 Pages
probe() {
  local u="$1" ip code
  [ -z "$u" ] && return 1
  code=$(curl -s -o /dev/null -L --max-time 12 -w '%{http_code}' "$u/" 2>/dev/null)
  [ "$code" = "200" ] && return 0
  ip=$(dig +short @1.1.1.1 "${u#https://}" 2>/dev/null | grep -E '^[0-9.]+$' | head -1)
  if [ -n "$ip" ]; then
    code=$(curl -s -o /dev/null -L --max-time 12 -w '%{http_code}' --noproxy '*' --resolve "${u#https://}:443:$ip" "$u/" 2>/dev/null)
    [ "$code" = "200" ] && return 0
    code=$(curl -s -o /dev/null -L --max-time 12 -w '%{http_code}' --resolve "${u#https://}:443:$ip" "$u/" 2>/dev/null)
    [ "$code" = "200" ] && return 0
  fi
  return 1
}

# ---- 2) 隧道探测 ----
# 重启冷却期内不重复重启（连接边缘需数十秒）
if [ -f "$RESTARTFILE" ] && [ "$(($(date +%s) - $(cat "$RESTARTFILE" 2>/dev/null || echo 0)))" -lt "$COOLDOWN" ]; then
  exit 0
fi

URL="$TUNURL"
if probe "$URL"; then
  rm -f "$FAILFILE"   # 探测成功，清零失败计数
  # 每小时整点写一条心跳，便于确认 cron 仍在工作
  [ "$(date +%M)" = "00" ] && echo "$(date '+%F %T') 心跳：隧道正常 ($URL)" >> "$LOG"
  exit 0
fi

FAIL=$(( $(cat "$FAILFILE" 2>/dev/null || echo 0) + 1 ))
echo "$FAIL" > "$FAILFILE"
echo "$(date '+%F %T') 隧道检查失败 $FAIL/$MAXFAIL (URL=$URL)" >> "$LOG"
[ "$FAIL" -lt "$MAXFAIL" ] && exit 0

# ---- 连续失败：重启命名隧道（地址固定，无需推送 Pages）----
rm -f "$FAILFILE"
date +%s > "$RESTARTFILE"
echo "$(date '+%F %T') 连续失败，重启命名隧道 $TUNNAME…" >> "$LOG"
pkill -x cloudflared 2>/dev/null
sleep 2
rm -f "$TUNLOG"
nohup "$CF" tunnel --config "$HOME/.cloudflared/config.yml" run "$TUNNAME" >> "$TUNLOG" 2>&1 &
NEW=""
for i in $(seq 1 30); do
  sleep 2
  if probe "$URL"; then NEW=1; break; fi
done
if [ -n "$NEW" ]; then
  echo "$(date '+%F %T') 命名隧道已恢复 ($URL，地址固定)" >> "$LOG"
else
  echo "$(date '+%F %T') 隧道重启后暂未可达，下轮冷却结束后再试" >> "$LOG"
fi
