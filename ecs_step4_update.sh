#!/usr/bin/env bash
# ============================================================
#  Phase 2 Step 4 —— 合并新品盘点到现有 SKU&SPU 服务（一键更新）
#  幂等可重跑；不做任何删除动作，请放心执行
#
#  用法（ECS Workbench 里粘这 3 行）：
#    curl -fsSL -o /tmp/step4.sh https://raw.githubusercontent.com/zl152012mly-svg/sku-spu-dashboard/main/ecs_step4_update.sh
#    bash /tmp/step4.sh
#
#  前提：代码已经 Push 到 GitHub main 分支
# ============================================================
set -u
APP=/opt/sku-spu
B=https://raw.githubusercontent.com/zl152012mly-svg/sku-spu-dashboard/main

say() { printf '\n\033[36m==== %s ====\033[0m\n' "$1"; }
ok()  { printf '  \033[32m[OK ]\033[0m %s\n' "$1"; }
bad() { printf '  \033[31m[!! ]\033[0m %s\n' "$1"; }
warn(){ printf '  \033[33m[.. ]\033[0m %s\n' "$1"; }

say "0. 环境体检"
[ -d "$APP" ] && ok "应用目录存在 $APP" || { bad "找不到 $APP，脚本终止"; exit 1; }
[ -f "$APP/.env" ] && ok "找到 .env" || warn "没有 .env（首次部署？请按手册创建）"

# 是否已经给新品盘点配了独立 OSS 桶
if grep -qE '^\s*(NA_OSS_BUCKET|ALIYUN_OSS_BUCKET)\s*=' "$APP/.env" 2>/dev/null; then
  ok "新品盘点 OSS 已配置（NA_OSS_* 或 ALIYUN_OSS_*）→ 沿用原新品盘点数据"
else
  warn "未配置 NA_OSS_* —— 新品盘点将运行在【ECS 本地盘】模式"
  warn "后果：看不到旧新品盘点已上传的文件，需要重新上传非共享数据源"
  warn "如需沿用旧数据，请先把新-arrival 的 .env 里 ALIYUN_OSS_* / NA_OSS_* 四行追加到 $APP/.env"
fi

# 找找旧的 new-arrival 装在哪（只读，不动）
echo "  ---- 候选的旧新品盘点目录 ----"
for d in /opt/new-arrival /opt/newarrival /opt/na /srv/new-arrival /root/new-arrival; do
  [ -d "$d" ] && echo "       发现: $d"
done
systemctl list-units --type=service --all --no-pager 2>/dev/null | grep -iE 'new|arrival|na\.service' | head -5

say "1. 拉取最新代码"
mkdir -p "$APP/na" "$APP/templates" "$APP/static"
pull() {  # $1=目标相对路径  $2=GitHub 路径
  if curl -fsSL -o "$APP/$1" "$B/$2"; then
    sz=$(stat -c%s "$APP/$1" 2>/dev/null || echo '?')
    ok "$1  ($sz bytes)"
  else
    bad "$1 拉取失败！请确认 GitHub Desktop 已经 Push main 分支"
  fi
}
pull app.py                  app.py
pull auth.py                 auth.py
pull common_store.py         common_store.py
pull na_core.py              na_core.py
pull na_routes.py            na_routes.py
pull templates/index.html    templates/index.html
pull templates/na_index.html templates/na_index.html
pull templates/console_index.html templates/console_index.html   # 界面改版：/console 统一工作台

for f in cloud_store process_script new_color_module sku_eval report_script color_dict_seed color_audit new_color_check; do
  pull "na/$f.py" "na/$f.py"
done

say "2. 安装新增依赖 pandas（新品盘点必需）"
if source "$APP/venv/bin/activate" 2>/dev/null; then
  pip install pandas -q 2>&1 | tail -3
  python -c "import pandas;print('pandas 版本:',pandas.__version__)" && ok "pandas 就绪" || bad "pandas 安装失败"
else
  bad "找不到 $APP/venv，请检查虚拟环境路径"
fi

say "3. 重启服务"
sudo systemctl restart sku-spu.service
sleep 5

say "4. 验证结果"
V=$(curl -s http://127.0.0.1:8092/ | grep -o 'app.js?v=[0-9a-z-]*' | head -1)
[ -n "$V" ] && ok "版本号: $V  （期望含 20261027-ops）" || bad "取不到版本号，服务可能没起来"
C=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8092/na/)
echo "  /na/ 未登录返回码 = $C  （302=正常跳转到登录；404=文件缺失；000/502=服务没起来）"
K=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8092/console)
echo "  /console 未登录返回码 = $K  （302=正常跳转到登录；404=console_index.html 漏传；000/502=服务没起来）"
sudo systemctl status sku-spu.service --no-pager | head -3

say "5. 若失败，看日志"
echo "  sudo journalctl -u sku-spu.service -n 40 --no-pager"
echo "  （常见：No module named 'pandas' → 安装依赖；No module named 'na_core' → 文件漏传；/na/ 404 → na 目录漏传）"
