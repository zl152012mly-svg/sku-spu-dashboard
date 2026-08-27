# -*- coding: utf-8 -*-
"""
storage.py —— 可插拔存储层（异常确认状态/备注 + 数据源版本元信息）

双模式：
  - JSON 模式（默认，未配置 Supabase 时）：数据落盘到本地 data/ 下的 JSON 文件。
    与既有 app.py 的 _save_state/_load_state 完全兼容，改动最小即可跑通。
  - Supabase 模式（配置 SUPABASE_URL + SUPABASE_KEY 后启用）：
    异常确认记录逐行存到云表（每行含主键 + version），支持行级冲突检测；
    数据源版本元信息存到云表；数据源文件本体上传到对象存储（Storage bucket），
    使 Render/HF 冷启动能从云端拉取最新数据源重建，实现「网页上传→全平台永久同步」。

统一对外接口（storage 就是模块级单例），app.py 只调用 storage.*，不关心后端：
  storage.annot_update(id_key, status, note, expect_version) -> (ok, msg)
      若 expect_version 与云端当前 version 不一致 -> 返回冲突错误（他人已改）
  storage.annot_get() -> {id_key: {status,note,version}}
  storage.ds_update(meta) / storage.ds_get()
  storage.ds_file_upload(key, data) / ds_file_download(key) / ds_file_exists(key)
      数据源文件本体上云/取云（key 为 OBJ_RAW/OBJ_REF/OBJ_LX/OBJ_WM 之一）

环境变量：
  SUPABASE_URL    如 https://<proj>.supabase.co
  SUPABASE_KEY    anon / service_role key（service_role 可直接写，无 RLS 限制）
  STORAGE_MODE    'json' | 'supabase'（默认按是否有连接串自动选择）
  SUPABASE_DS_BUCKET 数据源对象存储 bucket名（默认 ds_data）
"""

import os
import json
import time
import threading

_SUPABASE_URL = os.environ.get('SUPABASE_URL', '').strip()
_SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '').strip()
MODE = os.environ.get('STORAGE_MODE', '').strip().lower()

# 本地根目录 + JSON 落盘文件名
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, 'data')
ANNOT_JSON = os.path.join(DATA_DIR, 'annotations.json')   # {id_key: {status,note,version}}

_LOCK = threading.Lock()

# ---------- 本地内存缓存（JSON 模式） ----------
_annot = {}           # id_key -> {status,note,version}
_ds_meta = {}         # 数据源版本元信息


def _use_supabase():
    if MODE == 'supabase':
        return True
    if MODE == 'json':
        return False
    # 自动：有连接串才用 supabase
    return bool(_SUPABASE_URL and _SUPABASE_KEY)


def _client():
    """惰性创建 supabase 客户端（仅 supabase 模式）。"""
    try:
        from supabase import create_client
    except ImportError as e:
        raise RuntimeError('未安装 supabase 客户端库（pip install supabase）') from e
    return create_client(_SUPABASE_URL, _SUPABASE_KEY)


# ==================== JSON 模式（本地落盘） ====================
def _json_load_annot():
    global _annot
    if os.path.exists(ANNOT_JSON):
        try:
            with open(ANNOT_JSON, encoding='utf-8') as f:
                _annot = json.load(f)
        except Exception:
            _annot = {}
    return _annot


def _json_save_annot():
    tmp = ANNOT_JSON + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(_annot, f, ensure_ascii=False)
    os.replace(tmp, ANNOT_JSON)


# ==================== Supabase 模式 ====================
_TABLE_ANNOT = 'sku_annotations'   # 异常确认表
_COL = ('id_key', 'status', 'note', 'version', 'updated_at')

# --- 数据源文件对象存储（Storage bucket）---
# web 上传数据源时，除存本地留档外，同时把文件二进制上传到此 bucket，
# 使 Render 冷启动（休眠后重启）时能优先从云端拉取最新数据源重建，实现「所有人上传→全平台永久同步」。
_BUCKET = os.environ.get('SUPABASE_DS_BUCKET', 'ds_data')   # bucket 名
# 每个数据源对应一个对象 key（覆盖式更新，保留最新一份即可）
OBJ_RAW = 'raw/latest_raw.csv'            # 原始报表 CSV
OBJ_REF = 'ref/latest_transfer.xlsx'      # 转单表 xlsx
OBJ_LX = 'lx/latest_lingxing.xlsx'        # 领星清单 xlsx
OBJ_WM = 'wm/latest_walmart.csv'          # walmart 报表 csv


def _ensure_bucket(c):
    """确保数据源 bucket 存在（service_role 可创建）。返回 bucket proxy。"""
    try:
        c.storage.get_bucket(_BUCKET)
    except Exception:
        # 不存在则尝试创建（需 service_role 具备 storage 权限）
        try:
            c.storage.create_bucket(_BUCKET, options={'public': False})
        except Exception:
            pass
    return c.storage.from_(_BUCKET)


def ds_file_upload(key, data):
    """上传单个数据源文件二进制到云 Storage。key 为 OBJ_* 之一。失败不抛出（尽量不影响主流程）。"""
    if not _use_supabase():
        return False
    try:
        c = _client()
        b = _ensure_bucket(c)
        b.upload(key, data, file_options={'content-type': 'application/octet-stream'})
        return True
    except Exception:
        return False


def ds_file_download(key):
    """从云 Storage 下载数据源二进制；不存在返回 None。"""
    if not _use_supabase():
        return None
    try:
        c = _client()
        b = _ensure_bucket(c)
        res = b.download(key)
        return res if res else None
    except Exception:
        return None


def ds_file_exists(key):
    """云 Storage 是否已存在某数据源对象。"""
    if not _use_supabase():
        return False
    try:
        c = _client()
        b = _ensure_bucket(c)
        return bool(b.exists(key))
    except Exception:
        return False


def _sb_annot_table():
    """确保异常确认表存在（幂等）。返回 client。"""
    c = _client()
    # 说明：实际建表建议在 Supabase SQL Editor 手动创建，字段如下：
    #   CREATE TABLE IF NOT EXISTS sku_annotations (
    #     id_key text PRIMARY KEY,
    #     status text DEFAULT '',
    #     note   text DEFAULT '',
    #     version int  DEFAULT 1,
    #     updated_at timestamptz DEFAULT now()
    #   );
    # 这里不做 DDL（免费版需 SQL 编辑器执行），仅约定表结构。
    return c


# ==================== 统一对外接口 ====================
def annot_get():
    """返回 {id_key: {status,note,version}}，用于页面初始化回填。"""
    if _use_supabase():
        c = _sb_annot_table()
        try:
            res = c.table(_TABLE_ANNOT).select('id_key,status,note,version').execute()
            return {row['id_key']: {
                'status': row.get('status') or '',
                'note': row.get('note') or '',
                'version': row.get('version') or 1,
            } for row in (res.data or [])}
        except Exception:
            return {}
    _json_load_annot()
    return dict(_annot)


def annot_update(id_key, status, note, expect_version=None):
    """写入一行异常确认。expect_version 为空 = 强制写；否则做行级冲突检测。"""
    with _LOCK:
        if _use_supabase():
            c = _sb_annot_table()
            try:
                return _sb_update(c, id_key, status, note, expect_version)
            except Exception as e:
                return False, '云存储写入失败：%s' % e

        # JSON 模式
        cur = _annot.get(id_key, {})
        cur_ver = cur.get('version', 0)
        if expect_version is not None and expect_version != cur_ver:
            return False, '已被他人修改，请刷新后再改（当前版本 %s，你的版本 %s）' % (
                cur_ver, expect_version)
        new_ver = cur_ver + 1
        _annot[id_key] = {'status': status, 'note': note, 'version': new_ver}
        try:
            _json_save_annot()
        except OSError as e:
            return False, '本地保存失败：%s' % e
        return True, ''


def _sb_update(c, id_key, status, note, expect_version):
    """Supabase 行级 upsert + pattern 冲突检测（读后比较后再写）。"""
    # 读当前 version
    res = c.table(_TABLE_ANNOT).select('version').eq('id_key', id_key).execute()
    cur_ver = (res.data[0]['version'] if res.data else 0)
    if expect_version is not None and expect_version != cur_ver:
        return False, '已被他人修改，请刷新后再改（当前版本 %s，你的版本 %s）' % (
            cur_ver, expect_version)
    new_ver = cur_ver + 1
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    payload = {'id_key': id_key, 'status': status, 'note': note,
               'version': new_ver, 'updated_at': now}
    c.table(_TABLE_ANNOT).upsert(payload, on_conflict='id_key').execute()
    return True, ''


def ds_update(meta):
    """保存数据源版本元信息（转单表/数据源 的最新口径）。meta 为 dict。"""
    global _ds_meta
    _ds_meta = meta or {}
    with _LOCK:
        if _use_supabase():
            c = _sb_annot_table()
            try:
                c.table('sku_ds_meta').upsert({'id': 'main', 'meta': meta}, on_conflict='id').execute()
            except Exception:
                pass
            return
        p = os.path.join(DATA_DIR, 'ds_meta.json')
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_ds_meta, f, ensure_ascii=False)
        os.replace(tmp, p)


def ds_get():
    """读取数据源版本元信息。"""
    global _ds_meta
    if _use_supabase():
        c = _sb_annot_table()
        try:
            res = c.table('sku_ds_meta').select('meta').eq('id', 'main').execute()
            return (res.data[0]['meta'] if res.data else {})
        except Exception:
            return {}
    p = os.path.join(DATA_DIR, 'ds_meta.json')
    if os.path.exists(p):
        try:
            with open(p, encoding='utf-8') as f:
                _ds_meta = json.load(f)
        except Exception:
            _ds_meta = {}
    return _ds_meta
