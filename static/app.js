/* SKU&SPU 清洗看板 —— 前端逻辑 */
'use strict';

let HEADER = [], ROWS = [], STATS = null, MISSING_ROWS = [];
let ANNOTS = {};              // id_key -> {status,note,version}，来自 /api/state.annotations
let REF = null, REF_STALE = false, HAS_RAW = false;   // 转单表状态
let LX = null, WM = null;                              // 可选数据源：领星清单 / walmart 报表
let ADMIN = null;                                     // 可选数据源：管理员检查表（品类）
let LX_STALE = false, WM_STALE = false, ADMIN_STALE = false;  // 数据源是否比当前看板新
let COL = {};                 // 列名 -> 索引
let TABLE = null, MISSING_TABLE = null;
let PKG_CHART = null, FAHUO_CHART = null;
const F = { stores: new Set(), pkgs: new Set(), spus: new Set(), skus: new Set(), tag: '', fahuo: '', anom: '' };
let noteTimer = null;

const $$ = (s, r = document) => r.querySelector(s);
const $$a = (s, r = document) => Array.from(r.querySelectorAll(s));

/** 安全解析 JSON：若后端返回 HTML 错误页，给出友好提示 */
async function safeJson(r) {
  const text = await r.text();
  try { return { ok: r.ok && (r.status >= 200 && r.status < 300), d: JSON.parse(text), text: '' }; }
  catch (e) {
    const snippet = text.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 160);
    return { ok: false, d: { msg: snippet || `服务器返回非 JSON（HTTP ${r.status}）` }, text: snippet };
  }
}

function escHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function escAttr(s) {
  return String(s == null ? '' : s).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* ---------- 访问口令（轻级鉴权） ---------- */
const AUTH_KEY = 'sku_spu_auth_token';

async function apiCall(url, opts = {}) {
  opts.headers = Object.assign({}, opts.headers || {});
  const tok = localStorage.getItem(AUTH_KEY);
  if (tok) opts.headers['X-Auth-Token'] = tok;
  const r = await fetch(url, opts);
  if (r.status === 401) { showLogin(); }
  return r;
}

function showLogin() {
  const box = $$('#login-modal');
  if (box) box.classList.remove('hide');
  const inp = $$('#login-pwd');
  if (inp) { inp.focus(); }
}

/* ---------- 明细导出 ---------- */
/** 把行数组导出为 CSV（前端生成，带 BOM 防 Excel 中文乱码；含全部 25 列） */
function exportCsv(rows, name) {
  if (!rows || !rows.length) { alert('没有可导出的数据'); return; }
  const q = v => {
    const s = String(v == null ? '' : v);
    return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const lines = [HEADER.map(q).join(',')];
  for (const r of rows) lines.push(r.map(q).join(','));
  // \ufeff = BOM，Excel 打开 CSV 中文不乱码
  const blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name + '_' + new Date().toISOString().slice(0, 10) + '.csv';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
}

/** 导出「当前筛选结果」（跟随 SPU/唯一SKU/店铺/包裹类型/下拉 等全部筛选条件） */
function exportFiltered() {
  if (!TABLE) { alert('表格尚未就绪'); return; }
  // search:'applied' = 应用了自定义过滤后的行（含筛选 + 表格内搜索）
  const rows = TABLE.rows({ search: 'applied' }).data().toArray();
  const total = ROWS.length;
  exportCsv(rows, 'SKU平台信息_清洗明细_筛选后' + rows.length + '行');
  const tip = $$('#dl-tip');
  if (tip) {
    tip.textContent = `已导出 ${rows.length} 行筛选结果（全部 ${total} 行）`;
    setTimeout(() => { tip.textContent = ''; }, 5000);
  }
}

/* ---------- 计算进度遮罩 ---------- */
let PG_TIMER = null;

function showProgress(title) {
  const m = $$('#prog-mask');
  if (!m) return;
  $$('#pg-title').textContent = title || '正在计算…';
  $$('#pg-fill').style.width = '0%';
  $$('#pg-stage').textContent = '准备中…';
  $$('#pg-meta').textContent = '';
  m.classList.remove('hide');
  if (PG_TIMER) clearInterval(PG_TIMER);
  PG_TIMER = setInterval(pollProgress, 500);   // 每 0.5s 轮询后端真实进度
}

function hideProgress() {
  const m = $$('#prog-mask');
  if (m) m.classList.add('hide');
  if (PG_TIMER) { clearInterval(PG_TIMER); PG_TIMER = null; }
}

function setProgress(pct, stage, elapsed) {
  const fill = $$('#pg-fill'), st = $$('#pg-stage'), mt = $$('#pg-meta');
  if (fill) fill.style.width = (pct || 0) + '%';
  if (st) st.textContent = stage || '';
  if (mt) {
    const parts = [];
    if (pct != null) parts.push((pct || 0) + '%');
    if (elapsed != null) parts.push('已用时 ' + elapsed + ' 秒');
    mt.textContent = parts.join('　·　');
  }
}

async function pollProgress() {
  try {
    const { ok, d } = await apiCall('/api/progress').then(safeJson);
    if (!ok || !d || !d.progress) return;
    const p = d.progress;
    setProgress(p.pct, p.stage, p.elapsed);
    if (!p.running && PG_TIMER) { clearInterval(PG_TIMER); PG_TIMER = null; }
  } catch (e) { /* 轮询失败不打断流程 */ }
}

/* 带 token 的下载：fetch 拿 blob → URL.createObjectURL 触发下载。
   浏览器原生导航(window.location.href)不会携带 X-Auth-Token，会被鉴权拦截 401。 */
async function authedDownload(url) {
  const tok = localStorage.getItem(AUTH_KEY);
  const r = await fetch(url, { headers: { 'X-Auth-Token': tok || '' } });
  if (r.status === 401) { showLogin(); return; }
  if (!r.ok) { alert('下载失败：' + r.status); return; }
  const blob = await r.blob();
  const cd = r.headers.get('Content-Disposition') || '';
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
  let name = 'download';
  if (m) { try { name = decodeURIComponent(m[1]); } catch (e) { name = m[1]; } }
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
}

async function doLogin() {
  const pwd = ($$('#login-pwd') || {}).value || '';
  const msg = $$('#login-msg');
  if (msg) msg.textContent = '';
  try {
    const r = await fetch('/api/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pwd })
    });
    const d = await r.json();
    if (d && d.ok && d.token) {
      localStorage.setItem(AUTH_KEY, d.token);
      const box = $$('#login-modal');
      if (box) box.classList.add('hide');
      location.reload();
    } else {
      if (msg) msg.textContent = (d && d.msg) || '口令错误';
    }
  } catch (e) {
    if (msg) msg.textContent = '登录失败：' + e;
  }
}

/* ---------- 启动（用 DOMContentLoaded，避免与 jQuery 的 $ 冲突） ---------- */
document.addEventListener('DOMContentLoaded', () => {
  bindUploadUI();
  bindMissingPanel();
  $$('#btn-download').onclick = () => { authedDownload('/api/download'); };
  $$('#btn-new').onclick = () => { refreshStatus().then(showUploadPanel); };
  $$('#btn-reset').onclick = resetFilters;
  $$('#btn-ref-change').onclick = () => { refreshStatus().then(showUploadPanel); };
  $$('#btn-rebuild').onclick = () => rebuild($$('#refbar-text'));
  // 明细导出：全部 / 按当前筛选结果
  $$('#btn-dl-all').onclick = () => exportCsv(ROWS, 'SKU平台信息_清洗明细_全部');
  $$('#btn-dl-filtered').onclick = exportFiltered;
  // 登录框回车提交
  const lp = $$('#login-pwd');
  if (lp) lp.addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
  // 筛选控件
  $$('#f-tag').onchange = e => { F.tag = e.target.value; if (TABLE) TABLE.draw(); };
  $$('#f-fahuo').onchange = e => { F.fahuo = e.target.value; if (TABLE) TABLE.draw(); };
  $$('#f-anom').onchange = e => { F.anom = e.target.value; if (TABLE) TABLE.draw(); };
  // 看板数据过滤（多选 + 下拉）：这里 $ 是 jQuery（DataTables 依赖）
  $.fn.dataTable.ext.search.push(function (settings, rowArr) {
    if (settings.nTable.id !== 'tbl') return true;
    if (F.stores.size && !F.stores.has(rowArr[COL['店铺']])) return false;
    if (F.pkgs.size && !F.pkgs.has(rowArr[COL['包裹类型']])) return false;
    if (F.spus.size && !F.spus.has(rowArr[COL['SPU']])) return false;
    if (F.skus.size && !F.skus.has(rowArr[COL['唯一SKU']])) return false;
    if (F.tag && rowArr[COL['是否重复维护']] !== F.tag) return false;
    if (F.fahuo && rowArr[COL['发货SKU']] !== F.fahuo) return false;
    if (F.anom && rowArr[COL['异常确认状态']] !== F.anom) return false;
    return true;
  });

  // 缺失平台信息异常面板过滤（SKU / SPU / 来源）
  $.fn.dataTable.ext.search.push(function (settings, data, dataIndex) {
    if (settings.nTable.id !== 'tbl-missing') return true;
    const r = settings.aoData[dataIndex]._aData;
    if (MISSING_FILTERS.sku && !(r.sku || '').toLowerCase().includes(MISSING_FILTERS.sku)) return false;
    if (MISSING_FILTERS.spu && !(r.spu || '').toLowerCase().includes(MISSING_FILTERS.spu)) return false;
    if (MISSING_FILTERS.source && r.source !== MISSING_FILTERS.source) return false;
    return true;
  });

  // 启动：先探测口令状态；通过后再加载看板/上传页
  checkAuth().then(auth => {
    if (!auth) { showLogin(); return; }
    refreshStatus().then(st => {
      if (st && st.ready) loadState();
      else showUploadPanel();
    });
  });
});

async function checkAuth() {
  try {
    const tok = localStorage.getItem(AUTH_KEY);
    const headers = {};
    if (tok) headers['X-Auth-Token'] = tok;
    const r = await fetch('/api/ping', { headers });
    const d = await r.json();
    return !!(d && d.auth);
  } catch (e) {
    return false;
  }
}

function refreshStatus() {
  return apiCall('/api/status')
    .then(safeJson)
    .then(({ ok, d }) => {
      if (!ok || !d) return {};
      REF = d.reference; REF_STALE = !!d.ref_stale; HAS_RAW = !!d.has_raw;
      LX = d.lingxing || null; WM = d.walmart || null; ADMIN = d.admin || null;
      LX_STALE = !!d.lx_stale; WM_STALE = !!d.wm_stale; ADMIN_STALE = !!d.admin_stale;
      renderRefInfo(); renderLxInfo(); renderWmInfo(); renderAdminInfo();
      return d;
    }).catch(() => ({}));
}

/* ---------- 转单表（参考表，可更新） ---------- */
function renderRefInfo() {
  const box = $$('#ref-info');
  if (!box) return;
  if (!REF) {
    box.innerHTML = '<span class="warn">尚未上传转单表</span>　—— 请先上传，之后每次只需上传原始报表即可。';
    const lnk = $$('#lnk-ref-dl');
    if (lnk) lnk.classList.add('hide');
    return;
  }
  const lnk = $$('#lnk-ref-dl');
  if (lnk) lnk.classList.remove('hide');
  const s = REF.summary || {};
  const sheets = (s.sheets || []).map(x => `${escHtml(x.name)}(${x.rows}行)`).join('、');
  box.innerHTML =
    `<span class="ok">当前生效</span>：<b>${escHtml(REF.filename)}</b>　上传于 ${escHtml(REF.uploaded_at)}<br>` +
    `Sheet：${sheets || '—'}　｜　转单行 ${s.total_rows || 0}　｜　涉及 SKU ${s.sku_count || 0}　｜　转单组 ${s.group_count || 0}`;
}

/* ---------- 领星清单 / walmart 报表（可选数据源） ---------- */
function renderLxInfo() {
  const box = $$('#lx-info');
  if (!box) return;
  if (!LX) {
    box.innerHTML = '<span class="warn">未上传领星清单</span>　—— Amazon 店（HLLdeco / Home Nest / Urban / JYT / XYT）的 ASIN 与 listing 后台状态留空。';
    return;
  }
  const s = LX.summary || {};
  box.innerHTML =
    `<span class="ok">当前生效</span>：<b>${escHtml(LX.filename)}</b>　上传于 ${escHtml(LX.uploaded_at)}<br>` +
    `有效记录 ${s.rows || 0} 行　｜　可匹配 (店铺,MSKU) ${s.matched || 0} 组`;
}

function renderWmInfo() {
  const box = $$('#wm-info');
  if (!box) return;
  if (!WM) {
    box.innerHTML = '<span class="warn">未上传 walmart 报表</span>　—— US-Walmart 店的 ASIN 与 listing 后台状态留空。';
    return;
  }
  const s = WM.summary || {};
  box.innerHTML =
    `<span class="ok">当前生效</span>：<b>${escHtml(WM.filename)}</b>　上传于 ${escHtml(WM.uploaded_at)}<br>` +
    `有效记录 ${s.rows || 0} 行　｜　可匹配 SKU ${s.matched || 0} 个`;
}

function renderAdminInfo() {
  const box = $$('#admin-info');
  if (!box) return;
  if (!ADMIN) {
    box.innerHTML = '<span class="warn">未上传管理员检查表</span>　—— 「品类」列将为空（不影响其他功能）。';
    return;
  }
  const s = ADMIN.summary || {};
  box.innerHTML =
    `<span class="ok">当前生效</span>：<b>${escHtml(ADMIN.filename)}</b>　上传于 ${escHtml(ADMIN.uploaded_at)}<br>` +
    `有效记录 ${s.rows || 0} 行　｜　可匹配 SKU ${s.matched || 0} 个`;
}

function bindUploadUI() {
  const zone = $$('#drop-raw'), input = $$('#file-input');
  $$('#btn-pick').onclick = () => input.click();
  zone.onclick = e => { if (e.target.id !== 'btn-pick') input.click(); };
  input.onchange = e => { if (e.target.files[0]) upload(e.target.files[0]); e.target.value = ''; };
  zone.ondragover = e => { e.preventDefault(); zone.classList.add('drag'); };
  zone.ondragleave = () => zone.classList.remove('drag');
  zone.ondrop = e => {
    e.preventDefault(); zone.classList.remove('drag');
    if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]);
  };
  // 转单表上传
  const rinput = $$('#ref-input');
  $$('#btn-pick-ref').onclick = () => rinput.click();
  rinput.onchange = e => { if (e.target.files[0]) uploadRef(e.target.files[0]); e.target.value = ''; };
  // 领星清单上传
  const linput = $$('#lx-input');
  $$('#btn-pick-lx').onclick = () => linput.click();
  linput.onchange = e => { if (e.target.files[0]) uploadLx(e.target.files[0]); e.target.value = ''; };
  // walmart 报表上传
  const winput = $$('#wm-input');
  $$('#btn-pick-wm').onclick = () => winput.click();
  winput.onchange = e => { if (e.target.files[0]) uploadWm(e.target.files[0]); e.target.value = ''; };
  // 管理员检查表（品类）上传
  const ainput = $$('#admin-input');
  $$('#btn-pick-admin').onclick = () => ainput.click();
  ainput.onchange = e => { if (e.target.files[0]) uploadAdmin(e.target.files[0]); e.target.value = ''; };
}

function uploadRef(file) {
  const msg = $$('#ref-msg');
  msg.className = 'msg'; msg.textContent = '解析中…';
  const fd = new FormData(); fd.append('file', file);
  apiCall('/api/reference/upload', { method: 'POST', body: fd })
    .then(safeJson)
    .then(({ ok, d }) => {
      if (!ok || !d.ok) { msg.className = 'msg err'; msg.textContent = '✗ ' + (d.msg || '上传失败'); return; }
      REF = d.reference; HAS_RAW = !!d.has_raw;
      renderRefInfo();
      const s = REF.summary || {};
      msg.className = 'msg';
      msg.innerHTML = `<span style="color:#16a34a">✓ 转单表已更新</span>：` +
        `${s.total_rows} 行 / ${s.group_count} 个转单组` +
        (HAS_RAW ? '　—— 已留档原始报表，可直接<a href="javascript:;" id="lnk-rebuild">按新转单表重算</a>，无需重新上传。' : '　—— 接着上传右侧原始报表即可。');
      const lk = $$('#lnk-rebuild');
      if (lk) lk.onclick = () => { if (HAS_RAW) rebuild(); };
    })
    .catch(err => { msg.className = 'msg err'; msg.textContent = '✗ 上传失败：' + ((err && err.message) || err); });
}

function rebuild(msgEl) {
  const msg = msgEl || $$('#ref-msg');
  if (msg) { msg.className = 'msg'; msg.style.color = ''; msg.textContent = ''; }
  showProgress('正在按当前数据源重算…');
  apiCall('/api/rebuild', { method: 'POST' })
    .then(safeJson)
    .then(({ ok, d }) => {
      if (!ok || !d.ok) {
        hideProgress();
        if (msg) { msg.className = 'msg err'; msg.textContent = '✗ ' + (d.msg || '重算失败'); }
        return;
      }
      // 重算不算上传新表格 → 数据更新时间保持不变
      loadState(msg, '重算完成，数据更新时间保持不变');
    })
    .catch(err => {
      hideProgress();
      if (msg) { msg.className = 'msg err'; msg.textContent = '✗ 重算失败：' + ((err && err.message) || err); }
    });
}

/* ---------- 领星清单 / walmart 报表上传 ---------- */
function uploadLx(file) {
  const msg = $$('#lx-msg');
  msg.className = 'msg'; msg.textContent = '解析中…';
  const fd = new FormData(); fd.append('file', file);
  apiCall('/api/upload/lingxing', { method: 'POST', body: fd })
    .then(safeJson)
    .then(({ ok, d }) => {
      if (!ok || !d.ok) { msg.className = 'msg err'; msg.textContent = '✗ ' + (d.msg || '上传失败'); return; }
      LX = d.lingxing || LX;
      renderLxInfo();
      msg.className = 'msg';
      const s = (LX && LX.summary) || {};
      msg.innerHTML = `<span style="color:#16a34a">✓ 领星清单已更新</span>：` +
        `${s.rows || 0} 行 / ${s.matched || 0} 组可匹配` +
        (d.ready ? '　—— 当前看板按新数据源重算后生效，可<a href="javascript:;" id="lnk-rebuild-lx">立即重算</a>。' : '　—— 可继续上传原始报表。');
      const lk = $$('#lnk-rebuild-lx');
      if (lk) lk.onclick = () => { if (d.ready) rebuild(msg); };
    })
    .catch(err => { msg.className = 'msg err'; msg.textContent = '✗ 上传失败：' + ((err && err.message) || err); });
}

function uploadWm(file) {
  const msg = $$('#wm-msg');
  msg.className = 'msg'; msg.textContent = '解析中…';
  const fd = new FormData(); fd.append('file', file);
  apiCall('/api/upload/walmart', { method: 'POST', body: fd })
    .then(safeJson)
    .then(({ ok, d }) => {
      if (!ok || !d.ok) { msg.className = 'msg err'; msg.textContent = '✗ ' + (d.msg || '上传失败'); return; }
      WM = d.walmart || WM;
      renderWmInfo();
      msg.className = 'msg';
      const s = (WM && WM.summary) || {};
      msg.innerHTML = `<span style="color:#16a34a">✓ walmart 报表已更新</span>：` +
        `${s.rows || 0} 行 / ${s.matched || 0} 个 SKU 可匹配` +
        (d.ready ? '　—— 当前看板按新数据源重算后生效，可<a href="javascript:;" id="lnk-rebuild-wm">立即重算</a>。' : '　—— 可继续上传原始报表。');
      const lk = $$('#lnk-rebuild-wm');
      if (lk) lk.onclick = () => { if (d.ready) rebuild(msg); };
    })
    .catch(err => { msg.className = 'msg err'; msg.textContent = '✗ 上传失败：' + ((err && err.message) || err); });
}

/* ---------- 管理员检查表（品类）上传 ---------- */
function uploadAdmin(file) {
  const msg = $$('#admin-msg');
  msg.className = 'msg'; msg.textContent = '解析中…';
  const fd = new FormData(); fd.append('file', file);
  apiCall('/api/upload/admin', { method: 'POST', body: fd })
    .then(safeJson)
    .then(({ ok, d }) => {
      if (!ok || !d.ok) { msg.className = 'msg err'; msg.textContent = '✗ ' + (d.msg || '上传失败'); return; }
      ADMIN = d.admin || ADMIN;
      renderAdminInfo();
      msg.className = 'msg';
      const s = (ADMIN && ADMIN.summary) || {};
      msg.innerHTML = `<span style="color:#16a34a">✓ 管理员检查表已更新</span>：` +
        `${s.matched || 0} 个 SKU 可匹配` +
        (d.ready ? '　—— 当前看板按新数据源重算后生效，可<a href="javascript:;" id="lnk-rebuild-admin">立即重算</a>。' : '　—— 可继续上传原始报表。');
      const lk = $$('#lnk-rebuild-admin');
      if (lk) lk.onclick = () => { if (d.ready) rebuild(msg); };
    })
    .catch(err => { msg.className = 'msg err'; msg.textContent = '✗ 上传失败：' + ((err && err.message) || err); });
}

/* ---------- 原始报表上传 ---------- */
function upload(file) {
  const msg = $$('#upload-msg');
  msg.className = 'msg err';
  msg.style.color = '';
  msg.textContent = '';
  const fd = new FormData();
  fd.append('file', file);
  showProgress('正在清洗计算…');
  apiCall('/api/upload', { method: 'POST', body: fd })
    .then(safeJson)
    .then(({ ok, d }) => {
      if (!ok || !d.ok) {
        hideProgress();
        msg.textContent = '✗ ' + (d.msg || '处理失败');
        return;
      }
      // 计算完成 → 载入看板（载入本身也走进度遮罩，避免"点了没反应"）
      const isNew = d.is_new_file;
      loadState(msg, isNew ? '新表格，数据已更新' : '与上次上传的表格内容相同，数据更新时间保持不变');
    })
    .catch(err => {
      hideProgress();
      msg.textContent = '✗ 上传失败：' + ((err && err.message) || err);
    });
}

/** 载入看板数据（17MB JSON，较慢，用进度遮罩给出反馈）
 *  msgEl: 可选，用于显示完成/失败提示的元素
 *  doneNote: 可选，完成时追加的说明文字 */
function loadState(msgEl, doneNote) {
  showProgress('正在载入看板…');
  setProgress(60, '载入明细数据…');
  apiCall('/api/state')
    .then(safeJson)
    .then(({ ok, d }) => {
      if (!ok || !d || !d.ready) {
        hideProgress();
        if (msgEl) {
          msgEl.className = 'msg err';
          msgEl.textContent = '✗ 数据载入失败，请重试或刷新页面';
        }
        showUploadPanel();
        return;
      }
      setProgress(85, '渲染看板…');
      HEADER = d.header; ROWS = d.rows; STATS = d.stats;
      ANNOTS = d.annotations || {};   // 行级冲突检测：id_key -> {status,note,version}
      MISSING_ROWS = (d.stats && d.stats.missing_platform_rows) || [];
      REF_STALE = !!d.ref_stale;
      COL = {}; HEADER.forEach((h, i) => COL[h] = i);
      renderDashboard(d);
      setProgress(100, '完成');
      setTimeout(hideProgress, 350);
      if (msgEl) {
        msgEl.className = 'msg';
        msgEl.style.color = '#16a34a';
        msgEl.innerHTML = '✓ 计算完成，共 ' + (d.stats && d.stats.total || 0) + ' 行' +
          (doneNote ? '（' + escHtml(doneNote) + '）' : '');
      }
    })
    .catch(err => {
      hideProgress();
      console.error('loadState', err);
      if (msgEl) {
        msgEl.className = 'msg err';
        msgEl.textContent = '✗ 数据载入失败：' + ((err && err.message) || err);
      }
      showUploadPanel();
    });
}

/* ---------- 视图切换 ---------- */
function showUploadPanel() {
  $$('#dashboard').classList.add('hide');
  $$('#upload-zone').classList.remove('hide');
  $$('#btn-download').disabled = true;
  $$('#status-bar').textContent = '';
  renderRefInfo();
  renderPersistHint();
}

function renderPersistHint() {
  const el = $$('#persist-hint');
  if (!el) return;
  apiCall('/api/status')
    .then(safeJson)
    .then(({ ok, d: st }) => {
      if (!ok || !st) { el.textContent = '状态获取失败，请刷新页面。'; el.classList.remove('hide'); return; }
      if (st.ready) {
        el.innerHTML = `已有最近一次数据：<b>${escHtml(st.filename || '—')}</b>，生成于 ${escHtml(st.uploaded_at || '—')}，` +
          `共 ${st.stats && st.stats.total || 0} 行。上传新数据会<b>直接覆盖</b>。` +
          `<a href="javascript:;" id="lnk-back">返回看板</a>`;
        const lk = $$('#lnk-back');
        if (lk) lk.onclick = () => {
          showProgress('正在载入看板…');
          setProgress(40, '读取已保存数据…');
          refreshStatus()
            .then(s => {
              if (s && s.ready) { loadState(el); }
              else {
                hideProgress();
                el.innerHTML = '暂无可载入的数据，请先上传原始报表。';
              }
            })
            .catch(err => {
              hideProgress();
              el.innerHTML = '载入失败：' + escHtml((err && err.message) || String(err));
            });
        };
        el.classList.remove('hide');
      } else {
        el.innerHTML = '暂无已保存数据，请先上传原始报表。';
        el.classList.remove('hide');
      }
    })
    .catch(() => { el.textContent = '状态获取失败，请刷新页面。'; el.classList.remove('hide'); });
}

function renderDashboard(d) {
  $$('#upload-zone').classList.add('hide');
  $$('#dashboard').classList.remove('hide');
  $$('#btn-download').disabled = false;
  const blankPkg = (d.stats.pkg_dist && d.stats.pkg_dist['']) || 0;
  const asinHit = d.stats.asin_hit || 0;
  const origN = d.stats.original || d.stats.total;
  $$('#status-bar').textContent =
    `数据源：${d.filename || '—'} ｜ 生成于 ${d.uploaded_at || '—'} ｜ 平台信息 ${d.stats.total} 行` +
    `　｜　ASIN 命中 ${asinHit} / ${origN} 行（原始）` +
    (blankPkg ? `　｜　包裹类型留空 ${blankPkg} 行（重复维护组/规则未覆盖）` : '');

  // 转单表信息条
  const t = (STATS && STATS.transfer) || {};
  const lxs = (STATS && STATS.lingxing) || null;
  const wms = (STATS && STATS.walmart) || null;
  const adm = (STATS && STATS.admin) || null;
  const mp = (STATS && STATS.missing_platform) || {};
  const lxTxt = lxs ? `领星 <b>${escHtml(d.lx_filename || '—')}</b>（${lxs.matched || 0} 组可匹配` +
    (mp.lingxing ? `｜缺平台信息 ${mp.lingxing}` : '') + `）` : '领星 未使用';
  const wmTxt = wms ? `walmart <b>${escHtml(d.wm_filename || '—')}</b>（${wms.matched || 0} SKU 可匹配` +
    (mp.walmart ? `｜缺平台信息 ${mp.walmart}` : '') + `）` : 'walmart 未使用';
  const admTxt = adm && adm.used ? `管理员检查表 <b>${escHtml(d.admin_filename || '—')}</b>（品类覆盖 ${adm.covered || 0} 行）` : '管理员检查表 未使用';
  $$('#refbar-text').innerHTML =
    `转单表：<b>${escHtml(d.ref_filename || '—')}</b>（上传于 ${escHtml(d.ref_uploaded_at || '—')}）` +
    `　｜　转单行 ${t.total_rows || 0}　涉及 SKU ${t.sku_count || 0}　转单组 ${t.group_count || 0}` +
    `　｜　本次扩展 ${STATS.expanded_groups} 组 / +${STATS.expanded} 行<br>` +
    `数据源：${lxTxt}　｜　${wmTxt}　｜　${admTxt}`;
  $$('#btn-rebuild').disabled = !(d.ref_filename !== null);
  $$('#ref-stale').classList.toggle('hide', !REF_STALE);
  $$('#ds-stale').classList.toggle('hide', !(LX_STALE || WM_STALE || ADMIN_STALE));

  renderCards();
  renderCharts();
  renderMissingPanel();
  buildFilters();
  initTable();
  updateAnomCount();
}

/* ---------- 卡片 ---------- */
function renderCards() {
  const s = STATS, pd = s.pkg_dist || {};
  const pend = ROWS.filter(r => r[COL['包裹类型']] === '异常' && r[COL['异常确认状态']] === '待确认').length;
  const cards = [
    { k: '总行数', v: s.total },
    { k: '原始行数', v: s.original },
    { k: '转单扩展行', v: '+' + s.expanded },
    { k: '转单组数', v: s.expanded_groups },
    { k: '单件', v: pd['单件'] || 0 },
    { k: '套件', v: pd['套件'] || 0 },
    { k: 'AB件', v: pd['AB件'] || 0 },
    { k: '套组', v: pd['套组'] || 0 },
    { k: '异常（待确认 ' + pend + '）', v: pd['异常'] || 0, anom: true },
  ];
  $$('#cards').innerHTML = cards.map(c =>
    `<div class="card ${c.anom ? 'anom' : ''}"><div class="k">${c.k}</div><div class="v">${c.v}</div></div>`
  ).join('');
}

/* ---------- 图表 ---------- */
function renderCharts() {
  const pd = STATS.pkg_dist || {};
  const order = ['单件', '套件', 'AB件', '套组', '异常'];
  const colorMap = { '单件': '#2f6fed', '套件': '#16a34a', 'AB件': '#d97706', '套组': '#8b5cf6', '异常': '#f59e0b' };
  const pieData = order.filter(t => pd[t]).map(t => ({ name: t, value: pd[t], itemStyle: { color: colorMap[t] } }));

  if (!PKG_CHART) PKG_CHART = echarts.init($$('#chart-pkg'));
  PKG_CHART.setOption({
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { bottom: 0 },
    series: [{
      type: 'pie', radius: ['38%', '66%'], center: ['50%', '46%'],
      label: { formatter: '{b}\n{c}' }, data: pieData
    }]
  }, true);

  if (!FAHUO_CHART) FAHUO_CHART = echarts.init($$('#chart-fahuo'));
  const fd = STATS.fahuo_dist || {};
  FAHUO_CHART.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 60, right: 20, top: 20, bottom: 30 },
    xAxis: { type: 'category', data: ['是（原始）', '否（转单扩展）'] },
    yAxis: { type: 'value' },
    series: [{
      type: 'bar', barWidth: '46%',
      data: [{ value: fd['是'] || 0, itemStyle: { color: '#16a34a' }, name: '是' },
             { value: fd['否'] || 0, itemStyle: { color: '#ef4444' }, name: '否' }],
      label: { show: true, position: 'top' }
    }]
  }, true);

  window.addEventListener('resize', () => { PKG_CHART && PKG_CHART.resize(); FAHUO_CHART && FAHUO_CHART.resize(); });
}

/* ---------- 缺失平台信息异常（独立面板） ---------- */
let MISSING_FILTERS = { sku: '', spu: '', source: '' };

function renderMissingPanel() {
  const rows = MISSING_ROWS || [];
  const mp = (STATS && STATS.missing_platform) || {};
  $$('#missing-count').textContent = `${mp.union || rows.length || 0} 条`;
  $$('#missing-panel').classList.toggle('hide', rows.length === 0);

  if (MISSING_TABLE) { MISSING_TABLE.destroy(); $('#tbl-missing').empty(); }

  MISSING_TABLE = $('#tbl-missing').DataTable({
    data: rows,
    columns: [
      { data: 'sku', title: 'SKU', render: v => escHtml(v || '') },
      { data: 'spu', title: 'SPU', render: v => escHtml(v || '') },
      { data: 'store', title: '店铺', render: v => escHtml(v || '') },
      { data: 'asin', title: 'ASIN', render: v => escHtml(v || '') },
      { data: 'listing', title: 'listing后台状态', render: v => escHtml(v || '') },
      { data: 'source', title: '来源', render: v => `<span class="source-tag source-${escAttr(v)}">${escHtml(v === 'lingxing' ? '领星' : 'walmart')}</span>` }
    ],
    scrollX: true,
    deferRender: true,
    pageLength: 25,
    lengthMenu: [10, 25, 50, 100, 200],
    order: [],
    language: {
      search: '表格内搜索：', lengthMenu: '每页 _MENU_ 条',
      info: '第 _START_–_END_ 条 / 共 _TOTAL_ 条',
      paginate: { first: '首页', last: '末页', next: '下页', previous: '上页' },
      zeroRecords: '无匹配记录'
    }
  });
}

function bindMissingPanel() {
  $$('#missing-sku').oninput = e => { MISSING_FILTERS.sku = e.target.value.trim().toLowerCase(); if (MISSING_TABLE) MISSING_TABLE.draw(); };
  $$('#missing-spu').oninput = e => { MISSING_FILTERS.spu = e.target.value.trim().toLowerCase(); if (MISSING_TABLE) MISSING_TABLE.draw(); };
  $$('#missing-source').onchange = e => { MISSING_FILTERS.source = e.target.value; if (MISSING_TABLE) MISSING_TABLE.draw(); };
  $$('#btn-missing-reset').onclick = () => {
    $$('#missing-sku').value = ''; $$('#missing-spu').value = ''; $$('#missing-source').value = '';
    MISSING_FILTERS = { sku: '', spu: '', source: '' };
    if (MISSING_TABLE) MISSING_TABLE.draw();
  };
  $$('#btn-missing-dl').onclick = () => { authedDownload('/api/missing_platform/download'); };
}

/* ---------- 筛选控件 ---------- */
function uniq(idx) { return [...new Set(ROWS.map(r => r[idx]))].filter(v => v !== ''); }

function buildFilters() {
  // 店铺
  const stores = uniq(COL['店铺']);
  $$('#f-store').innerHTML = stores.map(s =>
    `<span class="chip" data-v="${escAttr(s)}">${escHtml(s)}</span>`).join('');
  $$a('#f-store .chip').forEach(ch => ch.onclick = () => {
    const v = ch.dataset.v;
    if (F.stores.has(v)) { F.stores.delete(v); ch.classList.remove('on'); }
    else { F.stores.add(v); ch.classList.add('on'); }
    if (TABLE) TABLE.draw();
  });
  // 包裹类型
  const pkgs = ['单件', '套件', 'AB件', '套组', '异常'].filter(p => (STATS.pkg_dist || {})[p]);
  $$('#f-pkg').innerHTML = pkgs.map(p =>
    `<span class="chip" data-v="${p}">${p}</span>`).join('');
  $$a('#f-pkg .chip').forEach(ch => ch.onclick = () => {
    const v = ch.dataset.v;
    if (F.pkgs.has(v)) { F.pkgs.delete(v); ch.classList.remove('on'); }
    else { F.pkgs.add(v); ch.classList.add('on'); }
    if (TABLE) TABLE.draw();
  });
  // SPU / 唯一SKU 搜索式多选（取值上千上万，不能用 chip 平铺）
  F.spus.clear(); F.skus.clear();
  buildMultiSelect('#f-spu', uniq(COL['SPU']), F.spus);
  buildMultiSelect('#f-sku', uniq(COL['唯一SKU']), F.skus);
  // 是否重复维护 / 发货SKU 下拉
  fillSelect('#f-tag', uniq(COL['是否重复维护']));
  fillSelect('#f-fahuo', uniq(COL['发货SKU']));
  F.tag = ''; F.fahuo = ''; F.anom = '';
  $$('#f-tag').value = ''; $$('#f-fahuo').value = ''; $$('#f-anom').value = '';
  F.stores.clear(); F.pkgs.clear();
}

function fillSelect(sel, vals) {
  const el = $$(sel);
  el.innerHTML = '<option value="">全部</option>' +
    vals.map(v => `<option value="${escAttr(v)}">${escHtml(v)}</option>`).join('');
}

/* ---------- 搜索式多选下拉（SPU / 唯一SKU 取值上万，不能用 chip 平铺） ----------
   结构：.msel > .msel-box(已选标签 + 搜索框) + .msel-pop(选项面板，限渲染 MAX_OPT 条)
   行为：输入关键字实时过滤；勾选后存入 target Set；已选标签可点 × 移除；
        点面板外关闭；Escape 关闭；支持「全选当前筛选结果 / 清空」。 */
const MAX_OPT = 200;          // 面板最多渲染选项条数（防止上万节点卡死）
const MSEL_STATE = {};        // id -> { all:[全部取值], target:Set, box, pop, input, selWrap }

function buildMultiSelect(id, values, target) {
  const root = $$(id);
  if (!root) return;
  const all = values.slice().sort();
  root.innerHTML =
    `<div class="msel-box">
       <div class="msel-sel" id="${id.slice(1)}-sel"></div>
       <input type="text" class="msel-input" id="${id.slice(1)}-input"
              placeholder="${escAttr(all.length > MAX_OPT ? '输入关键字搜索…' : '搜索 / 选择…')}" autocomplete="off">
     </div>
     <div class="msel-pop hide" id="${id.slice(1)}-pop">
       <div class="msel-pop-hd">
         <span class="msel-cnt" id="${id.slice(1)}-cnt"></span>
         <span class="grow"></span>
         <button type="button" class="msel-mini" id="${id.slice(1)}-all">全选筛选结果</button>
         <button type="button" class="msel-mini" id="${id.slice(1)}-clear">清空</button>
       </div>
       <div class="msel-list" id="${id.slice(1)}-list"></div>
     </div>`;

  const st = {
    all, target,
    box: $$('.msel-box', root),
    selWrap: $$('.msel-sel', root),
    input: $$('.msel-input', root),
    pop: $$('.msel-pop', root),
    list: $$('.msel-list', root),
    cnt: $$('.msel-cnt', root)
  };
  MSEL_STATE[id] = st;

  const key = id.slice(1);
  $$('#' + key + '-input').addEventListener('focus', () => openMsel(st));
  $$('#' + key + '-input').addEventListener('input', () => { openMsel(st); renderMsel(st); });
  $$('#' + key + '-all').onclick = e => {
    e.stopPropagation();
    filterVals(st).forEach(v => st.target.add(v));
    renderMselSel(st); renderMsel(st);
    if (TABLE) TABLE.draw();
  };
  $$('#' + key + '-clear').onclick = e => {
    e.stopPropagation();
    st.target.clear();
    renderMselSel(st); renderMsel(st);
    if (TABLE) TABLE.draw();
  };
  // 点面板外关闭
  document.addEventListener('click', ev => {
    if (!root.contains(ev.target)) closeMsel(st);
  });

  renderMselSel(st);
  renderMsel(st);
}

function filterVals(st) {
  const kw = (st.input.value || '').trim().toLowerCase();
  if (!kw) return st.all;
  return st.all.filter(v => v.toLowerCase().includes(kw));
}

function openMsel(st) {
  st.pop.classList.remove('hide');
  st.box.classList.add('open');
}

function closeMsel(st) {
  st.pop.classList.add('hide');
  st.box.classList.remove('open');
  st.input.value = '';
  renderMsel(st);
}

/** 渲染已选标签区 */
function renderMselSel(st) {
  const arr = [...st.target];
  if (!arr.length) {
    st.selWrap.innerHTML = '<span class="msel-ph">未筛选</span>';
    return;
  }
  const MAX_SHOW = 12;
  const shown = arr.slice(0, MAX_SHOW);
  st.selWrap.innerHTML =
    shown.map(v => `<span class="msel-tag" data-v="${escAttr(v)}">${escHtml(v)}<b>×</b></span>`).join('') +
    (arr.length > MAX_SHOW ? `<span class="msel-tag more">+${arr.length - MAX_SHOW}</span>` : '');
  $$a('.msel-tag[data-v]', st.selWrap).forEach(t => {
    t.querySelector('b').onclick = ev => {
      ev.stopPropagation();
      st.target.delete(t.dataset.v);
      renderMselSel(st); renderMsel(st);
      if (TABLE) TABLE.draw();
    };
  });
}

/** 渲染下拉选项面板（限制 DOM 条数） */
function renderMsel(st) {
  const vals = filterVals(st);
  const shown = vals.slice(0, MAX_OPT);
  st.cnt.textContent = `匹配 ${vals.length} 项` +
    (vals.length > MAX_OPT ? `（显示前 ${MAX_OPT} 条，请输入更多关键字）` : '');
  st.list.innerHTML = shown.map(v => {
    const on = st.target.has(v);
    return `<div class="msel-opt${on ? ' on' : ''}" data-v="${escAttr(v)}">
              <span class="msel-cb">${on ? '✓' : ''}</span><span class="msel-ox">${escHtml(v)}</span>
            </div>`;
  }).join('') || '<div class="msel-empty">无匹配项</div>';
  $$a('.msel-opt', st.list).forEach(o => {
    o.onclick = ev => {
      ev.stopPropagation();
      const v = o.dataset.v;
      if (st.target.has(v)) st.target.delete(v); else st.target.add(v);
      renderMselSel(st); renderMsel(st);
      if (TABLE) TABLE.draw();
    };
  });
}

function resetFilters() {
  F.stores.clear(); F.pkgs.clear(); F.spus.clear(); F.skus.clear();
  F.tag = ''; F.fahuo = ''; F.anom = '';
  $$a('#f-store .chip').forEach(c => c.classList.remove('on'));
  $$a('#f-pkg .chip').forEach(c => c.classList.remove('on'));
  $$('#f-tag').value = ''; $$('#f-fahuo').value = ''; $$('#f-anom').value = '';
  // 重建 SPU / 唯一SKU 多选（清空已选 + 复位搜索框）
  buildMultiSelect('#f-spu', uniq(COL['SPU']), F.spus);
  buildMultiSelect('#f-sku', uniq(COL['唯一SKU']), F.skus);
  if (TABLE) TABLE.draw();
}

/* ---------- 表格 ---------- */
function initTable() {
  if (TABLE) { TABLE.destroy(); $('#tbl').empty(); }

  const cols = HEADER.map((h, i) => {
    if (i === COL['异常确认状态']) return { data: i, title: h, render: renderAst };
    if (i === COL['异常确认备注']) return { data: i, title: h, render: renderAnot };
    if (i === COL['包裹类型']) return { data: i, title: h, render: v => v ? `<span class="tag t-${v}">${escHtml(v)}</span>` : '' };
    if (i === COL['是否重复维护']) {
      return {
        data: i, title: h,
        render: v => {
          if (!v) return '';
          const cls = v === '否' ? 't-normal' : (v === '非活动' ? 't-inactive' : 't-dup');
          return `<span class="tag ${cls}">${escHtml(v)}</span>`;
        }
      };
    }
    if (i === COL['发货SKU']) return { data: i, title: h, render: v => v === '是' ? '<span class="pill pill-y">是</span>' : '<span class="pill pill-n">否</span>' };
    return { data: i, title: h };
  });

  TABLE = $('#tbl').DataTable({
    data: ROWS,
    columns: cols,
    scrollX: true,
    deferRender: true,
    pageLength: 50,
    lengthMenu: [25, 50, 100, 200, 500],
    order: [],
    columnDefs: [
      { targets: [COL['内部 ID'], COL['德国生命周期状态'], COL['日本生命周期状态'], COL['英国生命周期状态'], COL['类型']], visible: false }
    ],
    language: {
      search: '全局搜索：', lengthMenu: '每页 _MENU_ 条',
      info: '第 _START_–_END_ 条 / 共 _TOTAL_ 条',
      paginate: { first: '首页', last: '末页', next: '下页', previous: '上页' },
      zeroRecords: '无匹配记录'
    },
    createdRow: function (row, data) {
      if (data[COL['包裹类型']] === '异常') row.classList.add('anom-row');
    }
  });

  // 异常确认状态（下拉）
  $('#tbl tbody').on('change', '.anom-select', function () {
    const tr = $(this).closest('tr');
    const idx = TABLE.row(tr).index();
    const val = this.value;
    ROWS[idx][COL['异常确认状态']] = val;
    saveAnomaly(idx, val, ROWS[idx][COL['异常确认备注']]);
    updateAnomCount();
    if (F.anom) TABLE.draw();
  });
  // 异常确认备注（输入，防抖）
  $('#tbl tbody').on('input', '.anom-note', function () {
    const tr = $(this).closest('tr');
    const idx = TABLE.row(tr).index();
    const val = this.value;
    ROWS[idx][COL['异常确认备注']] = val;
    clearTimeout(noteTimer);
    noteTimer = setTimeout(() => saveAnomaly(idx, ROWS[idx][COL['异常确认状态']], val), 600);
  });
}

function rowIdKey(row) {
  // 与服务端 _row_id_key 保持一致的稳定主键：ID+库存SKU，保证同 ID 多行（同一 listing 拆出不同成员货品）能区分
  const idv = String(row[COL['ID']] ?? '').trim();
  const mem = String(row[COL['库存SKU']] ?? '').trim();
  if (idv) return mem ? `${idv}｜${mem}` : `${idv}｜（无成员）`;
  const sku = String(row[COL['上架SKU']] ?? '').trim();
  return `${sku}|${mem}`;
}

function renderAst(data, type, row) {
  if (type !== 'display') return data;
  if (row[COL['包裹类型']] !== '异常') return '';
  const sel = ['待确认', '已确认'].map(o =>
    `<option value="${o}" ${o === data ? 'selected' : ''}>${o}</option>`).join('');
  return `<select class="anom-select">${sel}</select>`;
}
function renderAnot(data, type, row) {
  if (type !== 'display') return data;
  if (row[COL['包裹类型']] !== '异常') return '';
  return `<input class="anom-note" type="text" value="${escAttr(data || '')}" placeholder="核查结论">`;
}

function saveAnomaly(idx, status, note) {
  const row = ROWS[idx];
  if (!row) return;
  const idKey = rowIdKey(row);
  const cur = ANNOTS[idKey] || {};
  const version = cur.version;   // 本次提交的冲突检测版本（undefined = 强制写）
  apiCall('/api/anomaly', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id_key: idKey, status: status, note: note, version: version })
  })
  .then(safeJson)
  .then(({ ok, d }) => {
    if (ok && d && d.ok) {
      // 记录服务端返回的新版本号，供下次冲突检测
      ANNOTS[idKey] = { status: status, note: note, version: d.version };
      return;
    }
    // 409 或其他失败：他人已改，提示刷新
    if (d && d.conflict) {
      alert('⚠️ ' + (d.msg || '该行已被他人修改，请刷新后再改。'));
    } else {
      console.warn('saveAnomaly 失败', d);
    }
  })
  .catch(() => {});
}

function updateAnomCount() {
  const all = ROWS.filter(r => r[COL['包裹类型']] === '异常').length;
  const pend = ROWS.filter(r => r[COL['包裹类型']] === '异常' && r[COL['异常确认状态']] === '待确认').length;
  $$('#anom-count').textContent = all ? `异常共 ${all} 条：待确认 ${pend} / 已确认 ${all - pend}` : '';
}
