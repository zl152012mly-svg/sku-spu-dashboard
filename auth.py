# -*- coding: utf-8 -*-
"""
auth.py —— 合并后统一鉴权（SKU&SPU 清洗看板 × 新品盘点系统）

设计（Phase 2 §3）：
- 单一登录：/api/login 校验 ACCESS_PASSWORD(admin) 或 SUB_ACCOUNTS(sub)，写 session['role']。
- 全局 before_request：凡 /api/* 默认拒绝；校验 **session 或合法 X-Auth-Token**（兼容既有前端 token 流）。
- 角色白名单：@role_required('admin'|'sub')；admin 拥有全部权限，sub 仅 new_arrival 侧接口（Step 3 接入）。
- 既有的 X-Auth-Token 客户端（无 session）一律视为 admin，保证现有看板零改动。

环境变量：
  ACCESS_PASSWORD        SKU&SPU 管理员口令（admin）
  NEW_ARRIVAL_ADMIN_PWD  new-arrival 管理员口令（admin，缺省回落 ACCESS_PASSWORD）
  SUB_ACCOUNTS           子账号，格式 "user1:pass1,user2:pass2"（可选）
  SECRET_KEY             session 签名密钥（建议强随机；缺省为开发值）
"""
import os
import hashlib
from functools import wraps
from flask import session, request, jsonify, redirect

# ---------- 凭据配置 ----------
ACCESS_PASSWORD = os.environ.get('ACCESS_PASSWORD', '').strip() or 'lucie2026'
NEW_ARRIVAL_ADMIN_PWD = os.environ.get('NEW_ARRIVAL_ADMIN_PWD', ACCESS_PASSWORD).strip() or ACCESS_PASSWORD
SECRET_KEY = os.environ.get('SECRET_KEY', 'sku-spu-secret-2026').strip() or 'sku-spu-secret-2026'

_AUTH_SALT = 'sku-spu-2026'
# 与历史前端完全一致：token = sha256(ACCESS_PASSWORD + 'sku-spu-2026')，避免旧 token 失效。
AUTH_TOKEN = hashlib.sha256((ACCESS_PASSWORD + _AUTH_SALT).encode('utf-8')).hexdigest()


def _parse_sub_accounts():
    """SUB_ACCOUNTS: 'user:pass,user2:pass2' 或纯密码列表 'pass1,pass2'。"""
    raw = os.environ.get('SUB_ACCOUNTS', '').strip()
    out = {}
    if not raw:
        return out
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        if ':' in part:
            u, p = part.split(':', 1)
            out[u.strip()] = p.strip()
        else:
            out[part] = part
    return out


SUB_ACCOUNTS = _parse_sub_accounts()


def current_role():
    """当前请求角色：session 优先；否则合法 X-Auth-Token 视为 admin（兼容旧前端）。"""
    r = session.get('role')
    if r:
        return r
    if request.headers.get('X-Auth-Token', '') == AUTH_TOKEN:
        return 'admin'
    return None


def check_request_auth():
    return current_role() is not None


def attempt_login(password):
    """校验口令，返回角色字符串或 None。"""
    if password == ACCESS_PASSWORD or password == NEW_ARRIVAL_ADMIN_PWD:
        return 'admin'
    for _user, pwd in SUB_ACCOUNTS.items():
        if password == pwd:
            return 'sub'
    return None


def role_required(role):
    """装饰器：要求当前角色满足 role。admin 可访问一切；role='sub' 允许 admin+sub。"""
    def deco(f):
        @wraps(f)
        def w(*args, **kwargs):
            r = current_role()
            if r is None:
                if request.path.startswith('/api/'):
                    return jsonify({'ok': False, 'msg': 'unauthorized', 'auth': False}), 401
                return redirect('/')
            if role == 'admin' and r != 'admin':
                return jsonify({'ok': False, 'msg': 'forbidden'}), 403
            if role == 'sub' and r not in ('admin', 'sub'):
                return jsonify({'ok': False, 'msg': 'forbidden'}), 403
            return f(*args, **kwargs)
        return w
    return deco


def init_app(app):
    """把统一鉴权挂到 Flask app：session 签名 + 全局 before_request + 登录/探测/登出接口。"""
    app.secret_key = SECRET_KEY

    @app.before_request
    def _guard():
        p = request.path
        if p.startswith('/static/'):
            return None
        # 兼容「只带 X-Auth-Token 请求头、没有 session」的老客户端：
        # 主站页面每次取数都带该头（所以看板能用），但点击链接跳转不会带请求头，
        # 导致 /na/ 被判定为未登录而弹回主站（表现为「点了没反应」）。
        # 这里在鉴权通过时顺手把角色写进 session，使后续页面跳转也带得上登录态。
        if not session.get('role') and request.headers.get('X-Auth-Token', '') == AUTH_TOKEN:
            session['role'] = 'admin'
            session.permanent = True
        # 白名单：登录/探测/登出本身无需已登录态（它们是「未登录」时唯一能调的接口）
        if p in ('/api/login', '/api/ping', '/logout'):
            return None
        if check_request_auth():
            return None
        # 新品盘点子系统（/na/*）：整站需登录。页面重定向回主站（主站会弹口令框），
        # 接口返回 401 JSON——与 SKU&SPU 侧保持同一种「默认拒绝」口径。
        # ⚠ 新品盘点原实现里 9/12 的 /api/* 是裸奔的，合并后这里补上了。
        if p.startswith('/na/'):
            if p.startswith('/na/api/') or p.startswith('/na/download/') \
                    or p in ('/na/upload', '/na/process'):
                return jsonify({'ok': False, 'msg': 'unauthorized', 'auth': False}), 401
            return redirect('/')
        # 默认拒绝：/api/* 返回 401（前端据此弹出口令框）；非 /api 页面当前保持公开（合并后改 /login）
        if p.startswith('/api/'):
            return jsonify({'ok': False, 'msg': 'unauthorized', 'auth': False}), 401
        return None

    @app.route('/api/ping')
    def auth_ping():
        return jsonify({'ok': True, 'auth': check_request_auth(), 'role': current_role()})

    @app.route('/api/login', methods=['POST'])
    def auth_login():
        """校验访问口令，通过则写 session['role'] 并返回 auth token。body: {password}"""
        body = request.get_json(force=True, silent=True) or {}
        pwd = str(body.get('password', ''))
        role = attempt_login(pwd)
        if not role:
            return jsonify({'ok': False, 'msg': '口令错误'}), 401
        session['role'] = role
        session.permanent = True
        return jsonify({'ok': True, 'token': AUTH_TOKEN, 'role': role})

    @app.route('/logout')
    def auth_logout():
        session.clear()
        return jsonify({'ok': True})
