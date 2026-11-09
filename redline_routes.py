# -*- coding: utf-8 -*-
"""
redline_routes.py —— 红线价监控模块（Flask 蓝图，挂载 /redline）

- 页面：GET /redline/  → 返回单文件应用（服务时注入 REST 配置 + 本地化前端库）
- 接口：/redline/api/sources             GET
        /redline/api/periods             GET
        /redline/api/sources/save        POST
        /redline/api/sources/remove      POST
        /redline/api/periods/sync        POST
        /redline/api/health              GET
- 鉴权：复用主系统 auth.current_role()；页面未登录跳 /login，接口返回 401 JSON
- 存储：redline_store（OSS 优先，JSON 兜底）

说明：全局 before_request 只对 /api/* 与 /na/* 做默认拒绝，/redline/* 不在其列，
故本蓝图内部自行做鉴权，避免接口裸奔。
"""
import os
import re
import json
from functools import wraps

from flask import Blueprint, request, jsonify, redirect, Response

import auth
import redline_store as store

bp = Blueprint('redline', __name__, url_prefix='/redline')

_BASE = os.path.dirname(os.path.abspath(__file__))
_HTML_PATH = os.path.join(_BASE, 'redline_app', 'index.html')

# 服务时注入：告诉前端走 rest 模式、接口前缀、当前角色；前端库全部本地化（不依赖公网 CDN）
_INJECT_HEAD = (
    "<script>window.REDLINE_BACKEND='rest';"
    "window.REDLINE_API_BASE='/redline/api';"
)


def _inject_script(role):
    """注入脚本：rest 模式 + 接口前缀 + 当前角色（前端据此隐藏「数据上传/异常」等 admin 模块）。"""
    return _INJECT_HEAD + "window.REDLINE_ROLE=" + json.dumps(role or '') + ";</script>"


_LIB_MAP = (
    ('https://unpkg.com/xlsx@0.18.5/dist/xlsx.full.min.js', '/static/libs/xlsx.full.min.js'),
    ('https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js', '/static/libs/echarts.min.js'),
    ('https://cdnjs.cloudflare.com/ajax/libs/pako/2.1.0/pako.min.js', '/static/libs/pako.min.js'),
)
# 兼容两种源码：① 新版已直接写 window.location.href；② 旧版残留 workbuddy.link 公网 SaaS 链接。
# 无论哪种，对外服务时都强制指向当前系统自身，杜绝「导出跳到旧公网页面」的残留。
_PUBLIC_URL_RE = re.compile(r"const PUBLIC_PAGE_URL\s*=\s*(['\"])[^'\"]*\1\s*;")


def _authed():
    return auth.current_role() is not None


def _page_auth(f):
    @wraps(f)
    def w(*a, **k):
        if not _authed():
            return redirect('/login')
        return f(*a, **k)
    return w


def _api_auth(f):
    @wraps(f)
    def w(*a, **k):
        if not _authed():
            return jsonify({'ok': False, 'msg': 'unauthorized', 'auth': False}), 401
        return f(*a, **k)
    return w


def _admin_only(f):
    """写操作仅 admin：运营（sub）可查看板/SKU明细/趋势，但不能上传数据源、撤回期次。"""
    @wraps(f)
    def w(*a, **k):
        role = auth.current_role()
        if not role:
            return jsonify({'ok': False, 'msg': 'unauthorized', 'auth': False}), 401
        if role != 'admin':
            return jsonify({'ok': False, 'msg': 'forbidden', 'auth': True}), 403
        return f(*a, **k)
    return w


def _render_page_html(role=''):
    with open(_HTML_PATH, encoding='utf-8') as f:
        html = f.read()
    # 1) 注入 REST 配置 + 角色（放在 <head> 之后，早于主脚本执行）
    if "REDLINE_API_BASE" not in html.split('</head>')[0]:
        html = re.sub(r'(<head[^>]*>)', lambda m: m.group(1) + '\n' + _inject_script(role), html, count=1)
    # 2) 前端库本地化
    for cdn, local in _LIB_MAP:
        html = html.replace(cdn, local)
    # 3) 导出兜底链接指向本模块（而非旧的 workbuddy.link）
    html = _PUBLIC_URL_RE.sub('const PUBLIC_PAGE_URL = window.location.href;', html, count=1)
    return html


@bp.route('/')
@bp.route('/index.html')
@_page_auth
def page():
    try:
        html = _render_page_html(auth.current_role() or '')
    except FileNotFoundError:
        return Response('红线价页面缺失：%s' % _HTML_PATH, status=500, mimetype='text/plain')
    resp = Response(html, content_type='text/html; charset=utf-8')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@bp.route('/api/health')
@_api_auth
def api_health():
    return jsonify({'ok': True, 'module': 'redline', 'role': auth.current_role(),
                    'storage': store.stats()})


@bp.route('/api/sources')
@_api_auth
def api_sources():
    # ?meta=1 → 只返回清单（src/store/file/gen/updated_at），不含数据体；
    # 前端据此判断哪些数据源需要重新拉取，避免一次性把几 MB 压缩数据全推下来。
    meta = request.args.get('meta') == '1'
    return jsonify(store.list_sources(meta_only=meta))


@bp.route('/api/sources/data')
@_api_auth
def api_sources_data():
    # 按需取单个数据源的压缩数据（b64），供前端「只拉云端更新的源」并行加载。
    src = (request.args.get('src') or '').strip()
    if not src:
        return jsonify({'ok': False, 'msg': 'src required'}), 400
    store_ = (request.args.get('store') or '').strip()
    file_ = (request.args.get('file') or '').strip()
    data = store.get_source_data(src, store_, file_)
    if data is None:
        return jsonify({'ok': False, 'msg': 'not found'}), 404
    return jsonify({'ok': True, 'data': data})


@bp.route('/api/periods')
@_api_auth
def api_periods():
    meta = request.args.get('meta') == '1'
    return jsonify(store.list_periods(meta_only=meta))


@bp.route('/api/sources/save', methods=['POST'])
@_admin_only
def api_sources_save():
    b = request.get_json(force=True, silent=True) or {}
    src = (b.get('src') or '').strip()
    if not src:
        return jsonify({'ok': False, 'msg': 'src required'}), 400
    chunks = b.get('chunks') or []
    ok = store.save_source(src, b.get('store') or '', b.get('file') or '',
                           chunks, b.get('updated_at') or '')
    return jsonify({'ok': bool(ok), 'chunks': len(chunks)}), (200 if ok else 500)


@bp.route('/api/sources/remove', methods=['POST'])
@_admin_only
def api_sources_remove():
    b = request.get_json(force=True, silent=True) or {}
    src = (b.get('src') or '').strip()
    if not src:
        return jsonify({'ok': False, 'msg': 'src required'}), 400
    n = store.remove_source(src, b.get('store') or '', b.get('file', None))
    return jsonify({'ok': True, 'removed': n})


@bp.route('/api/periods/sync', methods=['POST'])
@_admin_only
def api_periods_sync():
    b = request.get_json(force=True, silent=True) or {}
    n = store.sync_periods(b.get('periods') or [])
    return jsonify({'ok': True, 'periods': n})


@bp.route('/api/import', methods=['POST'])
@_admin_only
def api_import():
    """一次性迁移：接收旧资料库导出 JSON {sources:[...], periods:[...]}。

    sources 每项 {src,store,file,updated_at,chunks}；periods 每项 {period,date,label,chunks}。
    覆盖式写入，可重复执行（幂等）。
    """
    b = request.get_json(force=True, silent=True) or {}
    sources = b.get('sources') or []
    periods = b.get('periods') or []
    ns = 0
    for s in sources:
        if store.save_source(s.get('src') or '', s.get('store') or '', s.get('file') or '',
                             s.get('chunks') or [], s.get('updated_at') or ''):
            ns += 1
    np = store.sync_periods(periods) if periods else 0
    return jsonify({'ok': True, 'sources': ns, 'periods': np})
