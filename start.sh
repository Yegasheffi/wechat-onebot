#!/usr/bin/env bash
# wechat-onebot 启动脚本：不需要安装依赖，直接用系统 Python 3.10+
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "找不到 python3，请先安装 Python 3.10 或更高版本" >&2
  exit 1
fi

export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m wechat_onebot "$@"
