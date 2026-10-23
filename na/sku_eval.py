# -*- coding: utf-8 -*-
"""SKU 异常判定 + 上架结论重算 —— process_script 与 web_app 共用。

为什么单独放一个模块:
  1) process_script 依赖 pandas, web_app 不应该为了重算去 import 它;
  2) 运营确认「无效SKU」后需要**立刻**看到结果, 而不是等后台约 6 分钟重跑,
     所以 web_app 读取报表时要能用同一套口径重算一遍。
本模块只依赖标准库。

异常分两类 (2026-10-20 新增第②类):
  ① dim_partial 同色部分缺尺寸: 同颜色分组里「有尺寸的」和「缺尺寸的」同时存在 → 缺尺寸的那个是异常,
    会被移出 clean_skus, 不参与包裹类型划分。
  ② no_data   无任何尺寸记录: 包名为 Unknown 但不满足①条件的 SKU (典型是 -0000AL 这种解析/收录都成问题的野SKU)。
    它**留在 clean_skus 里**, 因此会把 has_unknown 顶成 True, 让整个 SPU 变成 warning ——
    而它自己又不出现在异常清单里, 运营完全看不见它。这就是「隐形拦路虎」, 第②类就是为了让它们现形。
"""
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ANOM_DIM_PARTIAL = 'dim_partial'
ANOM_NO_DATA = 'no_data'

ANOM_TYPE_LABEL = {
    ANOM_DIM_PARTIAL: '同色部分缺尺寸',
    ANOM_NO_DATA: '无任何尺寸记录',
}

NOTE_FILENAME = 'sku_anomaly_note.json'
STATUS_VALID = 'valid'
STATUS_INVALID = 'invalid'
STATUS_COMBO = 'combo'
STATUS_PENDING = ''

STATUS_LABEL = {
    STATUS_VALID: '正常SKU',
    STATUS_INVALID: '无效SKU',
    STATUS_COMBO: '组合SKU',
    STATUS_PENDING: '待确认',
}

# 确认面板可选项 (顺序即按钮顺序)
CONFIRM_OPTIONS = [STATUS_VALID, STATUS_INVALID, STATUS_COMBO]


# ============================================================
# 基础解析 (从 process_script.py 迁移, 保持完全相同的行为)
# ============================================================
def parse_sku_color_suffix(sku, spu):
    """从SKU中提取颜色和可选后缀(A/B/C)
    返回: (color, suffix or None)
    """
    if sku == spu or not spu:
        return '', None
    if sku.startswith(spu + '-'):
        remainder = sku[len(spu) + 1:]
    elif sku.startswith(spu):
        remainder = sku[len(spu):].lstrip('-')
    else:
        return '', None

    if not remainder:
        return '', None

    parts = remainder.split('-')
    # 尺寸段(KB/QB/TB)剥离: SPU 边界修正后尺寸段不再进 SPU,
    # 颜色也应回归真实色名 (BDJS0893-KB-CRL → CRL), 两侧同口径不受影响
    if len(parts) >= 2 and parts[0].strip().upper() in ('KB', 'QB', 'TB'):
        parts = parts[1:]
    last = parts[-1].upper()
    if len(last) == 1 and last in 'ABCDE':
        color = '-'.join(parts[:-1]) if len(parts) > 1 else ''
        return color, last
    else:
        return '-'.join(parts), None


def detect_dim_anomalies(sku_details, spu):
    """数据清洗: 同SPU同颜色下, 若部分SKU缺重量/尺寸(Unknown),
    这些SKU标记为尺寸异常, 从SPU包裹类型划分中排除, 单独记录。

    返回: (clean, anomalies_dim_partial, wild_no_data)
      clean                 参与包裹划分/统计的干净SKU
      anomalies_dim_partial ①类: 同色部分缺失 (原有逻辑)
      wild_no_data          ②类: 留在 clean 里的 Unknown SKU (隐形拦路虎)
    """
    color_groups = defaultdict(list)
    for s in sku_details:
        color, suffix = parse_sku_color_suffix(s['sku'], spu)
        if color:
            color_groups[color].append(s)

    clean = []
    anomalies = []
    for s in sku_details:
        color, suffix = parse_sku_color_suffix(s['sku'], spu)
        if not color:
            # 无法解析颜色, 无法判定是否异常, 保留为正常SKU
            clean.append(s)
            continue
        group = color_groups[color]
        has_present = any(g['package_type'] != 'Unknown' for g in group)
        has_absent = any(g['package_type'] == 'Unknown' for g in group)
        if has_present and has_absent and s['package_type'] == 'Unknown':
            anomalies.append(s)
        else:
            clean.append(s)

    anomaly_names = {s['sku'] for s in anomalies}
    wild = [s for s in clean
            if s['package_type'] == 'Unknown' and s['sku'] not in anomaly_names]
    return clean, anomalies, wild


def detect_multi_package(skus, spu, sku_to_pkg_type=None):
    """检测多包裹: 按颜色分组, 取最大包裹数
    增强版: 同时返回每个包裹的FedEx类型和展示字符串
    返回: (max_package_count, color_details dict, display_str, spu_max_type)
    """
    type_rank = {'LTL': 4, 'Oversize': 3, 'AHS-Dimension': 2, 'Standard': 1, 'Unknown': 0}
    type_short = {'LTL': 'LTL', 'Oversize': 'Oversize', 'AHS-Dimension': 'AHS',
                  'Standard': 'Standard', 'Unknown': 'Unknown'}

    groups = {}
    for sku in skus:
        color, suffix = parse_sku_color_suffix(sku, spu)
        if not color:
            continue
        key = (color, suffix or 'SINGLE')
        if key not in groups:
            groups[key] = []
        if sku_to_pkg_type:
            groups[key].append(sku_to_pkg_type.get(sku, 'Unknown'))
        else:
            groups[key].append('Unknown')

    color_details = {}
    color_packages = {}
    for (color, suffix), types in groups.items():
        if color not in color_packages:
            color_packages[color] = []
        max_type = max(types, key=lambda t: type_rank.get(t, 0))
        color_packages[color].append(max_type)

    max_packages = 1
    for color, pkg_types in color_packages.items():
        count = len(pkg_types)
        color_details[color] = {'count': count, 'package_types': pkg_types}
        max_packages = max(max_packages, count)

    if not color_packages:
        if sku_to_pkg_type:
            all_types = [sku_to_pkg_type.get(sku, 'Unknown') for sku in skus]
        else:
            all_types = ['Unknown']
        spu_max_type = max(all_types, key=lambda t: type_rank.get(t, 0)) if all_types else "Unknown"
        display = f"单包{type_short.get(spu_max_type, spu_max_type)}"
    else:
        max_color = max(color_packages.keys(), key=lambda c: len(color_packages[c]))
        max_color_types = color_packages[max_color]

        if len(max_color_types) == 1:
            display = f"单包{type_short.get(max_color_types[0], max_color_types[0])}"
        else:
            type_counts = {}
            for t in max_color_types:
                short = type_short.get(t, t)
                type_counts[short] = type_counts.get(short, 0) + 1
            _rev = {'LTL': 'LTL', 'Oversize': 'Oversize', 'AHS': 'AHS-Dimension',
                    'Standard': 'Standard', 'Unknown': 'Unknown'}
            sorted_types = sorted(type_counts.items(),
                                  key=lambda x: type_rank.get(_rev.get(x[0], 'Unknown'), 0),
                                  reverse=True)
            parts = [f"{cnt}包{t}" for t, cnt in sorted_types]
            display = "，".join(parts)

        all_types = [t for types in color_packages.values() for t in types]
        spu_max_type = max(all_types, key=lambda t: type_rank.get(t, 0)) if all_types else "Unknown"

    return max_packages, color_details, display, spu_max_type


# ============================================================
# 归档: SKU 异常确认结果
# ============================================================
def default_note_path():
    try:
        return Path(__file__).resolve().parent / 'output' / NOTE_FILENAME
    except Exception:
        return Path('output') / NOTE_FILENAME


def read_note_file(path=None):
    """读取本机 sku_anomaly_note.json 镜像"""
    try:
        p = Path(path) if path else default_note_path()
        with open(p, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def invalid_sku_set(note):
    """从归档里取出已确认「无效SKU」的 SKU 集合"""
    return {k for k, v in (note or {}).items()
            if isinstance(v, dict) and v.get('status') == STATUS_INVALID}


# ============================================================
# 重算: 剔除已确认无效SKU后, 还原该SPU的全部派生结论
# ============================================================
def _earliest_transit_date(transit_info):
    best = None
    for t in transit_info or []:
        if not isinstance(t, dict):
            continue
        raw = t.get('arrival_date') or t.get('eta') or t.get('date')
        if not raw:
            continue
        txt = str(raw)[:10]
        try:
            dt = datetime.strptime(txt, '%Y-%m-%d')
        except ValueError:
            continue
        if best is None or dt < best:
            best = dt
    return best.strftime('%Y-%m-%d') if best else None


def evaluate_inventory(skus):
    """按剩余SKU重算 SPU 级库存状态。
    口径与 evaluate_spu_inventory 一致: 有在库 > 在途 > 无记录。
    返回: (status, earliest_eta_str or None)
    """
    if any((s.get('in_stock') or 0) > 0 for s in skus):
        return 'in_stock', None
    earliest = None
    has_transit = False
    for s in skus:
        info = s.get('in_transit') or []
        if info:
            has_transit = True
        e = _earliest_transit_date(info)
        if e and (earliest is None or e < earliest):
            earliest = e
    if has_transit:
        return 'in_transit', earliest
    return 'no_record', None


def relaunch_status(has_ltl, all_found, has_unconfirmed_unknown, inv_status, earliest_eta):
    """上架可行性判定链 (四态: 需要上架 / 不可上架 / 待确认 / 暂无需上架)

    规则 (2026-10-21 用户确认重构):
      ① 任意SKU包裹类型 = LTL                               → 不可上架
      ② 找不到任何SKU记录 (all_found=False)                  → 暂无需上架
      ③ 存在未确认(confirm_status 为空)的 Unknown 包裹类型SKU → 待确认
      ④ 全部确定且无LTL, 有在库/在途                         → 需要上架
      ⑤ 全部确定且无LTL, 全部无在库/在途                     → 暂无需上架
    确认「正常SKU」解除③的阻塞(按库存判, 无需补尺寸); 「无效SKU」已在 recompute/process 中剔除。
    """
    if not all_found:
        return 'no_need', '未找到任何匹配SKU记录'
    if has_ltl:
        return 'cannot_launch', '含LTL包裹类型SKU,部门无LTL资源'
    if has_unconfirmed_unknown:
        return 'warning', '存在包裹类型为Unknown的SKU,需逐SKU确认'
    if inv_status == 'in_stock':
        return 'can_launch', '库存已到仓'
    if inv_status == 'in_transit':
        return 'wait_arrival', '在途中,预计到仓: ' + (earliest_eta or '未知')
    return 'no_need', '全部SKU均无在库/在途库存'


def need_launch_label(status):
    """列「SPU是否需要上架」只表达**四态决策**: 需要上架 / 不可上架 / 待确认 / 暂无需上架。

    真实原因不在这一列承担 —— 移到列「上架说明」(launch_reason, 由 need_launch_reason 产出)。
    这样决策列保持干净可筛选(4项), 细节在说明列看, 解决了原来 8 态碎片化的问题。

    can_launch 与 wait_arrival 同为「需要上架」; cannot_launch→「不可上架」;
    warning(未确认Unknown)→「待确认」; no_need(无SKU记录/全无库存)→「暂无需上架」。
    """
    if status in ('can_launch', 'wait_arrival'):
        return '需要上架'
    if status == 'cannot_launch':
        return '不可上架'
    if status == 'warning':
        return '待确认'
    if status == 'no_need':
        return '暂无需上架'
    return ''


def need_launch_reason(status, reason, pending_anomaly=True, anomaly_count=0):
    """列「上架说明」的详细原因, 与四态 need_launch_label 配套。

    - can_launch/wait_arrival/cannot_launch: 保留 relaunch_status 给的真实原因(已到仓/在途/无LTL资源)
    - no_need: 无SKU记录 / 全部SKU无在库在途 → 暂无需上架
    - warning(未确认Unknown):
        * 未找到匹配SKU   → 未找到匹配SKU,需人工确认
        * 无库存记录      → 无库存记录
        * 其他(默认)      → 存在包裹类型为Unknown的SKU,需逐SKU确认,共N个（待确认 / 已确认无效,待补尺寸）
    anomaly_count 仅用于 warning 时显示条数; 其他状态异常由「SKU异常记录」列承载。
    """
    reason = reason or ''
    if status == 'can_launch':
        return '库存已到仓'
    if status == 'wait_arrival':
        return reason or '在途中'
    if status == 'cannot_launch':
        return reason or '含LTL包裹,无LTL资源'
    if status == 'no_need':
        return reason or '暂无需上架'
    if status == 'warning':
        if '未找到匹配SKU' in reason:
            return '未找到匹配SKU,需人工确认'
        if '无库存记录' in reason:
            return '无库存记录'
        tail = '（待确认）' if pending_anomaly else '（已确认无效,待补尺寸）'
        count_txt = ('共%d个%s' % (anomaly_count, tail)) if anomaly_count else ''
        base = '存在包裹类型为Unknown的SKU,需逐SKU确认'
        return base + (',' + count_txt if count_txt else '')
    return reason


def build_anomaly_records(sku_details, spu, clean, anomalies, wild, note=None):
    """把两类异常合成统一结构, 并附上已归档的确认结果"""
    note = note or {}
    out = []
    for bucket, atype in ((wild, ANOM_NO_DATA), (anomalies, ANOM_DIM_PARTIAL)):
        for s in bucket:
            rec = {k: v for k, v in s.items() if k != 'is_dim_anomaly'}
            rec['anomaly_type'] = atype
            rec['anomaly_type_label'] = ANOM_TYPE_LABEL.get(atype, atype)
            rec['package_type'] = s.get('package_type') or 'Unknown'
            rec['inventory_status'] = s.get('inventory_status') or 'no_record'
            rec['in_stock'] = s.get('in_stock') or 0
            rec['in_transit'] = s.get('in_transit') or []
            n = note.get(s['sku'])
            n = n if isinstance(n, dict) else {}
            rec['confirm_status'] = n.get('status') or STATUS_PENDING
            rec['confirm_status_label'] = STATUS_LABEL.get(rec['confirm_status'], '待确认')
            rec['confirm_note'] = n.get('note') or ''
            rec['confirm_updated_at'] = n.get('updated_at') or ''
            rec['confirm_updated_by'] = n.get('updated_by') or ''
            out.append(rec)
    out.sort(key=lambda r: (r['anomaly_type'] != ANOM_NO_DATA, r['sku']))
    return out


def recompute_product(p, invalid, note=None):
    """按归档确认结果重算该SPU的全部派生字段。返回新的 dict。

    需求4/5(2026-10-22)重构:
      - 已确认「无效SKU」不再从明细中剔除消失, 而是单独进入 np_['invalid_skus'] 列展示;
      - 已确认「组合SKU」的包裹类型/库存判定由其成员货品(套件成员)接管:
        该SKU自身的 Unknown 不计入评估集, 改由其 kit_members(处理阶段已解析并存档)参与;
      - 评估集 eval_skus = 正常SKU + 组合SKU的成员(去重), 无效SKU 完全排除;
      - np_['combo_skus'] 每行带 members(二级折叠展示成员货品数据)。

    注意: 上架DDL 不重算 —— DDL 由后台 process 沿用历史, 此处重算只影响判定/库存/包裹。
    """
    note = note or {}
    spu = p.get('spu') or ''
    invalid = invalid or set()

    # 全量SKU: 正常 + 无效 + 组合 三个分栏的明细汇总 (含各自的 kit_members), confirm_status 以 note 为准。
    # 注意: 处理阶段已把组合/无效SKU从 p['skus'] 拆分到各自分栏, 这里必须回并, 否则组合SKU的成员无法接管判定。
    base = list(p.get('skus') or [])
    base += list(p.get('invalid_skus') or [])
    base += list(p.get('combo_skus') or [])
    all_skus = []
    seen = set()
    for s in base:
        sku = s.get('sku')
        if sku in seen:      # 防重复(重复确认/历史结构残留) → 只保留首条, 避免 SKU数重复计数
            continue
        seen.add(sku)
        item = {k: v for k, v in s.items() if k != 'is_dim_anomaly'}
        st = (note.get(sku) or {}).get('status') or STATUS_PENDING
        item['confirm_status'] = st
        all_skus.append(item)

    invalid_skus = [s for s in all_skus if s['confirm_status'] == STATUS_INVALID]
    combo_skus = [s for s in all_skus if s['confirm_status'] == STATUS_COMBO]
    normal_skus = [s for s in all_skus
                   if s['confirm_status'] not in (STATUS_INVALID, STATUS_COMBO)]

    # 评估集: 正常SKU + 组合SKU成员(去重, 组合自身Unknown不计入)
    normal_names = {s['sku'] for s in normal_skus}
    eval_skus = list(normal_skus)
    for c in combo_skus:
        for m in (c.get('kit_members') or []):
            mn = m.get('sku')
            if mn and mn not in normal_names and not any(e['sku'] == mn for e in eval_skus):
                eval_skus.append(m)

    clean, anomalies, wild = detect_dim_anomalies(normal_skus, spu)
    anomaly_names = {s['sku'] for s in anomalies}
    for s in normal_skus:
        s['is_dim_anomaly'] = s['sku'] in anomaly_names

    has_ltl = any(s.get('package_type') == 'LTL' for s in eval_skus)
    all_found = len(eval_skus) > 0
    # 是否有 Unknown 包裹类型 SKU (仅标记用, 仅正常SKU; 供 has_unknown_dim 展示)
    has_unknown = any(s.get('package_type') == 'Unknown' for s in clean)
    # 阻塞「待确认」的 Unknown: clean 中包裹类型=Unknown 且未确认(confirm_status 为空)
    # 已确认「无效SKU」已从评估集排除; 已确认「正常SKU/组合SKU」视为已确定(解除阻塞, 按库存判)
    has_unconfirmed_unknown = any(
        s.get('package_type') == 'Unknown'
        and not (note.get(s['sku'], {}) or {}).get('status')
        for s in clean
    )

    inv_status, earliest_eta = evaluate_inventory(eval_skus)
    launch_status, launch_reason = relaunch_status(
        has_ltl, all_found, has_unconfirmed_unknown, inv_status, earliest_eta)

    records = build_anomaly_records(normal_skus, spu, clean, anomalies, wild, note)
    # 异常面板只展示「待确认」(confirm_status 为空)的记录, 已确认的移至各自分栏
    records = [r for r in records if not r['confirm_status']]
    pending = [r for r in records if not r['confirm_status']]
    launch_reason = need_launch_reason(
        launch_status, launch_reason,
        pending_anomaly=bool(pending), anomaly_count=len(records))

    sku_to_pkg = {s['sku']: (s.get('package_type') or 'Unknown') for s in clean}
    mpc, cpd, disp, maxt = detect_multi_package(
        [s['sku'] for s in clean], spu, sku_to_pkg)

    np_ = dict(p)
    np_['skus'] = normal_skus
    np_['invalid_skus'] = invalid_skus
    np_['combo_skus'] = [{**c, 'members': c.get('kit_members') or []} for c in combo_skus]
    np_['sku_anomalies'] = records
    np_['dim_anomaly_count'] = len([r for r in records
                                    if r['anomaly_type'] == ANOM_DIM_PARTIAL])
    np_['wild_anomaly_count'] = len([r for r in records
                                     if r['anomaly_type'] == ANOM_NO_DATA])
    np_['anomaly_count'] = len(records)
    np_['multi_package_count'] = mpc
    np_['multi_package_display'] = disp
    np_['spu_package_type'] = maxt
    np_['color_package_details'] = cpd
    np_['spu_inventory_status'] = inv_status
    np_['earliest_eta'] = earliest_eta
    np_['launch_status'] = launch_status
    np_['launch_reason'] = launch_reason
    np_['has_ltl'] = has_ltl
    np_['has_unknown_dim'] = has_unknown
    np_['need_launch_label'] = need_launch_label(launch_status)
    np_['sku_count_total'] = len(all_skus)

    info = {
        'total_sku': len(all_skus),
        'clean_pkg_counter': {t: sum(1 for s in clean
                                     if (s.get('package_type') or 'Unknown') == t)
                              for t in {s.get('package_type') or 'Unknown' for s in clean}},
        'launch_status': launch_status,
        'anomaly': len(records),
        'multi': 1 if mpc > 1 else 0,
    }
    return np_, info


def apply_confirmations(data, note=None, recompute_all=True):
    """按归档的确认结果重算整份报表。空归档时应当与原报表完全一致。

    data  -- report_data.json 解析出的 dict (会被就地修改并返回)
    note  -- {sku: {status, note, updated_at, updated_by}}
    """
    if not isinstance(data, dict):
        return data
    note = note or {}
    invalid = invalid_sku_set(note)
    products = data.get('products') or []

    new_products = []
    agg = {'total_sku': 0, 'can_launch': 0, 'wait_arrival': 0, 'warning': 0,
           'cannot_launch': 0, 'no_record': 0, 'no_need': 0, 'multi_pkg_spus': 0,
           'anomaly_records': 0, 'spu_with_anomaly': 0}
    pkg_counter = {}

    for p in products:
        try:
            np_, info = recompute_product(p, invalid, note)
        except Exception:
            np_, info = p, None
        new_products.append(np_)
        if not info:
            continue
        agg['total_sku'] += info['total_sku']
        if info['launch_status'] in agg:
            agg[info['launch_status']] += 1
        agg['multi_pkg_spus'] += info['multi']
        agg['anomaly_records'] += info['anomaly']
        if info['anomaly']:
            agg['spu_with_anomaly'] += 1
        for t, c in info['clean_pkg_counter'].items():
            pkg_counter[t] = pkg_counter.get(t, 0) + c

    data['products'] = new_products
    data['sku_anomaly_note'] = note

    stats = data.get('stats')
    if isinstance(stats, dict) and new_products:
        stats['total_sku'] = agg['total_sku']
        stats['can_launch'] = agg['can_launch']
        stats['wait_arrival'] = agg['wait_arrival']
        stats['warning'] = agg['warning']
        stats['cannot_launch'] = agg['cannot_launch']
        stats['no_record'] = agg['no_record']
        stats['no_need'] = agg['no_need']
        stats['multi_pkg_spus'] = agg['multi_pkg_spus']
        stats['anomaly_records'] = agg['anomaly_records']
        # 指标卡「SKU异常」= 异常清单总条数; 确认无效的 SKU 已被剔除, 所以这里会同步减少
        stats['dim_anomaly_skus'] = agg['anomaly_records']
        stats['spu_with_anomaly'] = agg['spu_with_anomaly']
        stats['package_types'] = pkg_counter
        try:
            stats['total_spu'] = len(new_products)
        except Exception:
            pass
    return data
