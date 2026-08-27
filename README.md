# SKU&SPU 清洗看板 —— 云端部署（Render）

把本地 Flask 看板部署到 Render（免费），满足「电脑关机后同事/家里仍可打开」，且人工编辑字段走 Supabase 云端持久化。

## 架构

- **应用**：Render Docker Web Service（免费实例，一直在线；空闲 15 分钟休眠，首访需 30~50 秒唤醒）
- **人工编辑字段**（异常确认状态/备注 + 数据源版本）：Supabase 云数据库（免费 500MB，永不丢，支持多人在线编辑）
- **数据源**（转单.xlsx + 原始 CSV + 领星/walmart 留档）：随代码仓库提交，应用启动时**自动重建主数据**

## 部署步骤

### 1. 推代码到 GitHub

```bash
cd deploy
git init
git add .
git commit -m "SKU&SPU 看板云端部署"
git remote add origin https://github.com/<你的用户名>/sku-spu-dashboard.git
git push -u origin main
```

> 需要先在 GitHub 网页创建同名空仓库。

### 2. 在 Render 创建 Web Service

1. 打开 https://render.com → 登录 → **New** → **Web Service**
2. 连接 GitHub，选择 `sku-spu-dashboard` 仓库
3. 配置：
   - **Name**：`sku-spu-dashboard`
   - **Region**：`Singapore`（离国内近）
   - **Branch**：`main`
   - **Runtime**：`Docker`
   - **Instance Type**：`Free`
4. 展开 **Advanced** → **Environment Variables**，添加：

| Key | Value |
|-----|-------|
| `ACCESS_PASSWORD` | 看板访问口令（自己定，如 `lucie2026`） |
| `STORAGE_MODE` | `supabase` |
| `SUPABASE_URL` | 你的 Supabase URL |
| `SUPABASE_KEY` | Supabase `service_role` 或 `anon` key（建议 `service_role`，无 RLS 限制） |

### 3. 部署完成

- 点 **Create Web Service**，Render 自动拉代码 → 构建 Docker → 部署
- 完成后会给出公网 URL（形如 `https://sku-spu-dashboard.onrender.com`）
- 首次访问可能等 30~50 秒（冷启动唤醒）

## 环境变量说明

- `ACCESS_PASSWORD`：看板访问口令。**未配置时默认 `lucie2026`**。生产建议修改。
- `STORAGE_MODE`：存储模式。
  - `supabase`（推荐）：人工编辑字段存 Supabase，多人可编辑、在线同步
  - `json` 或不填：存容器临时磁盘，重启/重建**可能丢失**，无法多人在线同步
- `SUPABASE_URL` / `SUPABASE_KEY`：连接 Supabase。**未配置时走 JSON 本地模式**（仅单机演示用）。

## Supabase 建表 SQL

在 Supabase SQL Editor 执行（一次即可）：

```sql
-- 异常确认表（人工编辑字段）
create table if not exists public.sku_annotations (
  id_key text primary key,
  status text,
  note text,
  version integer,
  updated_at timestamptz default now()
);

-- 数据源版本元信息表
create table if not exists public.sku_ds_meta (
  id text primary key,
  meta jsonb,
  updated_at timestamptz default now()
);

-- 授权给 service_role（否则 Python 无权限读写）
grant all on public.sku_annotations to service_role;
grant all on public.sku_ds_meta to service_role;
```

## 注意事项

- **免费实例休眠**：Render 免费层 15 分钟无访问会休眠；下次访问自动唤醒，需几十秒。若需一直即时响应，可升级付费（$7/月）关闭休眠。
- **数据源更新后的重算**：在新代码里更新 `reference/转单.xlsx`、`uploads/latest_*.csv` 后重新推送即可；应用启动会自动重建最新数据。人工编辑字段不受影响（在 Supabase）。
- **端口**：Render 注入 `PORT` 环境变量，应用已适配；本地默认 8092。
