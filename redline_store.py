# -*- coding: utf-8 -*-
"""
redline_store.py —— 红线价监控模块存储层（OSS 优先，JSON 兜底）

与主系统 storage.py 共用同一套环境变量与 OSS bucket，仅靠对象键前缀
`redline/` 与 SKU&SPU 的数据隔离，互不干扰。

对象键：
  redline/src/<md5(src|store|file)>.json
      {"src","store","file","gen","updated_at","b64"}
  redline/period/<period>.json
      {"period","date","label","b64"}

说明：前端把 pako.deflate 后的内容 base64 后再按 400000 字符分块上传；
本层把分块直接 join 成一条 b64 存在单个对象里（OSS 无字段长度限制），
读取时还原成「一条记录、chunk=0」返回，兼容前端 restoreGroupRecords。

对外接口（供 redline_routes.py 调用）：
  backend_name() -> 'oss' | 'json'
  list_sources() -> [{id,src,store,file,gen,chunk,data,updated_at}]
  save_source(src, store, file, chunks, updated_at) -> bool
  remove_source(src, store, file) -> int
  list_periods() -> [{id,period,date,label,chunk,data}]
  sync_periods(periods) -> int
"""
import os
import json
import time
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import storage as _st

BASE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(BASE, 'data', 'redline')
_SRC_DIR = os.path.join(LOCAL_DIR, 'src')
_PER_DIR = os.path.join(LOCAL_DIR, 'period')
_META_DIR = os.path.join(LOCAL_DIR, 'meta')

_PREFIX_SRC = 'redline/src/'
_PREFIX_PER = 'redline/period/'
# 2026-11-09 首屏提速：轻量元数据对象。原 list_sources(meta_only=True) 会把每个
# 数据源对象（含几 MB b64）完整下载后只取 src/updated_at 再丢掉，首屏平白多下一遍大数据。
# 现在保存时双写一份只有几十字节的 meta，清单接口只读它；老数据没有 meta 时自动回退并补写。
_PREFIX_META = 'redline/meta/'

_LOCK = threading.Lock()


def _is_oss():
    """当前是否走 OSS 后端（复用主系统 storage 的后端判定）。"""
    try:
        return _st._backend() == 'oss'
    except Exception:
        return False


def backend_name():
    return 'oss' if _is_oss() else 'json'


def _src_hash(src, store, file):
    raw = '%s\x01%s\x01%s' % (src or '', store or '', file or '')
    return hashlib.md5(raw.encode('utf-8')).hexdigest()


def _src_key(src, store, file):
    return _PREFIX_SRC + _src_hash(src, store, file) + '.json'


def _meta_key_of_src(src_key):
    """src 对象键 → 对应的轻量 meta 对象键（同一 md5 后缀）。"""
    return _PREFIX_META + src_key[len(_PREFIX_SRC):]


def _meta_obj(o):
    """从完整对象里抽出清单接口需要的字段（绝不含 b64）。"""
    return {
        'src': o.get('src') or '',
        'store': o.get('store') or '',
        'file': o.get('file') or '',
        'gen': int(o.get('gen') or 1),
        'updated_at': o.get('updated_at') or '',
        'b64_size': len(o.get('b64') or ''),
    }


def _local_path(key):
    if key.startswith(_PREFIX_SRC):
        return os.path.join(_SRC_DIR, key[len(_PREFIX_SRC):])
    if key.startswith(_PREFIX_META):
        return os.path.join(_META_DIR, key[len(_PREFIX_META):])
    if key.startswith(_PREFIX_PER):
        return os.path.join(_PER_DIR, key[len(_PREFIX_PER):])
    return os.path.join(LOCAL_DIR, key.replace('/', '_'))


def _put(key, obj):
    data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
    if _is_oss():
        return bool(_st._oss_put_bytes(key, data))
    path = _local_path(key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)
    return True


def _get(key):
    if _is_oss():
        b = _st._oss_get_bytes(key)
    else:
        p = _local_path(key)
        b = open(p, 'rb').read() if os.path.exists(p) else None
    if not b:
        return None
    try:
        return json.loads(b.decode('utf-8'))
    except Exception:
        return None


def _delete(key):
    if _is_oss():
        try:
            _st._oss_client().delete_object(_st._oss_key(key))
            return True
        except Exception:
            return False
    p = _local_path(key)
    try:
        if os.path.exists(p):
            os.remove(p)
            return True
    except Exception:
        pass
    return False


def _list_keys(prefix):
    """列出某前缀下的逻辑对象键（已剥离 OSS_PREFIX）。"""
    if _is_oss():
        bucket = _st._oss_client()
        full_prefix = _st._oss_key(prefix)
        prefix_len = len(_st._OSS_PREFIX or '')
        keys = []
        marker = ''
        while True:
            res = bucket.list_objects(prefix=full_prefix, marker=marker, max_keys=1000)
            for o in (res.object_list or []):
                k = o.key
                if prefix_len and k.startswith(_st._OSS_PREFIX):
                    k = k[prefix_len:]
                if k.startswith(prefix):
                    keys.append(k)
            if not getattr(res, 'is_truncated', False):
                break
            marker = getattr(res, 'next_marker', '') or ''
            if not marker:
                break
        return keys
    d = {_PREFIX_SRC: _SRC_DIR, _PREFIX_META: _META_DIR, _PREFIX_PER: _PER_DIR}.get(prefix, _PER_DIR)
    if not os.path.isdir(d):
        return []
    return [prefix + f for f in sorted(os.listdir(d)) if f.endswith('.json')]


# ---------------------------------------------------------------- 数据源
def _batch_get(keys):
    """并发取回多个对象（OSS 模式用线程池并行，消除逐条串行往返导致的首屏慢）。"""
    if not keys:
        return []
    if _is_oss():
        n = min(8, len(keys)) or 1
        try:
            with ThreadPoolExecutor(max_workers=n) as ex:
                return list(ex.map(_get, keys))
        except Exception:
            pass
    return [_get(k) for k in keys]


def list_sources(meta_only=False):
    keys = _list_keys(_PREFIX_SRC)
    if meta_only:
        # 轻量清单：只读几十字节的 meta 对象，绝不下载 b64。
        meta_keys = [_meta_key_of_src(k) for k in keys]
        metas = _batch_get(meta_keys)
        out = []
        for k, mk, m in zip(keys, meta_keys, metas):
            if not m:
                # 迁移前保存的数据没有 meta：回退读一次完整对象并补写（自愈，只会慢这一次）
                o = _get(k)
                if not o:
                    continue
                m = _meta_obj(o)
                try:
                    _put(mk, m)
                except Exception:
                    pass
            out.append({
                'id': k,
                'src': m.get('src') or '',
                'store': m.get('store') or '',
                'file': m.get('file') or '',
                'gen': int(m.get('gen') or 1),
                'chunk': 0,
                'updated_at': m.get('updated_at') or '',
            })
        return out
    objs = _batch_get(keys)
    out = []
    for k, o in zip(keys, objs):
        if not o:
            continue
        rec = {
            'id': k,
            'src': o.get('src') or '',
            'store': o.get('store') or '',
            'file': o.get('file') or '',
            'gen': int(o.get('gen') or 1),
            'chunk': 0,
            'updated_at': o.get('updated_at') or '',
            'data': o.get('b64') or '',
        }
        out.append(rec)
    return out


def get_source_data(src, store, file):
    """按 src|store|file 取单个数据源的压缩数据（b64）；不存在返回 None。"""
    return (_get(_src_key(src, store, file)) or {}).get('b64') or None


def save_source(src, store, file, chunks, updated_at):
    """覆盖式保存一个数据源组（src+store+file 唯一）。"""
    obj = {
        'src': src or '',
        'store': store or '',
        'file': file or '',
        'gen': 1,
        'updated_at': updated_at or time.strftime('%Y-%m-%d %H:%M:%S'),
        'b64': ''.join(chunks or []),
    }
    with _LOCK:
        ok = _put(_src_key(src, store, file), obj)
        if ok:
            # 轻量元数据双写：清单接口靠它避免重复下载几 MB 的 b64。
            # meta 写失败不影响主数据（下次清单接口会自动自愈补写）。
            try:
                _put(_PREFIX_META + _src_hash(src, store, file) + '.json', _meta_obj(obj))
            except Exception:
                pass
        return ok


def remove_source(src, store, file):
    """删除数据源组。store 传 '' 表示不限店铺；file 为 None 表示不限文件。"""
    n = 0
    with _LOCK:
        for k in _list_keys(_PREFIX_SRC):
            # 优先读轻量 meta 做匹配，避免为了判断归属把整个 b64 下载下来
            m = _get(_meta_key_of_src(k)) or _get(k)
            if not m:
                continue
            if (m.get('src') or '') != (src or ''):
                continue
            if store not in (None, '') and (m.get('store') or '') != store:
                continue
            if file is not None and (m.get('file') or '') != (file or ''):
                continue
            if _delete(k):
                n += 1
            _delete(_meta_key_of_src(k))
    return n


# ---------------------------------------------------------------- 历史期
def list_periods(meta_only=False):
    keys = _list_keys(_PREFIX_PER)
    objs = _batch_get(keys)
    out = []
    for k, o in zip(keys, objs):
        if not o:
            continue
        pid = o.get('period') or 0
        rec = {
            'id': pid,
            'period': pid,
            'date': o.get('date') or '',
            'label': o.get('label') or '',
            'chunk': 0,
        }
        if not meta_only:
            rec['data'] = o.get('b64') or ''
        out.append(rec)
    return out


def sync_periods(periods):
    """整体同步历史期：先清空再全量写入（与资料库原语义一致）。"""
    with _LOCK:
        for k in _list_keys(_PREFIX_PER):
            _delete(k)
        n = 0
        for p in (periods or []):
            pid = p.get('period') or 0
            obj = {
                'period': pid,
                'date': p.get('date') or '',
                'label': p.get('label') or '',
                'b64': ''.join(p.get('chunks') or []),
            }
            if _put(_PREFIX_PER + str(pid) + '.json', obj):
                n += 1
        return n


def stats():
    """诊断用：当前后端与对象数量。"""
    return {
        'backend': backend_name(),
        'sources': len(_list_keys(_PREFIX_SRC)),
        'metas': len(_list_keys(_PREFIX_META)),
        'periods': len(_list_keys(_PREFIX_PER)),
    }
