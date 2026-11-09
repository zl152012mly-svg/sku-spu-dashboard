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
# 统计文件里某个固定串出现的行数，永远返回单个整数。
# 坑（2026-11-09 ECS 实测）：grep -c 在 0 命中时仍输出 "0" 但退出码为 1，
# 旧写法 `grep -c ... || echo 0` 会再追加一个 "0" → 变量变成 "0\n0"，
# `[ "$X" -eq 0 ]` 报 integer expression expected，自查误报 [..]。
# 这里用 `|| true` 只吞退出码、保留 grep 的输出，再用 ${n:-0} 兜住「文件不存在」。
count_in() {  # $1=文件  $2=固定字符串
  local n
  n=$(grep -F -c "$2" "$1" 2>/dev/null || true)
  printf '%s' "${n:-0}"
}

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
mkdir -p "$APP/na" "$APP/templates" "$APP/static" "$APP/redline_app"
pull() {  # $1=目标相对路径  $2=GitHub 路径
  if curl -fsSL -o "$APP/$1" "$B/$2"; then
    sz=$(stat -c%s "$APP/$1" 2>/dev/null || echo '?')
    ok "$1  ($sz bytes)"
  else
    bad "$1 拉取失败！请确认 GitHub Desktop 已经 Push main 分支"
  fi
}
pull ASSET_VERSION           ASSET_VERSION              # 2026-11-09 阶段0: 前端版本号唯一来源（漏拉会退回 dev- 前缀）
pull app.py                  app.py
pull auth.py                 auth.py
pull common_store.py         common_store.py
pull na_core.py              na_core.py
pull na_routes.py            na_routes.py
pull bridge_main.py          bridge_main.py   # 2026-10-29 入口统一: 系统设置→清洗看板反向同步桥
pull ddl_notify.py           ddl_notify.py   # 2026-11-04 上架DDL到期企微群提醒（ECS 定时脚本；优先读系统设置保存的配置）
pull ddl_notify_config.example.json ddl_notify_config.example.json   # 字段说明/离线兜底模板（不含密钥；映射已在系统设置里可视化维护）
pull templates/index.html    templates/index.html
pull templates/na_index.html templates/na_index.html
pull templates/console_index.html templates/console_index.html   # 界面改版：/console 统一工作台
pull static/app.js           static/app.js
pull static/style.css        static/style.css                  # 2026-11-09 补漏: 阶段0补修的 .pg-actions 失败出口样式
pull storage.py              storage.py                       # 2026-11-09 首屏提速: OSS Bucket 按线程复用
pull redline_routes.py       redline_routes.py                 # 2026-11-09 阶段0: 导出兜底链接强制本地化
pull redline_store.py        redline_store.py                  # 2026-11-09 首屏提速: 轻量 meta 对象分离
pull redline_app/index.html  redline_app/index.html            # 2026-11-09 阶段0: 统一 title + 清旧 SaaS 文案

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
V=$(curl -s http://127.0.0.1:8092/ | grep -o 'app.js?v=[0-9A-Za-z._-]*' | head -1)
[ -n "$V" ] && ok "前端版本号: $V  （ASSET_VERSION + 文件内容哈希，由 app.py 注入）" || bad "取不到版本号，服务可能没起来"
AV=$(cat "$APP/ASSET_VERSION" 2>/dev/null | head -1 | tr -d '\r\n')
if [ -n "$AV" ]; then
  ok "ASSET_VERSION 基准号: $AV"
  case "$V" in
    *"$AV"*) ok "页面版本号与 ASSET_VERSION 一致" ;;
    *) warn "页面版本号未包含基准号，可能是服务未重启或 index.html 漏拉" ;;
  esac
else
  bad "$APP/ASSET_VERSION 缺失，版本号会退回 dev- 前缀"
fi
C=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8092/na/)
echo "  /na/ 未登录返回码 = $C  （302=正常跳转到登录；404=文件缺失；000/502=服务没起来）"
K=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8092/console)
echo "  /console 未登录返回码 = $K  （302=正常跳转到登录；404=console_index.html 漏传；000/502=服务没起来）"
sudo systemctl status sku-spu.service --no-pager | head -3

say "4.5 DDL 到期提醒 · 可视化配置 / 工作日分Tab 部署自查（2026-11-04 第二版）"
G1=$(count_in "$APP/na_routes.py" "ddl-notify-config")
[ "$G1" -ge 2 ] && ok "na_routes.py 已含配置/预览接口 ($G1 处)" || bad "na_routes.py 缺少 DDL 配置接口，检查是否拉到最新版"
G2=$(count_in "$APP/ddl_notify.py" "na_data' / 'ddl_notify_config.json'")
[ "$G2" -ge 1 ] && ok "ddl_notify.py 已优先读系统设置保存的配置" || bad "ddl_notify.py 未读 na_data 配置，定时脚本会退回旧 /etc 文件"
G3=$(count_in "$APP/templates/na_index.html" "ddlNotifyCard")
[ "$G3" -ge 1 ] && ok "系统设置页已含「DDL 到期提醒设置」卡" || bad "na_index.html 缺少设置卡"
G4=$(grep -F -o 'v={{ ASSET_VER }}' "$APP/templates/console_index.html" 2>/dev/null | wc -l | tr -d ' ')
G4=${G4:-0}
[ "$G4" -eq 7 ] && ok "工作台 iframe 版本号已改为 ASSET_VER 注入（模板 7 处）" || warn "console_index.html 版本号异常 ($G4 处 ASSET_VER，期望 7)，可能是旧版文件"
G5=$(count_in "$APP/ddl_notify.py" "def is_workday")
[ "$G5" -ge 1 ] && ok "ddl_notify.py 已含工作日判断（非工作日静默 / 顺延补发）" || bad "ddl_notify.py 缺少 is_workday，节假日仍会照常发消息"
G6=$(count_in "$APP/ddl_notify.py" "def group_mentions")
[ "$G6" -ge 1 ] && ok "ddl_notify.py 已含「未分配运营 → 按组默认成员艾特」" || bad "ddl_notify.py 缺少 group_mentions，未分配运营仍无人认领"
G7=$(count_in "$APP/templates/na_index.html" "dmPane3")
[ "$G7" -ge 2 ] && ok "系统设置已拆 3 个横向 Tab（数据源 / 子账号 / DDL 提醒）" || bad "na_index.html 缺少 dmPane3，说明没拉到拆页版"
G8=$(count_in "$APP/na_routes.py" "workday_overrides")
[ "$G8" -ge 1 ] && ok "na_routes.py 已支持可编辑工作日历（holidays / workday_overrides）" || bad "na_routes.py 缺少工作日历字段，界面填了也存不上"
echo "  提示：群 webhook / 运营成员 / 组默认成员 / 工作日历请到【系统设置 → ③ DDL 到期提醒设置】里填并保存，无需改 /etc 文件。"
echo "  提醒：cron 需改成每天 10:00 触发（脚本自己判断工作日），见手册 §20："
echo "    ( crontab -l 2>/dev/null | grep -v 'ddl_notify.py' ; echo '0 10 * * * cd /opt/sku-spu && /usr/bin/python3 ddl_notify.py >> /var/log/ddl_notify.log 2>&1' ) | crontab -"

say "4.6 页面一体化阶段0 自查（2026-11-09）"
S1=$(count_in "$APP/app.py" "def compute_asset_version")
[ "$S1" -ge 1 ] && ok "app.py 已含统一版本号生成器" || bad "app.py 缺少 ASSET_VER，模板会渲染出空白占位"
S2=$(count_in "$APP/app.py" "def state_fingerprint")
[ "$S2" -ge 1 ] && ok "/api/state 已含结果缓存 + ETag 共用指纹" || warn "app.py 缺少结果缓存，刷新看板仍会重复序列化 19MB"
S3=$(count_in "$APP/templates/console_index.html" 'id="loading"')
[ "$S3" -ge 1 ] && ok "工作台已含切换加载态" || warn "console_index.html 缺少加载态，切模块仍像卡住"
S4=$(count_in "$APP/templates/console_index.html" "na-console-nav-ack")
[ "$S4" -ge 1 ] && ok "工作台已接管跨模块导航（高亮与内容同步）" || warn "console_index.html 缺少导航回执"
S5=$(count_in "$APP/templates/na_index.html" "v=c2")
[ "$S5" -eq 0 ] && ok "na_index.html 不再残留 v=c2 旧硬编码" || warn "na_index.html 仍有 $S5 处 v=c2，跳系统设置可能看到旧页"
S6=$(count_in "$APP/redline_app/index.html" "本地模式：数据仅保存在当前浏览器")
[ "$S6" -eq 0 ] && ok "红线价旧 SaaS 文案已清理" || warn "红线价页面仍有 $S6 处旧 SaaS 文案"
echo "  提示：/console 外壳已设 no-store；模块页版本号由 app.py 自动计算，改前端后无需再手升 6 处 v=cN。"

say "4.7 阶段0补修自查（2026-11-09 · 卡死出口 / 视觉残留 / 顶部Tab / iframe保活）"
T1=$(count_in "$APP/static/app.js" "function fetchWithTimeout")
[ "$T1" -ge 1 ] && ok "清洗看板请求已加超时护栏" || bad "app.js 缺少超时护栏，明细载入仍可能永久转圈"
T2=$(count_in "$APP/templates/index.html" 'id="pg-actions"')
[ "$T2" -ge 1 ] && ok "明细载入失败有「重新载入」出口" || warn "index.html 缺少失败出口"
T3=$(count_in "$APP/templates/na_index.html" "45000")
[ "$T3" -ge 2 ] && ok "新品报告 / 新色取数已加 45 秒超时" || warn "na_index.html 超时出口数量异常 ($T3，期望 2)"
T4=$(count_in "$APP/templates/console_index.html" "var frameMap = {}")
[ "$T4" -ge 1 ] && ok "工作台 iframe 已保活（切回不重载）" || bad "console_index.html 缺少 frameMap，切模块仍会整页重载"
T5=$(count_in "$APP/redline_app/index.html" 'id="rlTabs"')
[ "$T5" -ge 1 ] && ok "红线价嵌入态已改顶部横向 Tab" || warn "redline_app/index.html 缺少顶部 Tab"
T6=$(count_in "$APP/redline_app/index.html" "共享空间")
[ "$T6" -eq 0 ] && ok "红线价旧共享空间文案已清零" || warn "红线价仍有 $T6 处旧文案"
T7=$(count_in "$APP/templates/index.html" 'id="file-input" class="hide"')
[ "$T7" -ge 1 ] && ok "看板旧上传触发器已隐藏" || warn "index.html 仍裸露「选择文件」"
echo "  提示：改前端后 ASSET_VER 会自动换号；本脚本末尾已含 restart，无需手工重启。"

say "4.8 红线价首屏提速 + 横幅来源自查（2026-11-09 下午）"
U1=$(count_in "$APP/redline_store.py" "_PREFIX_META")
[ "$U1" -ge 1 ] && ok "轻量 meta 清单已启用（不再重复下载几 MB b64）" || bad "redline_store 缺少 meta 分离，首屏仍会重复下载大对象"
U2=$(count_in "$APP/storage.py" "_OSS_CLIENT_LOCAL")
[ "$U2" -ge 1 ] && ok "OSS Bucket 已按线程复用" || warn "storage.py 未复用 OSS 客户端"
U3=$(count_in "$APP/redline_app/index.html" "redline_calc_cache")
[ "$U3" -ge 1 ] && ok "计算结果已缓存（数据源未变时跳过全量重算）" || warn "redline_app 缺少结果缓存"
U4=$(count_in "$APP/redline_app/index.html" "超过 90 秒")
[ "$U4" -ge 1 ] && ok "遮罩兜底已放宽到 90 秒（不再中途误报失败）" || warn "redline_app 仍是过早的失败兜底"
U5=$(count_in "$APP/redline_app/index.html" "数据源：ECS 工作台共享存储")
[ "$U5" -ge 1 ] && ok "横幅已写明真实数据来源" || warn "横幅文案仍是旧表述"
U6=$(count_in "$APP/static/style.css" ".pg-actions")
[ "$U6" -ge 1 ] && ok "style.css 已补齐（阶段0补修失败出口样式）" || warn "style.css 漏拉，遮罩按钮样式会失效"
echo "  提示：/redline/api/health 的 storage.backend 可确认走 oss 还是本地 JSON；metas 数量应与 sources 一致。"

say "4.9 红线价「关网页重进仍 2 分钟」修复自查（2026-11-09 傍晚）"
V1=$(count_in "$APP/redline_store.py" "_list_object_info")
[ "$V1" -ge 1 ] && ok "数据指纹只读对象元信息（不下 b64）" || bad "redline_store 缺少轻量指纹，快照快速通道无法启用"
V2=$(count_in "$APP/redline_routes.py" "/api/state-fp")
[ "$V2" -ge 1 ] && ok "已提供 /redline/api/state-fp" || bad "redline_routes 缺少指纹接口"
V3=$(count_in "$APP/redline_routes.py" "/api/snapshot")
[ "$V3" -ge 1 ] && ok "已提供共享快照读/写接口" || bad "redline_routes 缺少快照接口"
V4=$(count_in "$APP/redline_app/index.html" "redline_page_snapshot")
[ "$V4" -ge 1 ] && ok "整页快照已落 IndexedDB（不受 localStorage 5MB 限制）" || bad "redline_app 缺少整页快照"
V5=$(count_in "$APP/redline_app/index.html" "await rlReadSnapshot(fp)")
[ "$V5" -ge 1 ] && ok "首屏已前置快照快速通道（跳过全部 b64 下载与重算）" || bad "快照判断未前置，重进仍会冷加载"
V6=$(count_in "$APP/redline_app/index.html" "> 3000) return")
[ "$V6" -ge 1 ] && ok "大表已不再误写 localStorage（配额静默失败已规避）" || warn "旧 localStorage 缓存路径可能仍在空转"
echo "  提示：部署后第一次仍会走完整链路一次并生成共享快照；之后关网页重进应秒开。"
echo "  浏览器里访问 /redline/api/state-fp 可看到 20 位指纹；/redline/api/health 的 snapshot=true 表示共享快照已生成。"

say "5. 子账号文件自查"
if [ -f "$APP/accounts.json" ]; then
  ok "找到 accounts.json（说明界面里保存过子账号）"
  # 只列账号名，不回显明文口令：部署截图/日志常常外发，避免泄露
  echo "  ---- 已保存的账号（口令已隐藏） ----"
  grep -o '"name"[[:space:]]*:[[:space:]]*"[^"]*"' "$APP/accounts.json" 2>/dev/null \
    | sed 's/.*:[[:space:]]*"/  - /; s/"$//' || true
  echo "  --------------"
else
  warn "accounts.json 不存在 —— 还没在「系统设置 → 账号设置」里成功保存过子账号"
  warn "请按：填名称密码 → 点「添加到列表」→ 点「保存（立即生效）」，看到绿色「已保存 N 个账号」才算成功"
fi

say "6. 若失败，看日志"
echo "  sudo journalctl -u sku-spu.service -n 40 --no-pager"
echo "  （常见：No module named 'pandas' → 安装依赖；No module named 'na_core' → 文件漏传；/na/ 404 → na 目录漏传）"
