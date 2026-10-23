#!/usr/bin/env python3
"""
新品跟进报告生成器 v2
SPU主行 + SKU可展开子行
新增: 多包裹数 / 库存状态 / 最早预计到仓时间 / 上架DDL
"""

import json
from pathlib import Path

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"


def generate_report():
    with open(OUTPUT_DIR / "report_data.json", 'r', encoding='utf-8') as f:
        data = json.load(f)

    stats = data['stats']
    sources = data['data_sources']
    products = data['products']
    today = data.get('today', '')

    # 构建表格行数据 (SPU级 + SKU级)
    spu_rows = []
    for idx, p in enumerate(products):
        skus = p.get('skus', [])
        spu_rows.append({
            'idx': idx,
            'spu': p['spu'],
            'aliases': p.get('aliases', []),
            'source': p.get('source_label', ''),
            'source_raw': p['source'],
            'category': p.get('category', ''),
            'operator': p.get('operator', ''),
            'multi_pkg': p.get('multi_package_count', 1),
            'multi_pkg_display': p.get('multi_package_display', ''),
            'spu_pkg_type': p.get('spu_package_type', 'Unknown'),
            'color_pkg': p.get('color_package_details', {}),
            'inv_status': p.get('spu_inventory_status', ''),
            'earliest_eta': p.get('earliest_eta', ''),
            'launch_ddl': p.get('launch_ddl', ''),
            'launch': p.get('launch_status', ''),
            'launch_reason': p.get('launch_reason', ''),
            'has_ltl': p.get('has_ltl', False),
            'has_unknown': p.get('has_unknown_dim', False),
            'anomaly_count': p.get('dim_anomaly_count', 0),
            'sku_anomalies': [{
                'sku': a.get('sku', ''),
                'inv_status': a.get('inventory_status', ''),
                'in_stock': a.get('in_stock', 0),
                'in_transit': a.get('in_transit', []),
            } for a in p.get('sku_anomalies', [])],
            'notify_time': p.get('notify_time', ''),
            'candidate_sources': p.get('candidate_sources', []),
            'lines': p.get('lines', []),
            'sku_count': len(skus),
            'skus': [{
                'sku': s['sku'],
                'pkg_type': s.get('package_type', 'Unknown'),
                'weight_lb': s.get('package_details', {}).get('weight_lb', ''),
                'longest': s.get('package_details', {}).get('longest_in', ''),
                'lpg': s.get('package_details', {}).get('length_plus_girth_in', ''),
                'volume': s.get('package_details', {}).get('volume_cu_in', ''),
                'inv_status': s.get('inventory_status', ''),
                'in_stock': s.get('in_stock', 0),
                'in_transit': s.get('in_transit', []),
                'is_anomaly': s.get('is_dim_anomaly', False),
            } for s in skus],
        })

    # 状态标签映射
    launch_map = {
        'can_launch': ('可上架', '#28a745'),
        'cannot_launch': ('不可上架', '#dc3545'),
        'wait_arrival': ('等到仓', '#ffc107'),
        'warning': ('需关注', '#fd7e14'),
    }
    inv_map = {
        'in_stock': '已到仓',
        'in_transit': '在途',
        'no_record': '无记录',
    }
    pkg_color_map = {
        'Standard': '#28a745',
        'AHS-Dimension': '#ffc107',
        'Oversize': '#fd7e14',
        'LTL': '#dc3545',
        'Unknown': '#6c757d',
    }

    # 构建JSON数据
    rows_json = json.dumps(spu_rows, ensure_ascii=False)
    launch_map_json = json.dumps({k: v[0] for k, v in launch_map.items()}, ensure_ascii=False)
    inv_map_json = json.dumps(inv_map, ensure_ascii=False)
    pkg_color_json = json.dumps(pkg_color_map, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>北美市场事业部新品跟进</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; background: #f5f5f5; color: #333; padding: 20px; }}
.header {{ background: linear-gradient(135deg, #1a237e, #3949ab); color: white; padding: 24px 30px; border-radius: 12px; margin-bottom: 20px; }}
.header h1 {{ font-size: 24px; margin-bottom: 6px; }}
.subtitle {{ font-size: 13px; opacity: 0.85; }}
.note {{ background: #e8eaf6; border-left: 4px solid #3949ab; padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; font-size: 13px; line-height: 1.8; }}
.metrics {{ display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }}
.metric {{ background: white; padding: 16px 20px; border-radius: 10px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); min-width: 110px; text-align: center; }}
.metric .label {{ font-size: 12px; color: #888; margin-bottom: 4px; }}
.metric .value {{ font-size: 26px; font-weight: 700; }}
.filters {{ display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; align-items: center; }}
.filter-btn {{ padding: 8px 16px; border: 2px solid #ddd; background: white; border-radius: 20px; cursor: pointer; font-size: 13px; transition: all 0.2s; }}
.filter-btn:hover {{ border-color: #3949ab; }}
.filter-btn.active {{ background: #3949ab; color: white; border-color: #3949ab; }}
.clear-filters-btn {{ padding: 7px 14px; border: 2px solid #dc3545; color: #dc3545; background: white; border-radius: 20px; cursor: pointer; font-size: 13px; margin-left: 4px; }}
.clear-filters-btn:hover {{ background: #dc3545; color: white; }}
.field-filter {{ position: relative; display: inline-block; }}
.field-filter-btn {{ padding: 7px 14px; border: 2px solid #ddd; background: white; border-radius: 20px; cursor: pointer; font-size: 13px; transition: all 0.2s; }}
.field-filter-btn:hover {{ border-color: #3949ab; }}
.field-filter-btn.active {{ background: #e8eaf6; border-color: #3949ab; color: #1a237e; font-weight: 600; }}
.field-filter-panel {{ display: none; position: absolute; top: 100%; left: 0; margin-top: 4px; background: white; border: 1px solid #ddd; border-radius: 8px; box-shadow: 0 4px 16px rgba(0,0,0,0.15); padding: 10px; min-width: 170px; max-height: 300px; overflow-y: auto; z-index: 100; }}
.field-filter-panel.show {{ display: block; }}
.field-filter-panel label {{ display: flex; align-items: center; gap: 6px; padding: 4px 6px; font-size: 13px; cursor: pointer; border-radius: 4px; }}
.field-filter-panel label:hover {{ background: #f0f4ff; }}
.field-filter-panel .panel-actions {{ display: flex; justify-content: space-between; margin-top: 8px; padding-top: 8px; border-top: 1px solid #eee; }}
.field-filter-panel .panel-actions button {{ font-size: 12px; padding: 3px 12px; border: 1px solid #ddd; border-radius: 12px; background: white; cursor: pointer; }}
.field-filter-panel .panel-actions button:hover {{ border-color: #3949ab; }}
.search-box {{ padding: 8px 14px; border: 2px solid #ddd; border-radius: 20px; font-size: 13px; width: 240px; outline: none; }}
.search-box:focus {{ border-color: #3949ab; }}
.table-container {{ background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
thead th {{ background: #f5f5f5; padding: 12px 10px; text-align: left; font-weight: 600; color: #555; border-bottom: 2px solid #ddd; cursor: pointer; white-space: nowrap; user-select: none; position: sticky; top: 0; z-index: 10; }}
thead th:hover {{ background: #e8eaf6; }}
tbody td {{ padding: 10px; border-bottom: 1px solid #eee; vertical-align: middle; }}
.spu-row {{ cursor: pointer; transition: background 0.15s; }}
.spu-row:hover {{ background: #f0f4ff; }}
.spu-row.expanded {{ background: #e8eaf6; }}
.sku-row {{ display: none; }}
.sku-row.visible {{ display: table-row; }}
.sku-row td {{ background: #fafafa; font-size: 12px; color: #666; }}
.expand-icon {{ display: inline-block; width: 16px; transition: transform 0.2s; color: #3949ab; font-weight: bold; }}
.spu-row.expanded .expand-icon {{ transform: rotate(90deg); }}
.pill {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; margin-right: 4px; }}
.pill-new {{ background: #e3f2fd; color: #1565c0; }}
.pill-blue {{ background: #e8eaf6; color: #1a237e; }}
.pill-ltl {{ background: #ffebee; color: #c62828; }}
.pill-multi {{ background: #fff3e0; color: #e65100; }}
.pill-anomaly {{ background: #fdecea; color: #c62828; }}
.anomaly-box {{ margin-top:8px;padding:8px 10px;background:#fdecea;border-left:4px solid #c62828;border-radius:6px;font-size:11px;color:#c62828; }}
.anomaly-box table {{ margin-top:4px; }}
.status-cell {{ font-weight: 600; }}
.alias-tag {{ font-size: 10px; color: #999; cursor: help; }}
.no-record {{ color: #999; font-style: italic; }}
.ddl-urgent {{ color: #dc3545; font-weight: 700; }}
.ddl-soon {{ color: #fd7e14; font-weight: 600; }}
.ddl-normal {{ color: #28a745; }}
.summary-bar {{ background: white; padding: 12px 20px; border-radius: 8px; margin-bottom: 12px; font-size: 13px; color: #666; display: flex; gap: 20px; align-items: center; flex-wrap: wrap; }}
.footer {{ margin-top: 20px; text-align: center; font-size: 12px; color: #999; }}
.btn-download {{ padding: 8px 16px; background: rgba(255,255,255,0.2); color: white; border: 1px solid rgba(255,255,255,0.4); border-radius: 8px; cursor: pointer; font-size: 13px; font-weight: 600; transition: all 0.2s; }}
.btn-download:hover {{ background: rgba(255,255,255,0.35); }}
</style>
</head>
<body>

<div class="header">
    <div style="display:flex;justify-content:space-between;align-items:center;">
        <div>
            <h1>北美市场事业部新品跟进</h1>
            <div class="subtitle">生成时间: {data['generated_at']} | 今日: {today} | 6大数据源全接入</div>
        </div>
        <div style="display:flex;gap:8px;">
            <button class="btn-download" onclick="downloadHTML()">下载报告</button>
            <button class="btn-download" onclick="exportCSV()">导出CSV</button>
        </div>
    </div>
</div>

<div class="note">
<strong>数据说明:</strong>
跟进范围 = 待上架新品(款式表蓝色) {stats['blue_pending_spus']}个 + 新增SPU(款式表无记录) {stats['new_spus']}个 = 共 {stats['total_spu']}个SPU / {stats['total_sku']}个SKU。
<br><strong>多包裹:</strong> 按颜色分组检测A/B/C后缀, SPU级取最大包裹数。
<strong>FedEx类型:</strong> 逐SKU判定(2026规则), 有LTL不可上架。
<strong>库存:</strong> SPU级评估, 有在库→已到仓, 无在库有在途→在途(取最早预计到仓时间), 无→无记录。
<strong>上架DDL:</strong> 已到仓=今日+2天(当天为第1天), 在途=预计到仓时间-7天。
<strong>别名:</strong> 新SPU-1/2/3为同一SPU的别名, 用于交叉匹配。
<strong>SKU异常记录:</strong> 同SPU同颜色下若部分SKU缺重量/尺寸, 该SKU移入异常记录, 不参与SPU包裹类型划分。
</div>

<div class="metrics">
    <div class="metric"><div class="label">总跟进SPU</div><div class="value">{stats['total_spu']}</div></div>
    <div class="metric"><div class="label">总SKU</div><div class="value">{stats['total_sku']}</div></div>
    <div class="metric"><div class="label">待上架</div><div class="value" style="color:#1a237e">{stats['blue_pending_spus']}</div></div>
    <div class="metric"><div class="label">新增</div><div class="value" style="color:#1565c0">{stats['new_spus']}</div></div>
    <div class="metric"><div class="label">可上架</div><div class="value" style="color:#28a745">{stats['can_launch']}</div></div>
    <div class="metric"><div class="label">不可上架</div><div class="value" style="color:#dc3545">{stats['cannot_launch']}</div></div>
    <div class="metric"><div class="label">等到仓</div><div class="value" style="color:#ffc107">{stats['wait_arrival']}</div></div>
    <div class="metric"><div class="label">无记录</div><div class="value" style="color:#6c757d">{stats['no_record']}</div></div>
    <div class="metric"><div class="label">需关注</div><div class="value" style="color:#fd7e14">{stats['warning']}</div></div>
    <div class="metric"><div class="label">多包裹SPU</div><div class="value" style="color:#e65100">{stats['multi_pkg_spus']}</div></div>
    <div class="metric"><div class="label">SKU异常</div><div class="value" style="color:#c62828">{stats['dim_anomaly_skus']}</div></div>
</div>

<div class="summary-bar">
    <span>数据源: 增长产品 {sources.get('growth_eligible',0)} | 上品通知表 {sources.get('self_operated_skus',0)}SKU | 款式表 {sources.get('existing_spus',0)}SPU | SKU尺寸 {sources.get('sku_dimensions',0)} | 库存 {sources.get('inventory',0)} | 在途 {sources.get('in_transit',0)}</span>
</div>

<div class="filters">
    <button class="filter-btn active" data-filter="all">全部</button>
    <button class="filter-btn" data-filter="anomaly">SKU异常记录</button>
    <span id="fieldFilters"></span>
    <button class="clear-filters-btn" id="clearFieldFilters" style="display:none;" onclick="clearFieldFilters()">清除筛选</button>
    <input type="text" class="search-box" id="searchBox" placeholder="搜索SPU/SKU/品类/运营...">
</div>

<div class="table-container">
<table id="mainTable">
<thead>
<tr>
<th style="width:30px;"></th>
<th data-sort="spu">SPU</th>
<th data-sort="source">来源</th>
<th data-sort="category">品类/运营组</th>
<th data-sort="operator">运营</th>
<th data-sort="multi_pkg">多包裹</th>
<th data-sort="spu_pkg_type">FedEx类型</th>
<th data-sort="inv_status">库存状态</th>
<th data-sort="earliest_eta">最早到仓</th>
<th data-sort="launch_ddl">上架DDL</th>
<th data-sort="launch">上架状态</th>
<th data-sort="launch_reason">说明</th>
<th data-sort="sku_count">SKU数</th>
<th data-sort="anomaly">SKU异常记录</th>
</tr>
</thead>
<tbody id="tableBody"></tbody>
</table>
</div>

<div class="footer">
    北美市场事业部新品跟进 | 候选库: 增长产品+上品通知表 | 主记录: 款式表(上架汇总-US) | FedEx 2026规则
</div>

<script>
const rows = {rows_json};
const launchMap = {launch_map_json};
const invMap = {inv_map_json};
const pkgColorMap = {pkg_color_json};
let currentFilter = 'all';
let currentSort = {{ key: 'spu', asc: true }};

function getLaunchLabel(status) {{
    const m = {{ can_launch: ['可上架','#28a745'], cannot_launch: ['不可上架','#dc3545'], wait_arrival: ['等到仓','#ffc107'], warning: ['需关注','#fd7e14'] }};
    return m[status] || ['未知','#999'];
}}

function getInvLabel(status) {{
    return invMap[status] || status;
}}

function getDdlClass(ddl) {{
    if (!ddl) return '';
    const today = new Date('{today}');
    const d = new Date(ddl);
    const diff = Math.ceil((d - today) / (1000*60*60*24));
    if (diff < 0) return 'ddl-urgent';
    if (diff <= 3) return 'ddl-urgent';
    if (diff <= 7) return 'ddl-soon';
    return 'ddl-normal';
}}

function anomalyCellHtml(row) {{
    if (!row.anomaly_count || row.anomaly_count === 0) return '<span class="no-record">-</span>';
    const names = row.sku_anomalies.map(function(s) {{ return s.sku; }}).join('<br>');
    return '<span class="pill pill-anomaly">异常×' + row.anomaly_count + '</span><br><span style="font-size:11px;color:#c62828;font-family:monospace;">' + names + '</span>';
}}

// ========== 多选字段筛选 ==========
const FILTER_FIELDS = [
    {{key:'source', label:'来源', get: r => r.source}},
    {{key:'category', label:'品类/运营组', get: r => r.category || '-'}},
    {{key:'operator', label:'运营', get: r => r.operator || '未分配运营'}},
    {{key:'pkg', label:'FedEx类型', get: r => r.spu_pkg_type}},
    {{key:'inv', label:'库存状态', get: r => getInvLabel(r.inv_status)}},
    {{key:'launch', label:'上架状态', get: r => getLaunchLabel(r.launch)[0]}},
];
let fieldFilters = {{}};

function buildFieldFilters() {{
    const container = document.getElementById('fieldFilters');
    if (!container || !rows) return;
    container.innerHTML = '';
    fieldFilters = {{}};
    FILTER_FIELDS.forEach(f => {{
        fieldFilters[f.key] = new Set();
        const values = new Set();
        rows.forEach(p => values.add(f.get(p)));
        if (f.key === 'pkg') values.add('LTL');
        if (f.key === 'launch') values.add('不可上架');
        const opts = [...values].filter(v => v !== '').sort();
        const wrap = document.createElement('span');
        wrap.className = 'field-filter';
        const labels = opts.map(o => '<label><input type="checkbox" value="' + o + '" onchange="onFieldFilterChange(\'' + f.key + '\', this)"> ' + o + '</label>').join('');
        wrap.innerHTML = '<button class="field-filter-btn" onclick="toggleFilterPanel(\'' + f.key + '\')">' + f.label + ' <span class="ff-count"></span>▾</button>' +
            '<div class="field-filter-panel" id="panel-' + f.key + '">' + labels +
            '<div class="panel-actions"><button onclick="selectAllInPanel(\'' + f.key + '\', true)">全选</button><button onclick="selectAllInPanel(\'' + f.key + '\', false)">清空</button></div></div>';
        container.appendChild(wrap);
    }});
    if (!window._fieldPanelBound) {{
        document.addEventListener('click', closeFieldPanelsOnOutside);
        window._fieldPanelBound = true;
    }}
}}

function closeFieldPanelsOnOutside(e) {{
    if (!e.target.closest('.field-filter')) {{
        document.querySelectorAll('.field-filter-panel.show').forEach(p => p.classList.remove('show'));
    }}
}}

function toggleFilterPanel(key) {{
    const panel = document.getElementById('panel-' + key);
    if (!panel) return;
    const isOpen = panel.classList.contains('show');
    document.querySelectorAll('.field-filter-panel.show').forEach(p => p.classList.remove('show'));
    if (!isOpen) panel.classList.add('show');
}}

function onFieldFilterChange(key, cb) {{
    if (cb.checked) fieldFilters[key].add(cb.value);
    else fieldFilters[key].delete(cb.value);
    updateFieldFilterUI(key);
    renderTable();
}}

function selectAllInPanel(key, all) {{
    const panel = document.getElementById('panel-' + key);
    if (!panel) return;
    panel.querySelectorAll('input[type=checkbox]').forEach(cb => {{
        cb.checked = all;
        if (all) fieldFilters[key].add(cb.value);
        else fieldFilters[key].delete(cb.value);
    }});
    updateFieldFilterUI(key);
    renderTable();
}}

function updateFieldFilterUI(key) {{
    const btn = document.querySelector('.field-filter-btn[onclick*="' + key + '"]');
    if (!btn) return;
    const cnt = fieldFilters[key].size;
    btn.querySelector('.ff-count').textContent = cnt > 0 ? '(' + cnt + ')' : '';
    if (cnt > 0) btn.classList.add('active');
    else btn.classList.remove('active');
    const clearBtn = document.getElementById('clearFieldFilters');
    if (clearBtn) clearBtn.style.display = FILTER_FIELDS.some(f => fieldFilters[f.key].size > 0) ? 'inline-block' : 'none';
}}

function clearFieldFilters() {{
    FILTER_FIELDS.forEach(f => {{
        fieldFilters[f.key].clear();
        const panel = document.getElementById('panel-' + f.key);
        if (panel) panel.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = false);
        updateFieldFilterUI(f.key);
    }});
    renderTable();
}}

function renderTable() {{
    let filtered = rows;
    if (currentFilter === 'anomaly') filtered = filtered.filter(r => r.anomaly_count > 0);

    // 字段多选筛选 (同字段OR, 跨字段AND)
    FILTER_FIELDS.forEach(f => {{
        const sel = fieldFilters[f.key];
        if (sel && sel.size > 0) {{
            filtered = filtered.filter(r => sel.has(f.get(r)));
        }}
    }});

    const search = document.getElementById('searchBox').value.toLowerCase();
    if (search) {{
        filtered = filtered.filter(r => {{
            const allText = (r.spu + ' ' + r.aliases.join(' ') + ' ' + r.category + ' ' + r.operator + ' ' +
                r.skus.map(s => s.sku).join(' ')).toLowerCase();
            return allText.includes(search);
        }});
    }}

    // Sort
    filtered.sort((a, b) => {{
        let va = a[currentSort.key], vb = b[currentSort.key];
        if (typeof va === 'string') va = va.toLowerCase();
        if (typeof vb === 'string') vb = vb.toLowerCase();
        if (va < vb) return currentSort.asc ? -1 : 1;
        if (va > vb) return currentSort.asc ? 1 : -1;
        return 0;
    }});

    const tbody = document.getElementById('tableBody');
    tbody.innerHTML = '';

    filtered.forEach((row, i) => {{
        const [launchLabel, launchColor] = getLaunchLabel(row.launch);
        const sourcePill = row.source_raw === 'blue_pending'
            ? '<span class="pill pill-blue">待上架</span>'
            : '<span class="pill pill-new">新增</span>';
        const ltlTag = row.has_ltl ? '<span class="pill pill-ltl">LTL</span>' : '';
        const multiTag = row.multi_pkg > 1
            ? `<span class="pill pill-multi">${{row.multi_pkg}}包裹</span>`
            : '';
        const aliasTag = row.aliases.length > 1
            ? `<span class="alias-tag" title="${{row.aliases.join(', ')}}">+${{row.aliases.length - 1}}别名</span>`
            : '';
        const ddlClass = getDdlClass(row.launch_ddl);

        // SPU主行
        const tr = document.createElement('tr');
        tr.className = 'spu-row';
        tr.dataset.idx = i;
        tr.innerHTML = `
            <td><span class="expand-icon">▶</span></td>
            <td style="font-weight:600;">${{row.spu}} ${{aliasTag}}</td>
            <td>${{sourcePill}} ${{ltlTag}} ${{multiTag}}</td>
            <td>${{row.category || '-'}}</td>
            <td>${{row.operator || '未分配运营'}}</td>
            <td>${{row.multi_pkg_display || (row.multi_pkg > 1 ? row.multi_pkg + '包' : '单包')}}</td>
            <td style="font-weight:600;color:${{pkgColorMap[row.spu_pkg_type] || '#999'}};">${{row.spu_pkg_type}}</td>
            <td>${{getInvLabel(row.inv_status)}}</td>
            <td>${{row.earliest_eta || '<span class="no-record">-</span>'}}</td>
            <td class="${{ddlClass}}">${{row.launch_ddl || '<span class="no-record">-</span>'}}</td>
            <td class="status-cell" style="color:${{launchColor}}">${{launchLabel}}</td>
            <td style="font-size:12px;color:#666;">${{row.launch_reason}}</td>
            <td style="text-align:center;">${{row.sku_count}}</td>
            <td>${{anomalyCellHtml(row)}}</td>
        `;
        tr.onclick = function() {{ toggleExpand(this, row, i); }};
        tbody.appendChild(tr);

        // SKU子行 (默认隐藏)
        if (row.skus.length > 0) {{
            const skuTr = document.createElement('tr');
            skuTr.className = 'sku-row';
            skuTr.dataset.parentIdx = i;
            let skuHtml = '<td></td><td colspan="13" style="padding:8px 12px;">';
            skuHtml += '<table style="width:100%;font-size:12px;border-collapse:collapse;">';
            skuHtml += '<thead><tr style="background:#f0f0f0;">';
            skuHtml += '<th style="padding:4px 8px;text-align:left;">SKU</th>';
            skuHtml += '<th style="padding:4px 8px;">FedEx类型</th>';
            skuHtml += '<th style="padding:4px 8px;">重量(lb)</th>';
            skuHtml += '<th style="padding:4px 8px;">最长边(in)</th>';
            skuHtml += '<th style="padding:4px 8px;">长+周长(in)</th>';
            skuHtml += '<th style="padding:4px 8px;">体积(cu.in)</th>';
            skuHtml += '<th style="padding:4px 8px;">库存状态</th>';
            skuHtml += '<th style="padding:4px 8px;">在库量</th>';
            skuHtml += '<th style="padding:4px 8px;">在途信息</th>';
            skuHtml += '</tr></thead><tbody>';
            row.skus.filter(s => !s.is_anomaly).forEach(s => {{
                const pkgColor = pkgColorMap[s.pkg_type] || '#999';
                const transitStr = s.in_transit && s.in_transit.length > 0
                    ? s.in_transit.map(t => `${{t.warehouse || ''}}: ${{t.quantity}}个 ${{t.arrival_date || t.eta || ''}}`).join('; ')
                    : '<span class="no-record">-</span>';
                skuHtml += `<tr>`;
                skuHtml += `<td style="padding:4px 8px;font-family:monospace;">${{s.sku}}</td>`;
                skuHtml += `<td style="padding:4px 8px;text-align:center;color:${{pkgColor}};font-weight:600;">${{s.pkg_type}}</td>`;
                skuHtml += `<td style="padding:4px 8px;text-align:center;">${{s.weight_lb !== '' ? s.weight_lb : '-'}}</td>`;
                skuHtml += `<td style="padding:4px 8px;text-align:center;">${{s.longest || '-'}}</td>`;
                skuHtml += `<td style="padding:4px 8px;text-align:center;">${{s.lpg || '-'}}</td>`;
                skuHtml += `<td style="padding:4px 8px;text-align:center;">${{s.volume || '-'}}</td>`;
                skuHtml += `<td style="padding:4px 8px;text-align:center;">${{getInvLabel(s.inv_status)}}</td>`;
                skuHtml += `<td style="padding:4px 8px;text-align:center;">${{s.in_stock > 0 ? s.in_stock : '-'}}</td>`;
                skuHtml += `<td style="padding:4px 8px;font-size:11px;">${{transitStr}}</td>`;
                skuHtml += `</tr>`;
            }});
            skuHtml += '</tbody></table>';
            // SKU异常记录 (同色部分SKU缺重量/尺寸, 已从包裹类型划分中排除)
            if (row.sku_anomalies && row.sku_anomalies.length > 0) {{
                let anomHtml = '<table style="width:100%;font-size:12px;border-collapse:collapse;margin-top:4px;">';
                anomHtml += '<thead><tr style="background:#fdecea;">';
                anomHtml += '<th style="padding:4px 8px;text-align:left;">SKU(尺寸缺失)</th>';
                anomHtml += '<th style="padding:4px 8px;">库存状态</th>';
                anomHtml += '<th style="padding:4px 8px;">在库量</th>';
                anomHtml += '<th style="padding:4px 8px;">在途信息</th>';
                anomHtml += '</tr></thead><tbody>';
                row.sku_anomalies.forEach(function(s) {{
                    const transitStr = s.in_transit && s.in_transit.length > 0
                        ? s.in_transit.map(t => `${{t.warehouse || ''}}: ${{t.quantity}}个 ${{t.arrival_date || t.eta || ''}}`).join('; ')
                        : '<span class="no-record">-</span>';
                    anomHtml += `<tr>`;
                    anomHtml += `<td style="padding:4px 8px;font-family:monospace;color:#c62828;">${{s.sku}}</td>`;
                    anomHtml += `<td style="padding:4px 8px;text-align:center;">${{getInvLabel(s.inv_status)}}</td>`;
                    anomHtml += `<td style="padding:4px 8px;text-align:center;">${{s.in_stock > 0 ? s.in_stock : '-'}}</td>`;
                    anomHtml += `<td style="padding:4px 8px;font-size:11px;">${{transitStr}}</td>`;
                    anomHtml += `</tr>`;
                }});
                anomHtml += '</tbody></table>';
                skuHtml += `<div class="anomaly-box"><strong>SKU异常记录</strong>（同色部分SKU缺重量/尺寸，已从包裹类型划分中排除）：${{anomHtml}}</div>`;
            }}
            // 多包裹详情
            if (row.multi_pkg > 1 && Object.keys(row.color_pkg).length > 0) {{
                const colorDetails = Object.entries(row.color_pkg)
                    .map(([c, d]) => `${{c}}: ${{d.package_types ? d.package_types.join('/') : d.count + '包'}}`)
                    .join(' | ');
                skuHtml += `<div style="margin-top:8px;padding:6px 10px;background:#fff3e0;border-radius:6px;font-size:11px;color:#e65100;"><strong>多包裹详情:</strong> ${{colorDetails}}</div>`;
            }}
            // 来源信息
            const sourceInfo = [];
            if (row.lines && row.lines.length > 0) sourceInfo.push('产品线: ' + row.lines.join(', '));
            if (row.candidate_sources && row.candidate_sources.length > 0) sourceInfo.push('候选来源: ' + row.candidate_sources.join(', '));
            if (row.notify_time) sourceInfo.push('通知上架时间: ' + row.notify_time);
            if (sourceInfo.length > 0) {{
                skuHtml += `<div style="margin-top:6px;padding:6px 10px;background:#e8eaf6;border-radius:6px;font-size:11px;color:#3949ab;">${{sourceInfo.join(' | ')}}</div>`;
            }}
            skuHtml += '</td>';
            skuTr.innerHTML = skuHtml;
            tbody.appendChild(skuTr);
        }}
    }});

    // 更新计数
    const countEl = document.querySelector('.summary-bar');
    const existingCount = countEl.querySelector('#filterCount');
    if (existingCount) existingCount.remove();
    const span = document.createElement('span');
    span.id = 'filterCount';
    span.innerHTML = `| 当前显示: <strong>${{filtered.length}}</strong> 个SPU`;
    countEl.appendChild(span);
}}

function toggleExpand(spuRow, rowData, idx) {{
    spuRow.classList.toggle('expanded');
    const skuRows = document.querySelectorAll(`.sku-row[data-parent-idx="${{idx}}"]`);
    skuRows.forEach(r => r.classList.toggle('visible'));
}}

// Filter buttons
document.querySelectorAll('.filter-btn').forEach(btn => {{
    btn.onclick = function() {{
        document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
        this.classList.add('active');
        currentFilter = this.dataset.filter;
        renderTable();
    }};
}});

// Search
document.getElementById('searchBox').oninput = () => renderTable();

// Sort
document.querySelectorAll('thead th[data-sort]').forEach(th => {{
    th.onclick = function() {{
        const key = this.dataset.sort;
        if (currentSort.key === key) {{
            currentSort.asc = !currentSort.asc;
        }} else {{
            currentSort.key = key;
            currentSort.asc = true;
        }}
        renderTable();
    }};
}});

// Download report as HTML
function downloadHTML() {{
    const html = '<!DOCTYPE html>\n' + document.documentElement.outerHTML;
    const blob = new Blob([html], {{type: 'text/html;charset=utf-8'}});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = '北美市场事业部新品跟进_' + '{today}' + '.html';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}}

// Export visible data as CSV
function exportCSV() {{
    let csv = '\\uFEFF';
    csv += 'SPU,来源,品类/运营组,运营,多包裹,库存状态,最早到仓,上架DDL,上架状态,说明,SKU数,SKU异常记录\n';
    let filtered = rows;
    if (currentFilter === 'anomaly') filtered = filtered.filter(r => r.anomaly_count > 0);

    // 字段多选筛选 (同字段OR, 跨字段AND)
    FILTER_FIELDS.forEach(f => {{
        const sel = fieldFilters[f.key];
        if (sel && sel.size > 0) {{
            filtered = filtered.filter(r => sel.has(f.get(r)));
        }}
    }});

    const search = document.getElementById('searchBox').value.toLowerCase();
    if (search) {{
        filtered = filtered.filter(r => {{
            const allText = (r.spu + ' ' + r.aliases.join(' ') + ' ' + r.category + ' ' + r.operator + ' ' +
                r.skus.map(s => s.sku).join(' ')).toLowerCase();
            return allText.includes(search);
        }});
    }}
    filtered.forEach(r => {{
        const launchInfo = getLaunchLabel(r.launch);
        const line = [
            r.spu,
            r.source_raw === 'blue_pending' ? '待上架' : '新增',
            r.category || '',
            r.operator || '未分配运营',
            r.multi_pkg_display || (r.multi_pkg > 1 ? r.multi_pkg + '包' : '单包'),
            getInvLabel(r.inv_status),
            r.earliest_eta || '',
            r.launch_ddl || '',
            launchInfo[0],
            r.launch_reason,
            r.sku_count,
            (r.sku_anomalies || []).map(s => s.sku).join('; ')
        ].map(v => `"${{String(v).replace(/"/g, '""')}}"`).join(',');
        csv += line + '\n';
    }});
    const blob = new Blob([csv], {{type: 'text/csv;charset=utf-8'}});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = '北美市场事业部新品跟进数据_' + '{today}' + '.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}}

buildFieldFilters();
renderTable();
</script>
</body>
</html>"""

    output_file = BASE_DIR / "北美市场事业部新品跟进.html"
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"报告已生成: {output_file}")
    return output_file


if __name__ == "__main__":
    generate_report()
