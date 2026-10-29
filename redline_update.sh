#!/usr/bin/env bash
# ============================================================
#  红线价监控模块 —— ECS 一键上线 / 更新脚本（幂等，可反复执行）
#
#  用法（阿里云 Workbench 远程连接里粘这 2 行）：
#    curl -fsSL -o /tmp/redline.sh https://raw.githubusercontent.com/zl152012mly-svg/sku-spu-dashboard/main/redline_update.sh
#    bash /tmp/redline.sh
#
#  想同时导入历史数据？（先把导出的 json 上传到 /root/ 下）
#    bash /tmp/redline.sh /root/redline_cloud_export.json
#
#  特性：
#   - 优先 git 快进（merge --ff-only，绝不 reset --hard）
#   - git 失败自动改走 curl 逐文件拉取（沿用 ecs_step4_update.sh 的兜底思路）
#   - 重启前先做 import 自检，语法/缺文件当场暴露，不会把站点搞挂
#   - 覆盖前把旧文件备份到 /tmp/redline_bak_<时间>/
# ============================================================
set -u
APP=/opt/sku-spu
B=https://raw.githubusercontent.com/zl152012mly-svg/sku-spu-dashboard/main
TS=$(date +%Y%m%d_%H%M%S)
BAK=/tmp/redline_bak_$TS
IMPORT_JSON="${1:-}"

say() { printf '\n\033[36m==== %s ====\033[0m\n' "$1"; }
ok()  { printf '  \033[32m[OK ]\033[0m %s\n' "$1"; }
bad() { printf '  \033[31m[!! ]\033[0m %s\n' "$1"; }
warn(){ printf '  \033[33m[.. ]\033[0m %s\n' "$1"; }

# ------------------------------------------------------------
say "0. 环境体检"
# ------------------------------------------------------------
[ -d "$APP" ] && ok "应用目录存在 $APP" || { bad "找不到 $APP，脚本终止"; exit 1; }
cd "$APP" || { bad "进不去 $APP"; exit 1; }
[ -f "$APP/.env" ] && ok "找到 .env" || warn "没有 .env —— OSS 凭证缺失，数据会落到本地盘 data/redline/"
[ -x "$APP/venv/bin/python" ] && ok "venv 就绪" || warn "没找到 $APP/venv/bin/python，后面自检跳过"

# ------------------------------------------------------------
say "1. 备份将被覆盖的文件到 $BAK"
# ------------------------------------------------------------
mkdir -p "$BAK"
for f in app.py redline_routes.py redline_store.py redline_import.py \
         templates/console_index.html redline_app/index.html; do
  if [ -f "$APP/$f" ]; then
    mkdir -p "$BAK/$(dirname "$f")"
    cp -p "$APP/$f" "$BAK/$f" 2>/dev/null && echo "        备份 $f"
  fi
done
ok "备份完成（回滚：把 $BAK 里的文件拷回 $APP 再重启即可）"

# ------------------------------------------------------------
say "2. 拉取最新代码（优先 git，失败转 curl）"
# ------------------------------------------------------------
GIT_OK=0
if [ -d "$APP/.git" ]; then
  git config --global --add safe.directory "$APP" 2>/dev/null
  if git fetch origin main -q 2>/dev/null && git merge --ff-only origin/main -q 2>/dev/null; then
    ok "git 快进成功，当前提交：$(git rev-parse --short HEAD)"
    GIT_OK=1
  else
    warn "git 快进失败（ECS 上可能有本地改动/断网），改用 curl 逐文件拉取"
    git merge --abort 2>/dev/null
  fi
else
  warn "$APP 不是 git 仓库，改用 curl 逐文件拉取"
fi

if [ "$GIT_OK" -ne 1 ]; then
  mkdir -p "$APP/redline_app" "$APP/templates" "$APP/static/libs"
  pull() {  # $1=目标相对路径  $2=GitHub 路径
    if curl -fsSL --retry 3 --retry-delay 2 -o "$APP/$1" "$B/$2"; then
      printf '        \033[32m✓\033[0m %s (%s bytes)\n' "$1" "$(stat -c%s "$APP/$1" 2>/dev/null || echo '?')"
    else
      printf '        \033[31m✗\033[0m %s 拉取失败\n' "$1"
    fi
  }
  pull app.py                        app.py
  pull redline_routes.py             redline_routes.py
  pull redline_store.py              redline_store.py
  pull redline_import.py             redline_import.py
  pull templates/console_index.html  templates/console_index.html
  pull redline_app/index.html        redline_app/index.html
  pull static/libs/xlsx.full.min.js  static/libs/xlsx.full.min.js
  pull static/libs/pako.min.js       static/libs/pako.min.js
  pull static/libs/echarts.min.js    static/libs/echarts.min.js
  ok "curl 拉取结束"
fi

# ------------------------------------------------------------
say "3. 关键文件点检"
# ------------------------------------------------------------
NEED="redline_routes.py redline_store.py redline_app/index.html templates/console_index.html static/libs/xlsx.full.min.js static/libs/pako.min.js"
MISS=0
for f in $NEED; do
  if [ -s "$APP/$f" ]; then
    ok "$f  $(stat -c%s "$APP/$f") bytes"
  else
    bad "$f 缺失或为空！"; MISS=1
  fi
done
[ "$MISS" -eq 1 ] && { bad "关键文件不全，终止（站点未被改动）"; exit 1; }

# 首页是否已挂上导航
if grep -q 'redline' "$APP/templates/console_index.html"; then
  ok "console_index.html 已含「红线价监控」导航"
else
  bad "console_index.html 里没有 redline 导航，请检查该文件是否为最新版"
fi

# ------------------------------------------------------------
say "4. 重启前自检（语法 + 蓝图可导入）"
# ------------------------------------------------------------
if [ -x "$APP/venv/bin/python" ]; then
  ( cd "$APP" && ./venv/bin/python -c "
import redline_store, redline_routes
print('  import OK, 存储后端 =', redline_store.backend_name())
print('  蓝图前缀 =', redline_routes.bp.url_prefix)
print('  路由数 =', len([r for r in redline_routes.bp.deferred_functions]) if hasattr(redline_routes.bp,'deferred_functions') else 'n/a')
" ) && ok "自检通过" || { bad "自检失败！已停止，服务未重启。请把上面报错发给 Lucie 的 AI 排查"; exit 1; }
fi

# ------------------------------------------------------------
say "5. 重启服务"
# ------------------------------------------------------------
sudo systemctl restart sku-spu.service
sleep 6
sudo systemctl is-active --quiet sku-spu.service && ok "服务已起来" || bad "服务没起来，看日志：sudo journalctl -u sku-spu.service -n 60 --no-pager"

# ------------------------------------------------------------
say "6. 验证结果"
# ------------------------------------------------------------
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:8092$1"; }
P=$(code /api/ping);            echo "  /api/ping              = $P   （200=主站正常）"
C=$(code /console);             echo "  /console               = $C   （302=正常跳登录）"
R=$(code /redline/);            echo "  /redline/              = $R   （302=正常跳登录；404=蓝图没挂上）"
H=$(code /redline/api/health);  echo "  /redline/api/health    = $H   （401=鉴权生效（未登录）；200=接口裸奔，需排查）"

if [ "$R" = "302" ] || [ "$R" = "200" ]; then
  ok "红线价模块已上线 → http://47.83.129.85:8092/redline/"
else
  bad "红线价模块仍未生效"
  echo "  ---- 最近报错 ----"
  sudo journalctl -u sku-spu.service -n 40 --no-pager | grep -iE 'redline|Traceback|Error' | tail -15
  echo "  （app.py 里蓝图注册是 try/except 包裹的，注册失败不会拖垮主站，日志里会打「红线价监控模块注册失败」）"
fi

# ------------------------------------------------------------
say "7. 数据导入（可选）"
# ------------------------------------------------------------
if [ -n "$IMPORT_JSON" ]; then
  if [ -f "$IMPORT_JSON" ]; then
    ok "开始导入 $IMPORT_JSON"
    ( cd "$APP" && set -a && . ./.env && set +a && ./venv/bin/python redline_import.py "$IMPORT_JSON" ) \
      && ok "导入完成" || bad "导入失败，看上面输出"
  else
    bad "找不到导入文件 $IMPORT_JSON"
  fi
else
  echo "  没传 json 参数，跳过导入。之后要导入历史数据："
  echo "    1) Workbench 左上「上传文件」把 redline_cloud_export.json 传到 /root/"
  echo "    2) cd $APP && set -a && . ./.env && set +a && ./venv/bin/python redline_import.py /root/redline_cloud_export.json"
  echo "    3) 浏览器打开 /redline/ 看数据是否都在"
fi

# ------------------------------------------------------------
say "8. 收尾"
# ------------------------------------------------------------
echo "  浏览器访问（先 Ctrl+F5 强刷）：http://47.83.129.85:8092/console  → 左侧「📉 红线价监控」"
echo "  备份目录：$BAK"
echo "  看日志：sudo journalctl -u sku-spu.service -n 60 --no-pager"
