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

import storage as _st

BASE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(BASE, 'data', 'redline')
_SRC_DIR = os.path.join(LOCAL_DIR, 'src')
_PER_DIR = os.path.join(LOCAL_DIR, 'period')

_PREFIX_SRC = 'redline/src/'
_PREFIX_PER = 'redline/period/'

_LOCK = threading.Lock()


def _is_oss():
    """当前是否走 OSS 后端（复用主系统 storage 的后端判定）。"""
    try:
        return _st._backend() == 'oss'
    except Exception:
        return False


def backend_name():
    return 'oss' if _is_oss() else 'json'


def _src_key(src, store, file):
    raw = '%s\x01%s\x01%s' % (src or '', store or '', file or '')
    return _PREFIX_SRC + hashlib.md5(raw.encode('utf-8')).hexdigest() + '.json'


def _local_path(key):
    if key.startswith(_PREFIX_SRC):
        return os.path.join(_SRC_DIR, key[len(_PREFIX_SRC):])
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
    d = _SRC_DIR if prefix == _PREFIX_SRC else _PER_DIR
    if not os.path.isdir(d):
        return []
    return [prefix + f for f in sorted(os.listdir(d)) if f.endswith('.json')]


# ---------------------------------------------------------------- 数据源
def list_sources():
    out = []
    for k in _list_keys(_PREFIX_SRC):
        o = _get(k)
        if not o:
            continue
        out.append({
            'id': k,
            'src': o.get('src') or '',
            'store': o.get('store') or '',
            'file': o.get('file') or '',
            'gen': int(o.get('gen') or 1),
            'chunk': 0,
            'data': o.get('b64') or '',
            'updated_at': o.get('updated_at') or '',
        })
    return out


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
        return _put(_src_key(src, store, file), obj)


def remove_source(src, store, file):
    """删除数据源组。store 传 '' 表示不限店铺；file 为 None 表示不限文件。"""
    n = 0
    with _LOCK:
        for k in _list_keys(_PREFIX_SRC):
            o = _get(k)
            if not o:
                continue
            if (o.get('src') or '') != (src or ''):
                continue
            if store not in (None, '') and (o.get('store') or '') != store:
                continue
            if file is not None and (o.get('file') or '') != (file or ''):
                continue
            if _delete(k):
                n += 1
    return n


# ---------------------------------------------------------------- 历史期
def list_periods():
    out = []
    for k in _list_keys(_PREFIX_PER):
        o = _get(k)
        if not o:
            continue
        pid = o.get('period') or 0
        out.append({
            'id': pid,
            'period': pid,
            'date': o.get('date') or '',
            'label': o.get('label') or '',
            'chunk': 0,
            'data': o.get('b64') or '',
        })
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
        'periods': len(_list_keys(_PREFIX_PER)),
    }
