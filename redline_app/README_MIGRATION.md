# 红线价监控模块 · 接入与数据迁移说明

## 1. 架构（已按此实现）

- **同一 Flask 进程**：新增蓝图 `/redline`（`redline_routes.py`），随主系统一起启动。
- **存储**：`redline_store.py`，复用主系统 `storage.py` 的 OSS 配置（同一 bucket，`redline/` 前缀隔离，互不干扰）；无 OSS 凭证时回落到本地 `data/redline/`。
- **页面**：`redline_app/index.html`（红线价单文件应用）。服务时自动：
  - 注入 `window.REDLINE_BACKEND='rest'`、`window.REDLINE_API_BASE='/redline/api'`；
  - 把 xlsx / echarts / pako 三个 CDN 换成 `/static/libs/` 本地库（不依赖公网 CDN）；
  - 导出兜底链接改为当前页面地址。
- **入口**：控制台 `/console` 左侧导航「📉 红线价监控」（管理员可见）。
- **鉴权**：复用 `auth.current_role()`；页面未登录跳 `/login`，接口返回 401 JSON。

## 2. 新增 / 修改文件

| 文件 | 说明 |
|---|---|
| `redline_store.py` | 新增，OSS + JSON 存储层 |
| `redline_routes.py` | 新增，`/redline` 蓝图（页面 + REST + 迁移导入） |
| `redline_import.py` | 新增，离线导入 CLI |
| `redline_app/index.html` | 新增，红线价单文件应用 |
| `static/libs/xlsx.full.min.js` | 新增，本地化（0.18.5，与 CDN 同版本） |
| `static/libs/pako.min.js` | 新增，本地化 |
| `app.py` | 修改，注册 `redline_bp`（na 之后） |
| `templates/console_index.html` | 修改，新增导航项 |

## 3. 部署

1. 在本地把 `system/` 的新增文件同步到 `deploy/`（本次已同步；`deploy/` 为 GitHub 仓库根）。
2. GitHub Desktop 提交并 push `main`（**需确认后再执行**）。
3. ECS 上（推荐用一键脚本，`deploy/redline_update.sh`，幂等可重跑）：

   ```bash
   curl -fsSL -o /tmp/redline.sh https://raw.githubusercontent.com/zl152012mly-svg/sku-spu-dashboard/main/redline_update.sh
   bash /tmp/redline.sh                                  # 只更新代码
   bash /tmp/redline.sh /root/redline_cloud_export.json   # 更新代码 + 顺便导数据
   ```

   脚本会：备份被覆盖文件到 `/tmp/redline_bak_<ts>/` → `git merge --ff-only`
   （失败自动转 curl 逐文件拉取）→ 关键文件点检 → **重启前** `import redline_routes`
   自检（语法/缺文件当场暴露，不会把站点搞挂）→ 重启 → 验证 4 个 URL。
   手动等价命令：

   ```bash
   cd /opt/sku-spu        # 以实际部署目录为准
   git pull
   systemctl restart sku-spu    # 或 docker restart sku-spu
   ```

4. 验证清单（脚本会打印前三项）：
   - [ ] `/api/ping` → 200、`/console` → 302、`/redline/` → **302**（不是 404）
   - [ ] `/redline/api/health` → **401**（未登录时；401 才说明鉴权生效）
   - [ ] 登录 `/console`，左侧出现「红线价监控」；
   - [ ] 打开后 F12 → Network：`/redline/api/sources` 返回 200（首次为 `[]`）；
   - [ ] health 里 `storage.backend` 为 `oss`（若为 `json` 说明 `.env` 凭证没加载，数据落本地盘）；
   - [ ] 上传一个数据源 → 刷新页面仍在（证明写入 OSS）；
   - [ ] 重启服务后再打开，数据仍在（证明冷启动从 OSS 恢复）。

## 4. 历史数据迁移（资料库 → OSS）

> 目标：把旧 `workbuddy.link` 页面里「资料库云表」的**全部数据源 + 历史期**原样搬到 ECS/OSS，保留历史。

### Step 1 · 在旧页面导出

打开旧页面 <https://workbuddy.link/p/qnbMlfGvsBiwUcU6yzGPoQ>，等数据加载完，
F12 → Console，把同目录下的 **`export_old_page.js`** 整段粘贴执行。

> 2026-10-29 已用 8/20 真实云快照在 jsdom 里跑过 15 条断言全绿
> （分组 5 组 / 分块顺序 / 真实 inflate 还原 5136·16207·61567·1363·7048 行 / 分代去重）。
> 早期贴在本文档里的旧版片段已删除，**不要再手工复制旧片段**。

该脚本的行为（只读，不写云表、不删任何东西，可重复执行）：

1. 分页读完整 `红线价-上传数据`（o8eueNCSALEFaEvwEOrsLp）与 `红线价-历史期`（qYAZ8rShRGqSLyWLQbtWXP）；
2. 按 `(数据源, 店铺, 文件)` 分组；同一组若因历史并发残留「两代」记录，
   按 `更新时间` 取最新一代（不依赖 `restoreGroupRecords`，自带同样的组合逻辑）；
3. 每块按 `分块` 号升序拼回；每组都**真解压一遍**（`atob` → `pako.inflate` → `JSON.parse`），
   控制台打印表格：块数 / 记录行数 / 解压 OK? / 同组代数；
4. 有组解压失败会先 `confirm` 让你决定是否继续（失败组会被导入端跳过，不影响其他组）；
5. 通过后下载 `redline_cloud_export.json`，并 alert 汇总（组数 / 期数 / 失败数 / 文件大小）。

> 若下载没反应：确认是在 `/p/` 公开链接页执行（编辑器预览 iframe 无 `allow-downloads` 权限）。


### Step 2 · 在 ECS 导入

把 `redline_cloud_export.json` 传到 ECS（scp / 任意上传目录），执行：

```bash
cd /opt/sku-spu
set -a; source .env; set +a          # 让 OSS 凭证生效
venv/bin/python redline_import.py redline_cloud_export.json
```

输出应类似：
```
存储后端: oss
待导入: sources=7, periods=4
  [1/7] 红线价表 / - / - -> OK (3 块)
  ...
导入完成：sources=7/7, periods=4
```

> 覆盖式写入，可重复执行。历史期会整体替换（与旧系统 `syncPeriods` 语义一致）。

## 5. 注意事项

- ⚠ **`system/` 与 `deploy/` 已分叉**：`deploy/app.py` 多了 gzip 响应压缩等 ECS 专属优化。**不要用 `system/app.py` 整文件覆盖 `deploy/app.py`**，否则会丢 gzip。本次已对两侧分别做精准插入。
- 单个数据源可能数 MB，`/redline/api/sources` 首次加载较慢属正常（返回的是已压缩的 base64，体积已比原始 JSON 小很多）。
- 若 `STORAGE_MODE` 不是 `oss`（例如 `json`），数据会落到 `data/redline/`，仅本机可见，重启/换机不保留。
