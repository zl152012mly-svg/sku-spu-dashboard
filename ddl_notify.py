# -*- coding: utf-8 -*-
r"""
ddl_notify.py —— 新品上架 DDL 到期企微群提醒（ECS 定时脚本，纯标准库）
=====================================================================
每天由 cron/systemd timer 在 ECS 上执行一次；本机调用只读接口取数，
按「运营组 → 群机器人」「运营 → 企微成员」分组，发消息并真实艾特对应运营。

设计要点
--------
1. **口径不重写**：筛选全部由后端 `/na/api/ddl-due` 完成，脚本只负责分组与发送。
   后端复用页面/CSV 导出的同一套「即时重算 + note_fields/stock_note 合并」逻辑。
2. **不进 git 的配置**：webhook、成员 userid、口令全部放在 ECS 本地配置文件
   （默认 /etc/sku-spu/ddl_notify.json，可用环境变量 DDL_NOTIFY_CONFIG 覆盖），
   仓库里只放不含密钥的模板 ddl_notify_config.example.json。
3. **真实艾特**：企微群机器人 text 消息用 mentioned_list(userid) /
   mentioned_mobile_list(手机号) 艾特人；一个运营一条消息，SPU 清单互不混淆。
4. **一天只发一次**：按「日期 + 运营组 + 运营」去重，重跑不会刷屏；--force 可强制重发。
5. **安全试跑**：--dry-run 只打印消息不发送、不写状态；--date 可指定任意日期回溯。
6. **只在工作日发**：默认周一~周五；「额外放假日 / 调休上班日」在系统设置里可编辑。
   非工作日直接静默退出；后端筛选本来就是「DDL <= 今天」，所以节假日到期的 SPU
   会在下一个最近工作日被一并带出（顺延补发），无需另写一套逻辑。
   运营为「未分配运营」时，改按「运营组 → 默认艾特成员」真艾特对应的人。

用法
----
    python3 ddl_notify.py --dry-run                 # 试跑（推荐先做）
    python3 ddl_notify.py --dry-run --date 2026-11-04
    python3 ddl_notify.py                           # 正式发送（cron 调用）
    python3 ddl_notify.py --force                   # 忽略去重强制重发
"""
import os
import re
import sys
import json
import hashlib
import argparse
import datetime
import urllib.request
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUTH_SALT = 'sku-spu-2026'
DEFAULT_PWD = 'lucie2026'
API_PATH = '/na/api/ddl-due'
CONFIG_CANDIDATES = [
    os.environ.get('DDL_NOTIFY_CONFIG', ''),
    # 2026-11-04 起：系统设置页保存的配置优先（改完即生效，不必改 /etc）
    # na_routes._write_ddl_notify_config() 写到这里，ECS 零配置直接读。
    str(HERE / 'na_data' / 'ddl_notify_config.json'),
    '/etc/sku-spu/ddl_notify.json',
    str(HERE / 'ddl_notify_config.json'),
]


# ---------------------------------------------------------------------------
# 配置 / 鉴权
# ---------------------------------------------------------------------------
def load_config(explicit=None):
    for p in [explicit] + CONFIG_CANDIDATES:
        if not p:
            continue
        f = Path(p)
        if f.exists():
            data = json.loads(f.read_text(encoding='utf-8'))
            data['_path'] = str(f)
            return data
    raise SystemExit('[ddl_notify] 找不到配置文件；请复制 ddl_notify_config.example.json '
                     '到 /etc/sku-spu/ddl_notify.json 并填写 webhook / 成员映射')


def auth_token(cfg):
    if cfg.get('auth_token'):
        return cfg['auth_token']
    pwd = (cfg.get('access_password') or os.environ.get('ACCESS_PASSWORD')
           or DEFAULT_PWD).strip()
    return hashlib.sha256((pwd + AUTH_SALT).encode('utf-8')).hexdigest()


def today_str():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    except Exception:
        return datetime.date.today().isoformat()


def _cfg_date_set(cfg, key):
    """配置里的日期列表 → YYYY-MM-DD 集合；兼容数组、逗号/换行分隔的字符串。"""
    raw = cfg.get(key) or []
    if isinstance(raw, str):
        raw = re.split(r'[\s,，;；]+', raw)
    out = set()
    for x in (raw or []):
        s = str(x).strip()
        if not s:
            continue
        try:
            out.add(datetime.date.fromisoformat(s[:10]).isoformat())
        except ValueError:
            continue
    return out


def is_workday(date_str, cfg):
    """是否工作日。优先级：调休上班日 > 额外放假日 > 周一~周五。

    - workday_overrides: 本该是周末、但要上班的日期（调休）
    - holidays: 本该是工作日、但放假的日期（法定节假日）
    两个清单都在系统设置「DDL 到期提醒设置」里可视化维护。
    """
    try:
        d = datetime.date.fromisoformat(str(date_str)[:10])
    except ValueError:
        return True
    s = d.isoformat()
    if s in _cfg_date_set(cfg, 'workday_overrides'):
        return True
    if s in _cfg_date_set(cfg, 'holidays'):
        return False
    return d.weekday() < 5


def next_workday(date_str, cfg, max_days=30):
    """从 date_str（含当天）起，返回最近的一个工作日 YYYY-MM-DD。"""
    try:
        d = datetime.date.fromisoformat(str(date_str)[:10])
    except ValueError:
        return str(date_str)
    for i in range(max_days + 1):
        s = (d + datetime.timedelta(days=i)).isoformat()
        if is_workday(s, cfg):
            return s
    return str(date_str)


def fetch_due(cfg, date_str, include_overdue=True, exclude_abnormal=True):
    base = (cfg.get('api_base') or 'http://127.0.0.1:8092').rstrip('/')
    url = ('%s%s?date=%s&include_overdue=%d&exclude_abnormal=%d'
           % (base, API_PATH, date_str, int(include_overdue), int(exclude_abnormal)))
    req = urllib.request.Request(url, headers={'X-Auth-Token': auth_token(cfg)})
    timeout = int(cfg.get('timeout', 20))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SystemExit('[ddl_notify] 鉴权失败(%s)：配置里的 access_password 必须'
                             '与系统管理员登录口令一致（或改填 auth_token，'
                             '即 sha256(口令 + "%s")）' % (e.code, AUTH_SALT))
        raise


# ---------------------------------------------------------------------------
# 分组 / 消息组装（纯函数，便于本地打桩验证）
# ---------------------------------------------------------------------------
def resolve_webhook(group_name, cfg):
    """运营组 → 群机器人 webhook。精确匹配 → 包含匹配 → fallback。"""
    groups = cfg.get('groups') or {}
    entry = groups.get(group_name)
    if entry:
        return entry.get('webhook', '') if isinstance(entry, dict) else entry
    for name, value in groups.items():
        if name and (name in group_name or group_name in name):
            return value.get('webhook', '') if isinstance(value, dict) else value
    return cfg.get('fallback_webhook', '') or ''


def operator_mention(name, cfg):
    """运营姓名 → (mentioned_list, mentioned_mobile_list)。找不到返回空列表。"""
    rec = (cfg.get('operators') or {}).get(name)
    if isinstance(rec, dict):
        uid, mob = rec.get('userid', ''), rec.get('mobile', '')
    elif isinstance(rec, str):
        uid, mob = (rec, '') if not rec.startswith('1') or not rec.isdigit() else ('', rec)
    else:
        uid, mob = '', ''
    return ([uid] if uid else []), ([str(mob)] if mob else [])


def group_mentions(grp, cfg):
    """运营组 → 默认艾特成员（运营为「未分配运营」时用）。

    配置形态：{运营组: [{type: userid|mobile, value: ...}, ...]}；
    兼容单条 dict、纯字符串（11 位数字视为手机号）。
    返回 (userid 列表, 手机号列表)。
    """
    rec = (cfg.get('group_mentions') or {}).get(grp)
    if isinstance(rec, dict):
        rows = [rec]
    elif isinstance(rec, list):
        rows = rec
    elif isinstance(rec, str):
        rows = [rec]
    else:
        rows = []
    uid, mob = [], []
    for r in rows:
        if isinstance(r, str):
            r = {'type': 'mobile' if (r.startswith('1') and r.isdigit()) else 'userid',
                 'value': r}
        if not isinstance(r, dict):
            continue
        typ = str(r.get('type') or 'userid').strip()
        val = str(r.get('value') or '').strip()
        if not val:
            continue
        (mob if typ == 'mobile' else uid).append(val)
    return uid, mob


def build_messages(payload, cfg):
    """把接口返回组装成「一条消息 = 一个运营组 × 一个运营」。

    返回 [{group, operator, webhook, text, mentioned_list, mentioned_mobile_list,
           spus, unmapped}]，不产生任何网络请求。
    """
    date_str = payload.get('date') or today_str()
    policy = cfg.get('unmapped_operator', 'plain')   # plain / skip / at_all
    msgs = []
    for grp, ops in (payload.get('groups') or {}).items():
        webhook = resolve_webhook(grp, cfg)
        if not webhook:
            print('[ddl_notify] 跳过：运营组「%s」没有配置群机器人，也没有兜底群' % grp)
            continue
        for op, items in ops.items():
            if not items:
                continue
            uid_list, mob_list = operator_mention(op, cfg)
            unmapped = not (uid_list or mob_list)
            group_pinged = False
            if unmapped:
                # 2026-11-04：运营=未分配运营时，改按「运营组 → 默认艾特成员」真艾特
                g_uid, g_mob = group_mentions(grp, cfg)
                if g_uid or g_mob:
                    uid_list, mob_list = g_uid, g_mob
                    unmapped = False
                    group_pinged = True
            if unmapped and policy == 'skip':
                print('[ddl_notify] 跳过：运营「%s」没有企微成员映射' % op)
                continue
            lines = []
            for it in items:
                n = it.get('days_overdue', 0)
                due = '今天到期' if n == 0 else ('已逾期 %d 天' % n)
                store = it.get('launch_store') or '-'
                lines.append('· %s   DDL %s（%s）  店铺 %s'
                             % (it.get('spu', ''), it.get('ddl', ''), due, store))
            if group_pinged:
                head = ('@%s 以下 %d 个 SPU（运营未分配，已按运营组默认成员提醒）'
                        % (grp, len(items)))
            elif not unmapped:
                head = '@%s 以下 %d 个 SPU' % (op, len(items))
            else:
                head = ('运营 %s（未配置企微成员，暂无法艾特）以下 %d 个 SPU'
                        % (op, len(items)))
            text = ('【新品上架 DDL 提醒】%s\n运营组：%s\n%s 上架已到期/逾期，'
                    '页面「是否完成上架」仍为「未完成」：\n\n%s\n\n'
                    '请完成上架，或到「新品上架跟进」页面把状态改为 已完成 / 无需上架。'
                    % (date_str, grp, head, '\n'.join(lines)))
            if unmapped and policy == 'at_all':
                uid_list = ['@all']
            msgs.append({
                'group': grp, 'operator': op, 'webhook': webhook, 'text': text,
                'mentioned_list': uid_list, 'mentioned_mobile_list': mob_list,
                'spus': [it.get('spu', '') for it in items], 'unmapped': unmapped,
                'group_pinged': group_pinged,
            })
    msgs.sort(key=lambda m: (m['group'], m['operator']))
    return msgs


def wecom_payload(msg):
    body = {'msgtype': 'text', 'text': {'content': msg['text']}}
    if msg.get('mentioned_list'):
        body['text']['mentioned_list'] = msg['mentioned_list']
    if msg.get('mentioned_mobile_list'):
        body['text']['mentioned_mobile_list'] = msg['mentioned_mobile_list']
    return body


def send_message(webhook, body, timeout=10):
    """POST 到企微群机器人。返回 (ok, 说明)。"""
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(webhook, data=data,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode('utf-8'))
        if out.get('errcode') == 0:
            return True, 'ok'
        return False, 'errcode=%s errmsg=%s' % (out.get('errcode'), out.get('errmsg'))
    except Exception as e:
        return False, '%s: %s' % (type(e).__name__, e)


# ---------------------------------------------------------------------------
# 去重状态
# ---------------------------------------------------------------------------
def load_state(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_state(path, state):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    tmp.replace(p)


def dedupe_key(msg, date_str):
    return '%s|%s|%s' % (date_str, msg['group'], msg['operator'])


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description='新品上架 DDL 到期企微群提醒')
    ap.add_argument('--config', help='配置文件路径（默认 /etc/sku-spu/ddl_notify.json）')
    ap.add_argument('--date', help='指定日期 YYYY-MM-DD（默认今天 Asia/Shanghai）')
    ap.add_argument('--dry-run', action='store_true', help='只打印消息，不发送、不写状态')
    ap.add_argument('--force', action='store_true',
                    help='忽略当天去重强制重发，并绕过「非工作日跳过」检查')
    ap.add_argument('--today-only', action='store_true', help='只提醒当天到期，不含已逾期')
    ap.add_argument('--include-abnormal', action='store_true',
                    help='连「异常（非上架新品）」也提醒（默认排除）')
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    date_str = args.date or today_str()
    state_path = cfg.get('state_path') or str(HERE / 'data' / 'ddl_notify_state.json')
    print('[ddl_notify] 配置=%s  日期=%s  模式=%s'
          % (cfg.get('_path'), date_str, 'DRY-RUN' if args.dry_run else 'SEND'))

    # 只在工作日发：DDL 落在节假日/周末的 SPU，会在下一个最近工作日
    # 被后端「DDL <= 今天」的筛选一并带出，等价于自动顺延补发。
    # 非工作日直接退出，不调接口、不发消息、不写去重状态。
    offday = not is_workday(date_str, cfg)
    if offday and not args.force:
        print('[ddl_notify] %s 非工作日，跳过（不发送任何消息）；'
              '到期项将在下一个工作日 %s 一并提醒'
              % (date_str, next_workday(date_str, cfg)))
        return 0
    if offday:
        print('[ddl_notify] 注意：%s 非工作日，--force 已绕过工作日检查' % date_str)

    payload = fetch_due(cfg, date_str,
                        include_overdue=not args.today_only,
                        exclude_abnormal=not args.include_abnormal)
    print('[ddl_notify] 命中 %s 个 SPU / %s 个运营组；跳过明细=%s'
          % (payload.get('total', 0), len(payload.get('groups') or {}),
             payload.get('skipped') or {}))
    if not payload.get('total'):
        print('[ddl_notify] 今天没有需要提醒的 SPU，结束。')
        return 0

    msgs = build_messages(payload, cfg)
    state = {} if args.force else load_state(state_path)
    failed = 0
    sent = 0
    for m in msgs:
        key = dedupe_key(m, date_str)
        if key in state:
            print('[ddl_notify] 已发过，跳过：%s / %s' % (m['group'], m['operator']))
            continue
        body = wecom_payload(m)
        if args.dry_run:
            hook = (m['webhook'].split('key=')[0] + 'key=***') if 'key=' in m['webhook'] else m['webhook']
            print('\n---- 群：%s | 运营：%s | webhook：%s ----' % (m['group'], m['operator'], hook))
            tag = ('  [按运营组默认成员]' if m.get('group_pinged')
                   else ('  [未配置映射，仅文本]' if m['unmapped'] else ''))
            print('艾特：userid=%s mobile=%s%s'
                  % (m['mentioned_list'], m['mentioned_mobile_list'], tag))
            print(m['text'])
            sent += 1
            continue
        ok, why = send_message(m['webhook'], body, timeout=int(cfg.get('send_timeout', 10)))
        if ok:
            state[key] = {'sent_at': datetime.datetime.now().isoformat(timespec='seconds'),
                          'spus': m['spus']}
            save_state(state_path, state)
            sent += 1
            print('[ddl_notify] 已发送：%s / %s（%d 个 SPU）'
                  % (m['group'], m['operator'], len(m['spus'])))
        else:
            failed += 1
            print('[ddl_notify] 发送失败：%s / %s -> %s'
                  % (m['group'], m['operator'], why))
    if args.dry_run:
        print('[ddl_notify] 预演完成：打印 %d 条，失败 %d 条，跳过 %d 条（未发送任何消息）'
              % (sent, failed, len(msgs) - sent - failed))
    else:
        print('[ddl_notify] 完成：成功 %d 条，失败 %d 条，跳过 %d 条'
              % (sent, failed, len(msgs) - sent - failed))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
