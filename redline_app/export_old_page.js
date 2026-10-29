/* ==========================================================================
   红线价监控 · 旧页面历史数据「全量打包」导出脚本（一次性，只读）
   ==========================================================================
   用在哪：
     浏览器打开 https://workbuddy.link/p/qnbMlfGvsBiwUcU6yzGPoQ
     （必须在这个公开页上，且已登录能正常看到数据）
     F12 → Console 控制台 → 整段粘贴 → 回车

   它会做什么：
     1) 分页读完「红线价-上传数据」和「红线价-历史期」两张云表的**全部**记录
     2) 按 (数据源, 店铺, 文件) 分组；同一组若因历史并发残留了「两代」记录，
        按「更新时间」取最新一代（避免把旧数据拼进新数据里，导致解码失败）
     3) 每组都真的解压一遍（pako.inflate + JSON.parse），当场告诉你解压是否成功
     4) 通过后自动下载 redline_cloud_export.json

   注意：
     - 只读，不写云表、不删任何东西，可以放心重复执行
     - 下载被拦的话，请换到公开链接 /p/ 页面再跑（编辑器预览 iframe 没有下载权限）
     - 得到的 json 上传到 ECS /root/ 后，执行：
         cd /opt/sku-spu && set -a && . ./.env && set +a \
           && ./venv/bin/python redline_import.py /root/redline_cloud_export.json
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

  // 防重复执行：控制台里误粘两次会跑两遍、下载两个文件
  window.__RL_LOADED = true;   // 供「一行加载器」的心跳检测用
  if (window.__RL_EXPORT_RUNNING) {
    toast('⏳ 导出已经在运行了，请看这里的进度（别重复粘贴）');
    return;
  }
  window.__RL_EXPORT_RUNNING = true;

  const db = (window.__SMART_PAGE__ && window.__SMART_PAGE__.database) || null;
  if (!db) {
    window.__RL_EXPORT_RUNNING = false;
    console.error('[导出] 没检测到资料库 SDK。请确认：① 当前就是 workbuddy.link/p/ 公开页；② 页面已正常加载出数据。');
    toast('❌ 没检测到资料库 SDK\n\n请在 workbuddy.link/p/ 这个公开页面上执行本脚本\n（不要在编辑器预览页、也不要在新系统 8092 页面执行）', 'err');
    alert('没检测到资料库 SDK —— 请在 workbuddy.link/p/ 这个公开页面上执行本脚本。');
    return;
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  toast('⏳ 已启动，正在连接云端数据库…');

  async function queryAll(databaseId, name) {
    const all = [];
    let cursor = '';
    for (let page = 1; page <= 500; page++) {
      if (page === 1 || page % 3 === 0) toast('⏳ 正在读取「' + name + '」…\n已读 ' + all.length + ' 条记录，请稍等（不要关页面）');
      const res = await db.query({ databaseId, pageSize: 200, startCursor: cursor || undefined });
      const rs = (res && res.results) || [];
      all.push.apply(all, rs);
      console.log(`[导出] ${name} 第 ${page} 页：+${rs.length} 条（累计 ${all.length}）`);
      cursor = (res && res.nextCursor) || '';
      if (!res || !res.hasMore || !cursor) break;
      await sleep(120);
    }
    return all;
  }

  const n2 = (v) => (v === undefined || v === null || v === '' ? 0 : Number(v) || 0);

  function tsMs(s) {
    if (!s) return 0;
    const m = String(s).match(/(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})[ T](\d{1,2}):(\d{1,2}):(\d{1,2})(?:\.(\d{1,3}))?/);
    if (!m) return 0;
    return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6], +(m[7] || 0));
  }

  // 按 key 分组；同组内按「更新时间」分「代」，取最新一代；代内按「分块」升序拼回
  function groupByGen(records, keyFn, timeFn) {
    const groups = new Map();
    for (const r of records) {
      const k = keyFn(r);
      if (!groups.has(k)) groups.set(k, { key: k, gens: new Map() });
      const g = groups.get(k);
      const t = timeFn(r);
      if (!g.gens.has(t)) g.gens.set(t, { t, recs: [] });
      g.gens.get(t).recs.push(r);
    }
    const out = [];
    for (const g of groups.values()) {
      let best = null;
      for (const gen of g.gens.values()) {
        if (!best || gen.t > best.t || (gen.t === best.t && gen.recs.length > best.recs.length)) best = gen;
      }
      const recs = best.recs.slice().sort((a, b) => n2(a['分块']) - n2(b['分块']));
      out.push({ key: g.key, t: best.t, recs, genCount: g.gens.size });
    }
    out.sort((a, b) => String(a.key).localeCompare(String(b.key)));
    return out;
  }

  // 真解压校验：base64 → inflate → JSON
  function probe(chunks) {
    try {
      const b64 = (chunks || []).join('');
      if (!b64) return { ok: false, err: '空数据' };
      const bin = atob(b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      const obj = JSON.parse(new TextDecoder().decode(pako.inflate(bytes)));
      return { ok: true, obj };
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

  console.log('%c[导出] 开始读取云表…', 'color:#2563eb;font-weight:bold');
  const upRecs = await queryAll(DB_UP, '上传数据表');
  const perRecs = await queryAll(DB_PER, '历史期表');
  console.log(`[导出] 读到：上传数据表 ${upRecs.length} 条，历史期表 ${perRecs.length} 条`);

  if (!upRecs.length && !perRecs.length) {
    alert('两张云表都是空的，没有可导出的数据。');
    return;
  }

  // ---- 数据源 ----
  const SEP = '\u0001';
  const srcGroups = groupByGen(
    upRecs,
    (r) => [r['数据源'] || '', r['店铺'] || '', r['文件'] || ''].join(SEP),
    (r) => tsMs(r['更新时间'])
  );

  const sources = [];
  const report = [];
  let badCount = 0;
  for (const g of srcGroups) {
    const parts = String(g.key).split(SEP);
    const chunks = g.recs.map((r) => r['数据'] || '');
    const pr = probe(chunks);
    if (!pr.ok) badCount++;
    sources.push({
      src: parts[0] || '',
      store: parts[1] || '',
      file: parts[2] || '',
      updated_at: g.recs[0]['更新时间'] || '',
      chunks: chunks
    });
    report.push({
      数据源: parts[0] || '-',
      店铺: parts[1] || '-',
      文件: parts[2] || '-',
      块数: chunks.length,
      记录行数: pr.ok ? rowsOf(pr.obj) : 0,
      解压: pr.ok ? 'OK' : 'FAIL(' + pr.err + ')',
      同组代数: g.genCount
    });
  }

  // ---- 历史期 ----
  const perMap = new Map();
  for (const r of perRecs) {
    const pid = String(n2(r['期次']));
    if (!perMap.has(pid)) perMap.set(pid, { pid, date: '', label: '', byChunk: new Map() });
    const p = perMap.get(pid);
    if (!p.date && r['日期']) p.date = r['日期'];
    if (!p.label && r['标签']) p.label = r['标签'];
    // 同一个 期次 若残留两代记录（同一分块号重复），后出现的覆盖前面的（= 更新）
    p.byChunk.set(n2(r['分块']), r['数据'] || '');
  }
  const periods = [];
  for (const p of Array.from(perMap.values()).sort((a, b) => Number(a.pid) - Number(b.pid))) {
    const ks = Array.from(p.byChunk.keys()).sort((a, b) => a - b);
    const chunks = ks.map((k) => p.byChunk.get(k));
    const pr = probe(chunks);
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
    const msg = '有 ' + badCount + ' 组数据解压失败（见控制台表格里「解压」列）。\n'
      + '这说明云端确实存在拼接错乱/残缺记录。\n'
      + '建议：先把这张表格截图给 AI 看，再决定是否导入（失败组会被跳过，不影响其他组）。\n\n仍要继续下载吗？';
    if (!confirm(msg)) { window.__RL_EXPORT_RUNNING = false; return; }
  }

  const payload = {
    manifest: {
      exported_at: new Date().toLocaleString('zh-CN'),
      source_page: location.href,
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
    + '下一步：把它上传到 ECS 的 /root/ 目录，再跑导入命令。';
  console.log('%c' + summary, 'color:#16a34a;font-weight:bold');
  window.__RL_EXPORT_RUNNING = false;
  toast(summary, 'ok');
  alert(summary);
})().catch((e) => {
  window.__RL_EXPORT_RUNNING = false;
  console.error('[导出] 出错：', e);
  try {
    const d = document.getElementById('__rl_exp_toast');
    if (d) {
      d.style.background = '#dc2626';
      d.textContent = '❌ 导出出错：' + (e && e.message ? e.message : e)
        + '\n\n请把控制台里红色的报错截图发给 AI。';
    }
  } catch (_) {}
});
