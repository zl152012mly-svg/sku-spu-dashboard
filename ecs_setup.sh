#!/usr/bin/env bash
# ============================================================================
# ecs_setup.sh —— SKU&SPU 看板 阿里云 ECS 一键部署（幂等，可重复执行）
# 用法：把部署包解压到 /opt/sku-spu 后，创建好 .env，然后：
#       bash /opt/sku-spu/ecs_setup.sh
# 前置：.env（含 ACCESS_PASSWORD / STORAGE_MODE=oss / OSS_* / PORT）
#       不存在时本脚本会打印模板并退出，填好后重跑本脚本即可。
# ============================================================================
set -e
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "应用目录: $APP_DIR"

# ---- 选一个 >=3.8 的 python3 ----
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)'; then
      PY="$cand"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "❌ 未找到 Python>=3.8。当前系统 python3 版本：$(python3 -V 2>&1 || echo 无)"
  echo "   Ubuntu: apt-get install -y python3 python3-venv python3-pip"
  echo "   Alibaba Cloud Linux/CentOS: dnf install -y python3.11 python3.11-pip"
  exit 1
fi
echo "[1/4] 使用 $PY ($($PY -V 2>&1))；确保 venv/pip 组件齐全..."
if command -v apt-get >/dev/null 2>&1; then
  apt-get install -y python3-venv python3-pip >/dev/null 2>&1 || true
fi

echo "[2/4] 创建虚拟环境并安装依赖（oss2 已含在 requirements.txt）..."
cd "$APP_DIR"
"$PY" -m venv venv
PIP="./venv/bin/pip"
"$PIP" install --upgrade pip -q -i https://pypi.tuna.tsinghua.edu.cn/simple \
  || "$PIP" install --upgrade pip -q
"$PIP" install -r requirements.txt -q -i https://pypi.tuna.tsinghua.edu.cn/simple \
  || "$PIP" install -r requirements.txt -q
mkdir -p logs data uploads reference

echo "[3/4] 检查 .env ..."
if [ ! -f "$APP_DIR/.env" ]; then
  echo "❌ 未发现 $APP_DIR/.env。复制下面【整段】，把 4 个「填这里」改好，整段粘贴执行："
  echo '----------------------------------------------------------------'
  cat <<'TPL'
cat > /opt/sku-spu/.env <<'EOF'
ACCESS_PASSWORD=lucie2026
STORAGE_MODE=oss
OSS_ENDPOINT=oss-cn-xxxx.aliyuncs.com
OSS_ACCESS_KEY_ID=填这里
OSS_ACCESS_KEY_SECRET=填这里
OSS_BUCKET=sku-spu-data
PORT=8092
EOF
TPL
  echo '----------------------------------------------------------------'
  echo "填好后重新运行: bash $APP_DIR/ecs_setup.sh"
  exit 0
fi
echo "  .env 存在 ✓（STORAGE_MODE=$(grep -E '^STORAGE_MODE=' .env | cut -d= -f2)）"

echo "[4/4] 写入 systemd 服务并启动 ..."
cat > /etc/systemd/system/sku-spu.service <<UNIT
[Unit]
Description=SKU&SPU Dashboard (gunicorn + OSS)
After=network.target

[Service]
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/gunicorn -c gunicorn.conf.py wsgi:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now sku-spu
sleep 3
systemctl --no-pager -l status sku-spu | head -n 10 || true

IP=$(curl -s --max-time 3 ifconfig.me || echo '<ECS公网IP>')
echo
echo "✅ 部署完成。浏览器访问: http://$IP:$(grep -E '^PORT=' .env | cut -d= -f2)"
echo "   若打不开：ECS 控制台 → 安全组 → 入方向放行 TCP 8092。"
echo "   看日志: journalctl -u sku-spu -f"
