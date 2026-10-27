#!/usr/bin/env python3
"""
新品跟进系统 - 数据处理脚本 v2
核心逻辑重构:
1. 只跟进: 待上架新品(款式表蓝色) + 新增SPU(候选库中不在款式表的)
2. SPU级别记录, SKU为子行
3. 多包裹检测: 按颜色分组取最大包裹数
4. SPU级库存评估: 有在库>在途>无记录
5. 上架DDL: 已到仓+2天 / 在途=预计到仓时间-7天
"""

import csv
import json
import zipfile
import xml.etree.ElementTree as ET
import pandas as pd
import os
import re
from datetime import datetime, date, timedelta
from pathlib import Path

# SKU 异常判定 / 上架结论重算: 抽到独立纯 Python 模块, web_app 读取报表时可用同一口径即时重算,
# 运营确认「无效SKU」后不必等约 6 分钟的后台重跑。这里再导出一次, 兼容老的 import 路径。
try:
    from sku_eval import (  # noqa: F401
        parse_sku_color_suffix, detect_dim_anomalies, detect_multi_package,
        read_note_file as _read_sku_anomaly_note,
        invalid_sku_set as _invalid_sku_set,
        build_anomaly_records, NOTE_FILENAME as SKU_ANOMALY_NOTE_NAME,
        need_launch_label, need_launch_reason,
        STATUS_VALID, STATUS_INVALID, STATUS_COMBO,
    )
except ImportError:  # 极端情况下在本目录外执行时的兜底
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from sku_eval import (  # noqa: F401
        parse_sku_color_suffix, detect_dim_anomalies, detect_multi_package,
        read_note_file as _read_sku_anomaly_note,
        invalid_sku_set as _invalid_sku_set,
        build_anomaly_records, NOTE_FILENAME as SKU_ANOMALY_NOTE_NAME,
        need_launch_label, need_launch_reason,
        STATUS_VALID, STATUS_INVALID, STATUS_COMBO,
    )

# 转单关系 (需求1: 新品候选SPU若转单链追溯到款式表非蓝色SPU → 不跟进)
try:
    from new_color_module import load_transfer_groups as _load_transfer_groups
except ImportError:  # 极端情况下在本目录外执行时的兜底
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from new_color_module import load_transfer_groups as _load_transfer_groups

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)


def find_data_file(pattern):
    """用glob查找数据文件, 返回最新匹配的文件路径"""
    matches = sorted(BASE_DIR.glob(pattern))
    if matches:
        return matches[-1]
    return None


def open_text_file(path):
    """读取文件内容为文本, 自动兼容 UTF-8 / GBK 编码 (Windows 中文CSV常为GBK)"""
    import io
    with open(path, 'rb') as f:
        raw = f.read()
    for enc in ('utf-8-sig', 'utf-8', 'gbk'):
        try:
            return io.StringIO(raw.decode(enc))
        except (UnicodeDecodeError, UnicodeError):
            continue
    return io.StringIO(raw.decode('gbk', errors='ignore'))


# ============================================================
# 1. FedEx 包裹类型判定 (2026年规则)
# ============================================================

def determine_fedex_package_type(weight_oz, length, width, height):
    """FedEx 2026: Standard / AHS-Dimension / Oversize / LTL"""
    if not all(v is not None and v > 0 for v in [weight_oz, length, width, height]):
        return "Unknown"

    weight_lb = weight_oz / 16.0
    dims = sorted([length, width, height], reverse=True)
    longest = dims[0]
    second_longest = dims[1]
    shortest = dims[2]
    girth = 2 * (second_longest + shortest)
    lpg = longest + girth
    volume = length * width * height

    if weight_lb > 150 or longest > 108 or lpg > 165:
        return "LTL"
    if longest > 96 or lpg > 130 or volume > 17280 or weight_lb > 110:
        return "Oversize"
    if longest > 48 or second_longest > 30 or lpg > 105 or volume > 10368 or weight_lb > 50:
        return "AHS-Dimension"
    return "Standard"


def combo_member_pkg_display(member_types):
    """全组合SPU的「SPU包裹情况」: 按成员货品包裹类型汇总 (2026-10-27 需求)。
    一种类型→「单包Oversize」(多个同类→「N包Oversize」); 多种→「1包AHS，1包Oversize」(严格的在前)。
    返回: (display, spu_max_type, member_count)"""
    _rank = {'LTL': 4, 'Oversize': 3, 'AHS-Dimension': 2, 'Standard': 1, 'Unknown': 0}
    _short = {'LTL': 'LTL', 'Oversize': 'Oversize', 'AHS-Dimension': 'AHS',
              'Standard': 'Standard', 'Unknown': 'Unknown'}
    if not member_types:
        return '', 'Unknown', 0
    spu_max = max(member_types, key=lambda t: _rank.get(t, 0))
    cnt_by = {}
    for t in member_types:
        cnt_by[t] = cnt_by.get(t, 0) + 1
    if len(cnt_by) == 1:
        t, n = list(cnt_by.items())[0]
        display = ('单包%s' % _short.get(t, t)) if n == 1 else ('%d包%s' % (n, _short.get(t, t)))
    else:
        parts = ['%d包%s' % (n, _short.get(t, t)) for t, n in
                 sorted(cnt_by.items(), key=lambda x: _rank.get(x[0], 0), reverse=True)]
        display = "，".join(parts)
    return display, spu_max, len(member_types)


def get_package_type_details(weight_oz, length, width, height):
    """返回包裹类型及详细尺寸信息"""
    if not all(v is not None and v > 0 for v in [weight_oz, length, width, height]):
        return {
            "package_type": "Unknown",
            "weight_lb": None, "longest_in": None,
            "second_longest_in": None, "length_plus_girth_in": None,
            "volume_cu_in": None,
        }
    weight_lb = round(weight_oz / 16.0, 2)
    dims = sorted([length, width, height], reverse=True)
    longest, second_longest, shortest = dims[0], dims[1], dims[2]
    girth = 2 * (second_longest + shortest)
    return {
        "package_type": determine_fedex_package_type(weight_oz, length, width, height),
        "weight_lb": weight_lb,
        "longest_in": longest,
        "second_longest_in": second_longest,
        "length_plus_girth_in": round(longest + girth, 2),
        "volume_cu_in": round(length * width * height, 2),
    }


# ============================================================
# 2. SKU尺寸信息 (CSV)
# ============================================================

def load_sku_dimensions():
    """读取SKU尺寸信息CSV"""
    filepath = find_data_file("SKU尺寸货品查询运营结果*.csv")
    if not filepath:
        print("[WARNING] SKU尺寸信息文件未找到")
        return {}
    print(f"  -> 使用文件: {filepath.name}")
    dimensions = {}
    with open_text_file(filepath) as f:
        reader = csv.DictReader(f)
        for row in reader:
            sku = row['SKU'].strip()
            try:
                details = get_package_type_details(
                    float(row['WeightOz']), float(row['Length']),
                    float(row['Width']), float(row['Height']))
                dimensions[sku] = details
            except (ValueError, KeyError):
                dimensions[sku] = {"package_type": "Unknown"}
    return dimensions


# ============================================================
# 3. 增长产品清单 (xlsx直接解析XML)
# ============================================================

def load_growth_products():
    """读取增长产品清单的sofa线/dining线/RBAC三个sheet (按sheet名称匹配, 避免文件顺序变化)"""
    filepath = find_data_file("增长产品清单*.xlsx")
    if not filepath:
        print("[WARNING] 增长产品清单文件未找到")
        return []
    z = zipfile.ZipFile(str(filepath))
    ns = {'main': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
          'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}

    shared = z.read('xl/sharedStrings.xml')
    shared_root = ET.fromstring(shared)
    strings = []
    for si in shared_root.findall('.//main:si', ns):
        texts = si.findall('.//main:t', ns)
        strings.append(''.join(t.text or '' for t in texts))

    # 1. workbook.xml: sheet名称 -> rId
    wb = ET.fromstring(z.read('xl/workbook.xml'))
    name_to_rid = {}
    for s in wb.find('main:sheets', ns):
        nm = s.get('name')
        rid = s.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
        name_to_rid[nm] = rid

    # 2. workbook.xml.rels: rId -> 目标sheet文件
    rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
    rid_to_target = {}
    for rel in rels:
        rid_to_target[rel.get('Id')] = rel.get('Target')

    def sheet_file_by_name(name):
        rid = name_to_rid.get(name)
        if not rid:
            return None
        target = rid_to_target.get(rid, '')
        # Target 可能是 "worksheets/sheet11.xml" 或 "/xl/worksheets/sheet11.xml"
        return target.split('/')[-1]

    # 3. 按sheet名称定位, 不再写死sheet编号
    sheets_config = [
        ('sofa线', 'C', 'D', 'S'),
        ('dining线', 'E', 'F', 'T'),
        ('RBAC', 'C', 'D', 'R'),
    ]

    products = []
    for sheet_name, spu_col, sku_col, channel_col in sheets_config:
        sheet_file = sheet_file_by_name(sheet_name)
        if not sheet_file:
            print(f"[WARNING] 增长产品清单中未找到sheet: {sheet_name}")
            continue
        try:
            xml_data = z.read(f'xl/worksheets/{sheet_file}')
            root = ET.fromstring(xml_data)
            rows = root.findall('.//main:row', ns)
            for row in rows[1:]:
                cells = {c.attrib.get('r', ''): c for c in row.findall('main:c', ns)}
                row_num = row.attrib.get('r', '')

                def get_cell(col):
                    cell = cells.get(f'{col}{row_num}', None)
                    if cell is None:
                        return ''
                    v_elem = cell.find('main:v', ns)
                    if v_elem is None:
                        return ''
                    t = cell.attrib.get('t', '')
                    val = v_elem.text
                    if t == 's' and val:
                        return strings[int(val)]
                    return val or ''

                spu = get_cell(spu_col).strip()
                sku_raw = get_cell(sku_col).strip()
                channel = get_cell(channel_col).strip()
                if not spu and not sku_raw:
                    continue
                skus = [s.strip() for s in sku_raw.replace('\r', '\n').split('\n') if s.strip()]
                # Amazon 渠道判定: 与上品通知表统一口径 (兼容 AMZ / AMAZON / 亚马逊)
                _ch = (channel or '').upper()
                is_amz = ('AMZ' in _ch) or ('AMAZON' in _ch) or ('亚马逊' in (channel or ''))
                products.append({
                    "source": "growth",
                    "line": sheet_name,
                    "spu": spu,
                    "skus": skus,
                    "channel": channel,
                    "is_department_eligible": is_amz,
                })
        except Exception as e:
            print(f"Error reading {sheet_name}: {e}")
    z.close()
    return products


# ============================================================
# 4. SPU提取 (从SKU/SPU名称中提取标准SPU码)
# ============================================================

# SPU 主码特征: 字母+数字混合且长度>=6 (如 SFAY1891 / BDNG1798D / MTBH1810)
# 变体段都不满足该特征: 尺寸(KB/QB/TB)与颜色(IVY/BRD/WTE/GRY)是纯字母,
# 包裹/仓码(AB/ABC/S2/S4/DEM1)不足 6 位
_SPU_BASE_RE = re.compile(r'^(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*[0-9])[A-Z0-9]{6,}$')

# 国家/地区码: 仅这些首段会被当作前缀剥离, 剥离后按正常规则重新解析 SPU。
# 用途: 如 US-SB1998-AUB-HMA → SB1998 (实际SPU为SB1998, 而非 US-SB1998);
#       US-CE1525-WAL-CWA → CE1525。剥离后必须能解析出合规SPU主码(::_SPU_BASE_RE)
#       或命中已知SPU才采用, 否则保留原样, 避免误伤非国家码开头的SPU。
_COUNTRY_CODES = {
    'US', 'UK', 'CA', 'AU', 'CN', 'JP', 'EU', 'DE', 'FR', 'IT', 'ES', 'MX',
    'BR', 'IN', 'KR', 'TW', 'HK', 'SG', 'NL', 'BE', 'CH', 'AT', 'SE', 'PL',
    'CZ', 'DK', 'FI', 'NO', 'PT', 'IE', 'NZ', 'ZA', 'TH', 'MY', 'PH', 'VN',
}


def extract_spu_from_sku(sku, known_spu_names=None):
    """从SKU或SPU名称中提取标准SPU码

    优先级:
    0. 国家/地区码前缀剥离: 首段∈_COUNTRY_CODES 时, 递归解析剩余部分,
       仅当剥离后能解析出合规SPU主码或命中已知SPU才采用 (如 US-SB1998-AUB-HMA → SB1998)
    1. 已知SPU列表最长前缀匹配 (最可靠)
    2. SPU 主码定位: 去掉末尾单字母段(多包裹后缀 A/B/C)后,
       从右往左找第一个满足"SPU主码特征"(字母+数字混合且>=6位)的段,
       SPU = 该段及其之前的内容 (如 SFAY1891-IVY-AB → SFAY1891)
    3. 回退: 第一个-段
    """
    if not sku:
        return sku

    parts = sku.split('-')
    # 0. 国家/地区码前缀剥离
    if len(parts) >= 2 and parts[0] in _COUNTRY_CODES:
        inner = extract_spu_from_sku('-'.join(parts[1:]), known_spu_names)
        inner_head = inner.split('-')[0] if inner else ''
        if inner and (inner in (known_spu_names or set()) or _SPU_BASE_RE.match(inner_head)):
            return inner

    # 1. 尝试已知SPU列表最长前缀匹配
    if known_spu_names:
        for i in range(len(parts), 0, -1):
            candidate = '-'.join(parts[:i])
            if candidate in known_spu_names:
                return candidate

    if len(parts) == 1:
        return parts[0]

    # 2a. 去掉末尾的单字母段(多包裹后缀 A/B/C, 业务规则见 §3.4)
    work_parts = list(parts)
    while len(work_parts) > 1 and len(work_parts[-1]) == 1 and work_parts[-1].isalpha():
        work_parts.pop()

    # 2b. 从右往左跳过变体段(颜色/尺寸/包裹), 定位 SPU 主码段
    #     旧逻辑只剥最右一个"颜色段", 遇 'SPU-颜色-包裹' 结构会把颜色留在 SPU 里
    #     (如 SFAY1891-IVY-AB → SFAY1891-IVY), 导致新品跟进出现 SKU 维度的行
    for i in range(len(work_parts) - 1, -1, -1):
        if _SPU_BASE_RE.match(work_parts[i]):
            return '-'.join(work_parts[:i + 1])

    # 3. 回退: 第一个-段
    return parts[0]


def spu_transfer_excluded(spu, sku_names, sku_to_group, group_names, all_existing_map, known_spu_names):
    """需求1: 判断该SPU是否应因「转单链追溯到已上架(款式表非蓝色)母体」而被排除。

    该SPU的任一SKU若落在某转单组内, 同组所有名字提取出的SPU中, 只要有任意一个
    在款式表中且底色非蓝色(=已上架/非待上架), 说明本品只是已上架品的换名/转单,
    新品跟进表不应出现该SPU记录。

    返回 (是否排除, 追溯到的SPU列表)。
    """
    if not sku_to_group:
        return False, []
    traced = set()
    checked_groups = set()
    for sku in sku_names:
        g = sku_to_group.get(sku)
        if g is None or g in checked_groups:
            continue
        checked_groups.add(g)
        for name in group_names.get(g, ()):
            tspu = extract_spu_from_sku(name, known_spu_names)
            if tspu:
                traced.add(tspu)
    # 兜底: SPU名本身也在转单组里
    g0 = sku_to_group.get(spu)
    if g0 is not None and g0 not in checked_groups:
        for name in group_names.get(g0, ()):
            tspu = extract_spu_from_sku(name, known_spu_names)
            if tspu:
                traced.add(tspu)
    for tspu in traced:
        rec = all_existing_map.get(tspu)
        if rec and not rec.get('is_blue'):
            return True, sorted(traced)
    return False, sorted(traced)


def _strip_country_prefix(sku):
    """去掉 SKU 首段的国家/地区码 (若首段∈_COUNTRY_CODES), 用于套件14K货号兜底匹配。"""
    parts = sku.split('-')
    if len(parts) >= 2 and parts[0] in _COUNTRY_CODES:
        return '-'.join(parts[1:])
    return sku


def load_kit_members(base_dir):
    """读取套件成员货品 CSV (*套件成员*.csv), 返回 {14K货号: [成员货品, ...]} (跨店铺去重合并)。

    用途: 需求4 —— 异常SKU被确认为「组合SKU」时, 按 14K货品(=该SKU)查成员货品,
    成员货品的包裹类型/库存接管上架判定。编码兼容 UTF-8 / GBK。
    """
    import csv as _csv
    matches = sorted(str(p) for p in Path(base_dir).glob('*套件成员*.csv'))
    if not matches:
        return {}
    members_map = {}
    for fp in matches:
        try:
            f = open(fp, 'rb')
            raw = f.read()
            f.close()
            enc = 'utf-8-sig'
            try:
                raw.decode('utf-8')
            except UnicodeDecodeError:
                enc = 'gbk'
            text = raw.decode(enc)
            reader = _csv.DictReader(text.splitlines())
            for row in reader:
                k14 = (row.get('14K货号') or '').strip()
                mem = (row.get('成员货品') or '').strip()
                if not k14 or not mem:
                    continue
                members_map.setdefault(k14, [])
                if mem not in members_map[k14]:
                    members_map[k14].append(mem)
        except Exception as e:
            print(f"[WARNING] 套件成员文件读取失败 {fp}: {e}")
    if members_map:
        print(f"  -> 套件成员: {len(members_map)} 个14K货号")
    return members_map


# ============================================================
# 4b. 上品通知表 (Excel)
# ============================================================

def load_self_operated_products(known_spu_names=None):
    """上品通知表: 表头第5行, 数据第6行起
    筛选: 上品平台含 AMZ / AMAZON / 亚马逊 (2026-10-20 起不再要求「通知上架时间」非空 —
    运营侧大量待上架记录未填通知时间, 会导致新品跟进整条记录缺失, 如 BEVT1953)
    返回 (products, diag): diag = 防呆诊断统计(总行数/亚马逊行数/非亚马逊排除行数+清单/通知时间为空行数)
    """
    filepath = BASE_DIR / "上品通知表.xlsx"
    if not filepath.exists():
        print("[WARNING] 上品通知表文件未找到")
        return [], {}
    df = pd.read_excel(str(filepath), engine='openpyxl', header=4)
    products = []
    no_notify = 0
    total_rows = 0
    non_amz_rows = 0
    non_amz_platforms = {}
    non_amz_spus = set()
    for _, row in df.iterrows():
        sku = str(row.get('SKU', '')).strip()
        platform = str(row.get('上品平台', '')).strip() if pd.notna(row.get('上品平台')) else ''
        notify_time = row.get('通知上架时间', None)
        if not sku or sku == 'nan':
            continue
        total_rows += 1
        # Amazon 渠道判定: 兼容英文 AMZ / AMAZON 与中文「亚马逊」写法
        _p = (platform or '').upper()
        is_amz = ('AMZ' in _p) or ('AMAZON' in _p) or ('亚马逊' in (platform or ''))
        has_notify = pd.notna(notify_time) and str(notify_time).strip() not in ('', 'NaT', 'nan')
        if not is_amz:
            # 防呆: 记录被平台口径排除的行, 便于核对「表里有但面板查不到」
            non_amz_rows += 1
            non_amz_platforms[platform or '(空)'] = non_amz_platforms.get(platform or '(空)', 0) + 1
            try:
                non_amz_spus.add(extract_spu_from_sku(sku, known_spu_names))
            except Exception:
                pass
            continue
        if is_amz:
            if not has_notify:
                no_notify += 1
            spu = extract_spu_from_sku(sku, known_spu_names)
            # 通知上架时间规范化: datetime→%Y-%m-%d; Excel序列号(数值)→按1899-12-30纪元换算
            notify_str = ''
            if has_notify:
                if hasattr(notify_time, 'strftime'):
                    notify_str = notify_time.strftime('%Y-%m-%d')
                else:
                    s = str(notify_time).strip()
                    try:
                        v = float(s)
                        if 20000 < v < 80000:      # 合理的Excel日期序列号范围
                            notify_str = (datetime(1899, 12, 30)
                                          + timedelta(days=v)).strftime('%Y-%m-%d')
                        else:
                            notify_str = s
                    except (ValueError, TypeError):
                        notify_str = s
            products.append({
                "source": "self_operated",
                "spu": spu,
                "sku": sku,
                "platform": platform,
                "notify_time": notify_str,
            })
    diag = {
        'total_rows': total_rows,
        'amz_rows': len(products),
        'non_amz_rows': non_amz_rows,
        'no_notify_rows': no_notify,
        'non_amz_platforms': dict(sorted(non_amz_platforms.items(),
                                         key=lambda kv: -kv[1])[:10]),
        'non_amz_spus': sorted(non_amz_spus),
    }
    return products, diag


# ============================================================
# 5. 库存 & 在途
# ============================================================

def load_inventory():
    """在库库存清单"""
    filepath = find_data_file("库存清单-*.xlsx")
    if not filepath:
        print("[WARNING] 库存清单文件未找到")
        return {}
    print(f"  -> 使用文件: {filepath.name}")
    df = pd.read_excel(str(filepath), engine='openpyxl')
    inventory = {}
    for _, row in df.iterrows():
        sku = str(row.get('SKU', '')).strip()
        qty = row.get('库存量', 0)
        if sku and pd.notna(qty):
            inventory[sku] = int(qty) if qty else 0
    return inventory


def load_in_transit():
    """在途动态库存表"""
    filepath = find_data_file("动态库存表_*.xlsx")
    if not filepath:
        print("[WARNING] 动态库存表文件未找到")
        return {}
    print(f"  -> 使用文件: {filepath.name}")
    df = pd.read_excel(str(filepath), engine='openpyxl')
    transit = {}
    for _, row in df.iterrows():
        sku = str(row.get('SKU', '')).strip()
        if not sku:
            continue
        qty = row.get('数量', 0)
        eta = row.get('ETA', None)
        arrival = row.get('预计到仓时间', None)
        warehouse = row.get('目的仓', '')
        spu = str(row.get('SPU', '')).strip()
        if sku not in transit:
            transit[sku] = []
        transit[sku].append({
            "quantity": int(qty) if pd.notna(qty) else 0,
            "eta": str(eta) if pd.notna(eta) else "",
            "arrival_date": str(arrival) if pd.notna(arrival) else "",
            "warehouse": str(warehouse) if pd.notna(warehouse) else "",
            "spu": spu,
        })
    return transit


# ============================================================
# 6. 款式表 (上架汇总-US)
# ============================================================

def parse_spu_aliases(item):
    """提取主SPU + 新SPU-1/2/3别名"""
    aliases = []
    spu = item.get('spu', '').strip()
    if spu:
        aliases.append(spu)
    for key in ['new_spu1', 'new_spu2', 'new_spu3']:
        val = item.get(key, '')
        if val:
            for line in val.replace('\r', '\n').split('\n'):
                line = line.strip()
                if line and '子件' not in line and line not in aliases:
                    aliases.append(line)
    return aliases


def regenerate_style_json():
    """从款式表.xlsx重新生成 款式表_完整数据.json
    读取: SPU(A) / 新SPU-1(B) / 新SPU-2(C) / 新SPU-3(D) / 运营组(E) / 运营(F)
    + SPU列背景色(蓝色FF0A43FF=待上架)
    """
    xlsx_path = BASE_DIR / "款式表.xlsx"
    json_path = BASE_DIR / "款式表_完整数据.json"

    if not xlsx_path.exists():
        print("[WARNING] 款式表.xlsx 未找到, 跳过JSON重新生成")
        return False

    try:
        z = zipfile.ZipFile(str(xlsx_path))
        ns = {'main': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}

        # 1. 读取共享字符串
        shared_root = ET.fromstring(z.read('xl/sharedStrings.xml'))
        strings = []
        for si in shared_root.findall('.//main:si', ns):
            texts = si.findall('.//main:t', ns)
            strings.append(''.join(t.text or '' for t in texts))

        # 2. 读取样式 (fills)
        styles_root = ET.fromstring(z.read('xl/styles.xml'))
        fills = []
        fills_elem = styles_root.find('main:fills', ns)
        if fills_elem is not None:
            for fill in fills_elem.findall('main:fill', ns):
                pf = fill.find('main:patternFill', ns)
                if pf is not None:
                    fg = pf.find('main:fgColor', ns)
                    if fg is not None:
                        fills.append(fg.attrib.get('rgb', ''))
                    else:
                        fills.append('')
                else:
                    fills.append('')

        # 3. 读取cellXfs (样式索引 → fillId映射)
        cell_xfs = []
        xfs_elem = styles_root.find('main:cellXfs', ns)
        if xfs_elem is not None:
            for xf in xfs_elem.findall('main:xf', ns):
                fill_id = int(xf.attrib.get('fillId', 0))
                cell_xfs.append(fill_id)

        # 4. 读取worksheet
        # 尝试找到第一个worksheet
        sheet_files = [n for n in z.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml')]
        sheet_files.sort()
        if not sheet_files:
            print("[ERROR] 款式表中未找到worksheet")
            z.close()
            return False

        root = ET.fromstring(z.read(sheet_files[0]))
        rows = root.findall('.//main:row', ns)

        # 5. 解析数据 (表头在Row1)
        result = [{'A': 'SPU', 'B': '新SPU-1', 'C': '新SPU-2',
                    'D': '新SPU-3', 'E': '运营组', 'F': '运营'}]

        for row in rows:
            row_num = int(row.attrib.get('r', 0))
            if row_num < 2:  # 跳过表头
                continue

            cells = {c.attrib.get('r', ''): c for c in row.findall('main:c', ns)}

            def get_cell(col):
                cell = cells.get(f'{col}{row_num}', None)
                if cell is None:
                    return ''
                v_elem = cell.find('main:v', ns)
                if v_elem is None:
                    return ''
                t = cell.attrib.get('t', '')
                val = v_elem.text
                if t == 's' and val:
                    return strings[int(val)]
                return val or ''

            def get_bg_color(col):
                cell = cells.get(f'{col}{row_num}', None)
                if cell is None:
                    return ''
                s_attr = cell.attrib.get('s', '')
                if not s_attr:
                    return ''
                style_idx = int(s_attr)
                if style_idx < len(cell_xfs):
                    fill_id = cell_xfs[style_idx]
                    if fill_id < len(fills):
                        return fills[fill_id]
                return ''

            spu = get_cell('A').strip()
            if not spu:
                continue

            result.append({
                'spu': spu,
                'new_spu1': get_cell('B').strip(),
                'new_spu2': get_cell('C').strip(),
                'new_spu3': get_cell('D').strip(),
                'group': get_cell('E').strip(),
                'operator': get_cell('F').strip(),
                'spu_bg_color': get_bg_color('A'),
                'row': row_num,
            })

        z.close()

        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        blue_count = sum(1 for r in result[1:] if r.get('spu_bg_color') == 'FF0A43FF')
        print(f"  -> 款式表JSON已重新生成: {len(result)-1} 行, 蓝色: {blue_count} 个")
        return True

    except Exception as e:
        print(f"[ERROR] 款式表JSON生成失败: {e}")
        return False


def load_existing_spu_list():
    """读取款式表: 蓝色=待上架, 别名映射"""
    json_file = BASE_DIR / "款式表_完整数据.json"
    if not json_file.exists():
        print("[INFO] 款式表_完整数据.json 未找到")
        return None

    with open(json_file, 'r', encoding='utf-8') as f:
        spu_data = json.load(f)

    existing_spus = set()
    blue_spus = []
    spu_to_aliases = {}
    alias_to_spu = {}
    all_existing_map = {}  # main_spu → full record

    for item in spu_data[1:]:
        spu = item.get('spu', '').strip()
        bg = item.get('spu_bg_color', '')
        group = item.get('group', '')
        operator = item.get('operator', '').strip()
        if operator in ('/', '-', '无', '待定', ''):
            operator = ''
        if not spu:
            continue

        existing_spus.add(spu)
        is_blue = bg == 'FF0A43FF'
        aliases = parse_spu_aliases(item)
        spu_to_aliases[spu] = aliases
        for alias in aliases:
            alias_to_spu[alias] = spu

        all_existing_map[spu] = {
            'spu': spu, 'bg_color': bg, 'group': group,
            'operator': operator, 'aliases': aliases,
            'is_blue': is_blue, 'row': item.get('row', 0),
        }
        if is_blue:
            blue_spus.append({
                'spu': spu, 'group': group, 'operator': operator,
                'aliases': aliases, 'row': item.get('row', 0),
            })

    from collections import Counter
    blue_groups = Counter(b['group'] for b in blue_spus if b['group'])
    print(f"  -> 款式表: {len(existing_spus)} 个SPU, 蓝色待上架: {len(blue_spus)} 个")
    print(f"  -> 别名映射: {len(alias_to_spu)} 个名称")
    print(f"  -> 蓝色新品运营组分布: {dict(blue_groups)}")

    return {
        'spu_set': existing_spus,
        'blue_spus': blue_spus,
        'all_existing_map': all_existing_map,
        'spu_to_aliases': spu_to_aliases,
        'alias_to_spu': alias_to_spu,
    }


# ============================================================
# 7. 新增helper函数: 多包裹检测 / SPU库存评估 / DDL计算
# ============================================================

# ------------------------------------------------------------
# parse_sku_color_suffix / detect_multi_package / detect_dim_anomalies
# 已迁移至 sku_eval.py (见文件头 import), 保持原行为, 仅 detect_dim_anomalies
# 多返回一个 wild_no_data 列表用于让「隐形拦路虎」SKU 现形。
# ------------------------------------------------------------


def evaluate_spu_inventory(sku_list, inventory, in_transit):
    """SPU级别库存评估
    优先级: 有在库 > 在途 > 无记录
    返回: (status, earliest_eta_str, detail)
    """
    has_stock = False
    total_stock = 0
    transit_records = []

    for sku in sku_list:
        stock_qty = inventory.get(sku, 0)
        if stock_qty > 0:
            has_stock = True
            total_stock += stock_qty
        transit_info = in_transit.get(sku, [])
        for t in transit_info:
            transit_records.append({**t, 'sku': sku})

    if has_stock:
        return ('in_stock', None, {'total_stock': total_stock})
    elif transit_records:
        earliest = None
        for t in transit_records:
            eta_str = t.get('arrival_date', '') or t.get('eta', '')
            if eta_str:
                try:
                    eta_date = datetime.strptime(str(eta_str)[:10], '%Y-%m-%d').date()
                    if earliest is None or eta_date < earliest:
                        earliest = eta_date
                except (ValueError, TypeError):
                    pass
        return ('in_transit', earliest, {'transit_count': len(transit_records)})
    else:
        return ('no_record', None, {})


def calculate_launch_ddl(inv_status, earliest_eta=None, carryover_ddl=None, is_new=False):
    """上架DDL计算
    carryover_ddl: 上次处理记录的上架DDL (需求4: 重新处理时沿用, 不再重算)
    is_new: 是否为本次新增的SPU (无上次记录); 新增且已到仓 → 当日+3
    in_stock: 当天+2天 (已有记录且无carryover时的兜底); 新增已到仓 → +3
    in_transit: 最早预计到仓时间 - 7天
    no_record: None
    """
    today = date.today()
    # 沿用上次记录的上架DDL (优先级最高)
    if carryover_ddl:
        try:
            return datetime.strptime(carryover_ddl, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            pass
    if inv_status == 'in_stock':
        return today + timedelta(days=3 if is_new else 2)
    elif inv_status == 'in_transit' and earliest_eta:
        return earliest_eta - timedelta(days=7)
    else:
        return None


def find_matching_skus(spu_names, sku_dims, inventory, in_transit, candidate_skus=None):
    """用SPU名(含别名)查找所有匹配的SKU"""
    all_skus = set()
    if candidate_skus:
        all_skus.update(candidate_skus)

    for name in spu_names:
        if not name:
            continue
        for sku_key in sku_dims:
            if sku_key == name or sku_key.startswith(name + '-'):
                all_skus.add(sku_key)
        for inv_sku in inventory:
            if inv_sku == name or inv_sku.startswith(name + '-'):
                all_skus.add(inv_sku)
        for trans_sku in in_transit:
            if trans_sku == name or trans_sku.startswith(name + '-'):
                all_skus.add(trans_sku)

    return sorted(all_skus)


# ============================================================
# 8. 主处理逻辑
# ============================================================

def load_old_report_maps(output_dir=None):
    """读取"上一次"的 report_data.json, 返回 (old_ddl_map, old_org_map)。

    old_ddl_map: spu -> 上次的上架DDL              (重新处理时沿用, 不重算)
    old_org_map: spu -> {'category': 上次展示的运营组, 'operator': 上次展示的运营}
                 上次展示值 = note_fields 里的人工调整值优先, 否则款式表来源值。

    用途: 本次款式表给不出运营/运营组(该SPU不在款式表、或 E/F 列为空)时,
          沿用上次的记录, 避免运营/运营组被清空。
    """
    if output_dir is None:
        output_dir = OUTPUT_DIR
    old_ddl_map = {}
    old_org_map = {}
    old_report = Path(output_dir) / "report_data.json"
    if not old_report.exists():
        return old_ddl_map, old_org_map
    try:
        with open(old_report, 'r', encoding='utf-8') as f:
            old_data = json.load(f)
        for p in old_data.get('products', []):
            spu = p.get('spu')
            if not spu:
                continue
            ddl = p.get('launch_ddl')
            if ddl:
                old_ddl_map[spu] = ddl
            _nf = p.get('note_fields')
            _nf = _nf if isinstance(_nf, dict) else {}
            old_org_map[spu] = {
                'category': str(_nf.get('operator_group') or p.get('category') or '').strip(),
                'operator': str(_nf.get('operator') or p.get('operator') or '').strip(),
            }
    except Exception as e:
        print(f"  读取上次 report_data.json 失败(忽略): {e}")
    return old_ddl_map, old_org_map


def resolve_org_fields(src_group, src_operator, prev=None):
    """解析运营组 / 运营两个字段。

    规则(需求): 款式表本次给出非空值 → 采用款式表值(有变化就更新);
               款式表给不出值(不在款式表 / E.F 列为空) → 沿用上次记录, 不重新计算。
    prev: load_old_report_maps() 里的 old_org_map[spu] = {'category':.., 'operator':..}
    返回 (运营组, 运营)。
    """
    def _clean(v):
        if v is None:
            return ''
        return v.strip() if isinstance(v, str) else str(v).strip()
    prev = prev if isinstance(prev, dict) else {}
    group = _clean(src_group) or _clean(prev.get('category'))
    operator = _clean(src_operator) or _clean(prev.get('operator'))
    return group, operator


def load_sku_anomaly_note():
    """读取 SKU 异常确认归档: 云端 → 本机镜像 (后读覆盖, 保留清空语义)。

    背景: 运营确认过的「无效SKU」必须留档, 每次重新处理都沿用, 不用反复确认。
    Supabase 免费层存在写后读延迟, 只依赖单一来源会读到旧值 —— 与颜色档案(
    color_dict.json)、新色备注(new_color_note.json)同一套健壮性原则。
    """
    note = {}
    try:
        import cloud_store
        if cloud_store.is_cloud():
            cloud = cloud_store.read_json(
                cloud_store.BUCKET_OUTPUTS, SKU_ANOMALY_NOTE_NAME,
                OUTPUT_DIR / SKU_ANOMALY_NOTE_NAME)
            if isinstance(cloud, dict):
                note.update(cloud)
    except Exception as e:
        print(f"  [warn] 云端读取 SKU 异常确认归档失败: {e}")
    local = _read_sku_anomaly_note()
    if isinstance(local, dict):
        for k, v in local.items():
            if k and isinstance(v, dict):
                note[k] = dict(v)
    return note


def process_all():
    """主处理函数 v2"""

    print("=" * 60)
    print("新品跟进系统 v2 - 数据处理开始")
    print("=" * 60)

    # 读取上次 report_data.json: 上架DDL(沿用) + 运营/运营组(本次款式表给不出值时沿用)
    old_ddl_map, old_org_map = load_old_report_maps()
    if old_ddl_map or old_org_map:
        print(f"  已读取上次 {len(old_ddl_map)} 个SPU的上架DDL / {len(old_org_map)} 个SPU的运营记录 (将沿用)")

    # SKU 异常确认归档: 已确认「无效SKU」的记录在处理阶段全流程剔除,
    # 保证后台重跑后的结论与网页端即时重算完全一致。
    sku_anomaly_note = load_sku_anomaly_note()
    invalid_sku_set = _invalid_sku_set(sku_anomaly_note)
    if sku_anomaly_note:
        print(f"  已归档 {len(sku_anomaly_note)} 条 SKU 异常确认"
              f" (其中 {len(invalid_sku_set)} 个确认为无效SKU, 将分栏展示不计入上架判定)")

    # 加载所有数据 (款式表优先, 为SPU提取提供已知SPU列表)
    print("\n[1/7] 加载SKU尺寸信息...")
    sku_dims = load_sku_dimensions()
    print(f"  -> {len(sku_dims)} 个SKU有尺寸信息")

    print("[2/7] 重新生成款式表JSON并加载...")
    regenerate_style_json()
    existing_data = load_existing_spu_list()
    if existing_data:
        existing_spus = existing_data['spu_set']
        blue_spus = existing_data['blue_spus']
        alias_to_spu = existing_data.get('alias_to_spu', {})
        spu_to_aliases = existing_data.get('spu_to_aliases', {})
        all_existing_map = existing_data.get('all_existing_map', {})
    else:
        existing_spus = set()
        blue_spus = []
        alias_to_spu = {}
        spu_to_aliases = {}
        all_existing_map = {}

    # 构建已知SPU名称集合 (主SPU + 所有别名), 用于SPU提取时的最长前缀匹配
    known_spu_names = set(existing_spus) | set(alias_to_spu.keys())
    print(f"  -> 已知SPU名称集合: {len(known_spu_names)} 个")

    # 转单关系 (需求1: 新品候选SPU若转单链追溯到款式表非蓝色SPU → 不跟进)
    transfer_data = _load_transfer_groups(BASE_DIR)
    transfer_sku_to_group = transfer_data[0] if transfer_data else {}
    transfer_group_names = transfer_data[1] if transfer_data else {}
    if transfer_sku_to_group:
        print(f"  -> 转单关系: {len(transfer_sku_to_group)} 个SKU名, {len(transfer_group_names)} 个组")

    # 套件成员 (需求4: 组合SKU 确认后由其成员货品接管上架判定)
    kit_map = load_kit_members(BASE_DIR)

    print("[3/7] 加载增长产品清单...")
    growth_products = load_growth_products()
    growth_eligible = [p for p in growth_products if p['is_department_eligible']]
    print(f"  -> 共 {len(growth_products)} 个产品, 其中 {len(growth_eligible)} 个部门可上架")

    print("[4/7] 加载上品通知表...")
    self_products, self_diag = load_self_operated_products(known_spu_names)
    self_no_notify = self_diag.get('no_notify_rows', 0)
    print(f"  -> {len(self_products)} 个部门可上架的自营SKU"
          f" (其中 {self_no_notify} 行通知上架时间为空, 按新规则仍纳入)")
    print(f"  -> 防呆: 上品通知表共 {self_diag.get('total_rows', 0)} 行, "
          f"非亚马逊平台排除 {self_diag.get('non_amz_rows', 0)} 行"
          f" (平台取值: {self_diag.get('non_amz_platforms', {})})")

    print("[5/7] 加载库存数据...")
    inventory = load_inventory()
    print(f"  -> {len(inventory)} 个SKU有在库库存")

    print("[6/7] 加载在途数据...")
    in_transit = load_in_transit()
    print(f"  -> {len(in_transit)} 个SKU有在途数据")

    # ============================================================
    # 构建候选产品库 (上品通知表 + 增长产品)
    # ============================================================
    print("\n" + "=" * 60)
    print("构建候选产品库...")
    print("=" * 60)

    candidate_pool = {}  # spu_name → {sources, skus, lines, notify_time}

    # 从增长产品
    for prod in growth_eligible:
        spu = prod['spu']
        if not spu:
            continue
        # SPU提取: 先尝试已知SPU列表匹配, 再正则匹配
        spu = extract_spu_from_sku(spu, known_spu_names)
        # 解析别名: 如果spu是某主SPU的别名, 用主SPU
        resolved = alias_to_spu.get(spu, spu)
        if resolved not in candidate_pool:
            candidate_pool[resolved] = {
                'sources': set(), 'skus': set(), 'lines': set(), 'notify_time': ''
            }
        candidate_pool[resolved]['sources'].add('growth')
        candidate_pool[resolved]['lines'].add(prod['line'])
        for sku in prod['skus']:
            clean = sku.split()[0] if ' ' in sku else sku
            candidate_pool[resolved]['skus'].add(clean)

    # 从上品通知表
    for prod in self_products:
        sku = prod['sku']
        spu = prod.get('spu', '') or extract_spu_from_sku(sku, known_spu_names)
        resolved = alias_to_spu.get(spu, spu)
        if resolved not in candidate_pool:
            candidate_pool[resolved] = {
                'sources': set(), 'skus': set(), 'lines': set(), 'notify_time': ''
            }
        candidate_pool[resolved]['sources'].add('self_operated')
        candidate_pool[resolved]['skus'].add(sku)
        if prod.get('notify_time'):
            candidate_pool[resolved]['notify_time'] = prod['notify_time']

    print(f"  候选库共 {len(candidate_pool)} 个SPU")
    print(f"  其中增长产品: {sum(1 for v in candidate_pool.values() if 'growth' in v['sources'])} 个")
    print(f"  其中上品通知表: {sum(1 for v in candidate_pool.values() if 'self_operated' in v['sources'])} 个")

    # ============================================================
    # 确定跟进SPU列表
    # ============================================================
    print("\n" + "=" * 60)
    print("确定跟进SPU列表...")
    print("=" * 60)

    tracked_spus = []
    tracked_names = set()  # 所有已加入跟进的SPU名(含别名)

    # 1. 待上架新品 (款式表蓝色)
    for blue in blue_spus:
        spu = blue['spu']
        aliases = blue.get('aliases', [spu])
        # 查找候选库中的信息
        candidate = candidate_pool.get(spu, {})
        # 也检查别名是否在候选库中
        if not candidate:
            for alias in aliases:
                if alias in candidate_pool:
                    candidate = candidate_pool[alias]
                    break

        tracked_spus.append({
            'spu': spu,
            'aliases': aliases,
            'track_type': 'blue_pending',
            'group': blue.get('group', ''),
            'operator': blue.get('operator', ''),
            'candidate_skus': candidate.get('skus', set()),
            'sources': candidate.get('sources', set()),
            'lines': candidate.get('lines', set()),
            'notify_time': candidate.get('notify_time', ''),
        })
        for name in aliases:
            tracked_names.add(name)

    # 2. 新增SPU (候选库中不在款式表的)
    new_count = 0
    for spu, info in candidate_pool.items():
        # 检查SPU或其别名是否在款式表中
        if spu in existing_spus or spu in alias_to_spu:
            continue  # 已在款式表, 不是新增
        # 也检查是否已被蓝色SPU追踪(通过别名)
        if spu in tracked_names:
            continue

        tracked_spus.append({
            'spu': spu,
            'aliases': [spu],
            'track_type': 'new_spu',
            'group': '',
            'operator': '',
            'candidate_skus': info.get('skus', set()),
            'sources': info.get('sources', set()),
            'lines': info.get('lines', set()),
            'notify_time': info.get('notify_time', ''),
        })
        tracked_names.add(spu)
        new_count += 1

    print(f"  待上架新品(蓝色): {sum(1 for t in tracked_spus if t['track_type'] == 'blue_pending')} 个")
    print(f"  新增SPU(款式表无记录): {new_count} 个")
    print(f"  总计跟进: {len(tracked_spus)} 个SPU")

    # ---- 防呆诊断: 候选库里有上品/增长记录, 但最终没进跟进的 SPU ----
    # 成因: 该SPU在款式表中存在(或作为别名存在)但底色非蓝色 → 既不是蓝色待上架也不是新SPU
    not_tracked = sorted(spu for spu in candidate_pool if spu not in tracked_names)
    if not_tracked:
        print(f"  [防呆] 候选库中有记录但未进跟进的SPU: {len(not_tracked)} 个 "
              f"(在款式表但非蓝色/已下架等) 例: {not_tracked[:10]}")

    # ============================================================
    # 处理每个跟进SPU
    # ============================================================
    print("\n" + "=" * 60)
    print("处理SPU数据...")
    print("=" * 60)

    all_products = []
    stats = {
        "total_spu": 0,
        "total_sku": 0,
        "blue_pending_spus": 0,
        "new_spus": 0,
        "can_launch": 0,
        "cannot_launch": 0,
        "wait_arrival": 0,
        "no_record": 0,
        "no_need": 0,
        "warning": 0,
        "multi_pkg_spus": 0,
        "dim_anomaly_skus": 0,
        "spu_with_anomaly": 0,
        "transfer_excluded_spus": 0,
        "package_types": {"Standard": 0, "AHS-Dimension": 0, "Oversize": 0, "LTL": 0, "Unknown": 0},
    }

    for tracked in tracked_spus:
        spu = tracked['spu']
        aliases = tracked['aliases']
        track_type = tracked['track_type']
        candidate_skus = tracked.get('candidate_skus', set())

        # 查找所有匹配的SKU
        all_skus = find_matching_skus(aliases, sku_dims, inventory, in_transit, candidate_skus)

        # 需求1: 转单母体已上架排除
        # 若该SPU的任一SKU落入转单组, 且同组任一名字提取出的SPU在款式表非蓝色(已上架),
        # 说明本品是已上架品的换名/转单 → 新品跟进表不记录。仅对「新增SPU」生效
        # (款式表已显式标记蓝色的待上架品, 以款式表为准, 不自动排除)。
        if track_type == 'new_spu' and transfer_sku_to_group:
            excl, traced = spu_transfer_excluded(
                spu, all_skus, transfer_sku_to_group, transfer_group_names,
                all_existing_map, known_spu_names)
            if excl:
                stats['transfer_excluded_spus'] = stats.get('transfer_excluded_spus', 0) + 1
                print(f"  [转单排除] {spu}: 转单链追溯到已上架SPU {traced} → 不进新品跟进")
                continue

        # ---- Per-SKU 详情 (需求4/5: 不再剔除无效SKU→分栏; 组合SKU 由成员货品接管判定) ----
        sku_details = []        # 全量(含无效/组合), 用于 sku_count_total
        normal_skus = []        # 正常SKU (参与包裹/库存/上架判定)
        invalid_skus_list = []  # 已确认无效SKU (仅展示, 不计入判定)
        combo_skus_list = []    # 已确认组合SKU (判定由其成员接管)
        has_ltl = False

        for sku in all_skus:
            dim_info = sku_dims.get(sku)
            if dim_info:
                pkg_type = dim_info['package_type']
                pkg_details = dim_info
            else:
                pkg_type = "Unknown"
                pkg_details = {"package_type": "Unknown"}

            in_stock_qty = inventory.get(sku, 0)
            transit_info = in_transit.get(sku, [])
            if in_stock_qty > 0:
                inv_status = "in_stock"
            elif transit_info:
                inv_status = "in_transit"
            else:
                inv_status = "no_record"

            # 套件成员预解析: 若该SKU是套件成员表的14K货号, 解析其成员货品(库存SKU)的
            # 实际包裹类型/库存, 存档供「组合SKU」确认后接管上架判定(网页端即时重算无需数据源)
            kit_members = []
            member_names = kit_map.get(sku) or kit_map.get(_strip_country_prefix(sku))
            if member_names:
                for mem in member_names:
                    m_dim = sku_dims.get(mem)
                    m_pkg = m_dim['package_type'] if m_dim else "Unknown"
                    m_stock = inventory.get(mem, 0)
                    m_transit = in_transit.get(mem, [])
                    if m_stock > 0:
                        m_inv = "in_stock"
                    elif m_transit:
                        m_inv = "in_transit"
                    else:
                        m_inv = "no_record"
                    kit_members.append({
                        "sku": mem, "package_type": m_pkg,
                        "package_details": m_dim or {"package_type": m_pkg},
                        "inventory_status": m_inv, "in_stock": m_stock,
                        "in_transit": m_transit,
                    })

            confirm_status = (sku_anomaly_note.get(sku, {}) or {}).get('status') or ''
            detail = {
                "sku": sku, "package_type": pkg_type, "package_details": pkg_details,
                "inventory_status": inv_status, "in_stock": in_stock_qty,
                "in_transit": transit_info, "kit_members": kit_members,
                "confirm_status": confirm_status,
            }
            sku_details.append(detail)
            if confirm_status == STATUS_INVALID:
                invalid_skus_list.append(detail)
            elif confirm_status == STATUS_COMBO:
                combo_skus_list.append(detail)
            else:
                normal_skus.append(detail)
                if pkg_type == "LTL":
                    has_ltl = True
            stats["total_sku"] += 1

        # 评估集: 正常SKU + 组合SKU成员(去重, 组合自身Unknown不计入)
        normal_names = {s['sku'] for s in normal_skus}
        eval_skus = list(normal_skus)
        for c in combo_skus_list:
            for m in (c.get('kit_members') or []):
                mn = m.get('sku')
                if mn and mn not in normal_names and not any(e['sku'] == mn for e in eval_skus):
                    eval_skus.append(m)
                    if m.get('package_type') == 'LTL':
                        has_ltl = True

        # 数据清洗: 同SPU同颜色, 部分SKU缺重量/尺寸 → 尺寸异常 (仅正常SKU参与)
        clean_skus, dim_partial, wild_no_data = detect_dim_anomalies(normal_skus, spu)
        anomaly_count = len(dim_partial)

        # package_types 统计 (仅使用干净SKU)
        for s in clean_skus:
            stats["package_types"][s['package_type']] = stats["package_types"].get(s['package_type'], 0) + 1

        # 是否有 Unknown 包裹类型 SKU (仅标记用, 仅正常SKU; 供 has_unknown_dim 展示)
        has_unknown = any(s['package_type'] == 'Unknown' for s in clean_skus)
        # 阻塞「待确认」的 Unknown: clean_skus 中包裹类型=Unknown 且未确认(confirm_status 为空)
        # 已确认「正常SKU/组合SKU」视为已确定(解除阻塞, 按库存判); 「无效SKU」不计入评估集
        has_unconfirmed_unknown = any(
            s['package_type'] == 'Unknown'
            and not (sku_anomaly_note.get(s['sku'], {}) or {}).get('status')
            for s in clean_skus
        )

        # 标记异常SKU (用于前端展示)
        anomaly_sku_set = {s['sku'] for s in dim_partial}
        for s in normal_skus:
            s['is_dim_anomaly'] = s['sku'] in anomaly_sku_set

        # 统一异常清单: 仅「待确认」(confirm_status 为空)进入确认面板; 已确认invalid/combo 移入各自分栏
        sku_anomalies = build_anomaly_records(
            normal_skus, spu, clean_skus, dim_partial, wild_no_data, sku_anomaly_note)
        sku_anomalies = [r for r in sku_anomalies if not r['confirm_status']]
        if sku_anomalies:
            stats["dim_anomaly_skus"] += len(sku_anomalies)
            stats["wild_anomaly_skus"] = stats.get("wild_anomaly_skus", 0) + len(wild_no_data)
            stats["anomaly_records"] = stats.get("anomaly_records", 0) + len(sku_anomalies)
            stats["spu_with_anomaly"] += 1

        # 多包裹检测 (使用干净SKU)
        # 「SPU包裹情况」 (仅使用干净SKU)
        # 2026-10-27 需求: SPU 的所有SKU都是组合SKU时, 组合自身无尺寸 → 按成员货品的包裹类型汇总判断
        if clean_skus:
            sku_to_pkg_type = {s['sku']: s['package_type'] for s in clean_skus}
            multi_pkg_count, color_pkg_details, multi_pkg_display, spu_max_type = detect_multi_package(
                [s['sku'] for s in clean_skus], spu, sku_to_pkg_type)
        elif not normal_skus and combo_skus_list:
            # 全组合SPU: 收集全部成员货品的包裹类型做汇总
            member_types = []
            for c in combo_skus_list:
                for m in (c.get('kit_members') or []):
                    member_types.append((m or {}).get('package_type') or 'Unknown')
            multi_pkg_display, spu_max_type, multi_pkg_count = combo_member_pkg_display(member_types)
            color_pkg_details = {}
        else:
            sku_to_pkg_type = {}
            multi_pkg_count, color_pkg_details, multi_pkg_display, spu_max_type = detect_multi_package(
                [], spu, sku_to_pkg_type)
        if multi_pkg_count > 1:
            stats["multi_pkg_spus"] += 1

        # SPU级库存评估 + DDL (基于评估集 eval_skus: 组合SKU由成员接管, 无效SKU排除)
        spu_inv_status, earliest_eta, inv_eval_detail = evaluate_spu_inventory(
            [e['sku'] for e in eval_skus], inventory, in_transit)

        # DDL计算 (沿用上次记录的DDL; 新增且已到仓 → 当日+3)
        ddl = calculate_launch_ddl(
            spu_inv_status, earliest_eta,
            carryover_ddl=old_ddl_map.get(spu),
            is_new=(spu not in old_ddl_map)
        )

        # 上架可行性 (四态: 需要上架 / 不可上架 / 待确认 / 暂无需上架; 2026-10-21 重构)
        if not eval_skus:
            launch_status = "no_need"
            launch_reason = "未找到任何匹配SKU记录"
            stats["no_need"] += 1
        elif has_ltl:
            launch_status = "cannot_launch"
            launch_reason = "含LTL包裹类型SKU,部门无LTL资源"
            stats["cannot_launch"] += 1
        elif has_unconfirmed_unknown:
            launch_status = "warning"
            launch_reason = "存在包裹类型为Unknown的SKU,需逐SKU确认"
            stats["warning"] += 1
        elif spu_inv_status == 'in_stock':
            launch_status = "can_launch"
            launch_reason = "库存已到仓"
            stats["can_launch"] += 1
        elif spu_inv_status == 'in_transit':
            launch_status = "wait_arrival"
            eta_str = earliest_eta.strftime('%Y-%m-%d') if earliest_eta else "未知"
            launch_reason = f"在途中,预计到仓: {eta_str}"
            stats["wait_arrival"] += 1
        else:
            launch_status = "no_need"
            launch_reason = "全部SKU均无在库/在途库存"
            stats["no_need"] += 1

        # 「上架说明」列: 真实原因
        launch_reason = need_launch_reason(
            launch_status, launch_reason,
            pending_anomaly=bool(sku_anomalies), anomaly_count=len(sku_anomalies))

        # 来源标签
        sources = tracked.get('sources', set())
        if track_type == 'blue_pending':
            source_label = "待上架"
        elif sources:
            source_label = "新增"
        else:
            source_label = "新增"

        # 运营/运营组: 款式表有值→用它(有变化就更新); 给不出值→沿用上次记录, 避免被清空
        _grp, _opr = resolve_org_fields(
            tracked.get('group'), tracked.get('operator'), old_org_map.get(spu))

        all_products.append({
            "source": track_type,
            "source_label": source_label,
            "spu": spu,
            "aliases": aliases,
            "category": _grp,
            "operator": _opr,
            "skus": normal_skus,
            "invalid_skus": invalid_skus_list,
            "combo_skus": [{**c, 'members': c.get('kit_members') or []} for c in combo_skus_list],
            "sku_anomalies": sku_anomalies,
            "dim_anomaly_count": len(sku_anomalies),
            "wild_anomaly_count": len(wild_no_data),
            "anomaly_count": len(sku_anomalies),
            # 「SPU是否需要上架」只输出四态决策: 需要上架 / 不可上架 / 待确认 / 暂无需上架
            # (真实原因见 launch_reason / need_launch_reason)
            "need_launch_label": need_launch_label(launch_status),
            "multi_package_count": multi_pkg_count,
            "multi_package_display": multi_pkg_display,
            "spu_package_type": spu_max_type,
            "color_package_details": color_pkg_details,
            "spu_inventory_status": spu_inv_status,
            "earliest_eta": earliest_eta.strftime('%Y-%m-%d') if earliest_eta else None,
            "launch_ddl": ddl.strftime('%Y-%m-%d') if ddl else None,
            "launch_status": launch_status,
            "launch_reason": launch_reason,
            "has_ltl": has_ltl,
            "has_unknown_dim": has_unknown,
            "sku_count_total": len(sku_details),
            "candidate_sources": sorted(sources) if sources else [],
            "lines": sorted(tracked.get('lines', set())),
            "notify_time": tracked.get('notify_time', ''),
        })

        if track_type == 'blue_pending':
            stats["blue_pending_spus"] += 1
        else:
            stats["new_spus"] += 1
        stats["total_spu"] += 1

    # ============================================================
    # 输出结果
    # ============================================================

    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "today": date.today().strftime("%Y-%m-%d"),
        "stats": stats,
        "products": all_products,
        "data_sources": {
            "sku_dimensions": len(sku_dims),
            "growth_total": len(growth_products),
            "growth_eligible": len(growth_eligible),
            "self_operated_skus": len(self_products),
            "self_operated_no_notify": self_no_notify,
            "inventory": len(inventory),
            "in_transit": len(in_transit),
            "existing_spus": len(existing_spus),
            "existing_aliases": len(alias_to_spu),
            "blue_pending": len(blue_spus),
            "candidate_pool": len(candidate_pool),
            # 防呆: 上品通知表口径统计 (总行/非亚马逊排除行/通知时间为空行)
            "self_operated_total": self_diag.get('total_rows', 0),
            "self_operated_non_amz": self_diag.get('non_amz_rows', 0),
            # 防呆: 候选库有记录但最终未进跟进的SPU数(在款式表但非蓝色)
            "candidate_not_tracked": len(not_tracked),
        },
        # 防呆诊断明细(供页面「漏项诊断」展示)
        "diagnostics": {
            "notify_total_rows": self_diag.get('total_rows', 0),
            "notify_amz_rows": self_diag.get('amz_rows', 0),
            "notify_non_amz_rows": self_diag.get('non_amz_rows', 0),
            "notify_no_notify_rows": self_diag.get('no_notify_rows', 0),
            "notify_non_amz_platforms": self_diag.get('non_amz_platforms', {}),
            "notify_non_amz_spus": self_diag.get('non_amz_spus', []),
            "candidate_not_tracked": not_tracked,
        },
    }

    # ============================================================
    # 新色跟进判定 (v4) — 写入 new_color_data.json
    # 母体三态: 完全无记录 / 仅已删除 → 跳过; 有≥1非删除 → 判定
    # Amazon 基准=在售∪停售; Walmart 基准=PUBLISHED∪UNPUBLISHED
    # 新色=在仓/在途颜色 − 基准 (已删除含库存→待上架新色)
    # 套件链路: 上架SKU → 店铺+14K货号 → 子件(成员货品) = 已上架库存SKU
    # 已上架明细=「上架SKU+库存SKU」一对一行; 未上架(新色)=库存SKU 一行
    # v4 颜色档案: 判定只认系统存档(种子+云端color_dict.json);
    #   非颜色剔除 / 已确认映射规范色名 / 档案外新色码→pending_colors 待人工确认
    # ============================================================
    try:
        import new_color_module
        nc = new_color_module.compute_new_colors(str(BASE_DIR))
        nc_file = OUTPUT_DIR / "new_color_data.json"
        with open(nc_file, 'w', encoding='utf-8') as f:
            json.dump(nc, f, ensure_ascii=False, indent=2, default=str)
        result['data_sources']['new_color_spu'] = len(nc['spu_rows'])
        result['data_sources']['new_color_sku'] = len(nc['sku_rows'])
        result['data_sources']['new_color_new'] = len(nc['new_colors'])
        result['data_sources']['new_color_pending'] = len(nc.get('pending_colors', []))
        pending_n = len(nc.get('pending_colors', []))
        print(f"\n新色跟进判定完成: SPU主行 {len(nc['spu_rows'])}, SKU明细 {len(nc['sku_rows'])} 行, 待上架新色 {len(nc['new_colors'])} 行")
        print(f"  颜色待确认: {pending_n} 个色码, 未命中套件: {len(nc['warn_unmatched'])} 条, 颜色无法解析: {len(nc['warn_unparse'])} 条")
        if pending_n:
            print("  [提示] 新色跟进页面「颜色待确认」面板需人工确认后存档, 下次处理生效")
    except Exception as e:
        import traceback
        print(f"[WARNING] 新色跟进判定失败(不影响主报告): {e}")
        traceback.print_exc()

    output_file = OUTPUT_DIR / "report_data.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n{'=' * 60}")
    print("处理完成!")
    print(f"{'=' * 60}")
    print(f"\n统计:")
    print(f"  总跟进SPU: {stats['total_spu']}")
    print(f"  总SKU: {stats['total_sku']}")
    print(f"  待上架新品: {stats['blue_pending_spus']}")
    print(f"  新增SPU: {stats['new_spus']}")
    print(f"  可上架: {stats['can_launch']}")
    print(f"  不可上架(LTL): {stats['cannot_launch']}")
    print(f"  等到仓(在途): {stats['wait_arrival']}")
    print(f"  暂无需上架(无记录/无SKU): {stats['no_need']}")
    print(f"  需关注(未确认Unknown): {stats['warning']}")
    print(f"  SKU尺寸异常(同色部分缺失): {stats['spu_with_anomaly']}个SPU / {stats['dim_anomaly_skus']}个SKU")
    print(f"  多包裹SPU: {stats['multi_pkg_spus']}")
    print(f"  包裹类型分布: {stats['package_types']}")
    print(f"\n输出文件: {output_file}")

    return result


if __name__ == "__main__":
    result = process_all()
