# -*- coding: utf-8 -*-
"""
na_core.py —— 新品盘点（new-arrival）业务内核 + 共享数据源桥接
================================================================
Phase 2 Step 3：把新品盘点系统并进来。

职责
----
1. **加载业务模块**：`system/na/` 下的 process_script / new_color_module / sku_eval /
   report_script 全部是**原样拷贝**（业务口径零改动），本模块把它们 import 进来，
   再把 `BASE_DIR` 重定向到 `system/na_data/`（数据源根目录）。
2. **共享数据源桥接**：SKU&SPU 看板已经上传的 4 类相同文件，直接同步给新品盘点复用，
   用户不必在两个系统里各传一次。

为什么能共享（已用真实文件逐列核对，2026-10-21）
------------------------------------------------
| 新品盘点数据源        | 匹配 glob            | SKU&SPU 侧      | 核对结论 |
|-----------------------|----------------------|-----------------|----------|
| Walmart上架链接       | *ItemReport*.csv     | walmart 报表     | 同构：SKU / Publish Status 列名一致 |
| 套件成员货品          | *套件成员*.csv       | 原始表 raw       | **同一张表**：内部ID/ID/店铺/14K货号/成员货品… |
| 转单关系表            | 转单*.xlsx           | 转单表 ref       | 同构：增长/自营 双 sheet |
| 店铺上架链接(Amazon)  | *Listing*.xlsx       | 领星清单 lx      | 同构：A=MSKU B=状态 C=店铺，店铺名命中 AMAZON_STORE_MAP |

⚠ 款式表 **不与** 产品运营表共享
--------------------------------
新品盘点依赖款式表 **A 列底色**：蓝色 FF0A43FF = 待上架新品，它是「跟进 SPU 列表」的
第一来源（process_script.py L1054 blue_pending）。产品运营表（AMZ组/综合组-AMZ）只有
SPU/运营组/运营，**没有底色标记**，<｜hy_place▁holder▁no▁813｜>会把全部 SPU 判成「非蓝」→
spu_transfer_excluded 直接排除掉待上架新品，跟进数据整批消失。
因此款式表继续在「数据管理」单独上传。
"""
import os
import sys
import json
import time
import shutil
import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent          # system/
NA_DIR = BASE_DIR / 'na'                            # 业务模块（私有 sys.path）
NA_DATA_DIR = BASE_DIR / 'na_data'                  # 新品盘点数据根目录
NA_OUTPUT_DIR = NA_DATA_DIR / 'output'              # report_data.json 等产出
NA_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# 1. 加载业务模块（原样拷贝，仅重定向 BASE_DIR）
# ---------------------------------------------------------------------------
if str(NA_DIR) not in sys.path:
    sys.path.insert(0, str(NA_DIR))

LOAD_ERROR = None
SCRIPTS_AVAILABLE = True
SCRIPT_IMPORT_ERROR = None
process_data = None
generate_report_html = None

try:
    import cloud_store            # -> system/na/cloud_store.py（转发到 common_store）
    import sku_eval               # -> system/na/sku_eval.py
    import process_script         # -> system/na/process_script.py
    import report_script          # -> system/na/report_script.py
    import color_dict_seed        # -> system/na/color_dict_seed.py

    # ⚠ 关键：这两个模块的 BASE_DIR 原本指向自己所在目录（system/na/），
    #        必须改指向 na_data/，否则 glob 找不到数据源。
    process_script.BASE_DIR = NA_DATA_DIR
    process_script.OUTPUT_DIR = NA_OUTPUT_DIR
    report_script.BASE_DIR = NA_DATA_DIR
    report_script.OUTPUT_DIR = NA_OUTPUT_DIR

    process_data = process_script.process_all
    generate_report_html = report_script.generate_report
    SCRIPTS_AVAILABLE = True
except Exception as e:  # 不让它拖垮整个站点
    import traceback
    SCRIPTS_AVAILABLE = False
    SCRIPT_IMPORT_ERROR = f'{type(e).__name__}: {e}'
    LOAD_ERROR = traceback.format_exc()
    print('[na_core] 业务模块加载失败: %s' % SCRIPT_IMPORT_ERROR)
    print(LOAD_ERROR)


# ---------------------------------------------------------------------------
# 2. SKU&SPU 侧数据源定位（不 import app，避免循环依赖；约定与 app.py 一致）
# ---------------------------------------------------------------------------
UPLOAD_DIR = BASE_DIR / 'uploads'
REF_DIR = BASE_DIR / 'reference'
DATA_DIR = BASE_DIR / 'data'


def _meta(name):
    p = DATA_DIR / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return None


def style_table_path():
    """返回新品盘点侧「款式表」文件路径（不存在返回 None）。

    方案 A（2026-10-27 用户确认）：款式表尾部含「AMZ组」「综合组-AMZ」两个 sheet，
    列结构与产品运营表同构（SPU / 新SPU* / 运营 / 运营组），
    故清洗侧的运营、运营组查询在未单独上传「产品运营表」时直接沿用款式表，避免双份维护。
    """
    if not NA_DATA_DIR.exists():
        return None
    cands = sorted(p for p in NA_DATA_DIR.glob('款式表*.xlsx') if not p.name.startswith('~$'))
    return cands[-1] if cands else None


def sku_source_local_path(kind):
    """返回 SKU&SPU 侧某类数据源的本地留档路径（不存在返回 None）。

    kind: raw / lx(领星) / wm(walmart) / ref(转单) / ops(产品运营表)
    本地磁盘没有时，尝试从 SKU&SPU 的统一存储层拉回来（OSS/Supabase）。
    """
    fallback = None
    if kind == 'raw':
        local, fallback = UPLOAD_DIR / 'latest_raw.csv', 'raw/latest_raw.csv'
    elif kind == 'lx':
        local, fallback = UPLOAD_DIR / 'latest_lingxing.xlsx', 'lx/latest_lingxing.xlsx'
    elif kind == 'wm':
        local, fallback = UPLOAD_DIR / 'latest_walmart.csv', 'wm/latest_walmart.csv'
    elif kind == 'temu_a':
        local, fallback = UPLOAD_DIR / 'latest_temu_a.xlsx', 'temu/latest_temu_a'
    elif kind == 'temu_b':
        local, fallback = UPLOAD_DIR / 'latest_temu_b.xlsx', 'temu/latest_temu_b'
    elif kind == 'ops':
        local, fallback = UPLOAD_DIR / 'latest_ops.xlsx', 'ops/latest_ops'
    elif kind == 'admin':
        # 管理员检查表：清洗侧留档 xlsx/csv 二选一（桥推送后与主看板同格式）
        local, fallback = UPLOAD_DIR / 'latest_admin.xlsx', 'admin/latest_admin'
        if not local.exists() and (UPLOAD_DIR / 'latest_admin.csv').exists():
            local = UPLOAD_DIR / 'latest_admin.csv'
    elif kind == 'ref':
        m = _meta('reference_meta.json')
        if m and m.get('stored'):
            p = REF_DIR / m['stored']
            if p.exists():
                return p
        cands = sorted(n for n in os.listdir(REF_DIR)
                       if n.lower().endswith(('.xlsx', '.xlsm')) and not n.startswith('~$')) \
            if REF_DIR.exists() else []
        return (REF_DIR / cands[-1]) if cands else None
    else:
        return None

    if local.exists():
        return local
    # 本地缺失（换机/冷启动后）→ 从云端拉取并落到标准留档路径
    try:
        import common_store as storage
        if storage.ds_file_exists(fallback):
            data = storage.ds_file_download(fallback)
            if data:
                UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                local.write_bytes(data)
                return local
    except Exception as e:
        print('[na_core] 云端取 %s 失败: %s' % (kind, e))
    return None


# ---------------------------------------------------------------------------
# 3. 共享数据源登记表
# ---------------------------------------------------------------------------
# target 文件名必须命中新品盘点侧的 glob 规则；kind 指 SKU&SPU 侧数据源
SHARED_SOURCES = [
    {'na_id': 'walmart', 'label': 'Walmart上架链接', 'pattern': '*ItemReport*.csv',
     'target': 'ItemReport_共享.csv', 'kind': 'wm',
     'src_desc': 'SKU&SPU 看板 · walmart 后台报表'},
    {'na_id': 'suite', 'label': '套件成员货品', 'pattern': '*套件成员*.csv',
     'target': 'SKU平台信息套件成员货品体现_共享.csv', 'kind': 'raw',
     'src_desc': 'SKU&SPU 看板 · 原始表（同一张表）'},
    {'na_id': 'transfer', 'label': '转单关系表', 'pattern': '转单*.xlsx',
     'target': '转单_共享.xlsx', 'kind': 'ref',
     'src_desc': 'SKU&SPU 看板 · 转单表'},
    {'na_id': 'listing', 'label': '店铺上架链接(Amazon)', 'pattern': '*Listing*.xlsx',
     'target': 'Amazon共享Listing.xlsx', 'kind': 'lx',
     'src_desc': 'SKU&SPU 看板 · 领星清单'},
    {'na_id': 'temu_a', 'label': 'Temu报表(A类·含店铺列)', 'pattern': '*temu_a*.xlsx',
     'target': 'Temu_A类报表_共享.xlsx', 'kind': 'temu_a',
     'src_desc': 'SKU&SPU 看板 · Temu A 类报表'},
    {'na_id': 'temu_b', 'label': 'Temu报表(B类·固定COZY店)', 'pattern': '*temu_b*.xlsx',
     'target': 'Temu_B类报表_共享.xlsx', 'kind': 'temu_b',
     'src_desc': 'SKU&SPU 看板 · Temu B 类报表'},
    # ---- 2026-10-29 入口统一：清洗侧这两类改到系统设置上传，正向同步仅作历史兜底 ----
    {'na_id': 'admin', 'label': '所有库存货品管理员检查表', 'pattern': '*管理员检查表*.xlsx',
     'target': '管理员检查表_共享.xlsx', 'kind': 'admin',
     'src_desc': 'SKU&SPU 看板 · 管理员检查表（品类）'},
    {'na_id': 'ops', 'label': '产品运营表', 'pattern': '*产品运营表*.xlsx',
     'target': '产品运营表_共享.xlsx', 'kind': 'ops',
     'src_desc': 'SKU&SPU 看板 · 产品运营表'},
]

_SHARED_MANIFEST = NA_DATA_DIR / '_shared_manifest.json'


def _read_manifest():
    try:
        return json.loads(_SHARED_MANIFEST.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _write_manifest(m):
    try:
        _SHARED_MANIFEST.write_text(json.dumps(m, ensure_ascii=False, indent=1),
                                    encoding='utf-8')
    except Exception:
        pass


def shared_status():
    """返回每个共享源的当前状态（给「数据管理」页展示）。"""
    man = _read_manifest()
    out = []
    for s in SHARED_SOURCES:
        tgt = NA_DATA_DIR / s['target']
        rec = man.get(s['na_id'], {})
        out.append({
            'na_id': s['na_id'],
            'label': s['label'],
            'src_desc': s['src_desc'],
            'target': s['target'],
            'synced': tgt.exists(),
            'synced_at': rec.get('synced_at', ''),
            'src_name': rec.get('src_name', ''),
            'src_mtime': rec.get('src_mtime', ''),
            'size_mb': (round(tgt.stat().st_size / 1048576, 2) if tgt.exists() else 0),
            'error': rec.get('error', ''),
        })
    return out


def sync_shared_sources(force=False):
    """把 SKU&SPU 侧已上传的共享文件同步到 na_data/。

    - 按「源文件 mtime+size」判新，源文件没变就跳过（避免每次重算都拷 6MB）。
    - 任一源缺失/失败不影响其它源，状态写进 _shared_manifest.json。
    - force=True 无视时间戳强制重拷。
    返回 (synced_count, [细项 dict])
    """
    man = _read_manifest()
    results = []
    synced = 0
    for s in SHARED_SOURCES:
        item = {'na_id': s['na_id'], 'label': s['label'], 'target': s['target'],
                'ok': False, 'msg': '', 'bytes': 0}
        try:
            src = sku_source_local_path(s['kind'])
            if not src or not Path(src).exists():
                item['msg'] = 'SKU&SPU 侧尚未上传该数据源'
                man[s['na_id']] = {'error': item['msg']}
                results.append(item)
                continue
            src = Path(src)
            st = src.stat()
            sig = '%.3f|%d' % (st.st_mtime, st.st_size)
            tgt = NA_DATA_DIR / s['target']
            prev = man.get(s['na_id'], {})
            if (not force) and tgt.exists() and prev.get('sig') == sig:
                item['ok'] = True
                item['msg'] = '无变化，沿用上次同步'
                item['bytes'] = tgt.stat().st_size
                synced += 1
                results.append(item)
                continue
            shutil.copy2(src, tgt)
            now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            man[s['na_id']] = {'sig': sig, 'synced_at': now_str, 'src_name': src.name,
                               'src_mtime': datetime.datetime.fromtimestamp(
                                   st.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                               'error': ''}
            item['ok'] = True
            item['msg'] = '已同步'
            item['bytes'] = tgt.stat().st_size
            synced += 1
        except Exception as e:
            item['msg'] = f'{type(e).__name__}: {e}'
            man[s['na_id']] = {'error': item['msg']}
        results.append(item)
    _write_manifest(man)
    return synced, results


if __name__ == '__main__':
    print('na_core: SCRIPTS_AVAILABLE=%s' % SCRIPTS_AVAILABLE)
    print('na_core: na_data=%s' % NA_DATA_DIR)
    n, rs = sync_shared_sources()
    print('na_core: shared synced=%d/%d' % (n, len(SHARED_SOURCES)))
    for r in rs:
        print('   -', r)
