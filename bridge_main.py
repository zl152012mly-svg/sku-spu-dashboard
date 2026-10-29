# -*- coding: utf-8 -*-
"""
bridge_main.py —— 系统设置(数据源管理) → SKU&SPU 清洗看板 反向同步桥
================================================================
2026-10-29 需求：所有面板的数据上传入口统一到「系统设置 → 数据源管理」。

背景（为什么要这个模块）
------------------------
此前两套存储完全独立：
  - 系统设置（NA 体系）上传 → system/na_data/ + 新品云端桶（common_store BUCKET_UPLOADS）
  - 清洗看板上传           → system/uploads/latest_* + system/data/*_meta.json + 清洗云端桶
且只有「清洗看板 → 新品/新色」的正向同步（na_core.sync_shared_sources），
用户在系统设置上传的共享文件清洗看板完全感知不到（看板「当前生效」不变）。

本模块把 na_data/ 里已上传的共享数据源按**清洗看板上传接口的同款口径**推送：
  1) 同款解析校验（pipeline 的 loader；坏文件当场报错，不污染看板）
  2) 原子写留档 latest_*（与 /api/upload/* 完全一致的路径与文件名）
  3) 写 data/*_meta.json（标注 source='console'）→ 驱动看板 /api/status 的
     *_stale「数据源已更新，需重算」提示（2026-10-29 用户拍板：手动重算生效）
  4) 云端 storage.ds_file_upload(OBJ_*)（与看板上传同一云端对象键，冷启动恢复链路一致）
  5) storage.ds_update 共享元信息（字段与 app._sync_source_meta 对齐）

注意：不 import app（避免循环依赖）；路径/对象键与 app.py 常量逐一对应，改动时两边同步。
转单表（ref）沿用历史版本机制（转单_<时间戳>.xlsx + reference_meta.json）。
"""
import os
import csv
import json
import datetime
import openpyxl

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')
DATA_DIR = os.path.join(BASE_DIR, 'data')
REF_DIR = os.path.join(BASE_DIR, 'reference')

# ---- 与 app.py 的常量逐一对应（勿单方面改动）----
LATEST_RAW = os.path.join(UPLOAD_DIR, 'latest_raw.csv')
LATEST_LINGXING = os.path.join(UPLOAD_DIR, 'latest_lingxing.xlsx')
LATEST_WALMART = os.path.join(UPLOAD_DIR, 'latest_walmart.csv')
LATEST_ADMIN_XLSX = os.path.join(UPLOAD_DIR, 'latest_admin.xlsx')
LATEST_ADMIN_CSV = os.path.join(UPLOAD_DIR, 'latest_admin.csv')
LATEST_OPS = os.path.join(UPLOAD_DIR, 'latest_ops.xlsx')
OPS_REMOVED_FLAG = os.path.join(DATA_DIR, 'ops_removed.json')
LATEST_TEMU_A = os.path.join(UPLOAD_DIR, 'latest_temu_a.xlsx')
LATEST_TEMU_B = os.path.join(UPLOAD_DIR, 'latest_temu_b.xlsx')
RAW_META = os.path.join(DATA_DIR, 'raw_meta.json')
LINGXING_META = os.path.join(DATA_DIR, 'lingxing_meta.json')
WALMART_META = os.path.join(DATA_DIR, 'walmart_meta.json')
ADMIN_META = os.path.join(DATA_DIR, 'admin_meta.json')
OPS_META = os.path.join(DATA_DIR, 'ops_meta.json')
TEMU_META = os.path.join(DATA_DIR, 'temu_meta.json')
REF_META = os.path.join(DATA_DIR, 'reference_meta.json')

_SRC_TAG = '系统设置同步'   # 写进 meta.source，看板可识别该版本来自数据源管理


# ============================================================
# 解析校验（与 app.py 各上传接口同口径；校验失败 raise ValueError）
# ============================================================
def summarize_raw(path):
    """原始表 csv 轻量校验 —— 与 app._summarize_raw 同口径。"""
    from pipeline import REQUIRED_COLS
    with open(path, encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError('原始表格为空')
        missing = [c for c in REQUIRED_COLS if c not in header]
        if missing:
            raise ValueError('原始表格缺少必要列：' + '、'.join(missing))
        rows = sum(1 for row in reader if any(str(c).strip() for c in row))
    return {'rows': rows, 'columns': len(header)}


def _load_and_check(loader, path, empty_msg):
    """调用 pipeline loader，返回 (match_map, summary)；空结果报错。"""
    match_map, summary = loader(path)
    if not match_map:
        raise ValueError(empty_msg)
    return summary


def validate_lingxing(path):
    from pipeline import load_lingxing
    return _load_and_check(load_lingxing, path,
                           '领星清单未匹配到任何可用的 (店铺,MSKU) 记录（检查「店铺」是否在映射表内）')


def validate_walmart(path):
    from pipeline import load_walmart
    return _load_and_check(load_walmart, path, 'walmart 报表未解析到任何 SKU 记录')


def validate_admin(path):
    from pipeline import load_admin_check
    return _load_and_check(load_admin_check, path,
                           '管理员检查表未匹配到任何 SKU（检查是否有「唯一SKU」或「名称」列）')


def validate_ops(path):
    """产品运营表 —— 与 app.api_upload_ops 同口径：load_ops + 运营 sheet 结构校验。"""
    from pipeline import load_ops
    ops_map = load_ops(path)
    ok, msg, _detail = validate_ops_book(path)
    if not ok:
        raise ValueError(msg)
    if not ops_map:
        raise ValueError('产品运营表未解析到任何含「SPU」列的 sheet')
    return {'sheets': sorted(ops_map.keys()),
            'rows': sum(len(v) for v in ops_map.values())}


def validate_ops_book(path):
    """产品运营表结构校验 —— 与 app._validate_ops_book 同口径。

    要求文件中至少含一个「AMZ组 / 综合组-AMZ」这类 sheet（方案 A 后款式表也可作为
    来源，因此不凭「上架汇总」sheet 拒收）。返回 (ok, msg, detail)。
    """
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = [str(s).strip() for s in wb.sheetnames]
    finally:
        wb.close()
    detail = {'sheets': sheets}
    ops_like = [s for s in sheets
                if ('综合组' in s) or ('AMZ' in s.upper() and '组' in s)]
    if not ops_like:
        return False, ('未找到运营归属可用的 sheet（当前 sheet：%s）。需要包含「AMZ组」或「综合组-AMZ」'
                       '这类 sheet（用于 Urban→综合组-AMZ、HLLdeco/HomeNest→AMZ组 的分店铺查询）。'
                       % ('、'.join(sheets[:6]) or '空')), detail
    detail['ops_sheets'] = ops_like
    return True, '', detail


def validate_temu(path):
    """Temu 报表 —— 与 app.api_upload_temu 同口径：temu_file_types + load_temu。

    返回 (types, rows)；坏结构/空数据 raise。
    """
    from pipeline import temu_file_types, load_temu
    types = temu_file_types(path) or {'B'}      # 判不出类型（异常表头）按 B 兜底
    _, trows = load_temu(path)                  # 坏结构/空数据在此抛错
    if not trows:
        raise ValueError('Temu 报表未解析到任何数据行（检查是否含「店铺」/「MSKU」或「SKU」列）')
    return types, len(trows)


def validate_transfer(path):
    from pipeline import summarize_transfer
    return summarize_transfer(path)


# ============================================================
# 原子写（与 app._atomic_write_bytes 同款：临时文件 + os.replace 重试）
# ============================================================
def _atomic_write_bytes(path, data, retry=6):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.bridge.tmp'
    last_err = None
    for i in range(retry):
        try:
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, path)
            return
        except PermissionError as e:      # Windows 文件锁（看板正在读取等）短暂等待重试
            last_err = e
            import time as _t
            _t.sleep(0.4 * (i + 1))
    raise last_err


def _read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.bridge.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ============================================================
# 云端同步（清洗侧统一存储层 storage.py / common_store 1:1 委托）
# ============================================================
def _cloud_sync(key, data):
    """与 app._cloud_sync 同口径。返回 '' 表示成功。"""
    import common_store as storage
    try:
        if not storage.has_cloud():
            return '未启用云端存储（STORAGE_MODE≠supabase/oss）'
        if storage.ds_file_upload(key, data):
            return ''
        return storage.last_error() or '云端写入返回 False（原因未知）'
    except Exception as e:
        return '云端写入异常：%r' % e


def _update_ds_meta(kind, entry, temu_entry=None):
    """更新共享元信息（字段与 app._sync_source_meta 对齐；只动本次推送涉及的部分）。"""
    import common_store as storage
    try:
        meta = storage.ds_get() or {}
        if kind == 'raw':
            meta['raw'] = entry
        elif kind == 'lx':
            meta['lingxing'] = entry
            meta['lx_filename'] = entry.get('filename')
            meta['lx_uploaded_at'] = entry.get('uploaded_at')
        elif kind == 'wm':
            meta['walmart'] = entry
            meta['wm_filename'] = entry.get('filename')
            meta['wm_uploaded_at'] = entry.get('uploaded_at')
        elif kind == 'admin':
            meta['admin'] = entry
            meta['admin_filename'] = entry.get('filename')
            meta['admin_uploaded_at'] = entry.get('uploaded_at')
        elif kind == 'temu':
            tm = meta.get('temu') or {}
            if isinstance(temu_entry, dict):
                tm.update(temu_entry)
            meta['temu'] = tm
            a, b = (tm.get('a') or {}), (tm.get('b') or {})
            meta['temu_a_filename'] = a.get('filename')
            meta['temu_a_uploaded_at'] = a.get('uploaded_at')
            meta['temu_b_filename'] = b.get('filename')
            meta['temu_b_uploaded_at'] = b.get('uploaded_at')
        elif kind == 'ops':
            e2 = dict(entry)
            e2['source'] = 'ops'
            meta['ops'] = e2
            meta['ops_filename'] = entry.get('filename')
            meta['ops_uploaded_at'] = entry.get('uploaded_at')
        elif kind == 'ref':
            meta['reference'] = entry
            meta['ref_filename'] = entry.get('filename')
            meta['ref_uploaded_at'] = entry.get('uploaded_at')
        storage.ds_update(meta)
    except Exception as e:
        print('[bridge] ds_meta 更新失败(不影响本地生效): %r' % e)


# ============================================================
# 推送主体
# ============================================================
def push_to_main(na_id, src_path, original_name):
    """把 na_data 里已上传的共享数据源推送到清洗看板存储。

    na_id: suite / listing / walmart / admin / ops / transfer / temu_a / temu_b
    src_path: na_data 里刚上传的文件（本地路径）
    original_name: 用户上传时的原始文件名（写进 meta 展示）

    返回 dict: {ok, kind, filename, summary, cloud_error, error}
    校验失败 → ok=False + error（不落盘，不污染看板）。
    """
    res = {'ok': False, 'kind': na_id, 'filename': original_name or '',
           'summary': None, 'cloud_error': '', 'error': ''}
    src_path = str(src_path)
    if not os.path.exists(src_path):
        res['error'] = '源文件不存在：%s' % src_path
        return res
    try:
        with open(src_path, 'rb') as f:
            raw = f.read()
    except Exception as e:
        res['error'] = '读取源文件失败：%r' % e
        return res

    try:
        if na_id == 'suite':
            summary = summarize_raw(src_path)
            _atomic_write_bytes(LATEST_RAW, raw)
            entry = {'filename': original_name, 'uploaded_at': _now(),
                     'hash': None, 'summary': summary, 'source': _SRC_TAG}
            _write_json(RAW_META, entry)
            import common_store as _st
            res['cloud_error'] = _cloud_sync(_st.OBJ_RAW, raw)
            _update_ds_meta('raw', {'filename': original_name,
                                    'uploaded_at': entry['uploaded_at'],
                                    'summary': summary})

        elif na_id == 'listing':
            summary = validate_lingxing(src_path)
            _atomic_write_bytes(LATEST_LINGXING, raw)
            entry = {'filename': original_name, 'uploaded_at': _now(),
                     'summary': summary, 'source': _SRC_TAG}
            _write_json(LINGXING_META, entry)
            import common_store as _st
            res['cloud_error'] = _cloud_sync(_st.OBJ_LX, raw)
            _update_ds_meta('lx', entry)

        elif na_id == 'walmart':
            summary = validate_walmart(src_path)
            _atomic_write_bytes(LATEST_WALMART, raw)
            entry = {'filename': original_name, 'uploaded_at': _now(),
                     'summary': summary, 'source': _SRC_TAG}
            _write_json(WALMART_META, entry)
            import common_store as _st
            res['cloud_error'] = _cloud_sync(_st.OBJ_WM, raw)
            _update_ds_meta('wm', entry)

        elif na_id == 'admin':
            summary = validate_admin(src_path)
            is_csv = (original_name or src_path).lower().endswith('.csv')
            keep = LATEST_ADMIN_CSV if is_csv else LATEST_ADMIN_XLSX
            drop = LATEST_ADMIN_XLSX if is_csv else LATEST_ADMIN_CSV
            try:
                os.remove(drop)          # 与 app.api_upload_admin 一致：清掉另一格式，避免误判
            except OSError:
                pass
            _atomic_write_bytes(keep, raw)
            entry = {'filename': original_name, 'uploaded_at': _now(),
                     'summary': summary, 'source': _SRC_TAG}
            _write_json(ADMIN_META, entry)
            import common_store as _st
            res['cloud_error'] = _cloud_sync(_st.OBJ_ADMIN, raw)
            _update_ds_meta('admin', entry)

        elif na_id == 'ops':
            summary = validate_ops(src_path)
            _atomic_write_bytes(LATEST_OPS, raw)
            try:
                if os.path.exists(OPS_REMOVED_FLAG):
                    os.remove(OPS_REMOVED_FLAG)   # 与 app.api_upload_ops 一致：清除移除标记
            except OSError:
                pass
            entry = {'filename': original_name, 'uploaded_at': _now(),
                     'summary': summary, 'source': _SRC_TAG}
            _write_json(OPS_META, entry)
            import common_store as _st
            res['cloud_error'] = _cloud_sync(_st.OBJ_OPS, raw)
            _update_ds_meta('ops', entry)

        elif na_id in ('temu_a', 'temu_b'):
            slot = 'a' if na_id == 'temu_a' else 'b'
            types, rows = validate_temu(src_path)
            expect = 'A' if slot == 'a' else 'B'
            if expect not in types:
                res['error'] = ('文件表头判定为 %s 类报表（%s），与所选卡片（%s 类）不符；'
                                '请确认上传到正确的卡片' % (expect, sorted(types), expect))
                return res
            slot_path = LATEST_TEMU_A if slot == 'a' else LATEST_TEMU_B
            _atomic_write_bytes(slot_path, raw)
            tm = _read_json(TEMU_META) or {'a': None, 'b': None}
            tm[slot] = {'filename': original_name, 'uploaded_at': _now(),
                        'summary': {'rows': rows, 'sheets_parsed': True},
                        'source': _SRC_TAG}
            _write_json(TEMU_META, tm)
            import common_store as _st
            obj_key = _st.OBJ_TEMU_A if slot == 'a' else _st.OBJ_TEMU_B
            res['cloud_error'] = _cloud_sync(obj_key, raw)
            _update_ds_meta('temu', None, temu_entry={slot: tm[slot]})

        elif na_id == 'transfer':
            summary = validate_transfer(src_path)
            ts = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
            stored = '转单_%s.xlsx' % ts
            os.makedirs(REF_DIR, exist_ok=True)
            dest = os.path.join(REF_DIR, stored)
            with open(dest, 'wb') as f:
                f.write(raw)
            entry = {'stored': stored, 'filename': original_name,
                     'uploaded_at': _now(), 'summary': summary, 'source': _SRC_TAG}
            _write_json(REF_META, entry)
            import common_store as _st
            res['cloud_error'] = _cloud_sync(_st.OBJ_REF, raw)
            _update_ds_meta('ref', {'filename': original_name,
                                    'uploaded_at': entry['uploaded_at'],
                                    'summary': summary})

        else:
            res['error'] = '未知数据源: %s' % na_id
            return res

        res['ok'] = True
        res['summary'] = summary
    except ValueError as e:
        res['error'] = str(e)          # 校验失败：不落盘，看板旧数据完好
    except Exception as e:
        res['error'] = '%s: %s' % (type(e).__name__, e)
    return res
