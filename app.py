# -*- coding: utf-8 -*-
"""
SKU&SPU 清洗系统 —— Flask 后端
================================
① 上传/更新「转单表」xlsx（数据会变，作为可替换的参考表，保留历史版本）
② 上传原始「SKU平台信息套件成员货品体现」CSV -> 跑 v9 口径流水线 -> 看板 + 下载
③ 可选数据源：领星清单 xlsx（MSKU/状态/店铺/ASIN）+ walmart 后台报表 csv
   （SKU/Item ID/Publish Status）-> 按 店铺+上架SKU(14K) 匹配出 ASIN + listing后台状态
④ 转单表/数据源更新后可「一键重算」，无需重新上传原始表
异常确认状态/备注：当次处理 + 本地 JSON 缓存（刷新/重启可恢复）；上云后走可插拔存储层。

访问口令：环境变量 ACCESS_PASSWORD（默认可配），未通过口令不能访问看板/编辑。
         上云（HF Spaces）时需设置 ACCESS_PASSWORD 环境变量。

运行：python app.py  ->  http://127.0.0.1:8092
"""
import os
import io
import json
import csv
import gzip
import shutil
import zipfile
import threading
import time
import hashlib
from datetime import datetime, timezone, timedelta
from flask import (Flask, request, jsonify, render_template,
                   send_file, send_from_directory)

# ============================ 北京时间 + 计算进度 ============================
# 服务器（Render）时区为 UTC，所有对外展示时间统一用北京时间 UTC+8
BJ_TZ = timezone(timedelta(hours=8))


def now_bj(fmt='%Y-%m-%d %H:%M:%S'):
    """返回当前「北京时间」字符串（服务器可能是 UTC，不能用 datetime.now()）。"""
    return datetime.now(BJ_TZ).strftime(fmt)


# 计算进度（供前端轮询 /api/progress 显示进度条）
# 结构：{running, pct, stage, started_at, finished_at, error, ok}
PROGRESS = {'running': False, 'pct': 0, 'stage': '', 'started_at': None,
            'finished_at': None, 'error': None, 'ok': None}
_PROGRESS_LOCK = threading.Lock()


def progress_set(pct, stage):
    """更新进度（0–100）。"""
    with _PROGRESS_LOCK:
        PROGRESS['pct'] = max(0, min(100, int(pct)))
        PROGRESS['stage'] = stage
        PROGRESS['running'] = True


def progress_start(stage='准备中…'):
    with _PROGRESS_LOCK:
        PROGRESS.update({'running': True, 'pct': 0, 'stage': stage,
                         'started_at': now_bj(), 'finished_at': None,
                         'error': None, 'ok': None})


def progress_end(ok, error=None):
    with _PROGRESS_LOCK:
        PROGRESS.update({'running': False, 'pct': 100 if ok else PROGRESS['pct'],
                         'stage': '完成' if ok else ('失败：' + str(error or '')),
                         'finished_at': now_bj(), 'ok': ok, 'error': str(error) if error else None})


def file_sha256(path, chunk=1 << 20):
    """算文件 sha256（用于判断「是否上传了新表格」）。"""
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()
    except Exception:
        return None

from pipeline import run_pipeline, summarize_transfer, load_lingxing, load_walmart, load_temu
import common_store as storage  # 合并后统一存储层（SKU&SPU 命名空间委托 storage.py，新增新品盘点命名空间）

BASE = os.path.dirname(os.path.abspath(__file__))
REF_DIR = os.path.join(BASE, 'reference')
UPLOAD_DIR = os.path.join(BASE, 'uploads')
DATA_DIR = os.path.join(BASE, 'data')
CURRENT_JSON = os.path.join(DATA_DIR, 'current.json')
REF_META = os.path.join(DATA_DIR, 'reference_meta.json')
LATEST_RAW = os.path.join(UPLOAD_DIR, 'latest_raw.csv')     # 留档，供重算
LATEST_LINGXING = os.path.join(UPLOAD_DIR, 'latest_lingxing.xlsx')   # 领星清单留档
LATEST_WALMART = os.path.join(UPLOAD_DIR, 'latest_walmart.csv')      # walmart 报表留档
LATEST_ADMIN_XLSX = os.path.join(UPLOAD_DIR, 'latest_admin.xlsx')    # 管理员检查表留档（xlsx）
LATEST_ADMIN_CSV = os.path.join(UPLOAD_DIR, 'latest_admin.csv')        # 管理员检查表留档（csv）
LINGXING_META = os.path.join(DATA_DIR, 'lingxing_meta.json')
WALMART_META = os.path.join(DATA_DIR, 'walmart_meta.json')
ADMIN_META = os.path.join(DATA_DIR, 'admin_meta.json')
RAW_META = os.path.join(DATA_DIR, 'raw_meta.json')
# Temu 报表（第 6 步独立上传源，Option ②）：留档 + 元信息
OPS_META = os.path.join(DATA_DIR, 'ops_meta.json')              # 产品运营表（第 7 步）元信息
LATEST_OPS = os.path.join(UPLOAD_DIR, 'latest_ops.xlsx')        # 产品运营表留档（Amazon 运营查询）
TEMU_META = os.path.join(DATA_DIR, 'temu_meta.json')          # 新格式 {'a': entry|None, 'b': entry|None}
LATEST_TEMU_A = os.path.join(UPLOAD_DIR, 'latest_temu_a.xlsx')  # Temu A 类（含「店铺」列）留档
LATEST_TEMU_B = os.path.join(UPLOAD_DIR, 'latest_temu_b.xlsx')  # Temu B 类（固定 COZY 店）留档
LATEST_TEMU = os.path.join(UPLOAD_DIR, 'latest_temu.xlsx')      # 旧单文件留档（仅迁移识别用）
for d in (REF_DIR, UPLOAD_DIR, DATA_DIR):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__)

# ---------- 统一鉴权（auth.py：session+role，兼容既有 X-Auth-Token 流） ----------
from auth import init_app, role_required, current_role
import auth as auth
init_app(app)

# ---------- 新品盘点子系统（Phase 2 Step 3）：蓝图挂载 /na ----------
try:
    from na_routes import bp as na_bp, MAX_CONTENT_LENGTH as NA_MAX_LEN
    app.config['MAX_CONTENT_LENGTH'] = NA_MAX_LEN   # 250MB：增长产品清单原始 216MB
    app.register_blueprint(na_bp)
    NA_ENABLED = True
    NA_IMPORT_ERROR = None
except Exception as _na_e:  # 子系统异常不得拖垮主站
    import traceback as _na_tb
    NA_ENABLED = False
    NA_IMPORT_ERROR = '%s: %s' % (type(_na_e).__name__, _na_e)
    print('[app] 新品盘点子系统注册失败：%s' % NA_IMPORT_ERROR)
    print(_na_tb.format_exc()[-1500:])
    app.config.setdefault('MAX_CONTENT_LENGTH', 250 * 1024 * 1024)


@app.errorhandler(Exception)
def handle_exception(e):
    code = getattr(e, 'code', 500)
    import traceback as _tb
    app.logger.exception('未捕获异常')
    return jsonify({'ok': False,
                    'msg': '服务器错误：' + str(e),
                    'detail': _tb.format_exc()[-300:]}), (code if isinstance(code, int) else 500)


# 登录 / 探测 / 登出 接口已迁移至 auth.py（auth.init_app(app) 注册 /api/login、/api/ping、/logout）


# ---------- 响应压缩（跨公网访问提速：/api/state 明细 JSON 约 19MB，gzip 后约 3~4MB） ----------
_GZIP_MIMES = ('application/json', 'application/javascript', 'text/javascript',
               'text/css', 'text/html')
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 43200   # 静态库（jquery/datatables/echarts）浏览器缓存 12h；
                                                  # app.js / style.css 走 ?v= 版本号刷新，改版必须同步升级版本号


@app.after_request
def _gzip_response(resp):
    """对大响应做 gzip（JSON/JS/CSS/HTML）。xlsx/csv 下载不压（本就压缩/文本导出另有 BOM 处理）。"""
    try:
        if resp.status_code != 200:
            return resp
        if resp.headers.get('Content-Encoding'):
            return resp
        if (resp.mimetype or '') not in _GZIP_MIMES:
            return resp
        if 'gzip' not in (request.headers.get('Accept-Encoding') or '').lower():
            return resp
        if resp.direct_passthrough:
            resp.direct_passthrough = False   # 静态文件(send_file)转缓冲后才能压缩
        data = resp.get_data()
        if len(data) < 1024:
            return resp
        gz = gzip.compress(data, compresslevel=5)
        if len(gz) >= len(data):
            return resp
        resp.set_data(gz)
        resp.headers['Content-Encoding'] = 'gzip'
        resp.headers['Content-Length'] = str(len(gz))
        resp.headers.add('Vary', 'Accept-Encoding')
    except Exception:
        pass
    return resp


# 当前处理结果（内存态，单用户本地工具够用）
STATE = {'header': None, 'rows': None, 'stats': None,
         'filename': None, 'uploaded_at': None,
         'ref_filename': None, 'ref_uploaded_at': None,
         'lx_filename': None, 'lx_uploaded_at': None,
         'wm_filename': None, 'wm_uploaded_at': None,
         'admin_filename': None, 'admin_uploaded_at': None,
         'temu_a_filename': None, 'temu_a_uploaded_at': None,
         'temu_b_filename': None, 'temu_b_uploaded_at': None,
         'ops_filename': None, 'ops_uploaded_at': None}
_LOCK = threading.Lock()


# ------------------------------------------------------------------ 元信息通用读写
def _read_meta_file(path):
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _write_meta_file(path, meta):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _atomic_write_bytes(path, data, retry=6):
    """原子写入字节流到固定路径，带重试，规避 Windows 文件锁 PermissionError。
    写唯一临时文件 -> os.replace 原子替换；replace 若因目标被占用而失败，
    自动回退「删除旧目标再 replace」与「直接覆盖写」，并延长退避。"""
    last_err = None
    for attempt in range(retry):
        tmp = "%s.%d.tmp" % (path, os.getpid())
        # 策略一：临时文件 + os.replace（最优先）
        try:
            with open(tmp, 'wb') as o:
                o.write(data)
            os.replace(tmp, path)
            return True
        except (PermissionError, OSError) as e:
            last_err = e
        # 策略二：目标可能被占导致 replace 失败 -> 尝试删除旧目标再 replace
        try:
            if os.path.exists(path):
                os.remove(path)
            os.replace(tmp, path)
            return True
        except (PermissionError, OSError) as e:
            last_err = e
        # 策略三：直接覆盖写目标（绕过 rename），适合目标被读打开但可写场景
        try:
            with open(path, 'wb') as o:
                o.write(data)
            return True
        except (PermissionError, OSError) as e:
            last_err = e
        # 清理临时文件并退避
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        time.sleep(0.3 * (attempt + 1))
    raise last_err


def _atomic_save_fileobj(path, fileobj, retry=6):
    """同 _atomic_write_bytes，但入参为 flask 上传的 fileobj（读其字节再原子写）。"""
    # fileobj 只能读一次，先整体读入内存缓存，重试时复用
    data = fileobj.read()
    last_err = None
    for attempt in range(retry):
        tmp = "%s.%d.tmp" % (path, os.getpid())
        try:
            with open(tmp, 'wb') as o:
                o.write(data)
            os.replace(tmp, path)
            return True
        except (PermissionError, OSError) as e:
            last_err = e
        try:
            if os.path.exists(path):
                os.remove(path)
            os.replace(tmp, path)
            return True
        except (PermissionError, OSError) as e:
            last_err = e
        try:
            with open(path, 'wb') as o:
                o.write(data)
            return True
        except (PermissionError, OSError) as e:
            last_err = e
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        time.sleep(0.3 * (attempt + 1))
    raise last_err


# ------------------------------------------------------------------ 转单表管理
def _read_ref_meta():
    return _read_meta_file(REF_META)


def _write_ref_meta(meta):
    _write_meta_file(REF_META, meta)


def _bootstrap_ref():
    """首次启动：若已存在旧的内置 reference/转单.xlsx 而无 meta，登记为初始版本。"""
    if _read_ref_meta():
        return
    legacy = os.path.join(REF_DIR, '转单.xlsx')
    if not os.path.exists(legacy):
        # 兜底：Linux unzip 解压中文文件名可能变乱码，按名字找不到时
        # 自动登记 reference/ 下任一 xlsx/xlsm（2026-10-22 ECS 部署实踩）
        try:
            cands = sorted(n for n in os.listdir(REF_DIR)
                           if n.lower().endswith(('.xlsx', '.xlsm')) and not n.startswith('~$'))
        except OSError:
            cands = []
        if not cands:
            return
        legacy = os.path.join(REF_DIR, cands[0])
    try:
        summary = summarize_transfer(legacy)
    except Exception:
        return
    ts = datetime.fromtimestamp(os.path.getmtime(legacy)).strftime('%Y-%m-%d %H:%M:%S')
    _write_ref_meta({'stored': os.path.basename(legacy), 'filename': '转单.xlsx',
                     'uploaded_at': ts, 'summary': summary})


def current_ref():
    """返回 (绝对路径, meta) ；无可用转单表时返回 (None, None)。"""
    meta = _read_ref_meta()
    if not meta:
        return None, None
    p = os.path.join(REF_DIR, meta['stored'])
    if not os.path.exists(p):
        return None, None
    return p, meta


def _ref_history():
    """reference 目录下的历史版本（按修改时间倒序）。"""
    out = []
    for n in os.listdir(REF_DIR):
        if n.lower().endswith(('.xlsx', '.xlsm')) and not n.startswith('~$'):
            p = os.path.join(REF_DIR, n)
            out.append({'stored': n,
                        'size_kb': round(os.path.getsize(p) / 1024, 1),
                        'mtime': datetime.fromtimestamp(
                            os.path.getmtime(p)).strftime('%Y-%m-%d %H:%M:%S')})
    out.sort(key=lambda x: x['mtime'], reverse=True)
    return out


# ------------------------------------------------------------------ 结果缓存
def _save_state(retry=3):
    payload = {k: STATE[k] for k in
               ('header', 'rows', 'stats', 'filename', 'uploaded_at',
                'ref_filename', 'ref_uploaded_at',
                'lx_filename', 'lx_uploaded_at',
                'wm_filename', 'wm_uploaded_at',
                'admin_filename', 'admin_uploaded_at',
                'temu_a_filename', 'temu_a_uploaded_at',
                'temu_b_filename', 'temu_b_uploaded_at',
                'ops_filename', 'ops_uploaded_at')}
    last_err = None
    for attempt in range(retry):
        try:
            tmp = CURRENT_JSON + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False)
            # os.replace 原子替换（Windows 下 shutil.move 会触发回收站限制）
            os.replace(tmp, CURRENT_JSON)
            return True
        except (PermissionError, OSError) as e:
            last_err = e
            time.sleep(0.2 * (attempt + 1))
    raise last_err


def _load_state():
    if not os.path.exists(CURRENT_JSON):
        return False
    try:
        with open(CURRENT_JSON, encoding='utf-8') as f:
            d = json.load(f)
        for k in ('header', 'rows', 'stats', 'filename', 'uploaded_at',
                  'ref_filename', 'ref_uploaded_at',
                  'lx_filename', 'lx_uploaded_at',
                  'wm_filename', 'wm_uploaded_at',
                  'admin_filename', 'admin_uploaded_at',
                  'temu_a_filename', 'temu_a_uploaded_at',
                  'temu_b_filename', 'temu_b_uploaded_at',
                  'ops_filename', 'ops_uploaded_at'):
            STATE[k] = d.get(k)
        return True
    except Exception:
        return False


_bootstrap_ref()
_load_state()


# ------------------------------------------------------------------ 数据源（领星/walmart）管理
def _current_admin_path():
    """返回当前生效的管理员检查表留档路径（xlsx/csv 按实际上传格式）。无则返回 None。"""
    for p in (LATEST_ADMIN_XLSX, LATEST_ADMIN_CSV):
        if os.path.exists(p):
            return p
    return None


def _current_ds():
    """返回本次重算应使用的数据源路径（存在才用）。"""
    lx = LATEST_LINGXING if os.path.exists(LATEST_LINGXING) else None
    wm = LATEST_WALMART if os.path.exists(LATEST_WALMART) else None
    admin = _current_admin_path()
    temu = [p for p in (LATEST_TEMU_A, LATEST_TEMU_B) if os.path.exists(p)]
    ops = LATEST_OPS if os.path.exists(LATEST_OPS) else None
    return lx, wm, admin, temu, ops


def _temu_meta_entry(m):
    """meta 条目 -> 展示结构（None 安全）。"""
    if not m:
        return None
    return {'filename': m.get('filename'),
            'uploaded_at': m.get('uploaded_at'),
            'summary': m.get('summary')}


def _ensure_temu_meta():
    """Temu meta 归一为 {'a': entry|None, 'b': entry|None}（A/B 双槽，2026-10-12 多文件上传改造）。

    兼容旧单文件格式（temu_meta.json 为 flat 结构 + uploads/latest_temu.xlsx）：
    首次访问时按 temu_file_types 自动归类迁移到对应槽并同步云端新对象键；
    此后旧文件 latest_temu.xlsx 不再参与重算。"""
    m = _read_meta_file(TEMU_META)
    if m and ('a' in m or 'b' in m):
        return {'a': _temu_meta_entry(m.get('a')), 'b': _temu_meta_entry(m.get('b'))}
    # ---- 旧单文件格式 -> 迁移 ----
    new = {'a': None, 'b': None}
    if os.path.exists(LATEST_TEMU):
        try:
            from pipeline import temu_file_types
            types = temu_file_types(LATEST_TEMU)
        except Exception:
            types = set()
        entry = (_temu_meta_entry(m)
                 or {'filename': 'Temu 报表（迁移）', 'uploaded_at': now_bj(), 'summary': {}})
        if entry.get('summary') is None:
            entry['summary'] = {}
        try:
            with open(LATEST_TEMU, 'rb') as _tf:
                raw = _tf.read()
        except OSError:
            raw = None
        if raw:
            if 'A' in types:
                _atomic_write_bytes(LATEST_TEMU_A, raw)
                new['a'] = entry
                _cloud_sync(storage.OBJ_TEMU_A, raw)
            if 'B' in types:
                _atomic_write_bytes(LATEST_TEMU_B, raw)
                new['b'] = entry
                _cloud_sync(storage.OBJ_TEMU_B, raw)
    # 先落新格式 meta（避免 _sync_source_meta -> _ds_info -> 本函数 递归再迁移），再同步共享元信息
    _write_meta_file(TEMU_META, new)
    try:
        _sync_source_meta()
    except Exception:
        pass
    return new


def _ds_info():
    lxm = _read_meta_file(LINGXING_META)
    wmm = _read_meta_file(WALMART_META)
    adm = _read_meta_file(ADMIN_META)
    ops = _read_meta_file(OPS_META)
    temu = _ensure_temu_meta()
    return {
        'lingxing': ({'filename': lxm.get('filename'),
                      'uploaded_at': lxm.get('uploaded_at'),
                      'summary': lxm.get('summary')} if lxm else None),
        'walmart': ({'filename': wmm.get('filename'),
                     'uploaded_at': wmm.get('uploaded_at'),
                     'summary': wmm.get('summary')} if wmm else None),
        'admin': ({'filename': adm.get('filename'),
                   'uploaded_at': adm.get('uploaded_at'),
                   'summary': adm.get('summary')} if adm else None),
        'temu': temu,
        'ops': ({'filename': ops.get('filename'),
                 'uploaded_at': ops.get('uploaded_at'),
                 'summary': ops.get('summary')} if ops else None),
    }


def _raw_info():
    """当前生效原始表的共享展示信息。"""
    meta = _read_meta_file(RAW_META)
    if meta:
        return {'filename': meta.get('filename'),
                'uploaded_at': meta.get('uploaded_at'),
                'summary': meta.get('summary') or {}}
    if not os.path.exists(LATEST_RAW):
        return None
    return {'filename': STATE.get('filename') or 'latest_raw.csv',
            'uploaded_at': STATE.get('uploaded_at'),
            'summary': {'rows': (STATE.get('stats') or {}).get('original', 0)}}


def _summarize_raw(path):
    """轻量校验原始 CSV，上传源文件时不触发完整清洗。"""
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


def _sync_source_meta():
    """把当前生效的数据源版本写入共享元信息，供所有用户和冷启动恢复展示。"""
    _, ref = current_ref()
    ds = _ds_info()
    meta = storage.ds_get() or {}
    meta.update({
        'raw': _raw_info(),
        'reference': ({'filename': ref.get('filename'),
                       'uploaded_at': ref.get('uploaded_at'),
                       'summary': ref.get('summary')} if ref else None),
        'lingxing': ds['lingxing'],
        'walmart': ds['walmart'],
        'admin': ds['admin'],
        'filename': STATE.get('filename'),
        'uploaded_at': STATE.get('uploaded_at'),
        'ref_filename': STATE.get('ref_filename'),
        'ref_uploaded_at': STATE.get('ref_uploaded_at'),
        'lx_filename': STATE.get('lx_filename'),
        'lx_uploaded_at': STATE.get('lx_uploaded_at'),
        'wm_filename': STATE.get('wm_filename'),
        'wm_uploaded_at': STATE.get('wm_uploaded_at'),
        'admin_filename': STATE.get('admin_filename'),
        'admin_uploaded_at': STATE.get('admin_uploaded_at'),
        'temu': ds['temu'],
        'temu_a_filename': STATE.get('temu_a_filename'),
        'temu_a_uploaded_at': STATE.get('temu_a_uploaded_at'),
        'temu_b_filename': STATE.get('temu_b_filename'),
        'temu_b_uploaded_at': STATE.get('temu_b_uploaded_at'),
        'ops': ds['ops'],
        'ops_filename': STATE.get('ops_filename'),
        'ops_uploaded_at': STATE.get('ops_uploaded_at'),
    })
    storage.ds_update(meta)


def _cloud_sync(key, data):
    """把数据源二进制同步到云端数据库（跨冷启动持久化）。

    返回 '' 表示成功；否则返回失败原因字符串（供接口回传给前端排障）。
    不再静默吞掉写入失败——这正是此前「网页上传显示成功，但 Render 冷启动后
    数据消失」的接口层根因：整文件单行写到 Supabase 会静默失败。
    """
    try:
        if not storage.has_cloud():
            return '未启用云端存储（STORAGE_MODE≠supabase/oss）'
        if storage.ds_file_upload(key, data):
            return ''
        return storage.last_error() or '云端写入返回 False（原因未知）'
    except Exception as e:
        return '云端写入异常：%r' % e


def _run_and_store(raw_path, raw_name, keep_uploaded_at=False):
    """跑流水线并写入 STATE / 缓存。返回 (ok, payload_or_msg, http_code)

    keep_uploaded_at=True 时不更新「数据更新时间」（重算/上传了相同文件的情况，
    业务口径：只有上传【新的】原始表格，数据更新时间才变）。
    """
    ref_path, meta = current_ref()
    if not ref_path:
        progress_end(False, '尚未上传转单表')
        return False, '尚未上传转单表，请先上传「转单.xlsx」', 400
    lx_path, wm_path, admin_path, temu_paths, ops_path = _current_ds()
    progress_start('读取原始报表…')
    try:
        header, rows, stats = run_pipeline(raw_path, ref_path,
                                           lingxing_path=lx_path,
                                           walmart_path=wm_path,
                                           admin_check_path=admin_path,
                                           temu_paths=temu_paths,
                                           ops_path=ops_path,
                                           progress=progress_set)
    except ValueError as e:
        progress_end(False, e)
        return False, str(e), 400
    except Exception as e:
        progress_end(False, e)
        return False, '处理失败：' + str(e), 500

    progress_set(97, '写入看板数据…')
    lxm = _read_meta_file(LINGXING_META)
    wmm = _read_meta_file(WALMART_META)
    adm = _read_meta_file(ADMIN_META)
    opm = _read_meta_file(OPS_META)
    tm = _ensure_temu_meta()
    with _LOCK:
        STATE['header'] = header
        STATE['rows'] = rows
        STATE['stats'] = stats
        STATE['filename'] = raw_name
        # 业务口径：只有上传【新的】原始表格才更新「数据更新时间」（北京时间）
        if not keep_uploaded_at or not STATE.get('uploaded_at'):
            STATE['uploaded_at'] = now_bj()
        STATE['ref_filename'] = meta.get('filename')
        STATE['ref_uploaded_at'] = meta.get('uploaded_at')
        STATE['lx_filename'] = (lxm or {}).get('filename')
        STATE['lx_uploaded_at'] = (lxm or {}).get('uploaded_at')
        STATE['wm_filename'] = (wmm or {}).get('filename')
        STATE['wm_uploaded_at'] = (wmm or {}).get('uploaded_at')
        STATE['admin_filename'] = (adm or {}).get('filename')
        STATE['admin_uploaded_at'] = (adm or {}).get('uploaded_at')
        STATE['temu_a_filename'] = ((tm.get('a') or {}).get('filename'))
        STATE['temu_a_uploaded_at'] = ((tm.get('a') or {}).get('uploaded_at'))
        STATE['temu_b_filename'] = ((tm.get('b') or {}).get('filename'))
        STATE['temu_b_uploaded_at'] = ((tm.get('b') or {}).get('uploaded_at'))
        STATE['ops_filename'] = (opm or {}).get('filename')
        STATE['ops_uploaded_at'] = (opm or {}).get('uploaded_at')
        _save_state()
        # 数据源版本元信息同步到存储层（JSON / Supabase），保证多人看到同一套口径
        try:
            _sync_source_meta()
        except Exception:
            pass
        # 把云端/本地已存的异常确认，回填本次 rows 的异常确认两列
        _apply_annotations()
    progress_end(True)
    return True, {'ok': True, 'stats': stats, 'header': header,
                  'filename': STATE['filename'],
                  'uploaded_at': STATE['uploaded_at'],
                  'ref_filename': STATE['ref_filename'],
                  'ref_uploaded_at': STATE['ref_uploaded_at'],
                  'admin_filename': STATE['admin_filename'],
                  'admin_uploaded_at': STATE['admin_uploaded_at'],
                  'temu_a_filename': STATE['temu_a_filename'],
                  'temu_a_uploaded_at': STATE['temu_a_uploaded_at'],
                  'temu_b_filename': STATE['temu_b_filename'],
                  'temu_b_uploaded_at': STATE['temu_b_uploaded_at'],
                  'ops_filename': STATE['ops_filename'],
                  'ops_uploaded_at': STATE['ops_uploaded_at']}, 200


def _row_id_key(row):
    """行的稳定主键：ID + 库存SKU(成员货品) 组合，保证同 ID 多行（同一 listing 拆出不同成员货品）也能区分。
    数据实测：64600 行该组合键 0 重复，异常行 27 条各自唯一。
    极端情况下再叠加 上架SKU 兜底，仍不唯一则退回行号（保底不覆盖）。"""
    try:
        idv = row[STATE['header'].index('ID')] if 'ID' in STATE['header'] else ''
    except Exception:
        idv = ''
    idv = (idv or '').strip()
    try:
        mem = row[STATE['header'].index('库存SKU')] if '库存SKU' in STATE['header'] else ''
    except Exception:
        mem = ''
    mem = (mem or '').strip()
    if idv:
        # ID 非空：ID + 成员货品
        if mem:
            return idv + '｜' + mem
        return idv + '｜（无成员）'
    # ID 为空：上架SKU + 库存SKU 兜底
    sku = row[STATE['header'].index('上架SKU')] if '上架SKU' in STATE['header'] else ''
    return '%s|%s' % ((sku or '').strip(), mem)


def _apply_annotations():
    """把 storage 里已持久化的异常确认(status/note)回填到 STATE['rows'] 对应行。"""
    try:
        ann = storage.annot_get()
    except Exception:
        return
    if not ann or STATE['rows'] is None:
        return
    hi = STATE['header'].index('异常确认状态')
    ni = STATE['header'].index('异常确认备注')
    for r in STATE['rows']:
        k = _row_id_key(r)
        if k in ann:
            r[hi] = ann[k].get('status', '')
            r[ni] = ann[k].get('note', '')


def _ensure_cloud_ds():
    """冷启动时，若云端 Storage 已有更新版数据源，则拉取覆盖本地留档文件。
    这样 Render/HF 休眠重启后能用「网页上传的最新数据源」重建，而非仓库里提交的旧数据源。
    返回 (has_raw, has_ref) —— 是否能从云取到原始表 / 转单表。"""
    if not storage.has_cloud():
        return False, False
    ok_raw = ok_ref = False
    try:
        cloud_meta = storage.ds_get() or {}
    except Exception:
        cloud_meta = {}
    # 原始报表
    try:
        data = storage.ds_file_download(storage.OBJ_RAW)
        if data:
            _atomic_write_bytes(LATEST_RAW, data)
            raw_meta = dict(cloud_meta.get('raw') or {})
            raw_meta.setdefault('filename', cloud_meta.get('filename') or 'latest_raw.csv')
            raw_meta.setdefault('uploaded_at', cloud_meta.get('uploaded_at') or now_bj())
            try:
                raw_meta.setdefault('summary', _summarize_raw(LATEST_RAW))
            except Exception:
                raw_meta.setdefault('summary', {})
            _write_meta_file(RAW_META, raw_meta)
            ok_raw = True
    except Exception:
        pass
    # 转单表：云端覆盖本地当前 reference（存为新版本文件并更新 meta）
    try:
        data = storage.ds_file_download(storage.OBJ_REF)
        if data:
            stored = '转单_cloud_%s.xlsx' % now_bj('%Y%m%d%H%M%S')
            p = os.path.join(REF_DIR, stored)
            _atomic_write_bytes(p, data)
            try:
                summary = summarize_transfer(p)
            except Exception:
                summary = None
            if summary:
                _write_ref_meta({'stored': stored, 'filename': '转单.xlsx',
                                 'uploaded_at': now_bj(),
                                 'summary': summary})
                ok_ref = True
    except Exception:
        pass
    # 领星 / walmart（可选，取到就覆盖留档 + 重建 meta，页面才能显示「已上传」）
    try:
        data = storage.ds_file_download(storage.OBJ_LX)
        if data:
            _atomic_write_bytes(LATEST_LINGXING, data)
            # 重建领星 meta（上传接口写 meta 的运行期文件，这里冷启动需同一套生成逻辑）
            try:
                _, summary = load_lingxing(LATEST_LINGXING)
                _write_meta_file(LINGXING_META, {'filename': 'Listing（云端同步）',
                                                 'uploaded_at': now_bj(),
                                                 'summary': summary})
            except Exception:
                pass
    except Exception:
        pass
    try:
        data = storage.ds_file_download(storage.OBJ_WM)
        if data:
            _atomic_write_bytes(LATEST_WALMART, data)
            # 重建 walmart meta
            try:
                _, summary = load_walmart(LATEST_WALMART)
                _write_meta_file(WALMART_META, {'filename': 'ItemReport（云端同步）',
                                                'uploaded_at': now_bj(),
                                                'summary': summary})
            except Exception:
                pass
    except Exception:
        pass
    # 管理员检查表（品类）：云端覆盖本地留档 + 重建 meta
    try:
        data = storage.ds_file_download(storage.OBJ_ADMIN)
        if data:
            # 云端对象无扩展名信息，先尝试按 xlsx 解析，失败再按 csv
            _atomic_write_bytes(LATEST_ADMIN_XLSX, data)
            try:
                from pipeline import load_admin_check
                _, summary = load_admin_check(LATEST_ADMIN_XLSX)
                _write_meta_file(ADMIN_META, {'filename': '管理员检查表（云端同步）',
                                             'uploaded_at': now_bj(),
                                             'summary': summary})
            except Exception:
                # 不是 xlsx：当作 csv 处理
                try:
                    os.remove(LATEST_ADMIN_XLSX)
                except OSError:
                    pass
                _atomic_write_bytes(LATEST_ADMIN_CSV, data)
                try:
                    _, summary = load_admin_check(LATEST_ADMIN_CSV)
                    _write_meta_file(ADMIN_META, {'filename': '管理员检查表（云端同步）',
                                                 'uploaded_at': now_bj(),
                                                 'summary': summary})
                except Exception:
                    pass
    except Exception:
        pass
    # Temu 报表（第 6 步）：云端 A/B 双槽拉取 + 旧单对象经本地迁移自动归类
    try:
        tm = _ensure_temu_meta()   # 旧格式迁移（含旧云对象归类落槽 + 同步新键）
        changed = False
        for slot, obj_key, slot_path in (('a', storage.OBJ_TEMU_A, LATEST_TEMU_A),
                                         ('b', storage.OBJ_TEMU_B, LATEST_TEMU_B)):
            try:
                data = storage.ds_file_download(obj_key)
            except Exception:
                data = None
            if not data:
                continue
            _atomic_write_bytes(slot_path, data)
            try:
                _, trows = load_temu(slot_path)
            except Exception:
                continue
            # summary 只存计数（与上传接口同构）；此前误存整表行数组，
            # 导致 /api/status 膨胀至 1MB+ 且前端「解析 N 行」显示 0
            tm[slot] = {'filename': 'Temu %s 类报表（云端同步）' % slot.upper(),
                        'uploaded_at': now_bj(),
                        'summary': {'rows': len(trows), 'sheets_parsed': True}}
            changed = True
        if changed:
            _write_meta_file(TEMU_META, tm)
    except Exception:
        pass
    # 产品运营表（第 7 步）：云端覆盖本地留档 + 重建 meta
    try:
        data = storage.ds_file_download(storage.OBJ_OPS)
        if data:
            _atomic_write_bytes(LATEST_OPS, data)
            try:
                from pipeline import load_ops
                ops_map = load_ops(LATEST_OPS)
                _write_meta_file(OPS_META, {'filename': '产品运营表（云端同步）',
                                            'uploaded_at': now_bj(),
                                            'summary': {'sheets': sorted(ops_map.keys()),
                                                        'rows': sum(len(v) for v in ops_map.values())}})
            except Exception:
                pass
    except Exception:
        pass
    return ok_raw, ok_ref


def _boot_rebuild():
    """云端/冷启动：若主数据未加载(空看板)且数据源齐备，则自动跑一次流水线重建。
    Hugging Face / Render 等平台磁盘是临时/全新的，data/current.json 会丢失，
    但数据源(reference/转单.xlsx + uploads/latest_raw.csv + 领星/walmart 留档)
    随代码仓库一起部署，故重启后自动重建，避免「打开看板是空的」。
    优先从云端 Storage 拉取「网页上传的最新数据源」重建（覆盖仓库旧数据源），
    云端无数据源时回退到仓库内提交的数据源。幂等：同进程只重建一次；已加载数据则跳过。
    人工确认字段走 storage 层，不受影响。"""
    if STATE['rows'] is not None:
        return
    if getattr(_boot_rebuild, '_done', False):
        return
    # 优先从云端拉取最新数据源（覆盖本地留档），失败则用仓库内数据源兜底
    try:
        _ensure_cloud_ds()
    except Exception:
        pass
    if not os.path.exists(LATEST_RAW):
        app.logger.info('冷启动：无原始 CSV(latest_raw.csv)，跳过自动重建')
        return
    ref_path, _ = current_ref()
    if not ref_path:
        app.logger.info('冷启动：无转单表，跳过自动重建')
        return
    _boot_rebuild._done = True
    try:
        app.logger.info('冷启动：检测到空看板，正在自动重建数据…')
        ok, _payload, code = _run_and_store(LATEST_RAW, 'latest_raw.csv')
        app.logger.info('冷启动自动重建完成：ok=%s http=%s', ok, code)
    except Exception as e:
        app.logger.error('冷启动自动重建失败：%s', e)


# ------------------------------------------------------------------ 路由
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/console')
@auth.role_required('sub')
def console():
    """统一工作台外壳：左侧导航（4 模块）+ 右侧 iframe。
    模块一 SKU&SPU数据清洗 / 模块四 系统设置 仅管理员可见；运营(sub)仅见新品/新色跟进。"""
    return render_template('console_index.html',
                           is_admin=auth.current_role() == 'admin')


@app.route('/api/progress')
def api_progress():
    """计算进度（前端轮询显示进度条）。返回 {running, pct, stage, elapsed, ...}"""
    with _PROGRESS_LOCK:
        snap = dict(PROGRESS)
    # 已耗时（秒）
    elapsed = None
    if snap.get('started_at'):
        try:
            t0 = datetime.strptime(snap['started_at'], '%Y-%m-%d %H:%M:%S')
            elapsed = round((datetime.now(BJ_TZ) - t0.replace(tzinfo=BJ_TZ)).total_seconds(), 1)
        except Exception:
            elapsed = None
    snap['elapsed'] = elapsed
    return jsonify({'ok': True, 'progress': snap})


@app.route('/api/status')
def api_status():
    _, meta = current_ref()
    ds = _ds_info()
    lxm = ds['lingxing']
    wmm = ds['walmart']
    adm = ds['admin']
    temu = ds['temu']
    return jsonify({
        'ready': STATE['rows'] is not None,
        'shared_storage': storage.has_cloud(),
        'filename': STATE['filename'],
        'uploaded_at': STATE['uploaded_at'],
        'stats': STATE['stats'],
        'header': STATE['header'],
        'has_raw': os.path.exists(LATEST_RAW),
        'raw': _raw_info(),
        'reference': ({'filename': meta['filename'],
                       'uploaded_at': meta['uploaded_at'],
                       'summary': meta.get('summary')} if meta else None),
        'ref_used': {'filename': STATE['ref_filename'],
                     'uploaded_at': STATE['ref_uploaded_at']},
        'ref_stale': bool(meta and STATE['rows'] is not None
                          and STATE['ref_uploaded_at'] != meta['uploaded_at']),
        'lingxing': lxm,
        'walmart': wmm,
        'admin': adm,
        'temu': temu,
        'lx_used': {'filename': STATE['lx_filename'],
                    'uploaded_at': STATE['lx_uploaded_at']},
        'wm_used': {'filename': STATE['wm_filename'],
                    'uploaded_at': STATE['wm_uploaded_at']},
        'admin_used': {'filename': STATE['admin_filename'],
                       'uploaded_at': STATE['admin_uploaded_at']},
        'temu_used': {'a': {'filename': STATE['temu_a_filename'],
                            'uploaded_at': STATE['temu_a_uploaded_at']},
                      'b': {'filename': STATE['temu_b_filename'],
                            'uploaded_at': STATE['temu_b_uploaded_at']}},
        'lx_stale': bool(lxm and STATE['rows'] is not None
                         and STATE['lx_uploaded_at'] != lxm['uploaded_at']),
        'wm_stale': bool(wmm and STATE['rows'] is not None
                         and STATE['wm_uploaded_at'] != wmm['uploaded_at']),
        'admin_stale': bool(adm and STATE['rows'] is not None
                            and STATE['admin_uploaded_at'] != adm['uploaded_at']),
        'temu_stale': bool(STATE['rows'] is not None and
                           ((temu.get('a') and STATE['temu_a_uploaded_at'] != temu['a']['uploaded_at']) or
                            (temu.get('b') and STATE['temu_b_uploaded_at'] != temu['b']['uploaded_at']))),
        'ops': ds['ops'],
        'ops_used': {'filename': STATE['ops_filename'],
                     'uploaded_at': STATE['ops_uploaded_at']},
        'ops_stale': bool(ds['ops'] and STATE['rows'] is not None
                          and STATE['ops_uploaded_at'] != ds['ops']['uploaded_at']),
        'cloud_error': storage.last_error(),
    })


@app.route('/api/reference')
def api_reference():
    _, meta = current_ref()
    return jsonify({'ok': True,
                    'reference': meta,
                    'history': _ref_history()})


@app.route('/api/reference/upload', methods=['POST'])
def api_reference_upload():
    """上传/更新转单表（xlsx）。保留历史版本，切换为当前版本。"""
    if 'file' not in request.files:
        return jsonify({'ok': False, 'msg': '未收到文件'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'ok': False, 'msg': '文件名为空'}), 400
    if not f.filename.lower().endswith(('.xlsx', '.xlsm')):
        return jsonify({'ok': False, 'msg': '转单表需为 .xlsx 文件'}), 400

    ts = now_bj('%Y%m%d%H%M%S')
    stored = '转单_%s.xlsx' % ts
    path = os.path.join(REF_DIR, stored)
    f.save(path)

    # 解析校验：不合格直接丢弃，保持旧版本可用
    try:
        summary = summarize_transfer(path)
    except Exception as e:
        try:
            os.remove(path)
        except OSError:
            pass
        return jsonify({'ok': False, 'msg': '转单表解析失败：' + str(e)}), 400

    meta = {'stored': stored, 'filename': f.filename,
            'uploaded_at': now_bj(),
            'summary': summary}
    _write_ref_meta(meta)
    # 校验通过后同步到云 Storage（供冷启动取最新转单表）；失败回传原因便于排障
    with open(path, 'rb') as _rf:
        cloud_err = _cloud_sync(storage.OBJ_REF, _rf.read())
    try:
        _sync_source_meta()
    except Exception:
        pass
    return jsonify({'ok': True, 'reference': meta,
                    'has_raw': os.path.exists(LATEST_RAW),
                    'cloud_error': cloud_err,
                    'ready': STATE['rows'] is not None})


@app.route('/api/upload/lingxing', methods=['POST'])
def api_upload_lingxing():
    """上传/更新领星清单 xlsx（MSKU/状态/店铺/ASIN）。校验后留档，供重算。"""
    if 'file' not in request.files:
        return jsonify({'ok': False, 'msg': '未收到文件'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'ok': False, 'msg': '文件名为空'}), 400
    if not f.filename.lower().endswith(('.xlsx', '.xlsm')):
        return jsonify({'ok': False, 'msg': '领星清单需为 .xlsx 文件'}), 400

    _atomic_save_fileobj(LATEST_LINGXING, f)   # 原子写，规避 Windows 文件锁
    try:
        match_map, summary = load_lingxing(LATEST_LINGXING)
    except Exception as e:
        try:
            os.remove(LATEST_LINGXING)
        except OSError:
            pass
        return jsonify({'ok': False, 'msg': '领星清单解析失败：' + str(e)}), 400
    if not match_map:
        try:
            os.remove(LATEST_LINGXING)
        except OSError:
            pass
        return jsonify({'ok': False, 'msg': '领星清单未匹配到任何可用的 (店铺,MSKU) 记录'
                                            '（检查「店铺」是否在映射表内）'}), 400

    meta = {'filename': f.filename,
            'uploaded_at': now_bj(),
            'summary': summary}
    _write_meta_file(LINGXING_META, meta)
    # 校验通过后同步到云 Storage；失败回传原因便于排障
    with open(LATEST_LINGXING, 'rb') as _lf:
        cloud_err = _cloud_sync(storage.OBJ_LX, _lf.read())
    try:
        _sync_source_meta()
    except Exception:
        pass
    return jsonify({'ok': True, 'lingxing': meta,
                    'cloud_error': cloud_err,
                    'ready': STATE['rows'] is not None,
                    'needs_rebuild': STATE['rows'] is not None})


@app.route('/api/upload/walmart', methods=['POST'])
def api_upload_walmart():
    """上传/更新 walmart 后台报表（.csv，或 .zip 内含 csv）。
    SKU / Item ID / Publish Status。校验后留档，供重算。"""
    if 'file' not in request.files:
        return jsonify({'ok': False, 'msg': '未收到文件'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'ok': False, 'msg': '文件名为空'}), 400
    name = f.filename.lower()
    if not (name.endswith('.csv') or name.endswith('.zip')):
        return jsonify({'ok': False, 'msg': 'walmart 报表需为 .csv 或 .zip 文件'}), 400

    # zip 包：解压取第一个 csv（walmart 后台下载的报表是 zip 打包）
    if name.endswith('.zip'):
        try:
            zf = zipfile.ZipFile(io.BytesIO(f.read()))
            csv_names = [n for n in zf.namelist()
                         if n.lower().endswith('.csv') and not n.startswith('__MACOSX')]
            if not csv_names:
                return jsonify({'ok': False, 'msg': 'zip 内未找到 csv 文件'}), 400
            data = zf.read(csv_names[0])
            zf.close()
        except zipfile.BadZipFile:
            return jsonify({'ok': False, 'msg': 'zip 文件损坏或格式不正确'}), 400
        _atomic_write_bytes(LATEST_WALMART, data)   # 原子写，规避 Windows 文件锁
        stored_name = os.path.basename(csv_names[0])
    else:
        _atomic_save_fileobj(LATEST_WALMART, f)      # 原子写，规避 Windows 文件锁
        stored_name = f.filename

    try:
        match_map, summary = load_walmart(LATEST_WALMART)
    except Exception as e:
        try:
            os.remove(LATEST_WALMART)
        except OSError:
            pass
        return jsonify({'ok': False, 'msg': 'walmart 报表解析失败：' + str(e)}), 400
    if not match_map:
        try:
            os.remove(LATEST_WALMART)
        except OSError:
            pass
        return jsonify({'ok': False, 'msg': 'walmart 报表未解析到任何 SKU 记录'}), 400

    meta = {'filename': stored_name,
            'uploaded_at': now_bj(),
            'summary': summary}
    _write_meta_file(WALMART_META, meta)
    # 校验通过后同步到云 Storage；失败回传原因便于排障
    with open(LATEST_WALMART, 'rb') as _wf:
        cloud_err = _cloud_sync(storage.OBJ_WM, _wf.read())
    try:
        _sync_source_meta()
    except Exception:
        pass
    return jsonify({'ok': True, 'walmart': meta,
                    'cloud_error': cloud_err,
                    'ready': STATE['rows'] is not None,
                    'needs_rebuild': STATE['rows'] is not None})


@app.route('/api/upload/admin', methods=['POST'])
def api_upload_admin():
    """上传/更新「所有库存货品管理员检查表」（.xlsx 或 .csv）。
    需含列 唯一SKU / 名称（匹配键）与 品类_3（取值）。校验后留档，供重算回填「品类」列。"""
    if 'file' not in request.files:
        return jsonify({'ok': False, 'msg': '未收到文件'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'ok': False, 'msg': '文件名为空'}), 400
    name = f.filename.lower()
    if not (name.endswith('.xlsx') or name.endswith('.xlsm') or name.endswith('.csv')):
        return jsonify({'ok': False, 'msg': '管理员检查表需为 .xlsx 或 .csv 文件'}), 400

    raw = f.read()
    admin_path = LATEST_ADMIN_CSV if name.endswith('.csv') else LATEST_ADMIN_XLSX
    # 若之前存过另一种格式，先清掉，避免 _current_admin_path 误判
    try:
        os.remove(LATEST_ADMIN_XLSX if name.endswith('.csv') else LATEST_ADMIN_CSV)
    except OSError:
        pass
    _atomic_write_bytes(admin_path, raw)        # 按原格式（xlsx/csv）字节保存
    stored_name = f.filename

    try:
        from pipeline import load_admin_check
        match_map, summary = load_admin_check(admin_path)
    except Exception as e:
        try:
            os.remove(admin_path)
        except OSError:
            pass
        return jsonify({'ok': False, 'msg': '管理员检查表解析失败：' + str(e)}), 400
    if not match_map:
        try:
            os.remove(admin_path)
        except OSError:
            pass
        return jsonify({'ok': False, 'msg': '管理员检查表未匹配到任何 SKU（检查是否有「唯一SKU」或「名称」列）'}), 400

    meta = {'filename': stored_name,
            'uploaded_at': now_bj(),
            'summary': summary}
    _write_meta_file(ADMIN_META, meta)
    # 校验通过后同步到云 Storage；失败回传原因便于排障
    with open(admin_path, 'rb') as _af:
        cloud_err = _cloud_sync(storage.OBJ_ADMIN, _af.read())
    try:
        _sync_source_meta()
    except Exception:
        pass
    return jsonify({'ok': True, 'admin': meta,
                    'cloud_error': cloud_err,
                    'ready': STATE['rows'] is not None,
                    'needs_rebuild': STATE['rows'] is not None})


@app.route('/api/upload/temu', methods=['POST'])
def api_upload_temu():
    """上传/更新「Temu 报表」（第 6 步独立上传源，Option ②）。

    2026-10-12 多文件改造：支持一次选择 1~2 个文件，A/B 类按各 sheet 表头自动判定、
    分别留档 latest_temu_a.xlsx / latest_temu_b.xlsx（同类型后传覆盖前传；
    单文件同时含 A/B sheet 时两槽共用该文件）。全部文件解析校验通过才落盘
    （任一失败整体不入库），供重算合并（成员货品键：富化已有 + 仅追加全新）。"""
    files = [f for f in request.files.getlist('file') if f and f.filename]
    if not files:
        return jsonify({'ok': False, 'msg': '未收到文件'}), 400
    for f in files:
        name = f.filename.lower()
        if not (name.endswith('.xlsx') or name.endswith('.xlsm')):
            return jsonify({'ok': False, 'msg': 'Temu 报表需为 .xlsx 文件：%s' % f.filename}), 400

    from pipeline import temu_file_types
    tmp_path = os.path.join(UPLOAD_DIR, '_temu_uploading.xlsx')  # 解析中转（保持 .xlsx 后缀，openpyxl 按扩展名校验）
    parsed = []                                 # {'filename','raw','types','rows'}
    try:
        for f in files:
            raw = f.read()
            _atomic_write_bytes(tmp_path, raw)
            try:
                types = temu_file_types(tmp_path) or {'B'}   # 判不出类型（异常表头）按 B 兜底
                _, trows = load_temu(tmp_path)               # 坏结构/空数据在此抛错
            except Exception as e:
                return jsonify({'ok': False,
                                'msg': 'Temu 报表解析失败（%s）：%s' % (f.filename, e)}), 400
            if not trows:
                return jsonify({'ok': False,
                                'msg': 'Temu 报表未解析到任何数据行（%s，检查是否含「店铺」/「MSKU」或「SKU」列）' % f.filename}), 400
            parsed.append({'filename': f.filename, 'raw': raw,
                           'types': types, 'rows': len(trows)})
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    # ---- 全部解析通过 -> 按类型落槽 + meta + 云同步 ----
    tm = _ensure_temu_meta()
    slot_errs = []
    for p in parsed:
        entry = {'filename': p['filename'], 'uploaded_at': now_bj(),
                 'summary': {'rows': p['rows'], 'sheets_parsed': True}}
        for slot, tflag, slot_path, obj_key in (
                ('a', 'A', LATEST_TEMU_A, storage.OBJ_TEMU_A),
                ('b', 'B', LATEST_TEMU_B, storage.OBJ_TEMU_B)):
            if tflag not in p['types']:
                continue
            _atomic_write_bytes(slot_path, p['raw'])
            tm[slot] = entry
            err = _cloud_sync(obj_key, p['raw'])
            if err:
                slot_errs.append('%s 类（%s）：%s' % (tflag, p['filename'], err))
    _write_meta_file(TEMU_META, tm)
    try:
        _sync_source_meta()
    except Exception:
        pass
    return jsonify({'ok': True, 'temu': tm,
                    'uploaded': [{'filename': p['filename'],
                                  'types': sorted(p['types']),
                                  'rows': p['rows']} for p in parsed],
                    'cloud_error': '；'.join(slot_errs),
                    'ready': STATE['rows'] is not None,
                    'needs_rebuild': STATE['rows'] is not None})


@app.route('/api/upload/ops', methods=['POST'])
def api_upload_ops():
    """上传/更新「产品运营表」（第 7 步，Amazon 店铺运营/运营组查询源）。

    支持 .xlsx，多 sheet（如「综合组-AMZ」「AMZ组」），各 sheet 需含 SPU 列
    （别名列 新SPU1/新SPU2… 自动纳入），并含 运营 / 运营组 列。
    校验后留档 latest_ops.xlsx，供重算时按店铺 + SPU 查运营归属。"""
    if 'file' not in request.files:
        return jsonify({'ok': False, 'msg': '未收到文件'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'ok': False, 'msg': '文件名为空'}), 400
    name = f.filename.lower()
    if not (name.endswith('.xlsx') or name.endswith('.xlsm')):
        return jsonify({'ok': False, 'msg': '产品运营表需为 .xlsx 文件'}), 400

    raw = f.read()
    tmp_path = os.path.join(UPLOAD_DIR, '_ops_uploading.xlsx')   # 保持 .xlsx 后缀（openpyxl 按扩展名校验）
    _atomic_write_bytes(tmp_path, raw)
    try:
        from pipeline import load_ops
        ops_map = load_ops(tmp_path)
    except Exception as e:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return jsonify({'ok': False, 'msg': '产品运营表解析失败：' + str(e)}), 400
    try:
        os.remove(tmp_path)
    except OSError:
        pass
    if not ops_map:
        return jsonify({'ok': False, 'msg': '产品运营表未解析到任何含「SPU」列的 sheet'}), 400

    _atomic_write_bytes(LATEST_OPS, raw)
    meta = {'filename': f.filename,
            'uploaded_at': now_bj(),
            'summary': {'sheets': sorted(ops_map.keys()),
                        'rows': sum(len(v) for v in ops_map.values())}}
    _write_meta_file(OPS_META, meta)
    cloud_err = _cloud_sync(storage.OBJ_OPS, raw)
    try:
        _sync_source_meta()
    except Exception:
        pass
    return jsonify({'ok': True, 'ops': meta,
                    'cloud_error': cloud_err,
                    'ready': STATE['rows'] is not None,
                    'needs_rebuild': STATE['rows'] is not None})


@app.route('/api/upload', methods=['POST'])
def api_upload():
    """上传原始报表 CSV，更新共享数据源；计算由 /api/rebuild 单独触发。"""
    if 'file' not in request.files:
        return jsonify({'ok': False, 'msg': '未收到文件'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'ok': False, 'msg': '文件名为空'}), 400
    # 先写临时文件并做表头校验，校验失败时保留上一份生效原始表。
    raw_bytes = f.read()
    tmp_path = LATEST_RAW + '.upload.tmp'
    try:
        _atomic_write_bytes(tmp_path, raw_bytes)
        summary = _summarize_raw(tmp_path)
    except Exception as e:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return jsonify({'ok': False, 'msg': '原始表格解析失败：' + str(e)}), 400
    try:
        os.remove(tmp_path)
    except OSError:
        pass

    # 用 sha256 比对同一份文件；仅新文件刷新「上传于」时间。
    prev_meta = _read_meta_file(RAW_META)
    new_hash = hashlib.sha256(raw_bytes).hexdigest()
    is_new_file = True
    if prev_meta and prev_meta.get('hash') and new_hash:
        is_new_file = (prev_meta['hash'] != new_hash)
    uploaded_at = now_bj() if is_new_file else (prev_meta or {}).get('uploaded_at')
    _atomic_write_bytes(LATEST_RAW, raw_bytes)
    meta = {'filename': f.filename, 'uploaded_at': uploaded_at,
            'hash': new_hash, 'summary': summary}
    _write_meta_file(RAW_META, meta)
    # 同步到云 Storage（跨冷启动持久化）；失败回传原因便于排障
    cloud_err = _cloud_sync(storage.OBJ_RAW, raw_bytes)
    try:
        _sync_source_meta()
    except Exception:
        pass
    return jsonify({'ok': True, 'raw': _raw_info(), 'is_new_file': is_new_file,
                    'cloud_error': cloud_err,
                    'ready': STATE['rows'] is not None, 'needs_rebuild': True,
                    'shared_storage': storage.has_cloud()})


@app.route('/api/rebuild', methods=['POST'])
def api_rebuild():
    """用留档的最近原始表 + 当前转单表重新生成（转单表更新后用）。"""
    if not os.path.exists(LATEST_RAW):
        return jsonify({'ok': False, 'msg': '没有留档的原始表，请上传原始报表'}), 400
    name = STATE['filename']
    prev_meta = _read_meta_file(RAW_META)
    if prev_meta:
        name = prev_meta.get('filename') or name
    # 重算不算「上传新表格」：保持原「数据更新时间」（北京时间）
    ok, payload, code = _run_and_store(LATEST_RAW, name or 'latest_raw.csv',
                                       keep_uploaded_at=True)
    if not ok:
        return jsonify({'ok': False, 'msg': payload}), code
    payload = dict(payload)
    payload['is_new_file'] = False
    return jsonify(payload)


@app.route('/api/state')
def api_state():
    """返回看板所需数据：表头 + 全量行 + 统计。
    带 ETag/304 协商缓存：数据未变（current.json 未重写 且 异常确认未改）时返回 304，
    浏览器复用上次已下载的明细（弱带宽下重复打开看板秒开，不再重传 1.5MB）。"""
    if STATE['rows'] is None:
        return jsonify({'ready': False})
    # 异常确认记录（含 version），供前端行级冲突检测初始化 + 参与版本指纹
    try:
        ann = storage.annot_get() or {}
    except Exception:
        ann = {}
    try:
        st_ = os.stat(CURRENT_JSON)
        cur_sig = '%.6f|%d' % (st_.st_mtime, st_.st_size)   # 任一次重算/上传都会重写 current.json
    except OSError:
        cur_sig = 'none'
    ann_fp = hashlib.md5(json.dumps(ann, sort_keys=True,
                                    ensure_ascii=False).encode('utf-8')).hexdigest()
    etag = '"%s"' % hashlib.md5((cur_sig + '|' + ann_fp).encode('utf-8')).hexdigest()
    inm = (request.headers.get('If-None-Match') or '').replace('W/', '').strip()
    if inm == etag:
        resp = app.response_class(status=304)
    else:
        _, meta = current_ref()
        tm = _ensure_temu_meta()
        payload = {
            'ready': True,
            'header': STATE['header'],
            'rows': STATE['rows'],
            'stats': STATE['stats'],
            'filename': STATE['filename'],
            'uploaded_at': STATE['uploaded_at'],
            'ref_filename': STATE['ref_filename'],
            'ref_uploaded_at': STATE['ref_uploaded_at'],
            'lx_filename': STATE['lx_filename'],
            'lx_uploaded_at': STATE['lx_uploaded_at'],
            'wm_filename': STATE['wm_filename'],
            'wm_uploaded_at': STATE['wm_uploaded_at'],
            'admin_filename': STATE['admin_filename'],
            'admin_uploaded_at': STATE['admin_uploaded_at'],
            'temu_a_filename': STATE['temu_a_filename'],
            'temu_a_uploaded_at': STATE['temu_a_uploaded_at'],
            'temu_b_filename': STATE['temu_b_filename'],
            'temu_b_uploaded_at': STATE['temu_b_uploaded_at'],
            'ops_filename': STATE['ops_filename'],
            'ops_uploaded_at': STATE['ops_uploaded_at'],
            'ref_stale': bool(meta and STATE['ref_uploaded_at'] != meta['uploaded_at']),
            'temu_stale': bool((tm.get('a') and STATE['temu_a_uploaded_at'] != tm['a']['uploaded_at']) or
                               (tm.get('b') and STATE['temu_b_uploaded_at'] != tm['b']['uploaded_at'])),
        }
        # 异常确认记录（含 version），供前端行级冲突检测初始化
        payload['annotations'] = ann
        resp = jsonify(payload)
    resp.headers['ETag'] = etag
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.route('/api/missing_platform')
def api_missing_platform():
    """返回「缺失平台信息异常」独立清单（sku/spu/store/asin/listing/source）。"""
    if STATE['rows'] is None:
        return jsonify({'ready': False})
    rows = (STATE['stats'] or {}).get('missing_platform_rows') or []
    return jsonify({'ready': True, 'rows': rows,
                    'count': (STATE['stats'] or {}).get('missing_platform', {}).get('union', 0)})


@app.route('/api/missing_platform/download')
def api_missing_platform_download():
    """导出「缺失平台信息异常」清单 CSV。"""
    if STATE['rows'] is None:
        return jsonify({'ok': False, 'msg': '暂无数据'}), 400
    rows = (STATE['stats'] or {}).get('missing_platform_rows') or []
    out_name = '缺失平台信息异常清单.csv'
    # 内存生成，避免 Windows 文件锁 PermissionError
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['SKU', 'SPU', '店铺', 'ASIN', 'listing后台状态', '来源'])
    for r in rows:
        w.writerow([r['sku'], r['spu'], r['store'], r['asin'], r['listing'], r['source']])
    data = buf.getvalue().encode('utf-8-sig')
    return send_file(io.BytesIO(data), as_attachment=True, download_name=out_name,
                     mimetype='text/csv')


@app.route('/api/anomaly', methods=['POST'])
def api_anomaly():
    """更新某行异常确认状态/备注。body: {index, status, note, version}

    行级冲突检测：传入 version（提交时行的当前版本），若服务端/云端已更新过该行
    （version 不一致），返回 409 提示「已被他人修改」，避免多人互相覆盖。
    body → 也支持 id_key（行稳定主键）优先；未传时用 index 行号兼容旧前端。
    """
    if STATE['rows'] is None:
        return jsonify({'ok': False, 'msg': '暂无数据'}), 400
    body = request.get_json(force=True, silent=True) or {}
    status = body.get('status', '')
    note = body.get('note', '')
    version = body.get('version')           # 可空：空/None = 强制写
    hi = STATE['header'].index('异常确认状态')
    ni = STATE['header'].index('异常确认备注')

    # 主键：优先 id_key，否则用 index 行号映射到行内 ID
    if 'id_key' in body and body['id_key']:
        id_key = str(body['id_key']).strip()
        row = None
        for ri, r in enumerate(STATE['rows']):
            k = _row_id_key(r)
            if k == id_key:
                row = (r, ri)
                break
        if not row:
            return jsonify({'ok': False, 'msg': '未找到该主键对应的行'}), 404
        r, idx = row
    else:
        idx = body.get('index')
        if not isinstance(idx, int) or idx < 0 or idx >= len(STATE['rows']):
            return jsonify({'ok': False, 'msg': '行索引越界'}), 400
        r = STATE['rows'][idx]
        id_key = _row_id_key(r)

    ok, msg = storage.annot_update(id_key, status, note, expect_version=version)
    if not ok:
        return jsonify({'ok': False, 'msg': msg, 'conflict': True}), 409

    with _LOCK:
        r[hi] = status
        r[ni] = note
        _save_state()
    # 返回新的版本号，供前端记下，下次提交做冲突检测
    new_ver = storage.annot_get().get(id_key, {}).get('version', None)
    return jsonify({'ok': True, 'version': new_ver})


@app.route('/api/download')
def api_download():
    """导出含异常确认栏的 CSV。"""
    if STATE['rows'] is None:
        return jsonify({'ok': False, 'msg': '暂无数据'}), 400
    out_name = 'SKU平台信息套件成员货品体现_清洗后.csv'
    # 在内存中生成 CSV，避免在磁盘反复写固定文件触发 Windows 文件锁 PermissionError
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(STATE['header'])
    w.writerows(STATE['rows'])
    data = buf.getvalue().encode('utf-8-sig')
    return send_file(io.BytesIO(data), as_attachment=True, download_name=out_name,
                     mimetype='text/csv')


@app.route('/api/reference/download')
def api_reference_download():
    """下载当前生效的转单表（核对用）。"""
    p, meta = current_ref()
    if not p:
        return jsonify({'ok': False, 'msg': '暂无转单表'}), 400
    return send_file(p, as_attachment=True, download_name=meta['filename'])


@app.route('/static/<path:p>')
def static_files(p):
    return send_from_directory(os.path.join(BASE, 'static'), p)


def _pick_port():
    """端口：优先 PORT 环境变量（Render/HF 注入），否则本地默认 8092。"""
    try:
        return int(os.environ.get('PORT', '8092'))
    except (TypeError, ValueError):
        return 8092


def _local_main():
    """本地直跑：单实例保护 + 冷启动重建 + 127.0.0.1:8092。"""
    # ---------- 单实例保护：若 8092 已被本系统实例占用，则直接退出 ----------
    # 避免「启动文件夹 + 计划任务 + start.bat 多重入口」重复启动产生多实例争用文件。
    import socket as _socket
    _lock_file = os.path.join(DATA_DIR, 'app.pid')
    _port = _pick_port()
    # 1) 端口已被占用 -> 已有实例在跑，退出（不重复起）
    _s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    _can_bind = True
    try:
        _s.bind(('127.0.0.1', _port))
    except OSError as _e:
        _can_bind = False
    finally:
        _s.close()
    if not _can_bind:
        print('端口 %s 已被占用，检测到看板可能已在运行，跳过本次启动。' % _port)
        # 把当前 PID 记录进去（便于外部判断），但不新起服务
        try:
            with open(_lock_file, 'w', encoding='utf-8') as _lf:
                _lf.write(str(os.getpid()))
        except OSError:
            pass
        raise SystemExit(0)
    # 2) 端口空闲 -> 本实例是唯一服务，记录 PID 锁文件后正常启动
    try:
        with open(_lock_file, 'w', encoding='utf-8') as _lf:
            _lf.write(str(os.getpid()))
    except OSError:
        pass
    _apply_annotations()   # 加载后恢复已持久化的异常确认状态
    _boot_rebuild()        # 冷启动自动重建（空看板 + 数据源齐备时）
    app.run(host='127.0.0.1', port=_port, debug=False, threaded=True)


if __name__ == '__main__':
    _local_main()
