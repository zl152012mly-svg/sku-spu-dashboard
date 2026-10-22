# -*- coding: utf-8 -*-
"""
storage.py —— 可插拔存储层（异常确认状态/备注 + 数据源版本元信息）

三模式（STORAGE_MODE 切换，三种后端共存，互不影响）：
  - JSON 模式（默认，未配置任何云端凭证时）：数据落盘到本地 data/ 下的 JSON 文件。
    与既有 app.py 的 _save_state/_load_state 完全兼容，改动最小即可跑通。
  - Supabase 模式（配置 SUPABASE_URL + SUPABASE_KEY 后启用）：
    异常确认记录逐行存到云表（每行含主键 + version），支持行级冲突检测；
    数据源版本元信息存到云表；数据源文件本体经 gzip+base64 分块上传到 Storage bucket，
    使 Render/HF 冷启动能从云端拉取最新数据源重建，实现「网页上传→全平台永久同步」。
  - OSS 模式（配置 OSS_ACCESS_KEY_ID + OSS_ACCESS_KEY_SECRET + OSS_BUCKET 后启用）：
    阿里云 OSS 原生对象存储。文件直接 put_object（无需分块 hack）；异常确认与数据源
    版本元信息存为 OSS 上的单个 JSON 文件（meta/annotations.json、meta/ds_meta.json），
    零额外数据库。所有云端写带 3 次重试，吸收阿里云/网络瞬时抖动。
    适用于国内 ECS 常驻部署，国内直连、无 Render 免费层冷启动与 Cloudflare 海外抖动。

统一对外接口（storage 就是模块级单例），app.py 只调用 storage.*，不关心后端：
  storage.annot_update(id_key, status, note, expect_version) -> (ok, msg)
      若 expect_version 与云端当前 version 不一致 -> 返回冲突错误（他人已改）
  storage.annot_get() -> {id_key: {status,note,version}}
  storage.ds_update(meta) / storage.ds_get()
  storage.ds_file_upload(key, data) / ds_file_download(key) / ds_file_exists(key)
      数据源文件本体上云/取云（key 为 OBJ_RAW/OBJ_REF/OBJ_LX/OBJ_WM/OBJ_TEMU 之一）
  storage.has_cloud() -> 是否启用了任意云端共享存储（supabase 或 oss 均算）

环境变量：
  STORAGE_MODE          'json' | 'supabase' | 'oss'（默认按是否有凭证自动选择，OSS 优先）
  SUPABASE_URL          如 https://<proj>.supabase.co
  SUPABASE_KEY          anon / service_role key（service_role 可直接写，无 RLS 限制）
  SUPABASE_DS_BUCKET    数据源对象存储 bucket名（默认 ds_data）
  OSS_ENDPOINT          如 oss-cn-hangzhou.aliyuncs.com（自动补 https://）
  OSS_ACCESS_KEY_ID     RAM 子账号 AccessKeyId（最小权限：仅该 bucket 读写）
  OSS_ACCESS_KEY_SECRET RAM 子账号 AccessKeySecret
  OSS_BUCKET            OSS bucket 名
  OSS_PREFIX            可选：统一对象键前缀（如 'sku-spu/'），多环境隔离用
"""

import os
import json
import time
import threading
import gzip
import base64

_SUPABASE_URL = os.environ.get('SUPABASE_URL', '').strip()
_SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '').strip()
MODE = os.environ.get('STORAGE_MODE', '').strip().lower()

# ---------- 阿里云 OSS 环境变量（凭证仅来自环境变量，绝不写进代码/记忆）----------
#   OSS_ENDPOINT           如 oss-cn-hangzhou.aliyuncs.com（自动补 https://）
#   OSS_ACCESS_KEY_ID     RAM 子账号 AccessKeyId（最小权限：仅该 bucket 的读写）
#   OSS_ACCESS_KEY_SECRET RAM 子账号 AccessKeySecret
#   OSS_BUCKET            OSS bucket 名
#   OSS_PREFIX           可选：统一对象键前缀（如 'sku-spu/'），用于多环境隔离
_OSS_ENDPOINT = os.environ.get('OSS_ENDPOINT', '').strip()
_OSS_ID = os.environ.get('OSS_ACCESS_KEY_ID', '').strip()
_OSS_SECRET = os.environ.get('OSS_ACCESS_KEY_SECRET', '').strip()
_OSS_BUCKET = os.environ.get('OSS_BUCKET', '').strip()
_OSS_PREFIX = os.environ.get('OSS_PREFIX', '').strip()
# OSS 上的 JSON 元数据（替代 Supabase 的两张表，零额外数据库）
_OSS_ANNOT_KEY = 'meta/annotations.json'     # 异常确认状态/备注 + 行级 version
_OSS_DS_META_KEY = 'meta/ds_meta.json'       # 数据源版本元信息（ds_update/ds_get）

# 本地根目录 + JSON 落盘文件名
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, 'data')
ANNOT_JSON = os.path.join(DATA_DIR, 'annotations.json')   # {id_key: {status,note,version}}

_LOCK = threading.Lock()

# ---------- 本地内存缓存（JSON 模式） ----------
_annot = {}           # id_key -> {status,note,version}
_ds_meta = {}         # 数据源版本元信息


def _backend():
    """返回当前存储后端：'supabase' | 'oss' | 'json'。"""
    if MODE in ('supabase', 'oss', 'json'):
        return MODE
    # 自动探测：OSS 凭证优先于 Supabase（迁移目标）
    if _OSS_ID and _OSS_SECRET and _OSS_BUCKET:
        return 'oss'
    if _SUPABASE_URL and _SUPABASE_KEY:
        return 'supabase'
    return 'json'


def _use_supabase():
    """是否为 Supabase 后端（仅 supabase 模式为真）。保留供内部分支判断。"""
    return _backend() == 'supabase'


def has_cloud():
    """是否启用了任意「云端共享存储」（supabase 或 oss 均算）。
    供 app.py 判断是否需要冷启动拉取 / 是否展示「云端同步」标志。"""
    return _backend() != 'json'


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
_TABLE_DS_META = 'sku_ds_meta'      # 数据源版本元信息 + 文件本体（按 id 分多行）
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
OBJ_ADMIN = 'admin/latest_admin'          # 管理员检查表（品类）：xlsx/csv 通用 key（无扩展名）
OBJ_TEMU = 'temu/latest_temu'             # Temu 报表旧单对象（仅迁移识别用，新上传不再写入）
OBJ_TEMU_A = 'temu/latest_temu_a'         # Temu A 类报表（含「店铺」列）xlsx 通用 key（无扩展名）
OBJ_TEMU_B = 'temu/latest_temu_b'         # Temu B 类报表（固定 COZY 店）xlsx 通用 key（无扩展名）
OBJ_OPS = 'ops/latest_ops'                # 产品运营表（第 7 步，Amazon 运营/运营组查询）xlsx 通用 key


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


# 数据源文件本体：gzip+base64 后【分块】写入数据库表 sku_ds_meta：
#   元数据行  id='__file__:<对象键>'        meta={'v':2,'size':N,'n':块数}
#   数据块行  id='__file__:<对象键>:p<序号>' meta={'d':'<base64片段(<=CHUNK_B64 字符)>'}
# 分块原因：整文件单行（admin gzip 后 base64 约 6-8MB）经 Supabase REST upsert
# 实测静默失败（疑似请求体大小限制），分块后单行仅几百 KB，稳定可靠。
# 兼容：下载时若元数据行仍是旧格式（meta.data 单行整文件），按旧格式取回。
_CHUNK_B64 = 400000    # 每块 base64 字符数（约合 300KB 二进制）
_LAST_DS_ERR = ''      # 最近一次数据源 DB 读/写失败原因（/api/status 暴露，便于排障）


def _file_row_id(key):
    return '__file__:' + key


def last_error():
    """返回最近一次数据源云端读/写失败原因（无失败为 ''）。"""
    return _LAST_DS_ERR


def ds_file_upload(key, data):
    """上传并覆盖单个数据源文件，跨冷启动持久化。返回 True/False；失败原因见 last_error()。

    - oss 模式：文件原样 put_object 到 OSS（带 3 次重试），无需分块。
    - supabase 模式：gzip+base64 分块写入数据库；2026-10-13 加重试，吸收 Supabase（经
      Cloudflare）偶发瞬时 502/503；分块均为 upsert，整体重跑幂等无副作用。
    """
    global _LAST_DS_ERR
    b = _backend()
    if b == 'oss':
        return _oss_put_bytes(key, data)
    if b != 'supabase':
        return False
    payload = base64.b64encode(gzip.compress(data)).decode('ascii')
    chunks = [payload[i:i + _CHUNK_B64] for i in range(0, len(payload), _CHUNK_B64)] or ['']
    rid = _file_row_id(key)
    last_exc = None
    for attempt in range(3):
        try:
            c = _client()
            # 先写数据块、最后写元数据行：下载端以元数据行为准，半途失败不会读到残缺文件
            for i, ch in enumerate(chunks):
                c.table(_TABLE_DS_META).upsert(
                    {'id': '%s:p%d' % (rid, i), 'meta': {'d': ch}},
                    on_conflict='id').execute()
            c.table(_TABLE_DS_META).upsert(
                {'id': rid, 'meta': {'v': 2, 'size': len(data), 'n': len(chunks)}},
                on_conflict='id').execute()
            _LAST_DS_ERR = ''
            return True
        except Exception as e:
            last_exc = e
            time.sleep(1 + attempt)          # 1s / 2s / 3s
    _LAST_DS_ERR = 'upload %s: %r' % (key, last_exc)
    return False


def ds_file_download(key):
    """取回数据源二进制；oss 模式直接从 OSS 取，supabase 模式兼容 v2 分块 / v1 单行。
    不存在或出错返回 None（原因见 last_error）。"""
    global _LAST_DS_ERR
    b = _backend()
    if b == 'oss':
        return _oss_get_bytes(key)
    if b != 'supabase':
        return None
    try:
        c = _client()
        res = c.table(_TABLE_DS_META).select('meta').eq('id', _file_row_id(key)).execute()
        if not res.data:
            return None
        m = (res.data[0] or {}).get('meta') or {}
        if not isinstance(m, dict):
            return None
        if m.get('v') == 2:                      # v2 分块格式
            n = int(m.get('n') or 0)
            if n <= 0:
                return None
            ids = ['%s:p%d' % (_file_row_id(key), i) for i in range(n)]
            got = {}
            for i in range(0, n, 50):            # 分批 in 查询
                r2 = c.table(_TABLE_DS_META).select('id,meta').in_('id', ids[i:i + 50]).execute()
                for row in (r2.data or []):
                    got[row.get('id')] = ((row.get('meta') or {}).get('d') or '')
            missing = [pid for pid in ids if pid not in got]
            if missing:
                _LAST_DS_ERR = 'download %s: %d/%d parts missing' % (key, len(missing), n)
                return None
            b64 = ''.join(got[pid] for pid in ids)
            return gzip.decompress(base64.b64decode(b64))
        b64 = m.get('data')                      # v1 旧版单行格式
        if b64:
            return gzip.decompress(base64.b64decode(b64))
        return None
    except Exception as e:
        _LAST_DS_ERR = 'download %s: %r' % (key, e)
        return None


def ds_file_exists(key):
    """是否已存在某数据源对象（oss 模式查 OSS，supabase 模式查数据库）。"""
    b = _backend()
    if b == 'oss':
        return _oss_object_exists(key)
    if b != 'supabase':
        return False
    try:
        c = _client()
        res = c.table(_TABLE_DS_META).select('id').eq('id', _file_row_id(key)).execute()
        return bool(res.data)
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
    b = _backend()
    if b == 'supabase':
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
    if b == 'oss':
        return _oss_annot_get()
    _json_load_annot()
    return dict(_annot)


def annot_update(id_key, status, note, expect_version=None):
    """写入一行异常确认。expect_version 为空 = 强制写；否则做行级冲突检测。"""
    with _LOCK:
        b = _backend()
        if b == 'supabase':
            c = _sb_annot_table()
            try:
                return _sb_update(c, id_key, status, note, expect_version)
            except Exception as e:
                return False, '云存储写入失败：%s' % e
        if b == 'oss':
            return _oss_annot_update(id_key, status, note, expect_version)

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


# ==================== 阿里云 OSS 模式 ====================
# 设计要点（相对 Supabase 的改进）：
#   1) 文件直接 put_object（OSS 原生对象存储，无需 gzip+base64 分块 hack）。
#   2) 标注/版本元信息存为 OSS 上的单个 JSON 文件（meta/annotations.json、
#      meta/ds_meta.json），零额外数据库，读写简单可靠。
#   3) 所有云端写均带 3 次重试（间隔 1s/2s/3s），吸收阿里云/网络的瞬时抖动。
# 凭证仅来自环境变量，绝不写进代码或记忆。
def _oss_endpoint():
    e = _OSS_ENDPOINT or 'oss-cn-hangzhou.aliyuncs.com'
    if not e.startswith('http'):
        e = 'https://' + e
    return e


def _oss_client():
    """惰性创建 OSS Bucket 客户端（仅 oss 模式、且真正读写时才 import oss2）。"""
    try:
        import oss2
    except ImportError as e:
        raise RuntimeError('未安装 oss2（pip install oss2）') from e
    auth = oss2.Auth(_OSS_ID, _OSS_SECRET)
    return oss2.Bucket(auth, _oss_endpoint(), _OSS_BUCKET)


def _oss_key(key):
    return (_OSS_PREFIX + key) if _OSS_PREFIX else key


def _oss_put_bytes(key, data, retries=3):
    """把字节写入 OSS 对象（覆盖式）；带重试。返回 True/False。"""
    global _LAST_DS_ERR
    full = _oss_key(key)
    last = None
    for i in range(retries):
        try:
            _oss_client().put_object(full, data)
            _LAST_DS_ERR = ''
            return True
        except Exception as e:
            last = e
            time.sleep(1 + i)          # 1s / 2s / 3s
    _LAST_DS_ERR = 'OSS put %s: %r' % (full, last)
    return False


def _oss_get_bytes(key):
    """从 OSS 取回对象字节；不存在或出错返回 None（原因见 last_error）。"""
    global _LAST_DS_ERR
    full = _oss_key(key)
    try:
        resp = _oss_client().get_object(full)
        data = resp.read()
        _LAST_DS_ERR = ''
        return data
    except Exception as e:
        name = type(e).__name__
        if 'NoSuchKey' in name or 'NotFound' in name:
            return None
        _LAST_DS_ERR = 'OSS get %s: %r' % (full, e)
        return None


def _oss_object_exists(key):
    """OSS 是否已存在某对象。"""
    try:
        return _oss_client().object_exists(_oss_key(key))
    except Exception:
        return False


def _oss_annot_get():
    data = _oss_get_bytes(_OSS_ANNOT_KEY)
    if not data:
        return {}
    try:
        return json.loads(data.decode('utf-8'))
    except Exception:
        return {}


def _oss_annot_update(id_key, status, note, expect_version):
    """OSS 上 annotations.json 的读-改-写（进程内加锁，单实例部署安全）。"""
    cur = _oss_annot_get()
    cur_ver = (cur.get(id_key) or {}).get('version', 0)
    if expect_version is not None and expect_version != cur_ver:
        return False, '已被他人修改，请刷新后再改（当前版本 %s，你的版本 %s）' % (cur_ver, expect_version)
    new_ver = cur_ver + 1
    cur[id_key] = {'status': status, 'note': note, 'version': new_ver}
    ok = _oss_put_bytes(_OSS_ANNOT_KEY, json.dumps(cur, ensure_ascii=False).encode('utf-8'))
    if not ok:
        return False, '云端写入失败：' + (last_error() or '未知')
    return True, ''


def _oss_ds_meta_put(meta):
    return _oss_put_bytes(_OSS_DS_META_KEY, json.dumps(meta or {}, ensure_ascii=False).encode('utf-8'))


def _oss_ds_meta_get():
    data = _oss_get_bytes(_OSS_DS_META_KEY)
    if not data:
        return {}
    try:
        return json.loads(data.decode('utf-8'))
    except Exception:
        return {}


def ds_update(meta):
    """保存数据源版本元信息（转单表/数据源 的最新口径）。meta 为 dict。"""
    global _ds_meta
    _ds_meta = meta or {}
    with _LOCK:
        b = _backend()
        if b == 'supabase':
            c = _sb_annot_table()
            try:
                c.table('sku_ds_meta').upsert({'id': 'main', 'meta': meta}, on_conflict='id').execute()
            except Exception:
                pass
            return
        if b == 'oss':
            _oss_ds_meta_put(meta)
            return
        p = os.path.join(DATA_DIR, 'ds_meta.json')
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_ds_meta, f, ensure_ascii=False)
        os.replace(tmp, p)


def ds_get():
    """读取数据源版本元信息。"""
    global _ds_meta
    b = _backend()
    if b == 'supabase':
        c = _sb_annot_table()
        try:
            res = c.table('sku_ds_meta').select('meta').eq('id', 'main').execute()
            return (res.data[0]['meta'] if res.data else {})
        except Exception:
            return {}
    if b == 'oss':
        return _oss_ds_meta_get()
    p = os.path.join(DATA_DIR, 'ds_meta.json')
    if os.path.exists(p):
        try:
            with open(p, encoding='utf-8') as f:
                _ds_meta = json.load(f)
        except Exception:
            _ds_meta = {}
    return _ds_meta
