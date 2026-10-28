#!/usr/bin/env bash
# 一键诊断：把这次踩过的坑（协议服务版本、端口占用、旧实例、登录状态）都检查一遍
set -uo pipefail

cd "$(dirname "$0")/.."

ok()   { printf '  ✅ %s\n' "$*"; }
warn() { printf '  ⚠️  %s\n' "$*"; }
bad()  { printf '  ❌ %s\n' "$*"; }
section() { printf '\n== %s ==\n' "$*"; }

PINNED_DIGEST="41161d425d5130cfb254811aef7a87bc790a7abefbc471918ecbdfaf77f885c6"

section "Docker"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  ok "Docker 可用"
else
  bad "Docker 不可用或未启动"
fi

for name in wechat-admin-redis wechat-ipad-v311; do
  state=$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo missing)
  if [ "$state" = "running" ]; then
    ok "容器 $name 运行中"
  else
    bad "容器 $name 状态：$state"
  fi
done

old_state=$(docker inspect -f '{{.State.Status}}' server_xjbCgzvPlBL8BdRt 2>/dev/null || true)
if [ "$old_state" = "running" ] || [ "$old_state" = "restarting" ]; then
  warn "旧容器 server_xjbCgzvPlBL8BdRt 还在运行/重启，可能抢占 Redis；建议 docker stop server_xjbCgzvPlBL8BdRt"
fi

image=$(docker inspect -f '{{.Config.Image}}' wechat-ipad-v311 2>/dev/null || true)
case "$image" in
  *"$PINNED_DIGEST"*) ok "协议服务镜像已按摘要固定（v3.1.1）" ;;
  *":v3.1.1")         warn "协议服务使用 v3.1.1 tag，建议用 scripts/recreate-wechat-ipad.sh 按摘要固定" ;;
  "")                 bad "找不到 wechat-ipad-v311 容器" ;;
  *)                  bad "协议服务镜像不是固定的 v3.1.1：$image" ;;
esac

section "配置"
driver=$(python3 -c "import json;print(json.load(open('config/wechat.json'))['driver'])" 2>/dev/null || echo "?")
base=$(python3 -c "import json;print(json.load(open('config/wechat.json'))['ipad']['base_url'])" 2>/dev/null || echo "?")
if [ "$driver" = "ipad" ]; then ok "驱动：ipad"; else warn "驱动：$driver（扫码登录需要 ipad）"; fi
echo "  协议服务地址：$base"

if nc -z 127.0.0.1 3010 2>/dev/null; then
  ok "协议服务 3010 可连接"
else
  bad "协议服务 3010 不可连接（docker logs wechat-ipad-v311）"
fi

section "wechat-onebot"
callback_port=$(python3 -c "import json;print(json.load(open('config/wechat.json'))['callback']['port'])" 2>/dev/null || echo 9002)
health=$(curl -s -m 3 "http://127.0.0.1:${callback_port}/health" 2>/dev/null || true)
if printf '%s' "$health" | grep -q '"ok"'; then
  ok "网关运行中（回调端口 ${callback_port}）"
else
  bad "网关未运行，执行 ./scripts/start-stack.sh"
fi

webui_port=$(python3 -c "import json;print(json.load(open('config/webui.json'))['port'])" 2>/dev/null || echo 6100)
token=$(python3 -c "import json;print(json.load(open('config/webui.json')).get('token',''))" 2>/dev/null || true)
state_json=$(curl -s -m 3 -H "Cookie: wo_token=${token}" "http://127.0.0.1:${webui_port}/api/login/state" 2>/dev/null || true)
if [ -n "$state_json" ]; then
  printf '%s' "$state_json" | python3 -c '
import json,sys
try:
    s=json.load(sys.stdin)["state"]
except Exception:
    print("  ⚠️  WebUI 返回异常（是否设了令牌？）"); raise SystemExit
print("  · 登录状态：", "已登录" if s.get("logged_in") else "未登录")
print("  · 协议口味：", s.get("protocol") or "unknown")
if s.get("logged_in"):
    acc=s.get("account") or {}
    print("  · 账号：", acc.get("nickname"), "(", acc.get("user_id"), ")")
    print("  · 提醒：客户端配置里的 selfId 要填上面的 wxid")
'
else
  warn "WebUI 未响应（端口 ${webui_port}）"
fi

section "端口"
ports=$(python3 - <<'PY'
import json
try:
    ob = json.load(open("config/onebot11.json"))
    wx = json.load(open("config/wechat.json"))
    ui = json.load(open("config/webui.json"))
except Exception:
    print("")
    raise SystemExit
values = []
for item in ob.get("network", {}).get("websocketServers", []):
    if item.get("enable"):
        values.append(str(item.get("port")))
for item in ob.get("network", {}).get("httpServers", []):
    if item.get("enable"):
        values.append(str(item.get("port")))
values.append(str(wx.get("callback", {}).get("port")))
values.append(str(wx.get("media", {}).get("port")))
if ui.get("enable", True):
    values.append(str(ui.get("port")))
print(" ".join(dict.fromkeys(v for v in values if v)))
PY
)
for port in $ports; do
  owner=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | tail -n +2 | awk 'NR==1{print $1" (pid "$2")"}')
  if [ -n "$owner" ]; then
    printf '  · %s：%s\n' "$port" "$owner"
  else
    printf '  · %s：空闲\n' "$port"
  fi
done

printf '\n提示：停止旧网关用 ./start.sh --stop；重建协议服务用 ./scripts/recreate-wechat-ipad.sh\n'
