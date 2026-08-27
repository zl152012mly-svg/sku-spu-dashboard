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
import shutil
import zipfile
import threading
import time
import hashlib
from datetime import datetime
from flask import (Flask, request, jsonify, render_template,
                   send_file, send_from_directory)

from pipeline import run_pipeline, summarize_transfer, load_lingxing, load_walmart
import storage  # 可插拔存储层：JSON 本地 / Supabase 云，含行级冲突检测

BASE = os.path.dirname(os.path.abspath(__file__))
REF_DIR = os.path.join(BASE, 'reference')
UPLOAD_DIR = os.path.join(BASE, 'uploads')
DATA_DIR = os.path.join(BASE, 'data')
CURRENT_JSON = os.path.join(DATA_DIR, 'current.json')
REF_META = os.path.join(DATA_DIR, 'reference_meta.json')
LATEST_RAW = os.path.join(UPLOAD_DIR, 'latest_raw.csv')     # 留档，供重算
LATEST_LINGXING = os.path.join(UPLOAD_DIR, 'latest_lingxing.xlsx')   # 领星清单留档
LATEST_WALMART = os.path.join(UPLOAD_DIR, 'latest_walmart.csv')      # walmart 报表留档
LINGXING_META = os.path.join(DATA_DIR, 'lingxing_meta.json')
WALMART_META = os.path.join(DATA_DIR, 'walmart_meta.json')
for d in (REF_DIR, UPLOAD_DIR, DATA_DIR):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__)

# ---------- 访问口令（轻级鉴权） ----------
# 环境变量 ACCESS_PASSWORD 设置口令；未设置时默认 'lucie2026'（本地可改）。
ACCESS_PASSWORD = os.environ.get('ACCESS_PASSWORD', '').strip() or 'lucie2026'
# 简单 token = sha256(口令|salt)，前端通过 localStorage 存 token，请求头带 X-Auth-Token。
_AUTH_SALT = 'sku-spu-2026'
AUTH_TOKEN = hashlib.sha256((ACCESS_PASSWORD + _AUTH_SALT).encode('utf-8')).hexdigest()


def _auth_ok():
    """校验请求头 X-Auth-Token 是否等于正确 token。"""
    tok = request.headers.get('X-Auth-Token', '')
    return tok == AUTH_TOKEN


@app.before_request
def _require_auth():
    """所有 /api/* 需先通过口令；/ 页面骨架放行（数据接口鉴权，前端据此弹口令框）。"""
    if request.path.startswith('/static/'):
        return None
    # 白名单：登录/探测接口本身无需 token（它们是「未登录」时唯一能调的接口）
    if request.path in ('/api/login', '/api/ping'):
        return None
    if _auth_ok():
        return None
    # 未通过：仅拦截 /api/*，返回 401 JSON（前端 showLogin 弹出口令框）
    if request.path.startswith('/api/'):
        return jsonify({'ok': False, 'msg': 'unauthorized', 'auth': False}), 401
    return None


@app.errorhandler(Exception)
def handle_exception(e):
    code = getattr(e, 'code', 500)
    import traceback as _tb
    app.logger.exception('未捕获异常')
    return jsonify({'ok': False,
                    'msg': '服务器错误：' + str(e),
                    'detail': _tb.format_exc()[-300:]}), (code if isinstance(code, int) else 500)


# ---------- 登录 / 探测 ----------
@app.route('/api/ping')
def api_ping():
    return jsonify({'ok': True, 'auth': _auth_ok()})


@app.route('/api/login', methods=['POST'])
def api_login():
    """校验访问口令，通过则返回 auth token。body: {password}"""
    body = request.get_json(force=True, silent=True) or {}
    pwd = str(body.get('password', ''))
    if pwd == ACCESS_PASSWORD:
        return jsonify({'ok': True, 'token': AUTH_TOKEN})
    return jsonify({'ok': False, 'msg': '口令错误'}), 401


# 当前处理结果（内存态，单用户本地工具够用）
STATE = {'header': None, 'rows': None, 'stats': None,
         'filename': None, 'uploaded_at': None,
         'ref_filename': None, 'ref_uploaded_at': None,
         'lx_filename': None, 'lx_uploaded_at': None,
         'wm_filename': None, 'wm_uploaded_at': None}
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
    if os.path.exists(legacy):
        try:
            summary = summarize_transfer(legacy)
        except Exception:
            return
        ts = datetime.fromtimestamp(os.path.getmtime(legacy)).strftime('%Y-%m-%d %H:%M:%S')
        _write_ref_meta({'stored': '转单.xlsx', 'filename': '转单.xlsx',
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
                'wm_filename', 'wm_uploaded_at')}
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
                  'wm_filename', 'wm_uploaded_at'):
            STATE[k] = d.get(k)
        return True
    except Exception:
        return False


_bootstrap_ref()
_load_state()


# ------------------------------------------------------------------ 数据源（领星/walmart）管理
def _current_ds():
    """返回本次重算应使用的数据源路径（存在才用）。"""
    lx = LATEST_LINGXING if os.path.exists(LATEST_LINGXING) else None
    wm = LATEST_WALMART if os.path.exists(LATEST_WALMART) else None
    return lx, wm


def _ds_info():
    lxm = _read_meta_file(LINGXING_META)
    wmm = _read_meta_file(WALMART_META)
    return {
        'lingxing': ({'filename': lxm.get('filename'),
                      'uploaded_at': lxm.get('uploaded_at'),
                      'summary': lxm.get('summary')} if lxm else None),
        'walmart': ({'filename': wmm.get('filename'),
                     'uploaded_at': wmm.get('uploaded_at'),
                     'summary': wmm.get('summary')} if wmm else None),
    }


def _run_and_store(raw_path, raw_name):
    """跑流水线并写入 STATE / 缓存。返回 (ok, payload_or_msg, http_code)"""
    ref_path, meta = current_ref()
    if not ref_path:
        return False, '尚未上传转单表，请先上传「转单.xlsx」', 400
    lx_path, wm_path = _current_ds()
    try:
        header, rows, stats = run_pipeline(raw_path, ref_path,
                                           lingxing_path=lx_path,
                                           walmart_path=wm_path)
    except ValueError as e:
        return False, str(e), 400
    except Exception as e:
        return False, '处理失败：' + str(e), 500

    lxm = _read_meta_file(LINGXING_META)
    wmm = _read_meta_file(WALMART_META)
    with _LOCK:
        STATE['header'] = header
        STATE['rows'] = rows
        STATE['stats'] = stats
        STATE['filename'] = raw_name
        STATE['uploaded_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        STATE['ref_filename'] = meta.get('filename')
        STATE['ref_uploaded_at'] = meta.get('uploaded_at')
        STATE['lx_filename'] = (lxm or {}).get('filename')
        STATE['lx_uploaded_at'] = (lxm or {}).get('uploaded_at')
        STATE['wm_filename'] = (wmm or {}).get('filename')
        STATE['wm_uploaded_at'] = (wmm or {}).get('uploaded_at')
        _save_state()
        # 数据源版本元信息同步到存储层（JSON / Supabase），保证多人看到同一套口径
        try:
            storage.ds_update({
                'filename': STATE['filename'],
                'uploaded_at': STATE['uploaded_at'],
                'ref_filename': meta.get('filename'),
                'ref_uploaded_at': meta.get('uploaded_at'),
                'lx_filename': (lxm or {}).get('filename'),
                'lx_uploaded_at': (lxm or {}).get('uploaded_at'),
                'wm_filename': (wmm or {}).get('filename'),
                'wm_uploaded_at': (wmm or {}).get('uploaded_at'),
            })
        except Exception:
            pass
        # 把云端/本地已存的异常确认，回填到本次 rows 的异常确认两列
        _apply_annotations()
    return True, {'ok': True, 'stats': stats, 'header': header,
                  'filename': STATE['filename'],
                  'uploaded_at': STATE['uploaded_at'],
                  'ref_filename': STATE['ref_filename'],
                  'ref_uploaded_at': STATE['ref_uploaded_at']}, 200


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
    if not storage._use_supabase():
        return False, False
    ok_raw = ok_ref = False
    # 原始报表
    try:
        data = storage.ds_file_download(storage.OBJ_RAW)
        if data:
            _atomic_write_bytes(LATEST_RAW, data)
            ok_raw = True
    except Exception:
        pass
    # 转单表：云端覆盖本地当前 reference（存为新版本文件并更新 meta）
    try:
        data = storage.ds_file_download(storage.OBJ_REF)
        if data:
            stored = '转单_cloud_%s.xlsx' % datetime.now().strftime('%Y%m%d%H%M%S')
            p = os.path.join(REF_DIR, stored)
            _atomic_write_bytes(p, data)
            try:
                summary = summarize_transfer(p)
            except Exception:
                summary = None
            if summary:
                _write_ref_meta({'stored': stored, 'filename': '转单.xlsx',
                                 'uploaded_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                 'summary': summary})
                ok_ref = True
    except Exception:
        pass
    # 领星 / walmart（可选，取到就覆盖留档）
    try:
        data = storage.ds_file_download(storage.OBJ_LX)
        if data:
            _atomic_write_bytes(LATEST_LINGXING, data)
    except Exception:
        pass
    try:
        data = storage.ds_file_download(storage.OBJ_WM)
        if data:
            _atomic_write_bytes(LATEST_WALMART, data)
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


@app.route('/api/status')
def api_status():
    _, meta = current_ref()
    ds = _ds_info()
    lxm = ds['lingxing']
    wmm = ds['walmart']
    return jsonify({
        'ready': STATE['rows'] is not None,
        'filename': STATE['filename'],
        'uploaded_at': STATE['uploaded_at'],
        'stats': STATE['stats'],
        'header': STATE['header'],
        'has_raw': os.path.exists(LATEST_RAW),
        'reference': ({'filename': meta['filename'],
                       'uploaded_at': meta['uploaded_at'],
                       'summary': meta.get('summary')} if meta else None),
        'ref_used': {'filename': STATE['ref_filename'],
                     'uploaded_at': STATE['ref_uploaded_at']},
        'ref_stale': bool(meta and STATE['rows'] is not None
                          and STATE['ref_uploaded_at'] != meta['uploaded_at']),
        'lingxing': lxm,
        'walmart': wmm,
        'lx_used': {'filename': STATE['lx_filename'],
                    'uploaded_at': STATE['lx_uploaded_at']},
        'wm_used': {'filename': STATE['wm_filename'],
                    'uploaded_at': STATE['wm_uploaded_at']},
        'lx_stale': bool(lxm and STATE['rows'] is not None
                         and STATE['lx_uploaded_at'] != lxm['uploaded_at']),
        'wm_stale': bool(wmm and STATE['rows'] is not None
                         and STATE['wm_uploaded_at'] != wmm['uploaded_at']),
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

    ts = datetime.now().strftime('%Y%m%d%H%M%S')
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
            'uploaded_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'summary': summary}
    _write_ref_meta(meta)
    # 校验通过后同步到云 Storage（供冷启动取最新转单表）
    try:
        with open(path, 'rb') as _rf:
            storage.ds_file_upload(storage.OBJ_REF, _rf.read())
    except Exception:
        pass
    return jsonify({'ok': True, 'reference': meta,
                    'has_raw': os.path.exists(LATEST_RAW),
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
            'uploaded_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'summary': summary}
    _write_meta_file(LINGXING_META, meta)
    # 校验通过后同步到云 Storage
    try:
        with open(LATEST_LINGXING, 'rb') as _lf:
            storage.ds_file_upload(storage.OBJ_LX, _lf.read())
    except Exception:
        pass
    return jsonify({'ok': True, 'lingxing': meta,
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
            'uploaded_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'summary': summary}
    _write_meta_file(WALMART_META, meta)
    # 校验通过后同步到云 Storage
    try:
        with open(LATEST_WALMART, 'rb') as _wf:
            storage.ds_file_upload(storage.OBJ_WM, _wf.read())
    except Exception:
        pass
    return jsonify({'ok': True, 'walmart': meta,
                    'ready': STATE['rows'] is not None,
                    'needs_rebuild': STATE['rows'] is not None})


@app.route('/api/upload', methods=['POST'])
def api_upload():
    """上传原始报表 CSV -> 清洗 -> 看板。"""
    if 'file' not in request.files:
        return jsonify({'ok': False, 'msg': '未收到文件'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'ok': False, 'msg': '文件名为空'}), 400
    if not current_ref()[0]:
        return jsonify({'ok': False, 'msg': '尚未上传转单表，请先在上方上传「转单.xlsx」'}), 400

    # 直接落盘到 latest_raw.csv（覆盖），避免产生临时文件触发 Windows 回收站限制
    _atomic_save_fileobj(LATEST_RAW, f)
    ok, payload, code = _run_and_store(LATEST_RAW, f.filename)
    if ok:
        try:
            with open(os.path.join(DATA_DIR, 'raw_meta.json'), 'w', encoding='utf-8') as m:
                json.dump({'filename': f.filename,
                           'uploaded_at': STATE['uploaded_at']}, m, ensure_ascii=False)
        except OSError:
            pass
        # 校验成功后才同步到云 Storage（供冷启动取最新原始表）
        try:
            with open(LATEST_RAW, 'rb') as _rf:
                storage.ds_file_upload(storage.OBJ_RAW, _rf.read())
        except Exception:
            pass
    if not ok:
        return jsonify({'ok': False, 'msg': payload}), code
    return jsonify(payload)


@app.route('/api/rebuild', methods=['POST'])
def api_rebuild():
    """用留档的最近原始表 + 当前转单表重新生成（转单表更新后用）。"""
    if not os.path.exists(LATEST_RAW):
        return jsonify({'ok': False, 'msg': '没有留档的原始表，请上传原始报表'}), 400
    name = STATE['filename']
    mp = os.path.join(DATA_DIR, 'raw_meta.json')
    if os.path.exists(mp):
        try:
            with open(mp, encoding='utf-8') as m:
                name = json.load(m).get('filename') or name
        except Exception:
            pass
    ok, payload, code = _run_and_store(LATEST_RAW, name or 'latest_raw.csv')
    if not ok:
        return jsonify({'ok': False, 'msg': payload}), code
    return jsonify(payload)


@app.route('/api/state')
def api_state():
    """返回看板所需数据：表头 + 全量行 + 统计。"""
    if STATE['rows'] is None:
        return jsonify({'ready': False})
    _, meta = current_ref()
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
        'ref_stale': bool(meta and STATE['ref_uploaded_at'] != meta['uploaded_at']),
    }
    # 异常确认记录（含 version），供前端行级冲突检测初始化
    try:
        payload['annotations'] = storage.annot_get()
    except Exception:
        payload['annotations'] = {}
    return jsonify(payload)


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
