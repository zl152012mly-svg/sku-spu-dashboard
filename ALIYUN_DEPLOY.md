# SKU&SPU 清洗看板 —— 迁移到阿里云（ECS + OSS）

把原 Render（海外 + Supabase）的云端部署迁移到**阿里云 ECS 常驻 + 对象存储 OSS**，
解决此前反复遇到的 502（Cloudflare 海外抖动）、免费层冷启动、以及 Supabase 分块上传脆弱性。

- **应用部署**：Render 免费层 → `ECS 常驻 Linux`（gunicorn + nginx），国内直连、无冷启动。
- **文件存储**：Supabase 表分块 base64 → `OSS 标准存储`，文件直接 `put_object`，无需分块。
- **标注/版本元数据**：Supabase 表 → `OSS 上的 JSON 文件`（零额外数据库）。

> 代码层面存储后端通过 `storage.py` 的 `STORAGE_MODE` 切换，**JSON / Supabase / OSS 三种后端共存**，
> 迁移是「加一个 oss 后端 + 改环境变量」的最小改动，不改业务逻辑。

---

## 一、前置准备（需你提供，密钥走环境变量，绝不写进代码/记忆）

1. **阿里云 RAM 子账号 AccessKey**（最小权限：仅目标 bucket 的 `oss:*` 或 `PutObject/GetObject/DeleteObject/ListObjects`）。
   - 不要用主账号 AK。在「RAM 控制台 → 创建 AccessKey」生成。
2. **OSS Bucket**（同地域，如 `oss-cn-hangzhou`），**读写权限选私有**即可（看板不依赖公网直链）。
3. **ECS 实例**（你已有）：Linux（推荐 Ubuntu 22.04 / Alibaba Cloud Linux 3），安全组放行 80/443/8092。

---

## 二、部署方式 A：Docker（推荐，复用在 Render 的同款镜像）

1. 把 `deploy/` 作为代码源（已含 Dockerfile）。首次在 ECS 上：
   ```bash
   git clone <你的仓库> sku-spu && cd sku-spu
   # 或在本地构建后推镜像到阿里云容器镜像 ACR，ECS 拉取
   docker build -t sku-spu-dashboard .
   ```
2. 运行（密钥走环境变量，不入镜像）：
   ```bash
   docker run -d --restart=always --name sku-spu \
     -p 8092:8092 \
     -e ACCESS_PASSWORD=lucie2026 \
     -e STORAGE_MODE=oss \
     -e OSS_ENDPOINT=oss-cn-hangzhou.aliyuncs.com \
     -e OSS_ACCESS_KEY_ID=<AK> \
     -e OSS_ACCESS_KEY_SECRET=<SK> \
     -e OSS_BUCKET=sku-spu-data \
     sku-spu-dashboard
   ```
3. 配 nginx 反代（见 `nginx.conf`），把 80/443 → 127.0.0.1:8092。

---

## 三、部署方式 B：直接 gunicorn（不用 Docker）

1. ECS 装 Python 3.11 + 建虚拟环境：
   ```bash
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt   # 已含 oss2
   ```
2. 复制 `.env.example` 为 `.env`，填入真实 OSS 凭证（`.env` 已被 `.gitignore` 忽略）：
   ```bash
   cp .env.example .env && vi .env
   ```
3. 启动（前台调试 / 或配合 systemd / nohup 常驻）：
   ```bash
   chmod +x start_oss.sh
   ./start_oss.sh
   ```
4. 配 nginx 反代（见 `nginx.conf`）。建议用 systemd 托管保证开机自启（示例 unit 见文末）。

---

## 四、迁移已有数据（可选，但建议做）

原 Render+Supabase 上的**异常确认字段**和**数据源文件**都在 Supabase；迁移到 OSS 后默认是空桶。
两种处理：
- **轻量**：新环境从头开始，同事在网页重新上传 7 个数据源即可（上传即同步 OSS，冷启动自动重建）。
- **全量迁移**（保留历史异常确认）：临时写个小脚本，用 `supabase` 模式 `annot_get()` 读出，再切 `oss`
  模式 `annot_update()` 写回；数据源文件用 `ds_file_download` → `ds_file_upload` 搬运。

> 注意：三种后端的数据**不互通**，切换 `STORAGE_MODE` 后需重新上传/迁移；不要把同一份数据同时写两种后端。

---

## 五、验证清单（部署后务必逐项确认）

- [ ] 访问看板，能登录、能看到「云端同步」标志（`/api/status` 的 `shared_storage=true`）。
- [ ] 网页上传 1 个数据源（如转单表）→ 提示成功 → 刷新后仍显示「已上传」（证明写入 OSS）。
- [ ] 停掉容器/ECS 进程再重启 → 冷启动自动从 OSS 拉回数据源重建（不再空白/回退旧数据）。
- [ ] 改一条异常确认状态 → 刷新页面仍在（证明 annotations.json 写入 OSS 且行级 version 生效）。
- [ ] `/api/status` 的 `cloud_error` 为空（有错会显示 OSS 报错，便于排障）。

---

## 六、systemd 常驻单元（方式 B 用，可选）

`/etc/systemd/system/sku-spu.service`：
```ini
[Unit]
Description=SKU&SPU Dashboard (gunicorn + OSS)
After=network.target

[Service]
WorkingDirectory=/opt/sku-spu
EnvironmentFile=/opt/sku-spu/.env
ExecStart=/opt/sku-spu/venv/bin/gunicorn -c gunicorn.conf.py wsgi:app
Restart=always
User=www-data

[Install]
WantedBy=multi-user.target
```
启用：`systemctl enable --now sku-spu`。

---

## 七、回滚

若阿里云有问题，把 `STORAGE_MODE` 改回 `supabase`（或删除 OSS 凭证走 `json`），重新部署即可回到原方案；
代码已同时支持两种后端，无需改代码回滚。
