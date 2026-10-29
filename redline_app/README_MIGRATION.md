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
3. ECS 上：
   ```bash
   cd /opt/sku-spu        # 以实际部署目录为准
   git pull
   systemctl restart sku-spu    # 或 docker restart sku-spu
   ```
4. 验证清单：
   - [ ] 登录 `/console`，左侧出现「红线价监控」；
   - [ ] 打开后 F12 → Network：`/redline/api/sources` 返回 200（首次为 `[]`）；
   - [ ] `/redline/api/health` 返回 `{"ok":true,"storage":{"backend":"oss",...}}`；
   - [ ] 上传一个数据源 → 刷新页面仍在（证明写入 OSS）；
   - [ ] 重启服务后再打开，数据仍在（证明冷启动从 OSS 恢复）。

## 4. 历史数据迁移（资料库 → OSS）

> 目标：把旧 `workbuddy.link` 页面里「资料库云表」的**全部数据源 + 历史期**原样搬到 ECS/OSS，保留历史。

### Step 1 · 在旧页面导出

打开旧页面 <https://workbuddy.link/p/qnbMlfGvsBiwUcU6yzGPoQ>，等数据加载完，在浏览器控制台粘贴执行：

```js
(async () => {
  const ups = await Cloud.queryUploads();
  const pers = await Cloud.queryPeriods();
  const groups = {};
  ups.forEach(r => {
    if (Number(r['期次']) !== 1) return;
    const k = [r['数据源'], r['店铺'] || '', r['文件'] || ''].join('\u0001');
    (groups[k] = groups[k] || []).push(r);
  });
  const sources = [];
  for (const k of Object.keys(groups)) {
    const g = restoreGroupRecords(groups[k]);
    if (!g.obj) { console.warn('跳过无法解压的数据源', k); continue; }
    const p = k.split('\u0001');
    const chunks = (g.recs || []).slice()
      .sort((a, b) => (Number(a['分块']) || 0) - (Number(b['分块']) || 0))
      .map(r => r['数据']);
    sources.push({
      src: p[0], store: p[1] || '', file: p[2] || '',
      updated_at: ((g.recs || [])[0] || {})['更新时间'] || '', chunks
    });
  }
  const pg = {};
  pers.forEach(r => { const k = r['期次']; (pg[k] = pg[k] || []).push(r); });
  const periods = Object.keys(pg).map(k => {
    const g = restoreGroupRecords(pg[k]);
    const first = (g.recs || [])[0] || {};
    const chunks = (g.recs || []).slice()
      .sort((a, b) => (Number(a['分块']) || 0) - (Number(b['分块']) || 0))
      .map(r => r['数据']);
    return { period: Number(k), date: first['日期'] || '', label: first['标签'] || '', chunks };
  });
  const blob = new Blob([JSON.stringify({ sources, periods })], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'redline_cloud_export.json';
  a.click();
  console.log('导出完成：sources=' + sources.length + ', periods=' + periods.length);
})().catch(e => console.error(e));
```

> 脚本复用了页面自带的 `restoreGroupRecords`，会自动处理「并发保存残留多代记录」，只导出完整的一代。

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
