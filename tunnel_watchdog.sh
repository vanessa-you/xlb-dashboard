#!/bin/bash
# 隧道看门狗（单次检查版，由 cron 每分钟调用）
# 1) 看护本机看板服务(8787)：无响应则拉起
# 2) 看护 cloudflared 隧道：连续 5 次外网探测失败 → 重启隧道 → 新地址自动推送 Pages
#    多重探测（系统代理 + 公共DNS直连）避免本机 DNS 负缓存造成误判
#    重启后 5 分钟冷却，避免"新隧道边缘未就绪"被反复重启
# 日志 /tmp/xlb_watchdog.log；失败计数 /tmp/xlb_watchdog.fail；冷却时间戳 /tmp/xlb_watchdog.restart
DIR="$(cd "$(dirname "$0")" && pwd)"
CF="$DIR/cloudflared"
[ -x "$CF" ] || CF=/tmp/cloudflared
LOG=/tmp/xlb_watchdog.log
FAILFILE=/tmp/xlb_watchdog.fail
RESTARTFILE=/tmp/xlb_watchdog.restart
TUNLOG=/tmp/xlb_tunnel.log
MAXFAIL=5
COOLDOWN=300
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
if ! curl -s -o /dev/null --max-time 5 http://localhost:8787/; then
  echo "$(date '+%F %T') 看板服务无响应，尝试拉起" >> "$LOG"
  (XLB_BIND=0.0.0.0 XLB_GH_SYNC=0 nohup "$PY" /tmp/xlb_web/server.py >> /tmp/xlb_server.log 2>&1 &)
fi

# 提取真实隧道地址：快速隧道主机名形如 xxx-yyy-zzz-www.trycloudflare.com（≥2 个连字符）
# 必须排除 api./update./region* 等非隧道主机（曾把日志里的 API 域名误推 Pages）
tunnel_url() {
  grep -oE 'https://[a-z0-9]+(-[a-z0-9]+){2,}\.trycloudflare\.com' "$TUNLOG" 2>/dev/null \
    | grep -vE 'https://(api|update|region[0-9]*)\.trycloudflare\.com' | tail -1
}

# 探测隧道是否可达：策略1 经系统代理；策略2 公共DNS解析后直连
probe() {
  local u="$1" ip
  [ -z "$u" ] && return 1
  curl -s -o /dev/null --max-time 12 "$u/" && return 0
  ip=$(dig +short @1.1.1.1 "${u#https://}" 2>/dev/null | grep -E '^[0-9.]+$' | head -1)
  if [ -n "$ip" ]; then
    curl -s -o /dev/null --max-time 12 --noproxy '*' --resolve "${u#https://}:443:$ip" "$u/" && return 0
    curl -s -o /dev/null --max-time 12 --resolve "${u#https://}:443:$ip" "$u/" && return 0
  fi
  return 1
}

# ---- 2) 隧道探测 ----
# 重启冷却期内不重复重启（新隧道域名在边缘生效需 30s~数分钟）
if [ -f "$RESTARTFILE" ] && [ "$(($(date +%s) - $(cat "$RESTARTFILE" 2>/dev/null || echo 0)))" -lt "$COOLDOWN" ]; then
  exit 0
fi

URL=$(tunnel_url)
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

# ---- 连续失败：重启隧道 ----
rm -f "$FAILFILE"
date +%s > "$RESTARTFILE"
echo "$(date '+%F %T') 连续失败，重启隧道…" >> "$LOG"
pkill -x cloudflared 2>/dev/null
sleep 2
rm -f "$TUNLOG"
nohup "$CF" tunnel --protocol http2 --url http://localhost:8787 --no-autoupdate > "$TUNLOG" 2>&1 &
NEW=""
for i in $(seq 1 30); do
  sleep 2
  NEW=$(tunnel_url)
  if [ -n "$NEW" ] && probe "$NEW"; then break; fi
  NEW=""
done
if [ -n "$NEW" ]; then
  echo "$(date '+%F %T') 新隧道地址: $NEW → 推送 Pages" >> "$LOG"
  export XLB_GH_TOKEN=$(cat "$HOME/.xlb_gh_token" 2>/dev/null)
  bash "$DIR/update_tunnel.sh" "$NEW" >> "$LOG" 2>&1 && echo "$(date '+%F %T') Pages 入口已更新" >> "$LOG"
else
  echo "$(date '+%F %T') 隧道重启后暂未可达（边缘生效中），下轮冷却结束后再试" >> "$LOG"
fi
