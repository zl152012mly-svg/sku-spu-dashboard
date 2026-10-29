/* ==========================================================================
   红线价监控 · 旧页面历史数据「全量打包」导出脚本（一次性，只读）
   ==========================================================================
   用在哪：
     浏览器打开 https://workbuddy.link/p/qnbMlfGvsBiwUcU6yzGPoQ
     （必须在这个公开页上，且已登录能正常看到数据）

   怎么跑（两种都行）：
     ① 书签栏点「红线价导出」（见 一键书签_离线版.html，脚本已编进书签里）
     ② F12 → Console → 先手打 allow pasting 回车 → 粘贴本文件全部内容

   它会做什么：
     1) 分页读完「红线价-上传数据」和「红线价-历史期」两张云表的**全部**记录
     2) 按 (数据源, 店铺, 文件) 分组；同一组若因历史并发残留了「两代」记录，
        按「更新时间」取最新一代（避免把旧数据拼进新数据里，导致解码失败）
     3) 每组都真的解压一遍（pako.inflate + JSON.parse），当场告诉你解压是否成功
     4) 通过后自动下载 redline_cloud_export.json

   注意：
     - 只读，不写云表、不删任何东西，可以放心重复执行
     - 得到的 json 上传到 ECS /root/ 后执行导入（见包内 README_IMPORT.txt）

   2026-10-29 v3：不再只认 window.__SMART_PAGE__.database —— 该全局在页面
   初始化后可能被撤掉，但应用自己还握着引用（Cloud.db）。改为四路兜底 +
   失败时打印诊断信息。
   ========================================================================== */
(async () => {
  const DB_UP = 'o8eueNCSALEFaEvwEOrsLp';   // 红线价-上传数据
  const DB_PER = 'qYAZ8rShRGqSLyWLQbtWXP';  // 红线价-历史期

  // ---- 页面右下角提示条：让不熟悉控制台的人也能看到「有反应」 ----
  function toast(text, kind) {
    try {
      const id = '__rl_exp_toast';
      let el = document.getElementById(id);
      if (!el) {
        el = document.createElement('div');
        el.id = id;
        el.style.cssText = 'position:fixed;right:18px;bottom:18px;z-index:2147483647;'
          + 'max-width:520px;max-height:70vh;overflow:auto;'
          + 'padding:14px 16px;border-radius:12px;white-space:pre-wrap;'
          + 'font:14px/1.65 -apple-system,"Microsoft YaHei",sans-serif;color:#fff;'
          + 'box-shadow:0 10px 30px rgba(0,0,0,.22);background:#2563eb;';
        document.body.appendChild(el);
      }
      el.style.background = kind === 'ok' ? '#16a34a' : (kind === 'err' ? '#dc2626' : '#2563eb');
      el.textContent = text;
      return el;
    } catch (e) { return null; }
  }

  // 防重复执行
  window.__RL_LOADED = true;
  if (window.__RL_EXPORT_RUNNING) {
    toast('⏳ 导出已经在运行了，请看这里的进度（别重复点）');
    return;
  }
  window.__RL_EXPORT_RUNNING = true;

  // ================= 1. 找到云表客户端（四路兜底） =================
  let db = null;          // 直接可用的云表客户端（有 .query）
  let appCloud = null;    // 页面自己的 Cloud 对象（有 queryUploads/queryPeriods）
  let via = '';
  const diag = [];

  try {
    if (window.__SMART_PAGE__ && window.__SMART_PAGE__.database) {
      db = window.__SMART_PAGE__.database; via = '__SMART_PAGE__.database';
    }
    diag.push('__SMART_PAGE__ = ' + (typeof window.__SMART_PAGE__)
      + (window.__SMART_PAGE__ ? '（字段: ' + Object.keys(window.__SMART_PAGE__).join(',') + '）' : ''));
  } catch (e) { diag.push('__SMART_PAGE__ 读取异常: ' + e.message); }

  try {
    // Cloud 是页面里 `const Cloud = {...}`，不是 window 属性，但全局词法环境里能直接拿到
    if (typeof Cloud !== 'undefined' && Cloud) {
      appCloud = Cloud;
      diag.push('Cloud.mode = ' + Cloud.mode + ' / Cloud.db = ' + (Cloud.db ? '有' : '空'));
      if (!db && Cloud.db) { db = Cloud.db; via = 'Cloud.db'; }
    } else {
      diag.push('Cloud = undefined');
    }
  } catch (e) { diag.push('Cloud 读取异常: ' + e.message); }

  try {
    if (!db && typeof window.db !== 'undefined' && window.db && typeof window.db.query === 'function') {
      db = window.db; via = 'window.db';
    }
  } catch (e) {}

  // 只有能取到数据才继续
  const canRead = !!db || !!(appCloud && typeof appCloud.queryUploads === 'function' && appCloud.mode === 'smartpage');
  if (!canRead) {
    window.__RL_EXPORT_RUNNING = false;
    const msg = '❌ 没找到云表客户端，取不到数据\n\n'
      + '请确认：你是在 https://workbuddy.link/p/qnbMlfGvsBiwUcU6yzGPoQ 打开的页面，\n'
      + '而且页面已经显示出数据（能看到 5510 SKU 那种）。\n\n'
      + '—— 诊断信息 ——\n' + diag.join('\n')
      + '\npako = ' + (typeof pako);
    console.error('[导出] ' + msg);
    toast(msg, 'err');
    alert(msg);
    return;
  }
  console.log('[导出] 数据通道 =', via || 'Cloud.queryUploads', '|', diag.join(' | '));

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  toast('⏳ 已启动，正在连接云端数据库…');

  // ================= 2. 读全部记录 =================
  async function queryAll(databaseId, name) {
    const all = [];
    let cursor = '';
    for (let page = 1; page <= 500; page++) {
      if (page === 1 || page % 3 === 0) {
        toast('⏳ 正在读取「' + name + '」…\n已读 ' + all.length + ' 条记录，请稍等（不要关页面）');
      }
      const res = await db.query({ databaseId: databaseId, pageSize: 200, startCursor: cursor || undefined });
      const rs = (res && res.results) || [];
      all.push.apply(all, rs);
      console.log('[导出] ' + name + ' 第 ' + page + ' 页：+' + rs.length + ' 条（累计 ' + all.length + '）');
      cursor = (res && res.nextCursor) || '';
      if (!res || !res.hasMore || !cursor) break;
      await sleep(120);
    }
    return all;
  }

  async function readTable(kind) {
    const isUp = kind === 'up';
    const name = isUp ? '上传数据表' : '历史期表';
    if (db) return await queryAll(isUp ? DB_UP : DB_PER, name);
    // 没有原始 db，就借页面自己的方法（它内部有分页逻辑）
    toast('⏳ 正在通过页面自身接口读取「' + name + '」…');
    const arr = isUp ? await appCloud.queryUploads() : await appCloud.queryPeriods();
    return Array.isArray(arr) ? arr : [];
  }

  // 字段名归一：smartpage 用中文列名，rest 用蛇形
  function pick(r, cn, en) {
    if (!r) return undefined;
    const v = r[cn];
    if (v !== undefined && v !== null) return v;
    return r[en];
  }

  const n2 = (v) => {
    if (v === undefined || v === null || v === '') return 0;
    const n = Number(v);
    return isNaN(n) ? 0 : n;
  };

  function tsMs(s) {
    if (!s) return 0;
    const m = String(s).match(/(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})[ T](\d{1,2}):(\d{1,2}):(\d{1,2})(?:\.(\d{1,3}))?/);
    if (!m) return 0;
    return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6], +(m[7] || 0));
  }

  const upRecs = await readTable('up');
  const perRecs = await readTable('per');
  console.log('[导出] 读到：上传数据表 ' + upRecs.length + ' 条，历史期表 ' + perRecs.length + ' 条');

  if (!upRecs.length && !perRecs.length) {
    window.__RL_EXPORT_RUNNING = false;
    toast('⚠️ 两张云表都是空的，没有可导出的数据。\n\n'
      + '如果你页面上明明看得到数据，说明数据只在本机浏览器里、没同步到云端 ——\n'
      + '这种情况只能重新上传原始文件到新系统。', 'err');
    return;
  }

  // ================= 3. 按 (数据源,店铺,文件) 分组 + 分代去重 =================
  const SEP = '\u0001';
  const groups = new Map();
  for (const r of upRecs) {
    const key = [pick(r, '数据源', 'src') || '', pick(r, '店铺', 'store') || '', pick(r, '文件', 'file') || ''].join(SEP);
    if (!groups.has(key)) groups.set(key, { key: key, gens: new Map() });
    const g = groups.get(key);
    const t = tsMs(pick(r, '更新时间', 'updated_at'));
    if (!g.gens.has(t)) g.gens.set(t, { t: t, recs: [] });
    g.gens.get(t).recs.push(r);
  }

  // 真解压校验
  function probe(chunks) {
    try {
      const b64 = (chunks || []).join('');
      if (!b64) return { ok: false, err: '空数据' };
      const bin = atob(b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      const obj = JSON.parse(new TextDecoder().decode(pako.inflate(bytes)));
      return { ok: true, obj: obj };
    } catch (e) {
      return { ok: false, err: e.message };
    }
  }

  function rowsOf(obj) {
    if (!obj) return 0;
    if (Array.isArray(obj.aoa)) return obj.aoa.length;
    if (Array.isArray(obj.raw)) return obj.raw.length;
    if (obj.fixes && typeof obj.fixes === 'object') return Object.keys(obj.fixes).length;
    return 0;
  }

  const sources = [];
  const report = [];
  let badCount = 0;

  const sortedGroups = Array.from(groups.values()).sort((a, b) => String(a.key).localeCompare(String(b.key)));
  for (const g of sortedGroups) {
    let best = null;
    for (const gen of g.gens.values()) {
      if (!best || gen.t > best.t || (gen.t === best.t && gen.recs.length > best.recs.length)) best = gen;
    }
    const recs = best.recs.slice().sort((a, b) => n2(pick(a, '分块', 'chunk')) - n2(pick(b, '分块', 'chunk')));
    const parts = String(g.key).split(SEP);
    const chunks = recs.map((r) => pick(r, '数据', 'data') || '');
    const pr = probe(chunks);
    if (!pr.ok) badCount++;
    sources.push({
      src: parts[0] || '',
      store: parts[1] || '',
      file: parts[2] || '',
      updated_at: pick(recs[0], '更新时间', 'updated_at') || '',
      chunks: chunks
    });
    report.push({
      数据源: parts[0] || '-',
      店铺: parts[1] || '-',
      文件: parts[2] || '-',
      块数: chunks.length,
      记录行数: pr.ok ? rowsOf(pr.obj) : 0,
      解压: pr.ok ? 'OK' : 'FAIL(' + pr.err + ')',
      同组代数: g.gens.size
    });
  }

  // ================= 4. 历史期（按期次去重） =================
  const perMap = new Map();
  for (const r of perRecs) {
    const pid = String(n2(pick(r, '期次', 'period')));
    if (!perMap.has(pid)) perMap.set(pid, { pid: pid, date: '', label: '', byChunk: new Map() });
    const p = perMap.get(pid);
    if (!p.date && pick(r, '日期', 'date')) p.date = pick(r, '日期', 'date');
    if (!p.label && pick(r, '标签', 'label')) p.label = pick(r, '标签', 'label');
    // 同 期次 若残留两代记录（同分块号重复），后出现的覆盖前面的
    p.byChunk.set(n2(pick(r, '分块', 'chunk')), pick(r, '数据', 'data') || '');
  }

  const periods = [];
  const perSorted = Array.from(perMap.values()).sort((a, b) => Number(a.pid) - Number(b.pid));
  for (const p of perSorted) {
    const ks = Array.from(p.byChunk.keys()).sort((a, b) => a - b);
    const chunks = ks.map((k) => p.byChunk.get(k));
    const pr = probe(chunks);
    if (!pr.ok) badCount++;
    periods.push({ period: Number(p.pid) || 0, date: p.date || '', label: p.label || '', chunks: chunks });
    report.push({
      数据源: '【历史期】',
      店铺: '-',
      文件: p.label || String(p.pid),
      块数: chunks.length,
      记录行数: (pr.ok && pr.obj && Array.isArray(pr.obj.rows)) ? pr.obj.rows.length : 0,
      解压: pr.ok ? 'OK' : 'FAIL(' + pr.err + ')',
      同组代数: 1
    });
  }

  console.log('%c[导出] 分组明细', 'color:#2563eb;font-weight:bold');
  console.table(report);

  if (badCount > 0) {
    const msg = '有 ' + badCount + ' 组数据解压失败（见控制台表格「解压」列）。\n'
      + '说明云端确实存在拼接错乱/残缺记录。\n'
      + '失败组会被导入端跳过，不影响其他组。\n\n仍要继续下载吗？';
    if (!confirm(msg)) { window.__RL_EXPORT_RUNNING = false; return; }
  }

  // ================= 5. 打包下载 =================
  const payload = {
    manifest: {
      exported_at: new Date().toLocaleString('zh-CN'),
      source_page: location.href,
      via: via || 'Cloud.queryUploads',
      upload_db: DB_UP,
      period_db: DB_PER,
      upload_records: upRecs.length,
      period_records: perRecs.length,
      sources: sources.length,
      periods: periods.length,
      failed_groups: badCount
    },
    sources: sources,
    periods: periods
  };

  const text = JSON.stringify(payload);
  const blob = new Blob([text], { type: 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'redline_cloud_export.json';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 8000);

  const summary = '✅ 导出完成，文件已开始下载：\n'
    + '· 数据源 ' + sources.length + ' 组（原始记录 ' + upRecs.length + ' 条）\n'
    + '· 历史期 ' + periods.length + ' 期（原始记录 ' + perRecs.length + ' 条）\n'
    + '· 解压失败 ' + badCount + ' 组\n'
    + '· 文件大小 ' + (text.length / 1024 / 1024).toFixed(2) + ' MB\n\n'
    + '文件名：redline_cloud_export.json\n'
    + '下一步：上传到 ECS 的 /root/ 目录，再跑导入命令。';

  console.log('%c' + summary, 'color:#16a34a;font-weight:bold');
  console.table(report);
  window.__RL_EXPORT_RUNNING = false;
  toast(summary, 'ok');
  alert(summary);
})().catch((e) => {
  window.__RL_EXPORT_RUNNING = false;
  console.error('[导出] 出错：', e);
  try {
    const d = document.getElementById('__rl_exp_toast');
    const msg = '❌ 导出出错：' + (e && e.message ? e.message : e) + '\n\n请把控制台里红色的报错截图发给 AI。';
    if (d) { d.style.background = '#dc2626'; d.textContent = msg; }
    else { alert(msg); }
  } catch (_) {}
});
