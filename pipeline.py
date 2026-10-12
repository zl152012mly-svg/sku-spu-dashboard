# -*- coding: utf-8 -*-
"""
SKU&SPU 清洗流水线（v9 口径，可复用模块）
========================================
把 clean_sku.py(阶段一) + build_final.py(阶段二) 合并为单一函数 run_pipeline，
供 Flask 系统调用：上传原始「SKU平台信息套件成员货品体现」CSV -> 返回清洗后数据。

输出列顺序（系统面板/下载报表展示顺序）：
  ID、上架SKU、ASIN、库存SKU、成员货品数量、listing后台状态（领星+后台）、店铺、
  唯一SKU、SPU、SPU带尺寸、包裹类型、品类、代运营、发货SKU、是否同步平台库存、美国生命周期状态、非活动、leadtime、
  是否重复维护、异常确认状态、异常确认备注、缺失平台信息异常、
  内部 ID、德国/日本/英国生命周期状态、类型
  注：
    - 品类 = 用「唯一SKU」去「所有库存货品管理员检查表」匹配「品类_3」取值（可选，未上传则为空）。
    - 代运营 已前移至「发货SKU」之前（原在末尾）。
    - 缺失平台信息异常 = 领星/walmart 数据源有、平台信息表无 的 SKU，追加为末行并标「是」。
"""
import csv
import re
import zipfile
from collections import defaultdict, Counter, OrderedDict

# ---------- 自定义排序字典（阶段一 & 最终排序共用） ----------
INACTIVE_ORDER = {'否': 0, '是': 1}          # 非活动：否 -> 是
STORE_ORDER = {
    'US-Amazon-HLLdeco-KH': 0,
    'US-Amazon-Home Nest-KH': 1,
    'US-Amazon-Urban-KH': 2,
    'US-Amazon-JYT-KH': 3,
    'US-Amazon-XYT-KH': 4,
    'US-Walmart': 5,
    # ---- Temu 店铺（2026-10 新增） ----
    'US-Temu-HK': 6,
    'US-Temu-HKXYT': 7,
    'US-Temu-HKJYT': 8,
    'US-Temu-COZY CORNER': 9,
    'EU-Temu-HK': 10,
}

# Temu 报表店铺名 -> 平台表规范化店铺名（A 类：报表有店铺列但 ≠ 平台店名）
TEMU_STORE_MAP = {
    'TEMU-HK 美区-FEIFEI CORNER': 'US-Temu-HK',
    'TEMU-HK 欧区-FEIFEI CORNER': 'EU-Temu-HK',
    'TEMU-XYT-WANG DESIGN STUDIO': 'US-Temu-HKXYT',
    'TEMU-XYT-FULL HOUSE DESIGN STUDIO': 'US-Temu-HKXYT',
    'TEMU-XYT-ELEGANT NOOK FURNITUR': 'US-Temu-HKXYT',
    'TEMU-XYT-Eclecticc Essance': 'US-Temu-HKXYT',
    'TEMU-XYT-CELESTIAL STORAGE NOO': 'US-Temu-HKXYT',
    'TEMU-JYT-Cozy Nest Oasis': 'US-Temu-HKJYT',
}
# B 类（报表无店铺列）固定店铺
TEMU_STORE_COZY = 'US-Temu-COZY CORNER'
# 运营归属：除 COZY CORNER = 吴琇仙，其余全 = 陆飞翔；运营组统一 = 综合组-TEMU
TEMU_OP_COZY = '吴琇仙'
TEMU_OP_OTHER = '陆飞翔'
TEMU_OP_GROUP = '综合组-TEMU'
# Temu A 类 listing 取 SKC状态；B 类取 详细状态
TEMU_LISTING_COL_A = 'SKC状态'
TEMU_LISTING_COL_B = '详细状态'
# Temu ASIN：A 类取「MSKU ID」，B 类取「SKU ID」（2026-10-12 用户口径，A 类由平台SKC 改为 MSKU ID）
TEMU_ASIN_COL_A = 'MSKU ID'
TEMU_ASIN_COL_B = 'SKU ID'
# 多条记录取值优先级（2026-10-12 用户口径）：同店铺+同 SKU 有多条时，按状态从上往下取，越靠前越优先。
# A 类比对「SKC状态」，B 类比对「详细状态」。未收录的状态排最后。
TEMU_STATUS_PRI_A = ['已下首单', '待下首单', '已加入站点', '待平台选品',
                     '平台核价中', '核价未通过', '已下架', '已终止']
TEMU_STATUS_PRI_B = ['在售', '售罄', '已下架', '不可售']


def _temu_status_rank(status, kind):
    """返回状态优先级序号（越小越优先）；未知状态排最后。kind='a'|'b'。"""
    seq = TEMU_STATUS_PRI_A if kind == 'a' else TEMU_STATUS_PRI_B
    s = (status or '').strip()
    try:
        return seq.index(s)
    except ValueError:
        return len(seq)


def _temu_status_rank_any(status):
    """跨 A/B 类比较用（两类状态词不重叠，直接各自查表取序号）。未知状态排最后。"""
    s = (status or '').strip()
    if s in TEMU_STATUS_PRI_A:
        return TEMU_STATUS_PRI_A.index(s)
    if s in TEMU_STATUS_PRI_B:
        return TEMU_STATUS_PRI_B.index(s)
    return 999
# Temu 合并用的统一列结构：原始报表 15 列 + ASIN + 运营/运营组 + TemuListing(转写中间列，最终丢弃)
RAW_HEADER = ['内部 ID', 'ID', '店铺', '14K货号', '是否同步平台库存', '成员货品',
              '成员货品数量', '美国生命周期状态', '德国生命周期状态', '日本生命周期状态',
              '英国生命周期状态', '类型', '代运营', '非活动', 'leadtime']
TEMU_UNIFIED_HEADER = RAW_HEADER + ['ASIN', '运营', '运营组', 'TemuListing', 'TemuSKC']

# ---- 运营 / 运营组：2026-10-12 用户口径，一律按「店铺归属」覆盖 ----
# Walmart：统一 吴琇仙 / 综合组-WLM
WALMART_OP = '吴琇仙'
WALMART_OP_GROUP = '综合组-WLM'
# Amazon 店铺固定运营（JYT HOME STORE-US / XYT HOME-US）
AMZ_OP_FIXED = {
    'US-Amazon-JYT-KH': ('MIA黄', '品牌组'),
    'US-Amazon-XYT-KH': ('MIA黄', '品牌组'),
}
# Amazon 店铺 -> 产品运营表 sheet（按 SPU 查对应关系）
AMZ_OP_SHEET = {
    'US-Amazon-Urban-KH': '综合组-AMZ',      # Urban Living-US
    'US-Amazon-HLLdeco-KH': 'AMZ组',         # HulalahomeDeco-US
    'US-Amazon-Home Nest-KH': 'AMZ组',       # HOME NEST-US
}
AMZ_OP_NOT_FOUND = '未查询到'
# Temu 行：链式从原始报表同款行照搬的平台字段（唯一SKU -> 上架SKU -> SPU，取首条）
TEMU_INHERIT_FIELDS = ['ID', '内部 ID', '代运营', '类型', '是否同步平台库存',
                       '美国生命周期状态', 'leadtime', '非活动',
                       '异常确认状态', '异常确认备注', '品类']


def normalize_store(s):
    """Temu 店铺名规范化：仅对含 TEMU 的店铺生效 —— 折叠连字符两侧空格、TEMU→Temu 大小写归一。
    例：US-TEMU-HKJYT→US-Temu-HKJYT；US-Temu - COZY CORNER→US-Temu-COZY CORNER；US-Temu- HK→US-Temu-HK。
    """
    s = (s or '').strip()
    if 'TEMU' in s.upper():
        s = re.sub(r'\s*-\s*', '-', s)
        s = re.sub(r'TEMU', 'Temu', s, flags=re.IGNORECASE)
    return s
SYNC_ORDER = {'是': 0, '否': 1, '': 2}        # 是否同步：是 -> 否 -> 空
PKG_ORDER = {'单件': 0, '套件': 1, 'AB件': 2, '套组': 3, '异常': 4}
# listing后台状态（领星+后台）排序：在售 -> PUBLISHED -> 停售 -> UNPUBLISHED -> 已删除 -> SYSTEM_PROBLEM -> IN_PROGRESS
LISTING_ORDER = {
    '在售': 0,
    'PUBLISHED': 1,
    '停售': 2,
    'UNPUBLISHED': 3,
    '已删除': 4,
    'SYSTEM_PROBLEM': 5,
    'IN_PROGRESS': 6,
}

REQUIRED_COLS = ['店铺', '14K货号', '成员货品', '成员货品数量', '非活动',
                 '是否同步平台库存', 'leadtime']

# 领星清单店铺名 -> 平台表店铺名（用于 ASIN/listing 状态匹配）
STORE_MAP = {
    'HulalahomeDeco-US': 'US-Amazon-HLLdeco-KH',
    'HOME NEST-US': 'US-Amazon-Home Nest-KH',
    'Urban Living-US': 'US-Amazon-Urban-KH',
    'JYT HOME STORE-US': 'US-Amazon-JYT-KH',
    'XYT HOME-US': 'US-Amazon-XYT-KH',
}
# 平台表 Amazon 店铺走领星清单匹配；其余店铺（US-Walmart）走 walmart 后台报表
AMAZON_STORES = set(STORE_MAP.values())

# 「是否重复维护」三态值
DUP_NO = '否'        # 原「正常」
DUP_YES = '是'       # 原「重复维护，需清洗」
DUP_INACTIVE = '非活动'   # 非活动=是 的行
TAG_ORDER = {DUP_NO: 0, DUP_YES: 1, DUP_INACTIVE: 2}


def parse_leadtime(s):
    s = s.strip()
    if s == '':
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _is_blank_record(row, idx):
    """14K货号为空 且 非活动=否 -> 空白记录（True）。
    这类记录不参与 是否重复维护/包裹类型/发货SKU/异常确认 判定，相关字段直接留空。
    """
    return row[idx['14K货号']].strip() == '' and row[idx['非活动']].strip() == '否'


# ========================== 阶段一：三步清洗 + 打标 ==========================
def _stage1(header, rows):
    # header / rows 已由 run_pipeline 读取并合并 Temu（可能含 运营/运营组/TemuListing 等额外列）
    rows = [row for row in rows if any(str(c).strip() for c in row)]

    idx = {name: i for i, name in enumerate(header)}
    missing = [c for c in REQUIRED_COLS if c not in idx]
    if missing:
        raise ValueError("原始表格缺少必要列：" + "、".join(missing))

    def sortkey(row):
        lt = parse_leadtime(row[idx['leadtime']])
        lt_key = (1, 0) if lt is None else (0, -lt)
        return (
            INACTIVE_ORDER.get(row[idx['非活动']].strip(), 99),
            STORE_ORDER.get(row[idx['店铺']].strip(), 99),
            row[idx['14K货号']].strip(),
            row[idx['成员货品']].strip(),
            SYNC_ORDER.get(row[idx['是否同步平台库存']].strip(), 2),
            lt_key,
        )

    rows_sorted = sorted(rows, key=sortkey)

    def group_key(row):
        return (
            row[idx['店铺']].strip(),
            row[idx['14K货号']].strip(),
            row[idx['成员货品']].strip(),
            row[idx['成员货品数量']].strip(),
            row[idx['非活动']].strip(),
        )

    group_seen = OrderedDict()
    for row in rows_sorted:
        group_seen.setdefault(group_key(row), []).append(row)

    for row in rows_sorted:
        grp = group_seen[group_key(row)]
        row.append(DUP_NO if grp[0] is row else DUP_YES)

    # 规则：非活动=是 的行，统一改为「非活动」（覆盖 否/是）
    for row in rows_sorted:
        if row[idx['非活动']].strip() == '是':
            row[-1] = DUP_INACTIVE

    # 规则（2026-08-27 用户拍板）：14K货号为空 且 非活动=否 的行，是否重复维护直接留空，
    # 后续不参与 包裹类型/发货SKU/异常确认状态/异常确认备注 的判定（这些字段由 _stage2 一并置空）。
    for row in rows_sorted:
        if _is_blank_record(row, idx):
            row[-1] = ''

    def sortkey2(row):
        lt = parse_leadtime(row[idx['leadtime']])
        lt_key = (1, 0) if lt is None else (0, -lt)
        return (
            TAG_ORDER.get(row[-1], 1),
            INACTIVE_ORDER.get(row[idx['非活动']].strip(), 99),
            STORE_ORDER.get(row[idx['店铺']].strip(), 99),
            row[idx['14K货号']].strip(),
            row[idx['成员货品']].strip(),
            SYNC_ORDER.get(row[idx['是否同步平台库存']].strip(), 2),
            lt_key,
        )

    rows_final = sorted(rows_sorted, key=sortkey2)
    header_final = header + ['是否重复维护']
    return header_final, rows_final


# ========================== 阶段二：增字段 + 转单扩展 ==========================
# Temu SPU 提取用的地区白名单（首段若是这些地区码则剥离）
REGION_PREFIX = {'EU', 'DE', 'ES', 'UK', 'FR', 'IT', 'US', 'CA', 'AU'}


def _spu_core(first_seg):
    """到第一个连字符段内最后一个「数字段+其后粘连字母」为止。"""
    mm = re.search(r'\d+[A-Za-z]*$', first_seg)
    if not mm:
        return first_seg
    return first_seg[:mm.end()]


def _spu(s):
    s = s.strip()
    prefix = ''
    if s.startswith('US-SA-SW-'):
        prefix = 'US-SA-SW-'
        s = s[len(prefix):]
    first_seg = s.split('-')[0]                 # 第一个连字符分隔段
    return prefix + _spu_core(first_seg)


def _spu_temu(s):
    """Temu 成员货品 SPU 提取（3 条规则，已样本验证）：
    ① 首段∈地区白名单{EU/DE/ES/UK/FR/IT/US/CA/AU} → 剥离（EU-HCHS0105-TAN→HCHS0105）
    ② 首段纯字母非地区 → 与第二段拼接（DU-00494-CO-KG-BLUE→DU-00494）
    ③ 提取无数字(纯颜色脏数据) → 返回空，由调用方回退用 SKC货号 提取（Camel→回退OFMYN0433）
    """
    s = s.strip()
    if not s:
        return ''
    segs = s.split('-')
    seg0 = segs[0]
    if seg0 in REGION_PREFIX:
        rest = segs[1:]
        if not rest:
            return ''
        return _spu_core(rest[0])
    if re.fullmatch(r'[A-Za-z]+', seg0):        # 纯字母非地区
        if len(segs) > 1:
            return seg0 + '-' + _spu_core(segs[1])
        return seg0
    return _spu_core(seg0)                       # 常规：数字段+粘连字母


def _spu_for_store(store, uniq_sku, skc14k):
    """按店铺选择 SPU 提取口径；Temu 店铺走 _spu_temu，且规则③无数字时回退 SKC货号。"""
    if store.startswith(('US-Temu', 'EU-Temu')):
        spu = _spu_temu(uniq_sku)
        if not re.search(r'\d', spu):            # 规则③：纯颜色脏数据，回退 SKC货号
            spu = _spu_temu(skc14k)
        return spu
    return _spu(uniq_sku)

# 尺寸词（独立连字符段）：床品/家具尺寸。数据实测每 SKU 最多 1 个，仅出现于第 2 段或末段。
SIZE_WORDS = {'KB', 'TB', 'QB', 'K', 'Q', 'T', 'KING', 'QUEEN', 'TWIN', 'FULL', 'TXL'}


def _spu_size(s, spu):
    """SPU带尺寸：若 SKU 任意独立段为尺寸词 => SPU-尺寸词；否则 = SPU。"""
    for seg in s.split('-'):
        if seg in SIZE_WORDS:
            return spu + '-' + seg
    return spu


def load_transfer_sheets(path):
    """读转单表【全部 sheet】。每行 = 同品的多个 SKU 名称（首行视为表头跳过）。

    返回 (per_sheet, all_rows)：
      per_sheet = [{'name': sheet名, 'rows': 有效行数}, ...]
      all_rows  = [[sku, sku, ...], ...]  合并全部 sheet
    兼容点：不再写死「增长 / 自营」两个 sheet 名，新增/改名 sheet 自动生效。
    """
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    per_sheet, all_rows = [], []
    for sn in wb.sheetnames:
        ws = wb[sn]
        data = list(ws.iter_rows(values_only=True))
        out = []
        for row in data[1:]:                      # 跳过表头
            skus = [str(c).strip() for c in row if c not in (None, '')]
            skus = [s for s in skus if s]
            if skus:
                out.append(skus)
        per_sheet.append({'name': sn, 'rows': len(out)})
        all_rows.extend(out)
    wb.close()
    return per_sheet, all_rows


def _union_find(all_rows):
    """把每行内的 SKU 并到同一组，返回 (groups, mem2rep)。"""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for skus in all_rows:
        for i in range(1, len(skus)):
            union(skus[0], skus[i])
    groups = defaultdict(list)
    for s in parent:
        groups[find(s)].append(s)
    mem2rep = {s: find(s) for s in parent}
    return groups, mem2rep


def summarize_transfer(path):
    """解析转单表并返回摘要（供上传后核对）。"""
    per_sheet, all_rows = load_transfer_sheets(path)
    groups, mem2rep = _union_find(all_rows)
    multi = sum(1 for g in groups.values() if len(g) > 1)
    if not all_rows:
        raise ValueError('转单表未解析到任何数据行（每行应为同品的多个 SKU 名称，首行为表头）')
    return {
        'sheets': per_sheet,
        'total_rows': len(all_rows),
        'sku_count': len(mem2rep),
        'group_count': len(groups),
        'multi_group_count': multi,
    }


COLOR_TRAIL = {'BGE', 'GRY', 'BRN', 'BLK', 'CRY', 'BLU', 'FLR', 'GRN', 'IVY', 'NAV',
               'ORG', 'RED', 'SAP', 'WTE', 'YEL', 'DRY', 'LGY'}  # 颜色尾随码，不判 AB件
PKG_SINGLE = re.compile(r'^[A-OQ-UW-Z]$')       # 单字母包裹位（不含 P、V）
PAT_END = re.compile(r'-(?:[A-OQ-UW-Z]|F[A-Z])$')


def _is_ab_member(m):
    if PAT_END.search(m):                        # 规则1：末段包裹位
        return True
    sg = m.split('-')                            # 规则2：包裹位 + 后续非颜色码
    for j, seg in enumerate(sg):
        if PKG_SINGLE.match(seg) and j < len(sg) - 1:
            nxt = sg[j + 1]
            if nxt not in COLOR_TRAIL and re.match(r'^[A-Z0-9]{1,}$', nxt):
                return True
    return False


def load_lingxing(path):
    """领星清单 xlsx（MSKU / 状态 / 店铺 / ASIN）。

    返回 (match_map, summary)：
      match_map[(平台店铺, MSKU)] = (ASIN, listing状态)
      店铺经 STORE_MAP 映射回平台表店名；未收录店铺跳过。
      同 (店铺, MSKU) 多行取第一条。
    """
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        raise ValueError('领星清单为空')
    hdr = [str(x).strip() if x is not None else '' for x in rows[0]]
    need = ['MSKU', '状态', '店铺', 'ASIN']
    for c in need:
        if c not in hdr:
            raise ValueError('领星清单缺少列：「%s」' % c)
    i = {c: hdr.index(c) for c in need}
    m, n_row = {}, 0
    for r in rows[1:]:
        if not any(str(x).strip() for x in r):
            continue
        n_row += 1
        plat = STORE_MAP.get(str(r[i['店铺']]).strip())
        msku = str(r[i['MSKU']]).strip()
        if not plat or not msku:
            continue
        key = (plat, msku)
        if key not in m:   # 同 key 多行取第一条
            m[key] = (str(r[i['ASIN']]).strip(), str(r[i['状态']]).strip())
    return m, {'rows': n_row, 'matched': len(m)}


def load_walmart(path):
    """walmart 后台报表 csv（SKU / Item ID / Publish Status …）。

    返回 (match_map, summary)：
      match_map[SKU] = (ItemID文本, Publish Status)
    Item ID 强制转文本（数字易被 Excel 科学计数）；SKU 唯一，重复取第一条。
    """
    with open(path, encoding='utf-8-sig', newline='') as f:
        r = csv.reader(f)
        hdr = [x.strip() for x in next(r)]
        rows = [row for row in r if any(c.strip() for c in row)]
    need = ['SKU', 'Item ID', 'Publish Status']
    for c in need:
        if c not in hdr:
            raise ValueError('walmart 报表缺少列：「%s」' % c)
    i = {c: hdr.index(c) for c in need}
    m = {}
    for row in rows:
        sku = row[i['SKU']].strip()
        if not sku:
            continue
        if sku not in m:
            m[sku] = (str(row[i['Item ID']]).strip(),
                      str(row[i['Publish Status']]).strip())
    return m, {'rows': len(rows), 'matched': len(m)}


def load_admin_check(path):
    """所有库存货品管理员检查表：匹配键 -> 品类_3。

    支持 .xlsx / .csv。要求列：
      匹配键列：唯一SKU（亦接受 名称 / 库存SKU / 成员货品 / SKU）
      取值列：  品类_3（亦接受 品类3 / 品类_3）
    返回 (match_map, summary)：match_map = {匹配键(去空格): 品类_3}，summary = {rows, matched}。
    同匹配键 多行取第一条。
    """
    if path.lower().endswith('.csv'):
        with open(path, encoding='utf-8-sig', newline='') as f:
            rows = [row for row in csv.reader(f)]
    else:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
    if not rows:
        raise ValueError('管理员检查表为空')
    hdr = [str(x).strip() if x is not None else '' for x in rows[0]]

    key_candidates = ['唯一SKU', '唯一sku', '名称', '库存SKU', '成员货品', 'SKU']
    key_idx = next((hdr.index(c) for c in key_candidates if c in hdr), None)
    if key_idx is None:
        raise ValueError('管理员检查表缺少匹配列「唯一SKU」（亦支持 名称 / 库存SKU / 成员货品 / SKU）')

    target_candidates = ['品类_3', '品类3', '品类_3']
    tgt_idx = next((hdr.index(c) for c in target_candidates if c in hdr), None)
    if tgt_idx is None:
        raise ValueError('管理员检查表缺少「品类_3」列（亦支持「品类3」）')

    m, n = {}, 0
    for r in rows[1:]:
        if not any(str(x).strip() for x in r):
            continue
        k = str(r[key_idx]).strip() if key_idx < len(r) else ''
        if not k:
            continue
        v = str(r[tgt_idx]).strip() if tgt_idx < len(r) else ''
        if k not in m:
            m[k] = v
        n += 1
    return m, {'rows': n, 'matched': len(m)}


def load_temu(path):
    """Temu 产品报表（A/B 两类）-> 统一列结构行（TEMU_UNIFIED_HEADER）。

    按【每个 sheet 表头】单独判定 A/B 类，故单个 xlsx 内含多 sheet（分别 A、B）也能正确解析。
    A 类：报表有「店铺」列（店名≠平台店名），按 TEMU_STORE_MAP 归一化；
         成员货品=MSKU，14K货号=MSKU（2026-10-12 订正，原误用 SKC货号），ASIN=MSKU ID，
         listing后台状态=SKC状态，运营/运营组按店归属；SKC货号仅用于 SPU 规则③回退。
    B 类：报表无「店铺」列，固定店铺=US-Temu-COZY CORNER（运营=吴琇仙）；
         成员货品=SKU，14K货号=SKU，listing后台状态=详细状态。
    非活动列留空（由 run_pipeline 按成员货品回查原始报表填充，查不到留空）。
    返回 (header, rows)；rows 每行为 TEMU_UNIFIED_HEADER 对齐的列表，缺失列补空串。
    """
    import openpyxl

    def _norm_store(raw):
        raw = (raw or '').strip()
        if raw in TEMU_STORE_MAP:
            return TEMU_STORE_MAP[raw]
        # 前缀兜底（应对报表店名截断/细微差异）
        if raw.startswith('TEMU-HK 美区'):
            return 'US-Temu-HK'
        if raw.startswith('TEMU-HK 欧区'):
            return 'EU-Temu-HK'
        if raw.startswith('TEMU-XYT'):
            return 'US-Temu-HKXYT'
        if raw.startswith('TEMU-JYT'):
            return 'US-Temu-HKJYT'
        return raw  # 未知店名原样保留（排序落最后）

    def _cell(row, idx):
        if idx is None or idx >= len(row):
            return ''
        v = row[idx]
        return '' if v is None else str(v).strip()

    def _emit(store, mem, skc, listing, asin='', skc_raw=''):
        op = TEMU_OP_COZY if store == TEMU_STORE_COZY else TEMU_OP_OTHER
        row = {c: '' for c in TEMU_UNIFIED_HEADER}
        row['店铺'] = store
        row['14K货号'] = skc
        row['成员货品'] = mem
        row['成员货品数量'] = '1'
        row['ASIN'] = asin
        row['运营'] = op
        row['运营组'] = TEMU_OP_GROUP
        row['TemuListing'] = listing
        row['TemuSKC'] = skc_raw      # 原始「SKC货号」：仅供 SPU 规则③（纯颜色脏数据）回退
        # 其余列（内部ID/ID/生命周期/类型/代运营/非活动/leadtime）留空；
        # 由 run_pipeline 末尾按「唯一SKU→上架SKU→SPU」链式从原始报表同款行照搬
        return [row[c] for c in TEMU_UNIFIED_HEADER]

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    best = {}      # (店铺, 成员货品) -> (状态优先级序号, 行)；跨 sheet 累积，同键取状态最优
    for sn in wb.sheetnames:
        rows_all = list(wb[sn].iter_rows(values_only=True))
        if not rows_all:
            continue
        hdr = [str(x).strip() if x is not None else '' for x in rows_all[0]]
        data = rows_all[1:]
        is_a = '店铺' in hdr
        if is_a:
            if 'MSKU' not in hdr:
                # A 类必须有成员货品列（MSKU），否则整 sheet 无法参与合并，直接跳过
                # （防无关 xlsx 被误判收下，产出空成员垃圾行）
                continue
            i_store = hdr.index('店铺')
            i_msku = hdr.index('MSKU')
            i_skc = hdr.index('SKC货号') if 'SKC货号' in hdr else None
            i_listing = hdr.index(TEMU_LISTING_COL_A) if TEMU_LISTING_COL_A in hdr else None
            i_asin = hdr.index(TEMU_ASIN_COL_A) if TEMU_ASIN_COL_A in hdr else None
            for r in data:
                if not any(str(x).strip() for x in r):
                    continue
                store = _norm_store(_cell(r, i_store))
                mem = _cell(r, i_msku)          # 成员货品 = MSKU
                if not mem:
                    continue                    # 无 MSKU 的报表行是空脏行，丢弃
                skc = _cell(r, i_skc)           # SKC货号：仅供 SPU 规则③（纯颜色脏数据）回退
                listing = _cell(r, i_listing)   # SKC状态
                asin = _cell(r, i_asin)         # ASIN = MSKU ID
                # 14K货号(上架SKU) = MSKU（2026-10-12 用户口径订正）
                # 原用 SKC货号，但其命中原始报表「14K货号」仅 35.1%，改用 MSKU 为 81.8%
                # 同店铺+同 MSKU 多条时，按「SKC状态」优先级取最优（不再按出现顺序取首条）
                _k = (store, mem)
                _rk = _temu_status_rank(listing, 'a')
                if _k not in best or _rk < best[_k][0]:
                    best[_k] = (_rk, _emit(store, mem, mem, listing, asin, skc))
        else:
            if 'SKU' not in hdr:
                # B 类必须有 SKU 列（成员货品=SKU），否则整 sheet 无法参与合并，直接跳过
                continue
            i_sku = hdr.index('SKU')
            i_listing = hdr.index(TEMU_LISTING_COL_B) if TEMU_LISTING_COL_B in hdr else None
            i_asin = hdr.index(TEMU_ASIN_COL_B) if TEMU_ASIN_COL_B in hdr else None
            for r in data:
                if not any(str(x).strip() for x in r):
                    continue
                mem = _cell(r, i_sku)           # 成员货品 = SKU
                if not mem:
                    continue                    # 无 SKU 的报表行是空脏行，丢弃
                listing = _cell(r, i_listing)   # 详细状态
                # B 类固定店铺；ASIN = SKU ID（14K货号 沿用 SKU）
                # 同店铺+同 SKU 多条时，按「详细状态」优先级取最优（在售 > 售罄 > 已下架 > 不可售）
                _k = (TEMU_STORE_COZY, mem)
                _rk = _temu_status_rank(listing, 'b')
                if _k not in best or _rk < best[_k][0]:
                    best[_k] = (_rk, _emit(TEMU_STORE_COZY, mem, mem, listing,
                                           _cell(r, i_asin)))
    wb.close()
    out.extend(v[1] for v in best.values())
    if not out:
        raise ValueError('Temu 报表为空（未解析到任何数据行）')
    return TEMU_UNIFIED_HEADER, out


def temu_file_types(path):
    """判定 Temu 报表文件包含的类型 -> set（'A' / 'B'，可能同时含两者）。

    与 load_temu 的逐 sheet 判定口径一致：
      A = 该 sheet 表头含「店铺」列；B = 不含「店铺」列的 sheet（固定 COZY）。
    仅读每个 sheet 首行表头，轻量不解析数据。空 sheet 跳过。"""
    import openpyxl
    types = set()
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for sn in wb.sheetnames:
            for r in wb[sn].iter_rows(min_row=1, max_row=1, values_only=True):
                hdr = [str(x).strip() if x is not None else '' for x in r]
                if any(hdr):
                    types.add('A' if '店铺' in hdr else 'B')
                break
    finally:
        wb.close()
    return types


def _pkg_type(g, idx):
    MEM = idx['成员货品']
    QTY = idx['成员货品数量']
    SKC = idx['14K货号']
    SKCRAW = idx.get('TemuSKC')          # Temu 原始 SKC货号（规则③回退；无该列时为 None）
    STORE = idx['店铺']
    store0 = g[0][STORE].strip() if g else ''
    is_temu = store0.startswith(('US-Temu', 'EU-Temu'))
    # 参与判定的行 = 【全量行】（含「是」=重复维护、含「非活动」）；相同成员货品去重，只算1条。
    # 2026-08-26 用户拍板：组内重复行也正常判定包裹，3个完全重复记录 => 去重后1条 => 单件。
    seen = set()
    judged = []
    for r in g:
        mem = r[MEM].strip()
        if mem not in seen:
            seen.add(mem)
            judged.append(r)
    if not judged:
        return ''
    # 空SKU行：成员货品为空（数据源本身空白行）不参与包裹判定，留空。
    # 2026-08-26 用户拍板：上架SKU/成员货品为空的记录不应误判为单件。
    if len(judged) == 1 and judged[0][MEM].strip() == '':
        return ''
    if is_temu:
        spus = {_spu_for_store(store0, r[MEM],
                               (r[SKCRAW].strip() if SKCRAW is not None else '') or r[SKC])
                for r in judged}
    else:
        spus = {_spu(r[MEM]) for r in judged}
    if len(spus) > 1:
        return '套组'
    if any(_is_ab_member(r[MEM]) for r in judged):
        return 'AB件'
    if len(judged) == 1:
        try:
            qf = float(judged[0][QTY])
        except ValueError:
            qf = 0
        if qf == 1:
            return '单件'
        if qf > 1:
            return '套件'
    else:
        # 同SPU + 多条记录（去重后） + 非AB件：Amazon 判「异常」待人工核查；
        # Temu 多色变体（同 SPU 多成员）视为「套件」。
        return '套件' if is_temu else '异常'
    return ''


def _stage2(header, rows, xlsx_path, lingxing_path=None, walmart_path=None,
            admin_check_path=None, progress=None):
    """阶段二：增字段 + 转单扩展。

    progress: 可选回调 progress(pct_0_100, stage)，由 run_pipeline 映射到总体进度。
    """

    def _p(pct, stage):
        if progress:
            try:
                progress(pct, stage)
            except Exception:
                pass

    _p(0, '解析转单表…')
    idx = {name: i for i, name in enumerate(header)}
    COL_MEM, COL_STORE, COL_14K, COL_QTY = (
        '成员货品', '店铺', '14K货号', '成员货品数量')
    if 'ID' not in idx:
        raise ValueError('原始表格缺少「ID」列，无法按 ID+店铺+14K货号 分组判定包裹类型')

    # ---- 转单 union-find（读转单表全部 sheet） ----
    _p(2, '解析转单表…')
    per_sheet, all_rows = load_transfer_sheets(xlsx_path)
    groups, mem2rep = _union_find(all_rows)

    # ---- 可选数据源：领星清单 / walmart 后台报表（获取 ASIN + listing后台状态） ----
    _p(12, '加载领星 / walmart 数据源…')
    lingxing_map, lx_summary = None, None
    walmart_map, wm_summary = None, None
    if lingxing_path:
        lingxing_map, lx_summary = load_lingxing(lingxing_path)
    if walmart_path:
        walmart_map, wm_summary = load_walmart(walmart_path)

    # ---- 可选数据源：所有库存货品管理员检查表（唯一SKU -> 品类_3） ----
    _p(18, '加载管理员检查表（品类）…')
    admin_map, admin_summary = None, None
    if admin_check_path:
        admin_map, admin_summary = load_admin_check(admin_check_path)

    def lookup_asin(row):
        """按 店铺+14K货号 匹配 ASIN 与 listing后台状态。未命中返回 ('', '')。"""
        store = row[idx[COL_STORE]].strip()
        sku = row[idx[COL_14K]].strip()
        if not sku:
            return '', ''
        if store in AMAZON_STORES:
            hit = lingxing_map.get((store, sku)) if lingxing_map else None
        elif store == 'US-Walmart':
            hit = walmart_map.get(sku) if walmart_map else None
        else:
            hit = None
        if hit:
            return hit
        # Temu / 其他店铺：listing 取预填的 TemuListing（A=SKC状态 / B=详细状态）
        temu_listing = row[idx['TemuListing']] if 'TemuListing' in idx else ''
        return '', temu_listing

    # ---- 包裹类型（仅基于【原始清洗数据】，扩展前计算） ----
    # 分组键：ID + 店铺 + 14K货号（同一 ID 的多个成员货品视为同一 listing 拆出的行）
    def pkg_key(row):
        return (row[idx['ID']].strip(), row[idx[COL_STORE]].strip(), row[idx[COL_14K]].strip())

    _p(22, '判定包裹类型…')
    by_key = defaultdict(list)
    for row in rows:
        by_key[pkg_key(row)].append(row)
    pkg_cache = {k: _pkg_type(g, idx) for k, g in by_key.items()}

    # ---- 阶段二前：先算每行的 ASIN/listing/pkg（不修改 row）----
    # 供「转单组最优模板行」按 (-有ASIN, -在售, -单件) 优先级排序。
    # 必须在赋值循环之前算好 best_rep，否则同转单组内的「原始行」+「扩展行」
    # 会出现唯一SKU 不一致（原始行用自己的成员货品，扩展行用模板的成员货品）。
    row_meta = {}                       # 行号 i -> (asin, listing, pkg)
    for i, row in enumerate(rows):
        asin, listing = lookup_asin(row)
        pkg = pkg_cache.get(pkg_key(row), '')
        row_meta[i] = (asin, listing, pkg)

    def _tmpl_prio_meta(asin, listing, pkg):
        return (
            -1 if (asin or '').strip() else 0,                # ①有ASIN 绝对优先
            -1 if (listing or '') in ('在售', 'PUBLISHED') else 0,  # ②在售(在售/PUBLISHED) 优先
            -1 if pkg == '单件' else 0,                        # ③单件 优先
        )

    # ① 为每个「店铺 × 转单组」挑一条「本店最优模板行」（仅用于扩展行的 ASIN/listing/包裹类型 等属性，按店区分）
    #    优先级：有 ASIN -> 在售(在售/PUBLISHED) -> 单件
    #    best_rep[(store, rep)] = (优先级元组, 行号 i)；同一店铺同组内只保留一条最优，供该店铺单独复制组内其他 SKU
    #    —— 同款 SKU 在 A、B 两店都存在时，两店各自独立扩展（不再只扩展其中一个店）。
    best_rep = {}
    for i, row in enumerate(rows):
        X = row[idx[COL_MEM]]
        if X in mem2rep:
            store = row[idx[COL_STORE]].strip()
            rep = mem2rep[X]
            asin, listing, pkg = row_meta[i]
            prio = _tmpl_prio_meta(asin, listing, pkg)
            cur = best_rep.get((store, rep))
            if cur is None or prio < cur[0]:
                best_rep[(store, rep)] = (prio, i)

    # ①-global：为每个「转单组」跨所有店铺挑一条「全局最优模板行」（仅用于决定 唯一SKU，保证跨店严格统一）
    #    —— 同一物理品（同一转单组）的全部成员，不论在哪个店铺，唯一SKU 都等于该全局最优模板的成员货品。
    best_rep_global = {}
    for i, row in enumerate(rows):
        X = row[idx[COL_MEM]]
        if X in mem2rep:
            rep = mem2rep[X]
            asin, listing, pkg = row_meta[i]
            prio = _tmpl_prio_meta(asin, listing, pkg)
            cur = best_rep_global.get(rep)
            if cur is None or prio < cur[0]:
                best_rep_global[rep] = (prio, i)

    # 成员货品 -> 全局最佳模板的成员货品（跨店统一唯一SKU）
    mem2best_member = {}
    for rep, (_, i) in best_rep_global.items():
        tmpl_mem = rows[i][idx[COL_MEM]]
        for sku in groups[rep]:
            mem2best_member[sku] = tmpl_mem

    # ---- 赋值 唯一SKU + SPU + SPU带尺寸 + 包裹类型 + 品类 + 发货SKU + ASIN + listing后台状态 ----
    # 唯一SKU：转单组内 = 组最佳模板的成员货品；非转单组 = 自己的成员货品
    _p(35, '生成唯一SKU / SPU…')
    n_asin = 0
    for i, row in enumerate(rows):
        mem = row[idx[COL_MEM]]
        store = row[idx[COL_STORE]].strip()
        uniq_sku = mem2best_member.get(mem, mem)    # 跨店严格统一：同一转单组所有成员=同一唯一SKU
        # SPU 规则③回退键：Temu 行优先用原始「SKC货号」(TemuSKC 中间列)，其余用 14K货号
        _skc14k = (row[idx['TemuSKC']].strip() if 'TemuSKC' in idx else '') \
            or row[idx['14K货号']].strip()
        spu = _spu_for_store(store, uniq_sku, _skc14k)
        blank = _is_blank_record(row, idx)                    # 空白记录：14K货号空 + 非活动=否
        row.append(uniq_sku)                                 # 唯一SKU
        row.append(spu)                                      # SPU
        row.append(_spu_size(uniq_sku, spu))                 # SPU带尺寸
        row.append('' if blank else pkg_cache[pkg_key(row)]) # 包裹类型（空白记录留空，跳过判定）
        row.append(admin_map.get(uniq_sku, '') if admin_map else '')  # 品类（按唯一SKU 匹配管理员检查表；未上传则为空）
        row.append('' if blank else '是')                    # 发货SKU（空白记录留空，跳过判定）
        asin, listing, _pkg = row_meta[i]                    # 复用提前算好的 ASIN/listing
        row.append(asin)                                     # ASIN
        row.append(listing)                                  # listing后台状态（领星+后台）
        if asin:
            n_asin += 1

    # ---- 扩展记录（转单）----
    # row 追加顺序：唯一SKU(idx+0)、SPU(idx+1)、SPU带尺寸(idx+2)、包裹类型(idx+3)、品类(idx+4)、发货SKU(idx+5)、ASIN(idx+6)、listing(idx+7)
    IDX_FAHUO = len(idx) + 5          # 发货SKU 列索引（扩展记录需改成「否」）
    IDX_ASIN = len(idx) + 6           # ASIN 列索引（追加列偏移）
    IDX_LISTING = len(idx) + 7        # listing后台状态 列索引（追加列偏移）
    result = list(rows)               # 先保留全部原始行（发货SKU=是）
    n_added = 0

    _p(55, '转单扩展（同品多 SKU 补齐）…')

    # 结果行追加列索引偏移（唯一SKU=+0, SPU=+1, SPU带尺寸=+2, 包裹类型=+3, 品类=+4, 发货SKU=+5, ASIN=+6, listing=+7）
    IDX_UNIQ = len(idx) + 0
    IDX_SPU = len(idx) + 1
    IDX_SPU_SIZE = len(idx) + 2
    IDX_CATEGORY = len(idx) + 4

    # ② 对每个「店铺×组」有最优模板的，用该店铺模板复制「组内除自身外」的所有 SKU
    #    —— 同款 SKU 在 A、B 两店都有时，两店各自独立生成变体行（发货SKU=否），互不影响。
    #    扩展行的 唯一SKU/SPU/SPU带尺寸/品类 取「全局唯一SKU」（跨店严格统一）；
    #    ASIN/listing后台状态/包裹类型 沿用本店模板（按店区分，正确反映各店 listing）。
    for (store, rep), (_prio, i) in best_rep.items():
        tmpl = rows[i]
        tmpl_X = tmpl[idx[COL_MEM]]
        g_tmpl = mem2best_member.get(tmpl_X, tmpl_X)   # 本组全局唯一SKU（用于扩展行覆盖）
        for sku in groups[rep]:
            if sku == tmpl_X:
                continue
            copy = list(tmpl)
            copy[idx[COL_MEM]] = sku
            copy[IDX_FAHUO] = '否'   # 发货SKU：转单扩展记录=否（勿用 copy[-1]，会误改 listing）
            # 唯一SKU/SPU/SPU带尺寸/品类 —— 跨店统一（覆盖本店模板值）
            g_sku = mem2best_member.get(sku, sku)
            copy[IDX_UNIQ] = g_sku
            _cskc = (copy[idx['TemuSKC']].strip() if 'TemuSKC' in idx else '') \
                or copy[idx['14K货号']].strip()
            copy[IDX_SPU] = _spu_for_store(store, g_sku, _cskc)
            copy[IDX_SPU_SIZE] = _spu_size(g_sku, _spu_for_store(store, g_sku, _cskc))
            copy[IDX_CATEGORY] = admin_map.get(g_sku, '') if admin_map else ''
            # 包裹类型/ASIN/listing后台状态 随 copy 继承（= 该店铺最优模板）
            result.append(copy)
            n_added += 1

    # 结果 = 所有原始行 + 扩展行（原始行在前，扩展行在后）

    # ---- 缺失平台信息异常：数据源(领星/walmart)有、平台信息表无 的 SKU ----
    # 平台信息表 SKU 集合 = 原始清洗数据的 14K货号（上架SKU）；数据源 SKU 集合从 match_map 键重建
    plat_skus = {str(r[idx[COL_14K]]).strip() for r in rows}
    lx_skus = {msku for (_, msku) in lingxing_map} if lingxing_map else set()   # 键=(平台店铺, MSKU)
    wm_skus = set(walmart_map) if walmart_map else set()
    lx_miss = lx_skus - plat_skus
    wm_miss = wm_skus - plat_skus
    union_miss = (lx_skus | wm_skus) - plat_skus

    # ---- 完整列索引（含阶段二新增列） ----
    COL_ASIN = 'ASIN'
    COL_LISTING = 'listing后台状态（领星+后台）'
    fidx = {name: i for i, name in enumerate(
        header + ['唯一SKU', 'SPU', 'SPU带尺寸', '包裹类型', '品类', '发货SKU', COL_ASIN, COL_LISTING,
                  '异常确认状态', '异常确认备注'])}

    # ---- 新增：异常确认状态 / 异常确认备注（仅异常行默认待确认；空白记录全程留空） ----
    for row in result:
        # 空白记录（14K货号空 + 非活动=否）：跳过异常确认判定，两列留空。
        if _is_blank_record(row, fidx):
            row.append('')           # 异常确认状态（空白记录留空）
            row.append('')           # 异常确认备注
        elif row[fidx['包裹类型']] == '异常':
            row.append('待确认')      # 异常确认状态
            row.append('')           # 异常确认备注
        else:
            row.append('')           # 异常确认状态（非异常留空）
            row.append('')           # 异常确认备注

    # ---- 最终排序（原规则在前；发货SKU、包裹类型 置后） ----

    def final_key(row):
        fahuo = row[fidx['发货SKU']]
        fahuo_k = 0 if fahuo == '是' else 1
        pkg = row[fidx['包裹类型']]
        pkg_k = PKG_ORDER.get(pkg, 99)
        tag = row[fidx['是否重复维护']]
        tag_k = TAG_ORDER.get(tag, 1)
        listing = row[fidx[COL_LISTING]]
        listing_k = LISTING_ORDER.get(listing, 99)
        lt = parse_leadtime(row[fidx['leadtime']])
        lt_k = (1, 0) if lt is None else (0, -lt)
        return (
            tag_k,
            INACTIVE_ORDER.get(row[fidx['非活动']].strip(), 99),
            STORE_ORDER.get(row[fidx['店铺']].strip(), 99),
            row[fidx['14K货号']].strip(),
            row[fidx['成员货品']].strip(),
            SYNC_ORDER.get(row[fidx['是否同步平台库存']].strip(), 2),
            lt_k,
            fahuo_k,
            pkg_k,
            listing_k,
        )

    result.sort(key=final_key)

    # ---- 列重排：系统面板/下载报表展示顺序在前，其余列在后 ----
    # 2026-09-03 调整：① 新增「品类」列，位于「包裹类型」之后；② 「代运营」前移至「发货SKU」之前（默认显示）。
    ORDER = [
        'ID', '14K货号', 'ASIN', '成员货品', '成员货品数量',
        'listing后台状态（领星+后台）', '店铺', '唯一SKU', 'SPU', 'SPU带尺寸',
        '包裹类型', '品类', '代运营', '发货SKU',
        '是否同步平台库存', '美国生命周期状态', '非活动', 'leadtime',
        '是否重复维护', '异常确认状态', '异常确认备注',
        '内部 ID', '德国生命周期状态', '日本生命周期状态', '英国生命周期状态',
        '运营', '运营组', '类型'
    ]
    RENAME = {'14K货号': '上架SKU', '成员货品': '库存SKU'}
    new_header = [RENAME.get(n, n) for n in ORDER]
    result = [[r[fidx[n]] for n in ORDER] for r in result]
    fidx = {name: i for i, name in enumerate(new_header)}

    # ---- 缺失平台信息异常：数据源(领星/walmart)有、平台信息表无 的 SKU ----
    # 组装为独立列表（不混入主表），每条包含：SKU/SPU/店铺/ASIN/listing/来源
    miss_rows = []
    if union_miss:
        lx_by_sku = {}                       # SKU -> (平台店铺, ASIN, listing)，同 SKU 取第一条
        if lingxing_map:
            for (plat, sku), (asin, listing) in lingxing_map.items():
                if sku not in lx_by_sku:
                    lx_by_sku[sku] = (plat, asin, listing)
        for sku in sorted(union_miss):
            src = lx_by_sku.get(sku)
            if src:                          # 领星优先
                store, asin, listing = src
                source = 'lingxing'
            else:                            # 否则 walmart（默认店铺 US-Walmart）
                store, asin, listing = 'US-Walmart', *walmart_map[sku]
                source = 'walmart'
            miss_rows.append({
                'sku': sku,
                'spu': _spu_for_store(store, sku, ''),
                'store': store,
                'asin': asin,
                'listing': listing,
                'source': source,
            })

    # ---- 统计（包裹类型/发货SKU/三态/listing 分布基于平台信息表行；缺失行单独存放） ----
    stats = {
        'total': len(result),
        'original': len(rows),
        'expanded': n_added,
        'expanded_groups': len(best_rep),
        'pkg_dist': dict(Counter(r[fidx['包裹类型']] for r in result)),
        'fahuo_dist': dict(Counter(r[fidx['发货SKU']] for r in result)),
        'anomaly_count': sum(1 for r in result if r[fidx['包裹类型']] == '异常'),
        'tag_dist': dict(Counter(r[fidx['是否重复维护']] for r in result)),
        'asin_hit': n_asin,
        'listing_dist': dict(Counter(r[fidx[COL_LISTING]] for r in result
                                     if r[fidx[COL_LISTING]] != '')),
        'missing_platform': {               # 数据源有、平台信息表无 的 SKU（异常清单）
            'lingxing': len(lx_miss),
            'walmart': len(wm_miss),
            'union': len(union_miss),
            'rows': len(miss_rows),
        },
        'missing_platform_rows': miss_rows, # 独立面板数据：sku/spu/store/asin/listing/source
        'transfer': {                       # 本次使用的转单表口径
            'sheets': per_sheet,
            'total_rows': len(all_rows),
            'sku_count': len(mem2rep),
            'group_count': len(groups),
        },
        'admin': {                          # 管理员检查表（品类）口径
            'used': bool(admin_map),
            'summary': admin_summary,
            'covered': sum(1 for r in result if r[fidx['品类']] != ''),
        },
        'lingxing': lx_summary,             # 本次使用的领星清单口径（None=未上传）
        'walmart': wm_summary,              # 本次使用的 walmart 报表口径（None=未上传）
    }
    return new_header, result, stats


def _is_temu_store(s):
    return (s or '').strip().startswith(('US-Temu', 'EU-Temu'))


def _temu_inherit(header, rows):
    """Temu 行 -> 从原始报表同款行照搬平台字段（2026-10-12 用户口径）。

    匹配键链式：唯一SKU -> 上架SKU -> SPU，命中即停；同键多行时取**首条**。
    仅填空值，不覆盖已有值；源行本身为空则结果仍为空（与 Amazon/Walmart 行一致）。
    返回被填充的单元格数。"""
    idx = {c: i for i, c in enumerate(header)}
    i_store = idx.get('店铺')
    if i_store is None:
        return 0
    chain = [(c, idx[c]) for c in ('唯一SKU', '上架SKU', 'SPU') if c in idx]
    if not chain:
        return 0
    pools = []
    for _, i in chain:
        pool = {}
        for r in rows:
            if _is_temu_store(r[i_store]):
                continue
            v = r[i].strip()
            if v and v not in pool:
                pool[v] = r
        pools.append((i, pool))
    n = 0
    for r in rows:
        if not _is_temu_store(r[i_store]):
            continue
        src = None
        for i, pool in pools:
            v = r[i].strip()
            if v and v in pool:
                src = pool[v]
                break
        if src is None:
            continue
        for c in TEMU_INHERIT_FIELDS:
            j = idx.get(c)
            if j is None:
                continue
            if not r[j].strip():
                r[j] = src[j].strip()
                n += 1
    return n


def load_ops(path):
    """产品运营表 -> {sheet名: {SPU别名: (运营, 运营组)}}

    真实表结构（2026-10-12 实测）：
      sheet「AMZ组」     列：SPU / 新SPU-1 / 新SPU-2 / 新SPU-3 / 26年运营组 / 26年运营
      sheet「综合组-AMZ」列：SPU / 新SPU1 / 新SPU2 / 运营组 / 负责运营
    故列名一律模糊识别：
      别名键 = `SPU` 及 `新SPU` 开头（允许 -/–/空格/数字，如 新SPU-1、新SPU1、新SPU 2）；
      运营列 = 含「运营」且不含「组」（负责运营 / 26年运营 / 运营）；
      运营组列 = 含「运营组」或「组」（运营组 / 26年运营组）。
    SPU 查不到时用新SPU1、再新SPU2…依次查（别名同权重）。"""
    import openpyxl, re
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = {}
    try:
        for sn in wb.sheetnames:
            rows_all = list(wb[sn].iter_rows(values_only=True))
            if not rows_all:
                continue
            hdr = [str(x).strip() if x is not None else '' for x in rows_all[0]]
            if 'SPU' not in hdr:
                continue
            alias_idx = [i for i, c in enumerate(hdr)
                         if c == 'SPU' or re.match(r'^新SPU', c)]
            op_idx = [i for i, c in enumerate(hdr) if c and '运营' in c and '组' not in c]
            grp_idx = [i for i, c in enumerate(hdr) if c and ('运营组' in c or c.endswith('组'))]
            i_op = op_idx[0] if op_idx else None
            i_grp = grp_idx[0] if grp_idx else None
            m = {}
            for r in rows_all[1:]:
                def cell(i):
                    if i is None or i >= len(r) or r[i] is None:
                        return ''
                    return str(r[i]).strip()
                op, grp = cell(i_op), cell(i_grp)
                for i in alias_idx:
                    v = cell(i)
                    if v and v not in m:
                        m[v] = (op, grp)
            out[sn] = m
    finally:
        wb.close()
    return out


def _apply_ops(header, rows, ops=None):
    """运营 / 运营组：一律按「店铺归属」覆盖（2026-10-12 用户口径）。

    Walmart -> 吴琇仙 / 综合组-WLM
    Temu    -> COZY=吴琇仙、其余=陆飞翔 / 综合组-TEMU
    Amazon  -> JYT·XYT 固定 MIA黄 / 品牌组；Urban 查「综合组-AMZ」sheet；
               HLLdeco·Home Nest 查「AMZ组」sheet（按 SPU，查不到填「未查询到」）。
    运营值形如「杨志怡&MIA黄」时取 & 前段。返回填充行数。"""
    idx = {c: i for i, c in enumerate(header)}
    i_store, i_op, i_grp = idx.get('店铺'), idx.get('运营'), idx.get('运营组')
    i_spu = idx.get('SPU')
    if i_store is None or i_op is None or i_grp is None:
        return 0
    ops = ops or {}
    n = 0
    for r in rows:
        st = r[i_store].strip()
        if not st:
            continue
        if 'walmart' in st.lower():
            op, grp = WALMART_OP, WALMART_OP_GROUP
        elif _is_temu_store(st):
            op = TEMU_OP_COZY if st == TEMU_STORE_COZY else TEMU_OP_OTHER
            grp = TEMU_OP_GROUP
        elif st in AMZ_OP_FIXED:
            op, grp = AMZ_OP_FIXED[st]
        elif st in AMZ_OP_SHEET:
            sheet = ops.get(AMZ_OP_SHEET[st]) or {}
            spu = r[i_spu].strip() if i_spu is not None else ''
            hit = sheet.get(spu) if spu else None
            if hit:
                op, grp = hit
            else:
                op, grp = AMZ_OP_NOT_FOUND, AMZ_OP_NOT_FOUND
        else:
            continue
        if op and '&' in op:
            op = op.split('&')[0].strip()
        if r[i_op] != op or r[i_grp] != grp:
            r[i_op], r[i_grp] = op, grp
            n += 1
    return n


# ========================== 对外入口 ==========================
def run_pipeline(raw_csv_path, xlsx_path, lingxing_path=None, walmart_path=None,
                 admin_check_path=None, temu_path=None, temu_paths=None,
                 ops_path=None, progress=None):
    """原始 CSV + 转单.xlsx + 可选(领星清单, walmart报表, 管理员检查表, Temu报表, 产品运营表) -> (header, rows, stats)

    Temu 报表支持多份：temu_paths 为路径列表（A/B 类各一份留档），temu_path 为
    单路径（向后兼容）；两者可混用，全部解析后按行拼接再合并。
    ops_path 为「产品运营表」（第 7 步），用于 Amazon 店铺按 SPU 查运营/运营组。
    progress: 可选回调 progress(pct, stage)，用于前端进度条。
              阶段：读取原始表 5% → 阶段一清洗打标 40% → 阶段二扩展赋值 80% → 完成 100%
    """
    def _p(pct, stage):
        if progress:
            try:
                progress(pct, stage)
            except Exception:
                pass

    # ---- 读取原始报表 CSV ----
    with open(raw_csv_path, encoding='utf-8-sig', newline='') as f:
        r = csv.reader(f)
        raw_header = next(r)
        raw_rows = [row for row in r if any(str(c).strip() for c in row)]

    # ---- 规范化 Temu 店铺名（仅含 TEMU 的店铺），与 Temu 报表映射口径对齐 ----
    ri = {c: i for i, c in enumerate(raw_header)}
    if '店铺' in ri:
        for row in raw_rows:
            row[ri['店铺']] = normalize_store(row[ri['店铺']])
    # 确保扩展列存在（原始报表可能无 运营/运营组/TemuListing/TemuSKC）
    # 注意：ASIN 不在此建列 —— 阶段二会追加新的 ASIN 列（领星匹配结果），
    # Temu 的 ASIN（MSKU ID / SKU ID）改为按成员货品记下、阶段二后回填，避免被覆盖。
    for c in ('运营', '运营组', 'TemuListing', 'TemuSKC'):
        if c not in ri:
            ri[c] = len(raw_header)
            raw_header = raw_header + [c]
    # 原始行补齐到统一列数（否则新增列索引越界）
    _w = len(raw_header)
    raw_rows = [row + [''] * (_w - len(row)) for row in raw_rows]

    # ---- 合并 Temu 报表（第 6 步，独立上传源，Option ②） ----
    # 合并策略：以「成员货品」为唯一键（Temu 的 MSKU/SKU 在平台表与产品报表间一致）——
    #   · 该成员货品已存在于原始报表 → 【富化】对应行（补 运营/运营组/listing后台状态），不新增重复行；
    #   · 原始报表完全没有的 Temu 成员货品 → 【新增】为独立行（按成员货品去重，避免产品报表自身重复行）。
    # 既适配「平台表已含 Temu」也适配「平台表不含 Temu」，且绝不产生重复 SKU。
    _temu_files = [p for p in (temu_paths or []) if p]
    if temu_path and temu_path not in _temu_files:
        _temu_files.append(temu_path)
    if _temu_files:
        th, trows = None, []
        for _tp in _temu_files:      # A/B 两槽（或多文件）分别解析后按行拼接；成员货品键去重在下方合并逻辑完成
            _h, _r = load_temu(_tp)
            th = _h
            trows.extend(_r)
        ti = {c: i for i, c in enumerate(th)}
        raw_member_keys = {row[ri['成员货品']].strip() for row in raw_rows}
        temu_by_member = {}     # 成员货品 -> (店铺, 运营, 运营组, TemuListing, 14K货号, ASIN)
        temu_rank = {}          # 成员货品 -> 状态优先级序号（跨店铺/跨文件多条时取最优，非首条优先）
        temu_asin_by_member = {}  # 成员货品 -> Temu ASIN（阶段二后回填用）
        add_best = {}           # 成员货品 -> (状态优先级序号, 新行)；新增行同样取状态最优那条
        add_rows = []
        for tr in trows:
            mem = tr[ti['成员货品']].strip()
            if not mem:
                continue
            _rk = _temu_status_rank_any(tr[ti['TemuListing']])
            if mem not in temu_by_member or _rk < temu_rank[mem]:
                temu_rank[mem] = _rk
                temu_by_member[mem] = (tr[ti['店铺']], tr[ti['运营']],
                                       tr[ti['运营组']], tr[ti['TemuListing']],
                                       tr[ti['14K货号']], tr[ti['ASIN']])
            # Temu ASIN（A类MSKU ID / B类SKU ID）按成员货品留存，阶段二后回填
            _asin_v = tr[ti['ASIN']].strip()
            if _asin_v and mem not in temu_asin_by_member:
                temu_asin_by_member[mem] = _asin_v
            # 仅当该成员货品在原始报表中完全不存在时才新增；按成员货品去重，
            # 多条时按「状态优先级」取最优（2026-10-12 用户口径），不再按出现顺序取首条
            if mem not in raw_member_keys:
                nr = [''] * len(raw_header)
                for c in th:
                    if c in ri:
                        nr[ri[c]] = tr[ti[c]]
                if mem not in add_best or _rk < add_best[mem][0]:
                    add_best[mem] = (_rk, nr)
        add_rows = [v[1] for v in add_best.values()]
        # ① 富化已有行：仅对「店铺本身就是 Temu」的已有行补全（2026-10-12 修正）
        #    旧逻辑按成员货品匹配会把 Temu 的运营写进 Walmart/Amazon 行
        #    （实测污染 4160 行 Walmart），现运营/运营组一律由 _apply_ops 按店铺归属覆盖。
        for row in raw_rows:
            if not row[ri['店铺']].strip().startswith(('US-Temu', 'EU-Temu')):
                continue
            e = temu_by_member.get(row[ri['成员货品']].strip())
            if e:
                if 'ASIN' in ri and not row[ri['ASIN']].strip():
                    row[ri['ASIN']] = e[5]
                if not row[ri['TemuListing']].strip():
                    row[ri['TemuListing']] = e[3]
        # ② 非活动：新增行按成员货品回查原始报表（设计口径：查不到留空）
        inact_map = {}
        if '成员货品' in ri and '非活动' in ri:
            for row in raw_rows:
                m = row[ri['成员货品']].strip()
                if m and m not in inact_map:
                    inact_map[m] = row[ri['非活动']].strip()
        for nr in add_rows:
            m = nr[ri['成员货品']].strip()
            nr[ri['非活动']] = inact_map.get(m, '')
        header, rows = raw_header, raw_rows + add_rows
    else:
        header, rows = raw_header, raw_rows

    _p(5, '读取原始报表…')
    header, rows = _stage1(header, rows)

    _p(40, '清洗 + 重复维护打标…')
    header, rows, stats = _stage2(header, rows, xlsx_path,
                                  lingxing_path=lingxing_path,
                                  walmart_path=walmart_path,
                                  admin_check_path=admin_check_path,
                                  progress=lambda p, s: _p(40 + int(p * 0.45), s))

    # ---- Temu 行：按「唯一SKU→上架SKU→SPU」链式从原始报表同款行照搬平台字段 ----
    # 口径：与 Amazon/Walmart 行一致，取值来源是原始报表，非跨平台回填。
    if _temu_files:
        _temu_inherit(header, rows)
        # Temu ASIN：阶段二的 ASIN 列来自领星匹配，此处用报表「MSKU ID / SKU ID」回填空值
        _fi = {c: i for i, c in enumerate(header)}
        if 'ASIN' in _fi and '库存SKU' in _fi:
            for _r in rows:
                if not _is_temu_store(_r[_fi['店铺']]):
                    continue
                if not _r[_fi['ASIN']].strip():
                    _v = temu_asin_by_member.get(_r[_fi['库存SKU']].strip(), '')
                    if _v:
                        _r[_fi['ASIN']] = _v
    # ---- 运营 / 运营组：一律按店铺归属覆盖（Amazon 走产品运营表按 SPU 查）----
    ops_map = {}
    if ops_path:
        try:
            ops_map = load_ops(ops_path)
        except Exception:
            ops_map = {}
    _apply_ops(header, rows, ops_map)

    _p(95, '生成统计…')
    return header, rows, stats


if __name__ == '__main__':
    import sys
    raw = sys.argv[1] if len(sys.argv) > 1 else r"D:/谷歌下载/SKU平台信息套件成员货品体现结果362.csv"
    xlsx = sys.argv[2] if len(sys.argv) > 2 else r"D:/WorkBuddy AI 文件/SKU&SPU清洗/system/reference/转单.xlsx"
    temu = sys.argv[3] if len(sys.argv) > 3 else None
    print("转单表摘要:", summarize_transfer(xlsx))
    h, rows, st = run_pipeline(raw, xlsx, temu_path=temu)
    print("列:", h)
    print("统计:", st)
