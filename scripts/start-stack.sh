#!/usr/bin/env bash
# 一键启动：微信协议服务（固定 v3.1.1）→ wechat-onebot 网关
set -euo pipefail

cd "$(dirname "$0")/.."

start_container() {
  local name="$1"
  if ! docker ps -a --format '{{.Names}}' | grep -qx "$name"; then
    echo "容器 $name 不存在。" >&2
    return 1
  fi
  if ! docker ps --format '{{.Names}}' | grep -qx "$name"; then
    docker start "$name" >/dev/null
    echo "已启动 $name"
  fi
}

start_container wechat-admin-redis
start_container wechat-ipad-v311 || {
  echo "协议服务不存在，先运行 ./scripts/recreate-wechat-ipad.sh" >&2
  exit 1
}

image=$(docker inspect -f '{{.Config.Image}}' wechat-ipad-v311 2>/dev/null || true)
case "$image" in
  *41161d425d5130cfb254811aef7a87bc790a7abefbc471918ecbdfaf77f885c6*)
    ;;
  *:v3.1.1)
    echo "提示：协议服务用的是 v3.1.1 tag（建议执行 scripts/recreate-wechat-ipad.sh 按摘要固定）"
    ;;
  "")
    ;;
  *)
    echo "警告：协议服务镜像不是固定的 v3.1.1：$image" >&2
    ;;
esac

for _ in $(seq 1 30); do
  if nc -z 127.0.0.1 3010 2>/dev/null; then
    break
  fi
  sleep 0.5
done

if ! nc -z 127.0.0.1 3010 2>/dev/null; then
  echo "协议服务没有监听 3010，请查看：docker logs wechat-ipad-v311" >&2
  exit 1
fi

echo "协议服务：http://127.0.0.1:3010（wechat-ipad v3.1.1）"
exec ./start.sh "$@"
