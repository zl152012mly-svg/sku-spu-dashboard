#!/usr/bin/env bash
# ============================================================================
# start_oss.sh —— 阿里云 ECS 直接部署启动脚本（非 Docker）
# 用法：
#   1) 在同目录创建 .env（含 OSS 凭证，见下方示例，已被 .gitignore 忽略，勿提交）
#   2) chmod +x start_oss.sh && ./start_oss.sh
# 说明：本脚本只负责拉起 gunicorn；密钥全部来自 .env，绝不写进脚本或代码。
# ============================================================================
set -e

cd "$(dirname "$0")"

# ---- 读取本地 .env（含敏感凭证，不入库） ----
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
  echo "[start_oss] 已从 .env 载入环境变量"
else
  echo "[start_oss] 警告：未发现 .env，请在 .env 中配置 OSS 凭证后再启动"
fi

# ---- 日志目录 ----
mkdir -p logs

# ---- 虚拟环境（首次需手动建：python3 -m venv venv && venv/bin/pip install -r requirements.txt） ----
VENV="${VENV:-venv}"
if [ -x "$VENV/bin/gunicorn" ]; then
  GUNICORN="$VENV/bin/gunicorn"
else
  GUNICORN="gunicorn"
fi

# ---- 存储模式强制为 oss（阿里云） ----
export STORAGE_MODE="${STORAGE_MODE:-oss}"

echo "[start_oss] 启动 gunicorn（STORAGE_MODE=$STORAGE_MODE, PORT=${PORT:-8092}）"
exec "$GUNICORN" -c gunicorn.conf.py wsgi:app
