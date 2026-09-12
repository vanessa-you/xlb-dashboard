#!/bin/bash
# 更新 GitHub Pages 上的隧道入口地址：bash update_tunnel.sh https://xxx.trycloudflare.com
set -e
URL="${1:?用法: update_tunnel.sh https://xxx.trycloudflare.com}"
cd "$(dirname "$0")"
printf 'window.XLB_TUNNEL_URL = "%s";\n' "$URL" > tunnel_url.js
git add tunnel_url.js index.html
git -c user.name="xlb-sync" -c user.email="xlb-sync@users.noreply.github.com" \
    commit -m "chore: update live tunnel url" --quiet 2>/dev/null || echo "(无变化)"
git push --quiet origin main
echo "OK: Pages 实时入口已更新 -> $URL"
