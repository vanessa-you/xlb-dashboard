#!/bin/bash
# 更新 GitHub Pages 上的隧道入口地址：bash update_tunnel.sh https://xxx.trycloudflare.com
# 【2026-09-17 改版】不再用 git push（github.com:443 常不通），统一走 GitHub Contents API
set -e
URL="${1:?用法: update_tunnel.sh https://xxx.trycloudflare.com}"
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="/Users/youww/.workbuddy/binaries/python/versions/3.13.12/bin/python3"

# 【2026-10-10 加固】校验必须是真实快速隧道地址：
# 排除 api.trycloudflare.com / update.trycloudflare.com 等非隧道主机（曾把日志里的 API 域名误推上去）
if ! printf '%s' "$URL" | grep -qE '^https://[a-z0-9]+(-[a-z0-9]+){2,}\.trycloudflare\.com$'; then
  echo "拒绝推送：不是合法的隧道地址 -> $URL" >&2
  exit 1
fi

# ① 写本地工作副本（/tmp/xlb_gh_page，供本地参考；仅此而已，推送不依赖它）
mkdir -p /tmp/xlb_gh_page
printf 'window.XLB_TUNNEL_URL = "%s";\n' "$URL" > /tmp/xlb_gh_page/tunnel_url.js

# ①-b 【2026-10-18】同步写本机看板目录（此前只写 /tmp/xlb_gh_page，导致
# /tmp/xlb_web/tunnel_url.js 长期停留在早已失效的旧地址，本机页面参考信息错误）
if [ -d /tmp/xlb_web ]; then
  printf 'window.XLB_TUNNEL_URL = "%s";\n' "$URL" > /tmp/xlb_web/tunnel_url.js
fi

# ② Contents API 直推 Pages 仓库
"$PY" "$DIR/push_file_api.py" "tunnel_url.js=/tmp/xlb_gh_page/tunnel_url.js"
echo "OK: Pages 实时入口已更新 -> $URL"
