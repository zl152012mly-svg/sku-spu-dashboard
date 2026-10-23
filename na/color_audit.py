# -*- coding: utf-8 -*-
"""
颜色识别核对报表 (2026-09-21)
目的: 把新色判定链路里"识别到的所有颜色"摊开给业务核对, 尤其是被误当成独立颜色的后缀/部件名。

数据来源:
  A. 在库/在途池颜色 —— output/new_color_data.json (process_all 生成的 sku_rows, 含每个库存SKU的解析颜色与上架判定)
  B. 已上架基准颜色 —— 现算: Listing(*Listing*.xlsx) + Walmart(*ItemReport*.csv) 平台SKU
                        → 套件表(*套件成员*.csv) 按 店铺+14K货号 取子件(成员货品) → 解析颜色
输出: output/颜色识别核对表_YYYYMMDD.xlsx
"""
import os
import re
import json
import glob
from collections import defaultdict, Counter

import pandas as pd

import new_color_module as ncm
from process_script import extract_spu_from_sku, parse_sku_color_suffix, load_existing_spu_list

BASE = os.path.dirname(os.path.abspath(__file__))


def _glob1(folder, pattern):
    hits = sorted(glob.glob(os.path.join(folder, pattern)))
    return hits[0] if hits else None


# ---------- 形态判断 ----------
PART_WORDS = {
    'HDW', 'HARDWARE', 'LEGS', 'LEG', 'TOE', 'CHASSIS', 'PART', 'SUPPORT', 'ADM',
    'MTL', 'SHD', 'FRN', 'FLT', 'TRD', 'SPA', 'QUEEN', 'KING', 'WIN', 'STO',
}
FULL_COLOR_WORDS = {
    'BLACK', 'WHITE', 'GREEN', 'BLUE', 'RED', 'PINK', 'PURPLE', 'YELLOW', 'ORANGE',
    'BROWN', 'GREY', 'GRAY', 'NAVY', 'BEIGE', 'IVORY', 'TEAL', 'TURQUOISE', 'WALNUT',
    'COCOA', 'ESPRESSO', 'OLIVE', 'INDIGO', 'LINEN', 'SAGE', 'MUSTARD', 'CAMEL',
    'CHARCOAL', 'CHERRY', 'SADDLE', 'TAUPE', 'OATMEAL', 'FUCHSIA', 'FLORAL', 'COWHIDE',
    'PEBBLE', 'BROCADE', 'BRICK', 'DOVE', 'FOG', 'KONA', 'TRUFFLE', 'PINE', 'BURGUNDY',
    'SPRING', 'GARDEN', 'BIRD', 'ASH',
}
SUFFIX_HINT = re.compile(r'-(CC|PL|PL-\d|A-[PV]|[FGH]\d?|[A-C]|S\d)$')


def classify(code):
    """返回 (形态, 是否疑似非颜色, 原因)"""
    c = code.strip()
    if not c:
        return ('空', True, '空值')
    if c.isdigit():
        return ('纯数字', True, '纯数字, 不是颜色')
    if ' ' in c:
        base = c.split(' ')[0]
        if base in PART_WORDS:
            return ('含空格-部件', True, '含空格且词根为部件名(%s)' % base)
        return ('含空格', True, '含空格, 疑似描述文字')
    if '*' in c:
        return ('含*号', True, '含*号, 疑似部件/数量说明')
    if '-' in c:
        parts = c.split('-')
        if parts[0] in PART_WORDS:
            return ('部件-后缀', True, '首段为部件名(%s)' % parts[0])
        if re.match(r'^\d+$', parts[0]):
            return ('数字前缀-连字符', True, '首段为数字')
        # 前缀型: KB-/QB-/S2-/G-/3- 等
        if re.match(r'^(KB|QB|S2|S3|TB|FB|G|K|Q|CO|3)$', parts[0]) and len(parts) == 2:
            return ('规格前缀+色码', True, '前缀(%s)疑为规格(如KB=King Bed/QB=Queen), 颜色实为 %s, 前缀未剥离→与本体色重复计数' % (parts[0], parts[1]))
        tail = parts[-1]
        if re.match(r'^[A-Z]$', tail) or tail in ('CC', 'PL', 'V', 'P', 'S2', 'S3', 'CV'):
            return ('色码+变体后缀', True, '后缀(-%s)疑为变体/部件未剥离, 颜色实为 %s→与本体色重复计数' % (tail, parts[0]))
        if re.match(r'^[FGH]\d?$', tail) or tail in ('FA', 'FB', 'FC'):
            return ('色码+面料后缀', True, '后缀(-%s)疑为面料变体未剥离, 颜色实为 %s→与本体色重复计数' % (tail, parts[0]))
        if len(parts) >= 3 and parts[-1].isdigit():
            return ('色码+多段后缀', True, '多段后缀(-%s)未剥离, 颜色实为 %s' % ('-'.join(parts[1:]), parts[0]))
        # 多段构成: 找其中真正的颜色段
        cand = [p for p in parts
                if p in FULL_COLOR_WORDS
                or (re.match(r'^[A-Z]{3,4}$', p) and p not in PART_WORDS)]
        if cand:
            return ('含色码的复合码', True,
                    '整串被当成一个颜色; 其中颜色段疑为 %s, 其余(%s)为规格/部件' % (
                        cand[0], '/'.join(p for p in parts if p != cand[0])))
        return ('多段连字符', True, '多段连字符, 疑似非标准色码')
    if c in PART_WORDS:
        return ('部件名', True, '部件/规格名, 不是颜色')
    if c in FULL_COLOR_WORDS:
        return ('颜色全称', False, '')
    if re.match(r'^[A-Z]{2,4}$', c):
        return ('短色码', False, '')
    if re.match(r'^[A-Z]{5,}$', c):
        return ('字母长词', True, '全字母长词, 需确认是否颜色')
    return ('其他', True, '未归类')


def main():
    # ---------- A. 池颜色 (来自 process_all 生成的 json) ----------
    jp = os.path.join(BASE, 'output', 'new_color_data.json')
    if not os.path.exists(jp):
        raise SystemExit('缺少 output/new_color_data.json, 请先运行 process_all')
    data = json.load(open(jp, encoding='utf-8'))
    sku_rows = data['sku_rows']

    # 颜色码 -> 统计
    pool_sku = defaultdict(set)      # code -> {库存SKU}
    pool_spu = defaultdict(set)      # code -> {SPU}
    listed_spu = defaultdict(set)    # code -> {SPU(该色被判已上架)}
    new_spu = defaultdict(set)       # code -> {SPU(该色被判新色)}
    for r in sku_rows:
        c = str(r.get('颜色') or '').strip()
        if not c:
            continue
        pool_sku[c].add(r['库存SKU'])
        pool_spu[c].add(r['SPU'])
        if r['上架状态'] == '已上架':
            listed_spu[c].add(r['SPU'])
        else:
            new_spu[c].add(r['SPU'])

    # 新色明细(仅未上架)
    new_rows = pd.DataFrame(data['new_colors'], columns=['SPU', '店铺', '新色', '库存状态', '数量', '预计到仓', '库存SKU'])

    # ---------- B. 已上架基准颜色 (现算) ----------
    style = load_existing_spu_list()
    known_spus = set(style['spu_set']) | set(style['spu_to_aliases'].keys()) | set(style['alias_to_spu'].keys())
    alias_to_spu = dict(style['alias_to_spu'])

    def sku_to_spu_color(sku):
        spu = extract_spu_from_sku(sku, known_spus)
        color, _sfx = parse_sku_color_suffix(sku, spu)
        return alias_to_spu.get(spu, spu), (color.upper() if color else '')

    f_listing = _glob1(BASE, '*Listing*.xlsx')
    f_wm = _glob1(BASE, '*ItemReport*.csv')
    f_suite = _glob1(BASE, '*套件成员*.csv')

    suite = ncm._read_csv_any(f_suite)
    suite = suite[suite['成员货品'].str.strip() != '']
    suite_members = defaultdict(set)
    for _, r in suite.drop_duplicates(subset=['店铺', '14K货号', '成员货品']).iterrows():
        suite_members[(r['店铺'].strip(), r['14K货号'].strip())].add(r['成员货品'].strip())

    listing_status = {}
    import openpyxl
    wb = openpyxl.load_workbook(f_listing, read_only=True)
    for row in wb.active.iter_rows(min_row=2, values_only=True):
        if not row or not row[0]:
            continue
        msku, status, shop = str(row[0]).strip(), str(row[1] or '').strip(), str(row[2] or '').strip()
        if shop not in ncm.AMAZON_STORE_MAP:
            continue
        listing_status[(ncm.AMAZON_STORE_MAP[shop][0], msku)] = status
    wb.close()
    wm = ncm._read_csv_any(f_wm)
    for _, r in wm.iterrows():
        sku = str(r['SKU'] or '').strip()
        if sku:
            listing_status[(ncm.WALMART_STORE[0], sku)] = str(r['Publish Status'] or '').strip()

    base_detail = []
    base_pool = defaultdict(set)     # (颜色码) -> {SPU}
    for (store, k14), status in listing_status.items():
        if not ncm._is_nondeleted(store, status):
            continue
        members = suite_members.get((store, k14))
        hit = bool(members)
        if not members:
            members = {k14}
        for m in members:
            spu, color = sku_to_spu_color(m)
            if not spu or not color:
                continue
            base_pool[color].add(spu)
            base_detail.append({
                '店铺': ncm.store_label(store),
                'SPU': spu,
                '已上架颜色': color,
                '子件(库存SKU)': m,
                '平台SKU': k14,
                '平台状态': status,
                '命中套件': '是' if hit else '否(回退)',
            })

    # ---------- C. 汇总颜色总表 ----------
    all_codes = sorted(set(pool_sku) | set(base_pool))
    rows = []
    for c in all_codes:
        shape, sus, why = classify(c)
        n_listed = len(listed_spu.get(c, set()))
        n_new = len(new_spu.get(c, set()))
        if c in base_pool and c in new_spu:
            verdict = '已上架+仍被判新色'
        elif c in new_spu:
            verdict = '仅被判新色'
        elif c in base_pool:
            verdict = '仅已上架基准'
        else:
            verdict = '仅池中出现(未参与判定)'
        in_pool = c in pool_sku
        in_base = c in base_pool
        source = '在库/在途池 + 已上架基准' if (in_pool and in_base) else ('在库/在途池' if in_pool else '已上架基准')
        ex_sku = sorted(pool_sku.get(c, set()))[:1]
        ex_spu = sorted(pool_spu.get(c, set()))[:1] or sorted(base_pool.get(c, set()))[:1]
        rows.append({
            '颜色码': c,
            '出现来源': source,
            '形态': shape,
            '疑似非颜色': '是' if sus else '',
            '疑点说明': why,
            '在库/在途SKU数': len(pool_sku.get(c, set())),
            '涉及SPU数': len(pool_spu.get(c, set())),
            '已上架SPU数': n_listed,
            '被判新色SPU数': n_new,
            '基准中已上架SPU数': len(base_pool.get(c, set())),
            '判定倾向': verdict,
            '示例库存SKU': ex_sku[0] if ex_sku else '',
            '示例SPU': ex_spu[0] if ex_spu else '',
            '你的判定(留空)': '',
        })
    all_df = pd.DataFrame(rows)
    total = all_df.sort_values(['疑似非颜色', '被判新色SPU数', '颜色码'],
                               ascending=[False, False, True])
    # 池颜色(真正参与新色判定的) —— 核心核对表
    pool_df = all_df[all_df['出现来源'].str.contains('池')].sort_values(
        ['被判新色SPU数', '在库/在途SKU数'], ascending=False)
    suspect = total[total['疑似非颜色'] == '是'].copy()

    # 只影响新色的可疑码(最需要处理)
    noisy = pool_df[(pool_df['疑似非颜色'] == '是') & (pool_df['被判新色SPU数'] > 0)].copy()

    # ---------- D. 已上架基准颜色汇总 ----------
    base_df = pd.DataFrame(base_detail)
    if len(base_df):
        base_sum = (base_df.groupby('已上架颜色')
                    .agg(店铺数=('店铺', 'nunique'), SPU数=('SPU', 'nunique'),
                         平台SKU数=('平台SKU', 'nunique'))
                    .reset_index().rename(columns={'已上架颜色': '颜色码'}))
        base_sum = base_sum.sort_values('SPU数', ascending=False)
    else:
        base_sum = pd.DataFrame(columns=['颜色码', '店铺数', 'SPU数', '平台SKU数'])

    # ---------- E. SPU×颜色 对照(色板) ----------
    board = (pd.DataFrame(sku_rows)
             .groupby(['SPU', '颜色', '上架状态'])
             .agg(店铺=('店铺', lambda s: ', '.join(sorted(set(s)))),
                  SKU数=('库存SKU', 'nunique'),
                  库存状态=('库存状态', lambda s: ', '.join(sorted(set(s)))))
             .reset_index().sort_values(['SPU', '上架状态', '颜色']))

    # ---------- 输出 ----------
    out = os.path.join(BASE, 'output', '颜色识别核对表_20260921.xlsx')
    # ---------- F. 误报影响量化 (按形态, 基于真实的新色行) ----------
    shape_of = dict(zip(all_df['颜色码'], all_df['形态']))
    why_of = dict(zip(all_df['颜色码'], all_df['疑点说明']))
    sus_of = dict(zip(all_df['颜色码'], all_df['疑似非颜色']))
    new_rows['形态'] = new_rows['新色'].map(shape_of).fillna('未归类')
    new_rows['疑似非颜色'] = new_rows['新色'].map(sus_of).fillna('')
    new_rows['疑点说明'] = new_rows['新色'].map(why_of).fillna('')
    only_new = new_rows[new_rows['疑似非颜色'] == '是']
    impact = (new_rows.groupby('形态')
              .agg(新色行数=('新色', 'count'), 涉及SPU数=('SPU', 'nunique'),
                   涉及颜色码数=('新色', 'nunique'))
              .reset_index())
    impact['其中疑似非颜色行数'] = impact['形态'].map(
        only_new.groupby('形态')['新色'].count()).fillna(0).astype(int)
    impact = impact.sort_values('新色行数', ascending=False)

    # 建议剥掉的前缀/后缀 → 命中本体色
    def suggest_base(code):
        for p in code.split('-'):
            p = p.strip()
            if not p or p in PART_WORDS:
                continue
            head = p.split(' ')[0]
            if head in FULL_COLOR_WORDS:
                return head
            if p in FULL_COLOR_WORDS:
                return p
            if re.match(r'^[A-Z]{3,10}$', p):
                return p
        return ''

    noisy = noisy.copy()
    noisy['本体颜色建议'] = noisy['颜色码'].map(suggest_base)
    # 精确口径: 该色被判新色的 (店铺,SPU) 上, 本体色是否本来就在该店已上架基准里 → 真误报
    bl_by_store_spu = defaultdict(set)
    for r in base_detail:
        bl_by_store_spu[(r['店铺'], r['SPU'])].add(r['已上架颜色'])
    new_by_code = defaultdict(list)   # code -> [(店铺, SPU)]
    for r in new_rows.itertuples():
        new_by_code[r.新色].append((r.店铺, r.SPU))

    def confirm_fp(code):
        b = suggest_base(code)
        if not b:
            return 0, 0
        pairs = new_by_code.get(code, [])
        hit = [p for p in pairs if b in bl_by_store_spu.get(p, set())]
        return len({p[1] for p in hit}), len(hit)

    cf = noisy['颜色码'].map(confirm_fp)
    noisy['本体色已上架的SPU数'] = cf.map(lambda v: v[0])
    noisy['确认为误报'] = noisy['本体色已上架的SPU数'].map(lambda n: '是' if n > 0 else '待人工判断')
    noisy['消除后可减少新色行(估)'] = cf.map(lambda v: v[1])
    fp = noisy[noisy['确认为误报'] == '是']

    note = pd.DataFrame({
        '项目': [
            '生成时间', '池颜色来源', '基准颜色来源',
            '颜色码总数', '其中在库/在途池颜色数', '其中已上架基准颜色数',
            '疑似非颜色颜色码', '其中在库/在途池中造成新色误报',
            '【已确认误报】颜色码数', '【已确认误报】吃掉新色行数',
            '【待人工判断】颜色码数',
            '在库/在途SKU行数', '待上架新色行数', 'SPU主行',
            '—', '判定口径说明',
            '① 已确认误报', '② 待人工判断', '③ 真新色',
            '—', '各表含义',
            '在库在途池颜色_核心', '误报影响_按形态', '待确认_误报新色',
            '颜色识别总表', '待确认_全部可疑', '已上架基准颜色', 'SPU颜色板', '待上架新色明细',
        ],
        '说明': [
            data.get('generated_at', ''),
            os.path.basename(jp) + '  (process_all 生成的 sku_rows, 含在库+在途全部SKU的解析颜色与上架判定)',
            os.path.basename(f_listing) + ' + ' + os.path.basename(f_wm) + ' + ' + os.path.basename(f_suite),
            len(total), len(pool_df), int((all_df['出现来源'].str.contains('基准')).sum()),
            len(suspect), len(noisy),
            len(fp), int(fp['消除后可减少新色行(估)'].sum()),
            len(noisy) - len(fp),
            len(sku_rows), len(new_rows), len(data['spu_rows']),
            '',
            '新色判定为「颜色级」: 待上架新色 = (在库+在途颜色) − 同店同SPU的已上架基准颜色',
            '颜色码形态为 后缀型(-CC/-PL/-A-P 等)或 前缀型(KB-/QB-/S2- 等), 剥掉前后缀后的本体色在该店该SPU已上架 → 属重复计数, 应剔除',
            '编码带前后缀/部件名, 但剥掉后的本体色在该店该SPU并未上架 → 可能是真新色, 只是编码不规范(需业务确认保留还是清洗)',
            '标准3-4位色码与颜色全称, 判定结果可信',
            '',
            '',
            '核心核对表: 只看真正参与新色判定的在库/在途颜色, 按造成的新色SPU数排序',
            '各形态吃掉多少新色行(量化修完能减少多少)',
            '在库/在途池中可疑码明细 + 本体色建议 + 是否确认为误报',
            '全部颜色码(池+基准)一览, 末列可手填判定',
            '全部疑似非颜色码',
            '已上架基准颜色(按颜色码汇总)',
            'SPU×颜色 是否已上架对照板',
            '待上架新色逐行明细',
        ],
    })
    with pd.ExcelWriter(out, engine='openpyxl') as w:
        note.to_excel(w, sheet_name='说明', index=False)
        pool_df.to_excel(w, sheet_name='在库在途池颜色_核心', index=False)
        impact.to_excel(w, sheet_name='误报影响_按形态', index=False)
        noisy.to_excel(w, sheet_name='待确认_误报新色', index=False)
        total.to_excel(w, sheet_name='颜色识别总表', index=False)
        suspect.to_excel(w, sheet_name='待确认_全部可疑', index=False)
        base_sum.to_excel(w, sheet_name='已上架基准颜色', index=False)
        board.to_excel(w, sheet_name='SPU颜色板', index=False)
        new_rows.to_excel(w, sheet_name='待上架新色明细', index=False)
        # 自适应列宽 + 冻结首行
        for ws in w.sheets.values():
            ws.freeze_panes = 'A2'
            for col in ws.columns:
                m = 0
                for c in col[:200]:
                    v = '' if c.value is None else str(c.value)
                    m = max(m, sum(2 if ord(ch) > 127 else 1 for ch in v))
                ws.column_dimensions[col[0].column_letter].width = min(max(m + 2, 8), 46)

    print('=' * 60)
    print('颜色码总数        :', len(total))
    print('  在库/在途池颜色 :', len(pool_df))
    print('  已上架基准颜色  :', int((all_df['出现来源'].str.contains('基准')).sum()))
    print('疑似非颜色        :', len(suspect))
    print('  其中造成新色误报:', len(noisy))
    print('输出:', out)
    print()
    print('--- 形态分布(仅池颜色) ---')
    print(pool_df.groupby('形态').agg(颜色码数=('颜色码', 'count'), 被判新色SPU数合计=('被判新色SPU数', 'sum'))
          .sort_values('颜色码数', ascending=False).to_string())
    print()
    print('--- 误报影响量化 (基于待上架新色 855 行) ---')
    print(impact.to_string(index=False))
    print()
    print(f"==> 已确认误报: {len(fp)} 个颜色码, 涉及 {int(fp['消除后可减少新色行(估)'].sum())} 行新色 "
          f"(占 855 行的 {fp['消除后可减少新色行(估)'].sum() / max(len(new_rows), 1) * 100:.1f}%)")
    print(f"==> 另有 {len(noisy) - len(fp)} 个可疑码需人工判断(本体色不在基准里, 可能是真新色但编码带前缀/后缀)")
    print()
    print('--- 在库/在途池 可疑码明细 (按新色SPU数) ---')
    for _, r in noisy.sort_values('被判新色SPU数', ascending=False).iterrows():
        print(f"{r['颜色码']:<16} 新色SPU={r['被判新色SPU数']:<3} 形态={r['形态']:<14} "
              f"本体={r['本体颜色建议'] or '-':<9} 本体已上架SPU数={r['本体色已上架的SPU数']:<3} "
              f"判定={r['确认为误报']:<6} 可减行={r['消除后可减少新色行(估)']:<3} 例SPU={r['示例SPU']}")
    print()
    print('--- 在库/在途池 颜色码 (按被判新色SPU数) ---')
    for _, r in pool_df.iterrows():
        flag = '  <-- 可疑' if r['疑似非颜色'] == '是' else ''
        print(f"{r['颜色码']:<18} 新色SPU={r['被判新色SPU数']:>3}  池SKU={r['在库/在途SKU数']:>3}  已上架SPU={r['已上架SPU数']:>3}{flag}")


if __name__ == '__main__':
    main()
