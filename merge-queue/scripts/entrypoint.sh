#!/bin/sh
# 容器启动：确保中心仓库存在，不存在则初始化样例仓库
set -e

echo "[entrypoint] 检查中心仓库: ${MQ_DATA_DIR}/central.git"
if [ ! -d "${MQ_DATA_DIR}/central.git/refs" ]; then
  echo "[entrypoint] 中心仓库不存在，初始化样例仓库"
  python scripts/init_sample_repo.py
else
  echo "[entrypoint] 中心仓库已存在，跳过初始化"
fi

echo "[entrypoint] 启动服务: $*"
exec "$@"
