#!/usr/bin/env bash
# 重建微信 iPad 协议服务，固定使用 v3.1.1（不使用 :latest）
#
# 说明：wechat-ipad:latest 已升级到 v3.2.x，强制要求内部鉴权变量，
# 而管理后台创建容器时不会传，容器会 panic 重启。这里固定到 v3.1.1。
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE_TAG="registry.cn-shenzhen.aliyuncs.com/houhou/wechat-ipad:v3.1.1"
# 固定到具体镜像摘要，即使 tag 被重新指向也不会变
IMAGE_REF="registry.cn-shenzhen.aliyuncs.com/houhou/wechat-ipad@sha256:41161d425d5130cfb254811aef7a87bc790a7abefbc471918ecbdfaf77f885c6"
CONTAINER="wechat-ipad-v311"
NETWORK="wechat-robot"
REDIS_CONTAINER="wechat-admin-redis"
REDIS_PASSWORD="${REDIS_PASSWORD:?请先 export REDIS_PASSWORD（Redis 密码）}"

if ! docker image inspect "$IMAGE_REF" >/dev/null 2>&1; then
  echo "本地没有 $IMAGE_TAG，正在拉取（linux/amd64）…"
  docker pull --platform linux/amd64 "$IMAGE_TAG"
fi

if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
  echo "Docker 网络 $NETWORK 不存在；请先启动原微信机器人栈，或手动创建该网络。" >&2
  exit 1
fi

if docker ps -a --format '{{.Names}}' | grep -qx "$REDIS_CONTAINER"; then
  docker start "$REDIS_CONTAINER" >/dev/null 2>&1 || true
else
  echo "警告：找不到 $REDIS_CONTAINER，协议服务可能无法启动。" >&2
fi

echo "重建协议服务容器 $CONTAINER …"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  --platform linux/amd64 \
  --network "$NETWORK" \
  -p 3010:9000 \
  -e WECHAT_PORT=9000 \
  -e REDIS_HOST="$REDIS_CONTAINER" \
  -e REDIS_PORT=6379 \
  -e REDIS_PASSWORD="$REDIS_PASSWORD" \
  -e REDIS_DB=1 \
  -e WECHAT_CLIENT_HOST=host.docker.internal:9002 \
  -e UUID_URL=http://wechat-slider:9000 \
  -e GIN_MODE=release \
  -e TZ=Asia/Shanghai \
  "$IMAGE_REF" >/dev/null

for _ in $(seq 1 30); do
  if nc -z 127.0.0.1 3010 2>/dev/null; then
    echo "协议服务已就绪：http://127.0.0.1:3010"
    echo "固定版本：$IMAGE_TAG"
    exit 0
  fi
  sleep 0.5
done

echo "协议服务 30 秒内没有监听 3010，请查看：docker logs $CONTAINER" >&2
exit 1
