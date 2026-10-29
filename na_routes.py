# -*- coding: utf-8 -*-
r"""
na_routes.py —— 新品盘点（new-arrival）业务蓝图，统一挂载前缀 **/na**
====================================================================
迁移来源：D:\WorkBuddy AI 文件\新品盘点\web_app.py（Phase 2 Step 3）

做了什么
--------
1. 业务函数（备货备注 stock_note / SKU 异常确认 / 颜色档案 / 新色备注 / CSV·Excel 导出）
   **原样保留**，口径零改动。
2. 220KB 内联 MAIN_TEMPLATE 抽到 templates/na_index.html（Jinja 占位不变）。
3. 鉴权统一：删除原 web_app 自带的 /admin 登录页，全部走 auth.py（session + role）。
   原先「9/12 路由裸奔」的洞已补上 —— 未登录访问 /na/* 一律被拦。
4. 存储统一：cloud_store 由 na_core 注入，实际走 common_store 新品盘点命名空间
   （独立 OSS 桶 NA_OSS_*）。
5. 数据源根目录：原 BASE_DIR=脚本所在目录 → 改为 system/na_data/。

路由鉴权策略
------------
- 页面 & 下载 & 读接口：@role_required('sub')（登录即可，sub 与 admin 都行）
- 数据管理写操作（上传 / 重新处理）：@role_required('admin')
"""
import os
import sys
import json
import datetime
import io
import traceback
from pathlib import Path

from flask import (Blueprint, request, jsonify, send_file, Response,
                   redirect, url_for, render_template)

import auth
import na_core
from na_core import (cloud_store, sku_eval, process_script, report_script,
                     NA_DATA_DIR, NA_OUTPUT_DIR,
                     SCRIPTS_AVAILABLE, SCRIPT_IMPORT_ERROR,
                     process_data, generate_report_html)

BASE_DIR = NA_DATA_DIR          # 数据源根目录（进程脚本据此 glob）
OUTPUT_DIR = NA_OUTPUT_DIR      # report_data.json / *.json 产出目录
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

bp = Blueprint('na', __name__, url_prefix='/na')

# 允许上传大文件 (增长产品清单原始 216MB, 瘦身后 1.4MB)
MAX_CONTENT_LENGTH = 250 * 1024 * 1024


def is_admin():
    """统一鉴权下的「管理员」判定 = 当前会话角色为 admin。"""
    return auth.current_role() == 'admin'


def _load_script_module(script_path, module_name):
    """兼容旧调用：不再按相对路径动态加载（迁移后 CWD 不可靠），
    直接返回 na_core 已加载好的同名模块。"""
    return {'process_script': process_script,
            'report_script': report_script}.get(module_name)


def load_report_data_with_confirmations():
    """读取 report_data.json 并按已归档的 SKU 异常确认结果重算 (供导出等场景复用同一口径)"""
    report_data_path = OUTPUT_DIR / "report_data.json"
    if not report_data_path.exists():
        if cloud_store.is_cloud():
            cloud_store.download_to_local(
                cloud_store.BUCKET_OUTPUTS, "report_data.json", report_data_path)
    if not report_data_path.exists():
        return None
    with open(report_data_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    try:
        data = sku_eval.apply_confirmations(data, load_sa_note())
    except Exception as e:
        print('[warn] SKU 异常确认重算失败(导出回退原始报表): %s' % e)
    return data


def need_launch_text(p):
    """列「SPU是否需要上架」的取值 —— 四态决策: 需要上架 / 不可上架 / 待确认 / 暂无需上架。
    优先用报表里已经算好的 need_launch_label; 没有则按 launch_status 现算一口径。
    真实原因(已到仓/在途/SKU未匹配/异常待补尺寸/无库存等)放在列「上架说明」(launch_reason)。
    """
    label = (p.get('need_launch_label') or '') if isinstance(p, dict) else ''
    if label:
        return label
    try:
        label = sku_eval.need_launch_label(p.get('launch_status', ''))
    except Exception:
        label = ''
    if label:
        return label
    return {'can_launch': '需要上架', 'cannot_launch': '不可上架',
            'wait_arrival': '需要上架', 'warning': '待确认',
            'no_need': '暂无需上架'}.get(
        p.get('launch_status', ''), '')


# ============================================================
# 新色跟进 × 新品跟进 三列联动 (2026-10-27 需求, 口径用户确认):
#   SPU包裹情况 / 预计到仓时间 —— 与新品上架跟进同名字段逻辑一致;
#   新色是否需要上架 —— 新品四态基础上加人工评估条件:
#     新色页手动把「是否完成上架」选成「无需上架」且新色特殊备注有内容
#     → 「运营评估无需上架」(人工结论, 优先级最高)。
#   SPU 不在新品报表(已上架老SPU大多不在)时: 预计到仓用新色自己的
#   库存SKU行算, 包裹情况/是否需要上架留空(前端与导出显示 '-')。
# ============================================================
def _build_na_spu_map():
    """SPU(含别名) -> {pkg, eta, launch}: 取自新品跟进报表(已套用SKU异常确认), 与新品页同口径。"""
    m = {}
    try:
        rep = load_report_data_with_confirmations()
    except Exception as e:
        print('[warn] 新色页联动: 新品报表读取失败, 三列按无新品记录处理: %s' % e)
        return m
    for p in (rep or {}).get('products', []):
        spu = p.get('spu', '')
        if not spu:
            continue
        inv = p.get('spu_inventory_status', '')
        rec = {
            'pkg': p.get('multi_package_display', '') or '',
            'eta': '已到仓' if inv == 'in_stock' else (p.get('earliest_eta') or ''),
            'launch': need_launch_text(p),
        }
        if spu not in m:
            m[spu] = rec
        for a in (p.get('aliases') or []):
            if a and a not in m:
                m[a] = rec
    return m


def _nc_eta_from_sku_rows(sku_rows, spu):
    """SPU 不在新品报表时的「预计到仓时间」: 用新色自己的库存SKU行算。
    口径与新品一致: 有在库(>0)→已到仓; 否则取最早在途日期; 都没有→''。
    ⚠️ 预计到仓时间是 SKU 级属性, 与店铺无关 (2026-10-27 用户确认):
    同一 SKU 出现在不同店铺 → 值必须相同, 所以这里**不按店铺过滤**,
    按 SPU 汇总全部店铺的 SKU 行后取「任一在库 / 最早在途」。
    (不做 SKU 去重: 去重会丢掉「另一店铺该SKU有在库」的信息, 取并集才是对的)"""
    has_stock = False
    earliest = ''
    for s in sku_rows or []:
        if str(s.get('SPU', '')) != str(spu):
            continue
        try:
            if int(s.get('在库库存') or 0) > 0:
                has_stock = True
        except (TypeError, ValueError):
            pass
        eta = str(s.get('在途最早预计到仓') or s.get('预计到仓') or '').strip()
        if eta and eta != '-' and (not earliest or eta < earliest):
            earliest = eta
    if has_stock:
        return '已到仓'
    return earliest or ''


def nc_launch_text(spu, na_map, nc_note, spu_eval=None):
    """「新色是否需要上架」:
    ① 新色页「是否完成上架」=无需上架 且 新色特殊备注有内容 → 运营评估无需上架
    ② SPU 在新品报表 → 新品页四态 (need_launch_text)
    ③ 新色管线自评估 (spu_eval, 尺寸+库存同口径) → 四态
    ④ 都不满足 → '' (显示 '-')"""
    if isinstance(nc_note, dict):
        if nc_note.get('launch_completed') == '无需上架' and str(nc_note.get('content') or '').strip():
            return '运营评估无需上架'
    rec = na_map.get(spu) if na_map else None
    if rec and rec.get('launch'):
        return rec['launch']
    se = (spu_eval or {}).get(spu)
    if se:
        return se.get('launch') or ''
    return ''


# ============================================================
# 数据源配置
# ============================================================
DATA_SOURCES = [
    {
        'id': 'self_operated',
        'name': '上品通知表',
        'pattern': '上品通知表.xlsx',
        'accept': '.xlsx',
        'desc': '自营产品上品通知, 表头第5行',
        'used_by': ['na'],   # 用途: 新品跟进
    },
    {
        'id': 'growth',
        'name': '增长产品清单',
        'pattern': '增长产品清单*.xlsx',
        'accept': '.xlsx',
        'desc': '增长产品, 含sofa/dining/RBAC三个sheet (上传后自动瘦身剥离产品图)。'
                '⚠️ 原始版体积大(几十MB含图片), 上传易中断——建议先删掉图片/冗余列再传',
        'used_by': ['na'],   # 用途: 新品跟进
    },
    {
        'id': 'style',
        'name': '款式表',
        'pattern': '款式表.xlsx',
        'accept': '.xlsx',
        'desc': '上架汇总-US, 蓝色=待上架新品',
        'used_by': ['na', 'nc'],   # 用途: 新品跟进 + 新色跟进 (故意不与清洗共用)
    },
    {
        'id': 'sku_dims',
        'name': 'SKU尺寸信息',
        'pattern': 'SKU尺寸货品查询运营结果*.csv',
        'accept': '.csv',
        'desc': 'SKU重量和尺寸数据 (WeightOz+L/W/H)',
        'used_by': ['na', 'nc'],   # 用途: 新品跟进 + 新色跟进 (2026-10-27 起新色也用)
    },
    {
        'id': 'in_transit',
        'name': '动态库存表',
        'pattern': '动态库存表_*.xlsx',
        'accept': '.xlsx',
        'desc': '在途数据, 含ETA和预计到仓时间',
        'used_by': ['na', 'nc'],   # 用途: 新品跟进 + 新色跟进
    },
    {
        'id': 'inventory',
        'name': '库存清单',
        'pattern': '库存清单-*.xlsx',
        'accept': '.xlsx',
        'desc': '在库库存数据',
        'used_by': ['na', 'nc'],   # 用途: 新品跟进 + 新色跟进
    },
    {
        'id': 'listing',
        'name': '店铺上架链接(Amazon)',
        'pattern': '*Listing*.xlsx',
        'accept': '.xlsx',
        'desc': '亚马逊全店铺上架链接 Listing (在售/停售/已删除)',
        # Phase 2: 与 SKU&SPU 看板「领星清单」共用（MSKU/状态/店铺 同构），无需重复上传
        'shared': True, 'shared_desc': 'SKU&SPU 看板 · 领星清单',
        'used_by': ['clean', 'nc'],   # 用途: 数据清洗(领星清单) + 新色跟进; 新品跟进不直接读它
    },
    {
        'id': 'walmart',
        'name': 'Walmart上架链接',
        'pattern': '*ItemReport*.csv',
        'accept': '.csv',
        'desc': 'Walmart ItemReport (PUBLISHED/UNPUBLISHED)',
        # Phase 2: 与 SKU&SPU 看板「walmart 后台报表」共用（SKU/Publish Status 同构）
        'shared': True, 'shared_desc': 'SKU&SPU 看板 · walmart 后台报表',
        'used_by': ['clean', 'nc'],   # 用途: 数据清洗 + 新色跟进
    },
    {
        'id': 'suite',
        'name': '套件成员货品',
        'pattern': '*套件成员*.csv',
        'accept': '.csv',
        'desc': 'SKU平台信息套件成员货品体现 (上架SKU→库存子件)',
        # Phase 2: 与 SKU&SPU 看板「原始表」是同一张表，直接共用
        'shared': True, 'shared_desc': 'SKU&SPU 看板 · 原始表（同一张表）',
        'used_by': ['clean', 'na', 'nc'],   # 用途: 数据清洗(原始表) + 新品跟进 + 新色跟进
    },
    {
        'id': 'transfer',
        'name': '转单关系表',
        'pattern': '转单*.xlsx',
        'accept': '.xlsx',
        'desc': '同品不同工厂/渠道的 SKU 名对照 (原商家SKU 与各次转单/流通sku 全部 sheet); '
                '新色判定里, 同组任一名字已上架 → 全组不算新色',
        # Phase 2: 与 SKU&SPU 看板「转单表」共用（增长/自营 双 sheet 同构）
        'shared': True, 'shared_desc': 'SKU&SPU 看板 · 转单表',
        'used_by': ['clean', 'na', 'nc'],   # 用途: 数据清洗(转单表) + 新品跟进 + 新色跟进
    },
    {
        'id': 'temu_a',
        'name': 'Temu报表(A类 · 含店铺列)',
        'pattern': '*temu_a*.xlsx',
        'accept': '.xlsx',
        'desc': 'Temu 平台 A 类报表（区分店铺）；与 SKU&SPU 看板 Temu A 类报表共用，'
                '当前仅集中管理/清洗使用，暂不参与新品与新色判定',
        'shared': True, 'shared_desc': 'SKU&SPU 看板 · Temu A 类报表',
        'used_by': ['clean'],
    },
    {
        'id': 'temu_b',
        'name': 'Temu报表(B类 · 固定COZY店)',
        'pattern': '*temu_b*.xlsx',
        'accept': '.xlsx',
        'desc': 'Temu 平台 B 类报表（无店铺列，固定 COZY CORNER）；与 SKU&SPU 看板 Temu B 类报表共用，'
                '当前仅集中管理/清洗使用，暂不参与新品与新色判定',
        'shared': True, 'shared_desc': 'SKU&SPU 看板 · Temu B 类报表',
        'used_by': ['clean'],
    },
    # ---- 2026-10-29 入口统一：以下数据源此前只有清洗看板能传，现集中到系统设置 ----
    {
        'id': 'admin',
        'name': '所有库存货品管理员检查表',
        'pattern': '*管理员检查表*.xlsx',
        'accept': '.xlsx',
        'desc': 'SKU平台信息管理员检查表（品类回填：唯一SKU/名称 → 品类_3）；'
                '上传后自动同步到 SKU&SPU 看板，仅集中管理/清洗使用',
        'shared': True, 'shared_desc': 'SKU&SPU 看板 · 管理员检查表（品类）',
        'used_by': ['clean'],
    },
    # 注: 产品运营表不设独立上传卡(2026-10-29 用户确认维持方案A合并) ——
    #     运营/运营组查询沿用「款式表」尾部「AMZ组」「综合组-AMZ」sheet, 一次上传双用;
    #     清洗侧若有历史单独上传的 ops 文件, 仍优先生效(na_core/bridge 保留 ops 通道)。
]


def find_current_file(pattern):
    """检查数据源文件是否存在 (云端 Supabase 或 本地磁盘)"""
    return cloud_store.find_file_info(pattern, cloud_store.BUCKET_UPLOADS)


def auto_slim_xlsx(file_path):
    """自动剥离 xlsx 中的 xl/media/ 内嵌图片 (如果有)
    增长产品清单.xlsx 原始 216MB 含 200 张产品图, 处理脚本从不读取这些图片。
    上传后自动剥离 → 1.4MB, 数据处理结果完全一致。
    返回 dict: {slimmed, original_mb, slim_mb, media_count}
    """
    import zipfile
    file_path = Path(file_path)
    if not file_path.exists() or file_path.suffix.lower() != '.xlsx':
        return {'slimmed': False, 'original_mb': 0, 'slim_mb': 0, 'media_count': 0}

    original_mb = round(file_path.stat().st_size / 1048576, 2)

    # 检查是否有 xl/media/ 目录
    media_count = 0
    try:
        with zipfile.ZipFile(str(file_path), 'r') as z:
            for name in z.namelist():
                if name.startswith('xl/media/'):
                    media_count += 1
    except Exception:
        return {'slimmed': False, 'original_mb': original_mb, 'slim_mb': original_mb, 'media_count': 0}

    if media_count == 0:
        return {'slimmed': False, 'original_mb': original_mb, 'slim_mb': original_mb, 'media_count': 0}

    # 剥离 media 到临时文件, 再替换原文件
    tmp_path = file_path.with_suffix('.tmp_slim')
    try:
        with zipfile.ZipFile(str(file_path), 'r') as zin, \
             zipfile.ZipFile(str(tmp_path), 'w', zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename.startswith('xl/media/'):
                    continue
                zout.writestr(item, zin.read(item.filename))
        file_path.unlink()
        tmp_path.rename(file_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        return {'slimmed': False, 'original_mb': original_mb, 'slim_mb': original_mb, 'media_count': media_count}

    slim_mb = round(file_path.stat().st_size / 1048576, 2)
    return {'slimmed': True, 'original_mb': original_mb, 'slim_mb': slim_mb, 'media_count': media_count}


def _clean_filename(name):
    """清洗上传时的文件名, 适配 Supabase Storage key 规则。

    1. 浏览器/Outlook 下载副本后缀 [1] [2] / (1) (2) → 去掉
       (这能让 process_script.py 的精确名匹配, 如 上品通知表.xlsx)
    2. Windows 保留字符 + Supabase 拒绝的 # ^  → 替换为 _
    3. 首尾空白/句点
    """
    import re
    name = re.sub(r'\s*\(\d+\)\s*', '', name)   # (1) (2)
    name = re.sub(r'\s*\[\d+\]\s*', '', name)   # [1] [2]
    name = re.sub(r'[<>:"/\\|?*\^\#]', '_', name)  # 危险字符
    return name.strip().rstrip('.')


def save_uploaded_file(file, pattern):
    """保存上传文件: 存新文件(本地+云端) → 成功后才删除同 pattern 旧文件
    自动瘦身 xlsx 内嵌图片 (xl/media/)
    文件名清洗: 去除 [1] 下载副本后缀, 替换 Supabase 拒绝的特殊字符

    2026-09-21 关键修复: 旧逻辑「先删旧 → 再传新」，一旦云端上传偶发失败，
    旧文件已被删除 → 数据源显示"未找到" → 用户被迫重新上传。
    改为「先传新 → 成功后再删旧(保留新文件本身)」，上传失败时旧数据完好无损。
    返回 (filename, slim_info, cloud_ok)
    """
    original = file.filename
    filename = _clean_filename(original)
    file.save(str(BASE_DIR / filename))
    # 自动瘦身: 剥离 xlsx 内嵌图片 (如增长产品清单的 200 张产品图)
    slim_info = auto_slim_xlsx(BASE_DIR / filename)
    # 云端模式: 先上传 (已瘦身的) 文件到 Supabase Storage
    cloud_ok = True
    try:
        cloud_store.upload_local_file(BASE_DIR / filename, cloud_store.BUCKET_UPLOADS, filename)
    except Exception as e:
        cloud_ok = False
        print(f"[upload] 云端同步失败(文件已存本地, 旧数据未删除): {e}")
    # 上传成功后再清理同 pattern 的旧版本 (保留刚上传的这个)
    if cloud_ok:
        cloud_store.delete_matching(pattern, cloud_store.BUCKET_UPLOADS, keep_name=filename)
    return filename, slim_info, cloud_ok


def _shared_target_name(na_id):
    """共享类数据源在 na_data 里的固定留档名（与 na_core.SHARED_SOURCES.target 一致）。

    统一固定名的目的：na_data 里该 pattern 永远只有一份文件，
    避免「_共享.csv」与用户原始文件名并存时 find_file_info 取 sorted[-1] 的歧义。
    """
    return next((s['target'] for s in na_core.SHARED_SOURCES if s['na_id'] == na_id), None)


def _cleanup_local_pattern(pattern, keep_name):
    """本地同 pattern 旧文件清理（保留刚上传的这份；云端版在 save_uploaded_file 内已做）。"""
    import fnmatch as _fn
    for p in Path(BASE_DIR).glob(pattern):
        if p.name != keep_name and not p.name.startswith('~$') \
                and _fn.fnmatch(p.name, pattern):
            try:
                p.unlink()
                print('[upload] 已清理本地旧数据源文件: %s' % p.name)
            except OSError:
                pass


def run_scripts():
    """运行处理脚本和报告生成脚本, 返回执行结果

    Phase 2: 处理前先把 SKU&SPU 看板侧的共享数据源同步过来，
    保证本次处理用的是对方最新上传的版本（源文件没变则不重拷）。
    """
    # Phase 2 共享数据源：统一在看板侧上传，这里取最新一份
    try:
        _n, _rs = na_core.sync_shared_sources()
        print('[na] 处理前共享数据源同步: %d/%d' % (_n, len(_rs)))
    except Exception as e:
        print('[na] 共享数据源同步失败(继续处理): %s' % e)

    if not SCRIPTS_AVAILABLE:
        return [{
            'script': '脚本导入',
            'success': False,
            'output': '',
            'error': f'脚本模块导入失败: {SCRIPT_IMPORT_ERROR}',
            'returncode': -1,
        }]

    results = []

    # 云端模式: 处理前确保所有数据源文件已下载到本地磁盘
    if cloud_store.is_cloud():
        patterns = [s['pattern'] for s in DATA_SOURCES]
        cloud_store.ensure_all_data_files_local(patterns)

    # 新品跟进处理脚本
    try:
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        sys.stdout = stdout_capture
        sys.stderr = stderr_capture
        try:
            result = process_data()
            success = True
            returncode = 0
        except Exception as e:
            success = False
            returncode = 1
            print(f"处理异常: {type(e).__name__}: {str(e)}", file=stderr_capture)
            import traceback
            traceback.print_exc(file=stderr_capture)
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        stdout_text = stdout_capture.getvalue()
        stderr_text = stderr_capture.getvalue()

        # 写入综合调试日志
        log_file = OUTPUT_DIR / "debug_process_script.log"
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(f'PYTHON: {sys.executable}\n')
            f.write(f'MODE: direct function call\n')
            f.write(f'RETURN CODE: {returncode}\n')
            f.write(f'--- STDOUT ---\n{stdout_text}\n')
            f.write(f'--- STDERR ---\n{stderr_text}\n')

        results.append({
            'script': '新品跟进处理脚本',
            'success': success,
            'output': stdout_text[-3000:] if stdout_text else '',
            'error': stderr_text[-1000:] if stderr_text else '',
            'returncode': returncode,
        })

        # 重新处理时: 把旧 stock_note 中同名 SPU 的可编辑字段继承到新 report_data.json
        # (上一次更新中存在的 SPU 且本次仍在数据中, 则保留其历史值)
        if success:
            try:
                carried = carryover_editable_fields()
                if carried:
                    print(f"已继承历史可编辑字段: {carried} 个SPU")
            except Exception as e:
                print(f"继承历史字段警告: {e}")

        # 云端模式: 处理成功后上传 report_data.json 到 Supabase Storage
        if cloud_store.is_cloud() and success:
            report_path = OUTPUT_DIR / "report_data.json"
            if report_path.exists():
                cloud_store.upload_local_file(
                    report_path, cloud_store.BUCKET_OUTPUTS, "report_data.json"
                )
            # 同步上传新色跟进数据
            nc_path = OUTPUT_DIR / "new_color_data.json"
            if nc_path.exists():
                cloud_store.upload_local_file(
                    nc_path, cloud_store.BUCKET_OUTPUTS, "new_color_data.json"
                )
    except Exception as e:
        results.append({
            'script': '新品跟进处理脚本',
            'success': False,
            'output': '',
            'error': f'调用异常: {type(e).__name__}: {str(e)}',
            'returncode': -1,
        })

    # 生成报告
    try:
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        sys.stdout = stdout_capture
        sys.stderr = stderr_capture
        try:
            generate_report_html()
            success = True
            returncode = 0
        except Exception as e:
            success = False
            returncode = 1
            print(f"报告生成异常: {type(e).__name__}: {str(e)}", file=stderr_capture)
            import traceback
            traceback.print_exc(file=stderr_capture)
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        stdout_text = stdout_capture.getvalue()
        stderr_text = stderr_capture.getvalue()

        log_file = OUTPUT_DIR / "debug_report_script.log"
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(f'PYTHON: {sys.executable}\n')
            f.write(f'MODE: direct function call\n')
            f.write(f'RETURN CODE: {returncode}\n')
            f.write(f'--- STDOUT ---\n{stdout_text}\n')
            f.write(f'--- STDERR ---\n{stderr_text}\n')

        results.append({
            'script': '生成报告',
            'success': success,
            'output': stdout_text[-3000:] if stdout_text else '',
            'error': stderr_text[-1000:] if stderr_text else '',
            'returncode': returncode,
        })
    except Exception as e:
        results.append({
            'script': '生成报告',
            'success': False,
            'output': '',
            'error': f'调用异常: {type(e).__name__}: {str(e)}',
            'returncode': -1,
        })

    return results


# ============================================================
# 备货备注 + 可编辑业务字段 (每个SPU独立, 所有账户可编辑, 同步后广播)
# 存储结构 v2 (向后兼容旧 v1):
#   { spu: {
#       "content": "SPU特殊备注",         # v1 字段, 保留
#       "updated_at": "2026-08-19 17:00", # v1: 最后更新时间
#       "updated_by": "admin",            # v1: 最后更新者
#       "abnormal_status": "normal",      # v2: normal/abnormal (异常处理)
#       "operator": "张三",                 # v2: 覆盖默认运营
#       "operator_group": "软包组",         # v2: 覆盖默认运营组
#       "launch_store": "DECO",           # v2: 上架店铺 (DECO/HN/Urban/XYT/JYT)
#       "launch_completed": "pending",    # v2: pending/done (是否上架完成)
#       "field_updated_at": {              # v2: 每字段独立时间戳 (审计)
#           "abnormal_status": "2026-08-19 17:00",
#           ...
#       }
#   }, ... }
# ============================================================
STOCK_NOTE_FILE = OUTPUT_DIR / "stock_note.json"

# v2 可编辑业务字段定义 (除 content 外)
EDITABLE_FIELDS = {
    'abnormal_status': {
        'label': '异常处理',
        'type': 'select',
        'options': [
            {'value': 'normal', 'label': '正常'},
            {'value': 'abnormal', 'label': '异常（非上架新品）'},
        ],
        'default': 'normal',
        'admin_only': True,   # 仅管理员可编辑
    },
    'operator_group': {
        'label': '运营组',
        'type': 'select_dynamic',   # 选项来自 style_options
        'options_key': 'groups',
        'default': '',              # 默认沿用款式表原值
        'admin_only': False,
    },
    'operator': {
        'label': '运营',
        'type': 'select_dynamic',
        'options_key': 'operators',
        'default': '',
        'admin_only': False,
    },
    'launch_store': {
        'label': '上架店铺',
        'type': 'select',
        'options': [
            {'value': '', 'label': '-'},
            {'value': 'DECO', 'label': 'DECO'},
            {'value': 'HN', 'label': 'HN'},
            {'value': 'Urban', 'label': 'Urban'},
            {'value': 'XYT', 'label': 'XYT'},
            {'value': 'JYT', 'label': 'JYT'},
        ],
        'default': '',
        'admin_only': False,
    },
    'launch_completed': {
        'label': '是否完成上架',
        'type': 'select',
        'options': [
            {'value': 'pending', 'label': '未完成'},
            {'value': 'done', 'label': '已完成'},
            {'value': 'not_needed', 'label': '无需上架'},
        ],
        'default': 'pending',
        'admin_only': False,
    },
    'launch_ddl': {
        'label': '上架DDL',
        'type': 'date',           # 手工调整后以调整值为准, 重处理不覆盖; 清空=恢复系统计算值
        'default': '',
        'admin_only': False,
    },
    'content': {
        'label': 'SPU特殊备注',
        'type': 'text',
        'default': '',
        'admin_only': False,
    },
}

# 字段顺序 (前端表格列从左到右, 异常处理在 SPU 之前)
FIELD_ORDER = ['abnormal_status', 'content']


def load_stock_note():
    """读取全部SPU备货备注+可编辑字段; 不存在或旧单条格式返回空dict (云端 Supabase / 本地磁盘)"""
    data = cloud_store.read_json(
        cloud_store.BUCKET_OUTPUTS, "stock_note.json", STOCK_NOTE_FILE
    )
    if data is None:
        return {}
    if isinstance(data, dict) and 'content' in data:
        return {}  # 旧的单条格式, 自动重置为空
    return data if isinstance(data, dict) else {}


def save_stock_note(notes_dict, by=''):
    """合并保存多个SPU的可编辑字段, 记录每字段独立更新时间, 返回最新全量 (云端+本地)

    notes_dict 格式 (v2):
        { spu: { field_name: value, ... }, ... }

    兼容旧 v1 调用:
        { spu: "content string" }  →  当作 {spu: {content: "content string"}}
    """
    existing = load_stock_note()
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    for spu, payload in notes_dict.items():
        if spu not in existing or not isinstance(existing[spu], dict):
            existing[spu] = {}
        spu_rec = existing[spu]
        if 'field_updated_at' not in spu_rec or not isinstance(spu_rec.get('field_updated_at'), dict):
            spu_rec['field_updated_at'] = {}

        # v1 兼容: payload 是字符串 -> 当作 content 字段
        if isinstance(payload, str):
            payload = {'content': payload}

        if not isinstance(payload, dict):
            continue

        for field, value in payload.items():
            # 仅接受白名单字段
            if field not in EDITABLE_FIELDS:
                continue
            # 空值 / 空白 / None -> 强制存空字符串 (不再 pop)
            # 目的: 用户"清空"某字段后, spuNotes 仍含该字段(值为空),
            #       前端字段级合并 (Object.assign(note_fields, snRec)) 不会回退到 note_fields 历史继承值
            if value is None or (isinstance(value, str) and value.strip() == ''):
                value = ''
            # 上架DDL: 仅接受 YYYY-MM-DD 日期格式 (空值=恢复系统计算值)
            if field == 'launch_ddl' and value:
                try:
                    datetime.datetime.strptime(value, '%Y-%m-%d')
                except ValueError:
                    continue
            spu_rec[field] = value
            spu_rec['field_updated_at'][field] = now_str + ' by ' + (by or '')

        # 兼容 v1 的顶层 updated_at/updated_by
        spu_rec['updated_at'] = now_str
        spu_rec['updated_by'] = by or ''

    ok = cloud_store.write_json(
        cloud_store.BUCKET_OUTPUTS, "stock_note.json", existing, STOCK_NOTE_FILE
    )
    if not ok:
        # 本地已写入, 但云端 (Supabase) 上传失败 -> 刷新后从云端读会"丢数据"
        raise RuntimeError("云端同步失败: stock_note.json 未能上传到 Supabase, 请检查云端存储配置后重试")
    return existing


def get_spu_field(spu_notes, spu, field, default=None):
    """从 stock_note 中安全读取某个 SPU 的某个字段, 没设置时返回 default"""
    if not spu_notes or spu not in spu_notes:
        return default
    rec = spu_notes[spu]
    if not isinstance(rec, dict) or field not in rec:
        return default
    return rec[field]


# 重新处理时需继承历史的可编辑字段 ("运营预计上架时间"已删除)
# 需求: 运营 / 运营组 / 上架店铺 / 是否完成上架 / SPU特殊备注 —— 上一次有记录, 本次该 SPU 仍在数据中,
#       且本次无更新 → 取上次的记录为准, 不重新计算; 在系统里调整过的, 以最新调整值优先。
# launch_ddl: 用户手工调整过的 DDL 一并继承, 系统计算值在 report_data 里, 前端调整值优先。
# operator/operator_group: 人工调整值一并继承; 款式表有非空值时仍以款式表为准(有变化才更新)。
CARRYOVER_FIELDS = ('abnormal_status', 'operator_group', 'operator',
                    'launch_store', 'launch_completed', 'launch_ddl', 'content')


def carryover_editable_fields():
    """重新处理 (上传新数据源并点"处理") 时, 把旧 stock_note 中同名 SPU 的可编辑字段
    (CARRYOVER_FIELDS: 运营 / 运营组 / 上架店铺 / 是否完成上架 / SPU特殊备注 / 上架DDL / 异常处理)
    继承到新生成的 report_data.json 的 note_fields, 作为历史保留值。

    匹配方式: 按 SPU 键精确匹配 (上一次更新中存在的 SPU 且本次仍在数据中, 则保留其历史值)。
    前端: stock_note 实时值优先, 若该 SPU 从未在 stock_note 中出现则回退到 note_fields。
    返回继承的 SPU 数量。"""
    old_note = load_stock_note()
    if not old_note:
        return 0
    report_path = OUTPUT_DIR / "report_data.json"
    if not report_path.exists():
        return 0
    try:
        with open(report_path, 'r', encoding='utf-8') as f:
            report = json.load(f)
    except Exception:
        return 0
    products = report.get('products', [])
    carried = 0
    for p in products:
        spu = p.get('spu')
        if not spu or spu not in old_note:
            continue
        rec = old_note[spu]
        if not isinstance(rec, dict):
            continue
        nf = p.setdefault('note_fields', {})
        changed = False
        for field in CARRYOVER_FIELDS:
            val = rec.get(field)
            if val is not None and val != '':
                if field not in nf:
                    nf[field] = val
                    changed = True
        if changed:
            carried += 1
    if carried > 0:
        try:
            with open(report_path, 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=1)
        except Exception:
            return 0
        if cloud_store.is_cloud():
            try:
                cloud_store.upload_local_file(report_path, cloud_store.BUCKET_OUTPUTS, "report_data.json")
            except Exception:
                pass
    return carried


def load_style_options():
    """读取 款式表_完整数据.json 提取所有 distinct 的运营组(group)和运营(operator) 选项
    返回 {"groups": [...], "operators": [...]} (已排序, 已过滤占位符)
    文件不存在或读取失败返回空列表 (前端会回退到默认值)"""
    style_json = BASE_DIR / "款式表_完整数据.json"
    if not style_json.exists():
        # 云端模式: 尝试从 Supabase 下载
        if cloud_store.is_cloud():
            try:
                cloud_store.ensure_data_file_local("款式表*.xlsx", cloud_store.BUCKET_UPLOADS)
                # 重新生成 JSON (调用 process_script 的函数)
                try:
                    process_data = _load_script_module(Path("process_script.py"), "process_script")
                    if hasattr(process_data, 'regenerate_style_json'):
                        process_data.regenerate_style_json()
                except Exception:
                    pass
            except Exception:
                pass
        if not style_json.exists():
            return {"groups": [], "operators": []}

    try:
        with open(style_json, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return {"groups": [], "operators": []}

    # 跳过表头项 (第一项 key 是 A/B/C/D/E/F, 后续项 key 是 spu/group/operator)
    groups = set()
    operators = set()
    placeholder = {'', '/', '-', '无', '待定', 'N/A', 'NA'}
    for item in data[1:] if data and isinstance(data[0], dict) and 'A' in data[0] else data:
        if not isinstance(item, dict):
            continue
        g = (item.get('group') or '').strip()
        if g and g not in placeholder:
            groups.add(g)
        op = (item.get('operator') or '').strip()
        if op and op not in placeholder:
            operators.add(op)

    return {
        "groups": sorted(groups),
        "operators": sorted(operators),
    }


# ============================================================
# API 端点
# ============================================================

@bp.route('/')
@auth.role_required('sub')
def index():
    # Phase 2 共享数据源：进入页面时增量同步一次（源表没变不会重拷，避免每次都搬 6MB）
    try:
        na_core.sync_shared_sources()
    except Exception as e:
        print('[na] 共享数据源增量同步失败: %s' % e)

    shared_map = {s['na_id']: s for s in na_core.shared_status()}
    sources_with_files = []
    for s in DATA_SOURCES:
        info = dict(s)
        info['file'] = find_current_file(s['pattern'])
        if s.get('shared'):
            st = shared_map.get(s['id'], {})
            info['shared_info'] = {
                'src': s.get('shared_desc', ''),
                'synced': bool(st.get('synced')),
                'synced_at': st.get('synced_at', ''),
                'src_name': st.get('src_name', ''),
                'error': st.get('error', ''),
            }
        sources_with_files.append(info)

    # 检查报告数据是否存在 (云端 Supabase / 本地磁盘)
    report_data_path = OUTPUT_DIR / "report_data.json"
    report_exists = report_data_path.exists()
    if not report_exists and cloud_store.is_cloud():
        report_exists = cloud_store.file_exists(
            cloud_store.BUCKET_OUTPUTS, "report_data.json"
        )

    admin = is_admin()

    return render_template(
        'na_index.html',
        sources_json=json.dumps(sources_with_files, ensure_ascii=False),
        report_exists=report_exists,
        is_admin='true' if admin else 'false',
        is_admin_bool=admin,
    )


@bp.route('/api/report-data')
@auth.role_required('sub')
def api_report_data():
    """返回报告JSON数据, 供前端交互式渲染 (云端优先下载到本地再读)
    附加 style_options: 款式表中所有运营组/运营选项, 供前端可编辑下拉框使用"""
    report_data_path = OUTPUT_DIR / "report_data.json"
    if not report_data_path.exists():
        # 云端模式: 尝试从 Supabase 下载到本地
        if cloud_store.is_cloud():
            cloud_store.download_to_local(
                cloud_store.BUCKET_OUTPUTS, "report_data.json", report_data_path
            )
    if not report_data_path.exists():
        return jsonify({'error': '报告数据不存在, 请先处理数据'}), 404

    with open(report_data_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 附加 style_options (款式表中的运营组/运营 distinct 集合)
    data['style_options'] = load_style_options()

    # 按已归档的 SKU 异常确认结果即时重算:
    # 运营把某 SKU 确认成「无效SKU」后, 刷新页面就能看到 SPU 结论变化,
    # 不必等后台约 6 分钟的重新处理 (后台重跑会由 process_script 走同一口径复核)。
    try:
        data = sku_eval.apply_confirmations(data, load_sa_note())
    except Exception as e:
        print('[warn] SKU 异常确认重算失败, 回退原始报表: %s' % e)

    return jsonify(data)


@bp.route('/api/stock-note', methods=['GET'])
@auth.role_required('sub')
def api_stock_note_get():
    """获取全部SPU备货备注 (所有账户可访问)"""
    return jsonify(load_stock_note())


@bp.route('/api/new-color')
@auth.role_required('sub')
def api_new_color():
    """返回新色跟进判定数据 (v4), 供前端渲染"""
    path = OUTPUT_DIR / "new_color_data.json"
    if not path.exists():
        if cloud_store.is_cloud():
            cloud_store.download_to_local(
                cloud_store.BUCKET_OUTPUTS, "new_color_data.json", path
            )
    if not path.exists():
        return jsonify({'error': '新色数据不存在, 请先处理数据'}), 404
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    data['style_options'] = load_style_options()
    # 2026-10-27: 新色页新增三列 (SPU包裹情况/预计到仓时间/新色是否需要上架) 的
    # 新品报表侧数据; 每次请求现算, 保证与新品页最新数据一致(含SKU异常确认)
    data['na_spu_map'] = _build_na_spu_map()
    return jsonify(data)


# ============================================================
# 颜色档案 (v4 archive-first)
#   - 种子: color_dict_seed.py (2026-09-21 用户核对表 281 条确认, 随代码部署)
#   - 云端: output/color_dict.json (网页端人工确认后写入, 覆盖种子)
#   - 判定规则: 判定只认系统存档; 档案外新颜色码以「颜色待确认」进系统,
#     人工确认后存档, 下次处理生效; 除非人工调整, 始终沿用历史存档
# ============================================================
COLOR_DICT_FILE = OUTPUT_DIR / "color_dict.json"


def _read_local_color_dict():
    """读取本机 output/color_dict.json (磁盘镜像, 用于规避云端读后写延迟)"""
    try:
        with open(COLOR_DICT_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _merge_color_source(merged, data):
    """把一份 {version, colors:{CODE:{name,status}}} 合并进 merged (后合并的覆盖先前的)"""
    if not isinstance(data, dict):
        return merged
    for k, v in (data.get('colors') or {}).items():
        if k and isinstance(v, dict):
            merged[str(k).strip().upper()] = {
                'name': str(v.get('name', '')).strip(),
                'status': v.get('status', '颜色'),
            }
    return merged


def load_color_archive_full():
    """合并颜色档案: 内置种子 → 云端 color_dict.json → 本机磁盘镜像 (后写覆盖)。
    返回 {CODE: {name, status}}

    本机镜像参与合并的原因 (2026-09-21 实测): Supabase 免费层写入后短时读不到新内容,
    若只以「云端读到的内容」为基准做 read-modify-write, 连续单条确认会互相覆盖,
    最终只剩最后一条。加入本机镜像后, 基准始终包含本实例已写过的全部记录。
    """
    from color_dict_seed import SEED_COLORS
    merged = {}
    for k, v in SEED_COLORS.items():
        merged[str(k).strip().upper()] = {
            'name': str(v.get('name', '')).strip(),
            'status': v.get('status', '颜色'),
        }
    _merge_color_source(merged, cloud_store.read_json(
        cloud_store.BUCKET_OUTPUTS, "color_dict.json", COLOR_DICT_FILE))
    _merge_color_source(merged, _read_local_color_dict())
    return merged


@bp.route('/api/color-dict', methods=['GET'])
@auth.role_required('sub')
def api_color_dict_get():
    """获取颜色档案 (所有账户可访问, 供展示确认状态)"""
    merged = load_color_archive_full()
    return jsonify({'colors': merged, 'count': len(merged)})


@bp.route('/api/color-dict', methods=['POST'])
@auth.role_required('sub')
def api_color_dict_post():
    """人工确认颜色并存档 (仅管理员)
    接收: {code: 色码, name: 规范色名(或'非颜色'), updated_by: ...}"""
    if not is_admin():
        return jsonify({'success': False, 'error': '权限不足: 仅管理员可确认颜色存档'}), 403
    data = request.get_json(silent=True) or {}
    code = str(data.get('code', '')).strip().upper()
    name = str(data.get('name', '')).strip()
    by = data.get('updated_by', '')
    if not code:
        return jsonify({'success': False, 'error': '缺少颜色码'})
    if name.upper() == '非颜色':
        name = ''
    if not name:
        status = '非颜色'
    else:
        status = '颜色'
    merged = load_color_archive_full()
    merged[code] = {'name': name if status == '颜色' else '', 'status': status}
    payload = {
        'version': 1,
        'updated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M') + ' by ' + (by or ''),
        'colors': merged,
    }
    # 写入 + 回读校验 (Supabase 免费层写入后短时读不到, 需重试; 基准含本机镜像故不会丢数据)
    import time
    verified = False
    for attempt in range(3):
        ok = cloud_store.write_json(
            cloud_store.BUCKET_OUTPUTS, "color_dict.json", payload, COLOR_DICT_FILE
        )
        if not ok:
            raise RuntimeError("云端同步失败: color_dict.json 未能上传到 Supabase, 请检查云端存储配置后重试")
        check = cloud_store.read_json(
            cloud_store.BUCKET_OUTPUTS, "color_dict.json", COLOR_DICT_FILE
        )
        codes_back = {str(x).strip().upper() for x in ((check or {}).get('colors') or {})}
        if code in codes_back:
            verified = True
            break
        if attempt < 2:
            time.sleep(1.5)
    print('[color-dict] 存档 %s -> %s (status=%s, verified=%s, count=%d)'
          % (code, name, status, verified, len(merged)))
    return jsonify({'success': True, 'code': code, 'name': name, 'status': status,
                    'count': len(merged), 'verified': verified})


# ============================================================
# 新色跟进备注 (2026-09-21 用户确认: 与新品跟进 stock_note.json 完全独立)
# 字段: launch_completed(未完成/已完成/无需上架) / content(特殊备注) /
#       operator / operator_group (覆盖店铺规则计算值)
# 读取: 云端 → 本机镜像合并 (本机镜像覆盖, 抗 Supabase 写后读延迟,
#       与颜色档案同一健壮性原则, 否则"保存后重新处理/刷新读回旧值")
# ============================================================
NC_NOTE_FILE = OUTPUT_DIR / "new_color_note.json"
NC_NOTE_FIELDS = ('launch_completed', 'content', 'operator', 'operator_group')
NC_COMPLETED_VALUES = ('未完成', '已完成', '无需上架', '')


def _read_local_nc_note():
    """读取本机 new_color_note.json 镜像 (可能比云端新)"""
    try:
        with open(NC_NOTE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def load_nc_note():
    """读取新色跟进备注: 云端 → 本机镜像 (后读覆盖, 按 SPU 键合并)"""
    merged = {}
    for data in (cloud_store.read_json(
            cloud_store.BUCKET_OUTPUTS, "new_color_note.json", NC_NOTE_FILE),
            _read_local_nc_note()):
        if isinstance(data, dict):
            for k, v in data.items():
                if k and isinstance(v, dict):
                    merged[k] = dict(v)
    return merged


def save_nc_note(notes_dict, by=''):
    """合并保存新色跟进备注 (仅白名单字段), 写云端+本机镜像, 返回最新全量"""
    existing = load_nc_note()
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    for spu, payload in notes_dict.items():
        if not isinstance(payload, dict):
            continue
        if spu not in existing or not isinstance(existing[spu], dict):
            existing[spu] = {}
        spu_rec = existing[spu]
        if 'field_updated_at' not in spu_rec or not isinstance(spu_rec.get('field_updated_at'), dict):
            spu_rec['field_updated_at'] = {}
        for field, value in payload.items():
            if field not in NC_NOTE_FIELDS:
                continue
            # 空值强制存 '' (清空语义, 与 stock_note 一致)
            if value is None or (isinstance(value, str) and value.strip() == ''):
                value = ''
            spu_rec[field] = value
            spu_rec['field_updated_at'][field] = now_str + ' by ' + (by or '')
        spu_rec['updated_at'] = now_str
        spu_rec['updated_by'] = by or ''
    ok = cloud_store.write_json(
        cloud_store.BUCKET_OUTPUTS, "new_color_note.json", existing, NC_NOTE_FILE
    )
    if not ok:
        raise RuntimeError("云端同步失败: new_color_note.json 未能上传到 Supabase, 请检查云端存储配置后重试")
    return existing


@bp.route('/api/nc-note', methods=['GET'])
@auth.role_required('sub')
def api_nc_note_get():
    """获取新色跟进备注 (所有账户可访问)"""
    return jsonify(load_nc_note())


@bp.route('/api/nc-note', methods=['POST'])
@auth.role_required('sub')
def api_nc_note_post():
    """保存新色跟进备注 (所有账户可编辑, 与新品跟进 stock_note 完全独立)
    接收: {notes: {spu: {field: value, ...}}, updated_by: ...}"""
    data = request.get_json(silent=True) or {}
    notes = data.get('notes', {})
    by = data.get('updated_by', '')
    for spu, payload in (notes or {}).items():
        if isinstance(payload, dict) and 'launch_completed' in payload:
            v = payload['launch_completed']
            if v not in NC_COMPLETED_VALUES:
                return jsonify({'success': False,
                                'error': '非法的是否完成上架取值: %s (仅 未完成/已完成/无需上架)' % v}), 400
    try:
        saved = save_nc_note(notes, by=by)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': True, 'notes': saved, 'count': len(saved)})


# ============================================================
# SKU 异常确认留档 (sku_anomaly_note.json)
# 需求 2026-10-20: SKU异常记录要能逐条确认「正常SKU / 无效SKU」, 结果必须留档——
# 重新处理数据时不能丢, 运营不必每次刷新都重新确认一遍; 但确认结果依然要可改。
# 确认「无效SKU」的 SKU 会被全流程剔除: 不再让该SPU落到「待确认(异常)」, SKU数/包裹统计也不再计入。
# 读取: 云端 → 本机镜像合并 (抗 Supabase 写后读延迟, 同 new_color_note.json)
# ============================================================
SA_NOTE_FILE = OUTPUT_DIR / "sku_anomaly_note.json"
SA_FIELDS = ('status', 'note')
SA_STATUS_VALUES = ('valid', 'invalid', 'combo', '')


def _read_local_sa_note():
    try:
        with open(SA_NOTE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def load_sa_note():
    """读取 SKU 异常确认留档: 云端 → 本机镜像 (后读覆盖, 保留清空语义)"""
    merged = {}
    for data in (cloud_store.read_json(
            cloud_store.BUCKET_OUTPUTS, "sku_anomaly_note.json", SA_NOTE_FILE),
            _read_local_sa_note()):
        if isinstance(data, dict):
            for k, v in data.items():
                if k and isinstance(v, dict):
                    merged[k] = dict(v)
    return merged


def save_sa_note(notes_dict, by=''):
    """合并保存 SKU 异常确认结果 (仅白名单字段), 写云端+本机镜像, 返回最新全量"""
    existing = load_sa_note()
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    for sku, payload in (notes_dict or {}).items():
        if not sku:
            continue
        if not isinstance(payload, dict):
            continue
        if sku not in existing or not isinstance(existing[sku], dict):
            existing[sku] = {}
        rec = existing[sku]
        for field, value in payload.items():
            if field not in SA_FIELDS:
                continue
            # 空值强制存 '' (清空语义, 支持改回「待确认」)
            if value is None or (isinstance(value, str) and value.strip() == ''):
                value = ''
            rec[field] = value
        rec['updated_at'] = now_str
        rec['updated_by'] = by or ''
    ok = cloud_store.write_json(
        cloud_store.BUCKET_OUTPUTS, "sku_anomaly_note.json", existing, SA_NOTE_FILE
    )
    if not ok:
        raise RuntimeError("云端同步失败: sku_anomaly_note.json 未能上传到 Supabase, 请检查云端存储配置后重试")
    return existing


@bp.route('/api/sku-anomaly-note', methods=['GET'])
@auth.role_required('sub')
def api_sa_note_get():
    """获取全部 SKU 异常确认留档"""
    return jsonify(load_sa_note())


@bp.route('/api/sku-anomaly-note', methods=['POST'])
@auth.role_required('admin')
def api_sa_note_post():
    """保存 SKU 异常确认结果 (支持批量) —— 仅管理员可操作
    （待确认SKU确认 / 组合SKU / 无效SKU调整；运营角色仅有查看权限）
    接收: {notes: {sku: {status: 'valid'|'invalid'|'combo'|'', note: '...'}}, updated_by: ...}
    一次请求可携带多个 SKU —— 用于「勾选多条后批量确认」(需求3)。"""
    data = request.get_json(silent=True) or {}
    notes = data.get('notes', {})
    by = data.get('updated_by', '')
    for sku, payload in (notes or {}).items():
        if isinstance(payload, dict) and 'status' in payload:
            v = payload['status']
            if v not in SA_STATUS_VALUES:
                return jsonify({'success': False,
                                'error': '非法的确认取值: %s (仅 正常SKU/无效SKU/组合SKU/待确认)' % v}), 400
    try:
        saved = save_sa_note(notes, by=by)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': True, 'notes': saved, 'count': len(saved)})


# ============================================================
# 账号管理 (系统设置 · 仅管理员)
#   子账号存于云端文件 accounts.json (auth.save_accounts)，界面增删/改密码立即生效，
#   无需改环境变量或重启。登录按密码匹配 (env SUB_ACCOUNTS 作基础，文件覆盖)。
# ============================================================
@bp.route('/api/accounts', methods=['GET'])
@auth.role_required('admin')
def api_accounts_get():
    """返回全部子账号（含明文密码，仅管理员可见）。"""
    accts = auth.load_accounts()
    return jsonify({'count': len(accts),
                    'accounts': [{'name': n, 'password': p} for n, p in accts.items()]})


@bp.route('/api/accounts', methods=['POST'])
@auth.role_required('admin')
def api_accounts_post():
    """保存全部子账号。body: {accounts: [{name, password}, ...]}；空名/空密码自动跳过，同名去重。
    返回 {ok, count, accounts}。"""
    data = request.get_json(silent=True) or {}
    try:
        res = auth.save_accounts(data.get('accounts', []))
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    return jsonify({'ok': True, **res})


@bp.route('/api/stock-note', methods=['POST'])
@auth.role_required('sub')
def api_stock_note_post():
    """保存SPU备货备注+可编辑字段 (所有账户可编辑大部分字段; abnormal_status 仅管理员)
    接收: {notes: {spu: {field: value, ...}}, updated_by: ...}
    兼容旧 v1: {notes: {spu: "content string"}, updated_by: ...}"""
    data = request.get_json(silent=True) or {}
    notes = data.get('notes', {})
    by = data.get('updated_by', '')
    admin_session = is_admin()

    # 权限过滤: 非管理员不能改 abnormal_status 字段 (静默忽略, 不报错)
    if not admin_session:
        for spu, payload in notes.items():
            if isinstance(payload, dict) and 'abnormal_status' in payload:
                # 移除该字段, 但保留其他字段
                payload.pop('abnormal_status', None)

    saved = None
    try:
        saved = save_stock_note(notes, by)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e) or '云端保存失败'})
    return jsonify({'success': True, 'notes': saved})


@bp.route('/api/data-sources')
@auth.role_required('sub')
def api_data_sources():
    """返回数据源状态"""
    shared_map = {s['na_id']: s for s in na_core.shared_status()}
    sources = []
    for s in DATA_SOURCES:
        info = dict(s)
        info['file'] = find_current_file(s['pattern'])
        if s.get('shared'):
            st = shared_map.get(s['id'], {})
            info['shared_info'] = {
                'src': s.get('shared_desc', ''),
                'synced': bool(st.get('synced')),
                'synced_at': st.get('synced_at', ''),
                'src_name': st.get('src_name', ''),
                'error': st.get('error', ''),
            }
        sources.append(info)
    return jsonify(sources)


@bp.route('/api/shared-sources')
@auth.role_required('sub')
def api_shared_sources():
    """共享数据源状态（Phase 2）：与 SKU&SPU 看板共用的 4 类同构表格。"""
    return jsonify({'sources': na_core.shared_status()})


@bp.route('/api/shared-sources/sync', methods=['POST'])
@auth.role_required('admin')
def api_shared_sources_sync():
    """强制重新同步共享数据源（源表没变也重拷一份）。"""
    body = request.get_json(silent=True) or {}
    force = bool(body.get('force', True))
    try:
        n, rs = na_core.sync_shared_sources(force=force)
        return jsonify({'success': True, 'synced': n, 'results': rs,
                        'sources': na_core.shared_status()})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


@bp.route('/upload', methods=['POST'])
@auth.role_required('admin')
def upload():
    if not is_admin():
        return jsonify({'success': False, 'error': '权限不足: 仅管理员可上传文件'}), 403
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': '未收到文件'})
    file = request.files['file']
    source_id = request.form.get('source_id', '')
    if file.filename == '':
        return jsonify({'success': False, 'error': '文件名为空'})
    source = next((s for s in DATA_SOURCES if s['id'] == source_id), None)
    if not source:
        return jsonify({'success': False, 'error': f'未知数据源: {source_id}'})
    try:
        original_name = file.filename
        is_shared = bool(source.get('shared'))
        # 2026-10-29 入口统一: 共享类数据源固定存「_共享」名（该 pattern 仅此一份），
        # 并在上传成功后自动推送到 SKU&SPU 清洗看板（bridge_main，含同口径解析校验）。
        if is_shared:
            _tname = _shared_target_name(source_id)
            if _tname:
                file.filename = _tname
        filename, slim_info, cloud_ok = save_uploaded_file(file, source['pattern'])
        if is_shared:
            _cleanup_local_pattern(source['pattern'], filename)
        # 反向同步: 系统设置 → 清洗看板（校验失败/推送失败不影响本侧上传成功）
        push = None
        if is_shared:
            try:
                import bridge_main
                push = bridge_main.push_to_main(source_id, str(Path(BASE_DIR) / filename),
                                                original_name)
            except Exception as e:
                push = {'ok': False, 'error': '%s: %s' % (type(e).__name__, e)}
            print('[upload] 共享源 %s 推送清洗看板: ok=%s %s' %
                  (source_id, push.get('ok'), push.get('error') or push.get('summary') or ''))
        return jsonify({
            'success': True,
            'filename': filename,
            'slim_info': slim_info,
            'cloud_ok': cloud_ok,
            'cloud_warn': '' if cloud_ok else '云端同步失败: 文件已存本地, 旧数据未删除。请稍后重试上传, 或联系管理员检查 Supabase 配置。',
            'push': push,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@bp.route('/process', methods=['POST'])
@auth.role_required('admin')
def process():
    if not is_admin():
        return jsonify({'success': False, 'error': '权限不足: 仅管理员可处理数据'}), 403
    results = run_scripts()
    all_success = all(r['success'] for r in results)
    report_data_path = OUTPUT_DIR / "report_data.json"
    report_exists = report_data_path.exists()
    if not report_exists and cloud_store.is_cloud():
        report_exists = cloud_store.file_exists(
            cloud_store.BUCKET_OUTPUTS, "report_data.json"
        )
    return jsonify({
        'success': all_success,
        'results': results,
        'report_exists': report_data_path.exists(),
    })


@bp.route('/download/html')
@auth.role_required('sub')
def download_html():
    """生成并下载静态HTML报告"""
    # 云端模式: 确保 report_data.json 在本地磁盘
    if cloud_store.is_cloud():
        cloud_store.ensure_report_data_local()
    try:
        if generate_report_html is not None:
            generate_report_html()
    except Exception as e:
        # 出错时记录但不阻断下载(可能已有旧报告)
        import traceback
        print(f"生成HTML报告异常: {type(e).__name__}: {str(e)}", file=sys.stderr)
        traceback.print_exc()
    report_file = BASE_DIR / "北美市场事业部新品跟进.html"
    if report_file.exists():
        return send_file(str(report_file), as_attachment=True,
                         download_name="北美市场事业部新品跟进.html")
    return "报告生成失败", 404


@bp.route('/download/csv')
@auth.role_required('sub')
def download_csv():
    """导出CSV数据"""
    data = load_report_data_with_confirmations()
    if data is None:
        return "数据不存在", 404

    import csv
    import io
    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output)
    writer.writerow(['异常处理', 'SPU', '来源', '运营组', '运营', 'SPU包裹情况', 'FedEx类型', '库存状态',
                     '预计到仓时间', '上架DDL', 'SPU是否需要上架', '上架店铺', '是否完成上架',
                     '上架说明', 'SKU数', 'SKU异常记录', '组合SKU', '无效SKU'])

    inv_map = {'in_stock': '已到仓', 'in_transit': '在途', 'no_record': '无记录'}
    abnormal_map = {'normal': '正常', 'abnormal': '异常（非上架新品）'}
    completed_map = {'pending': '未完成', 'done': '已完成', 'not_needed': '无需上架'}

    # 读取 stock_note (含可编辑字段覆盖)
    spu_notes = load_stock_note()
    is_admin_session = is_admin()

    for p in data.get('products', []):
        spu = p['spu']
        sn_rec = spu_notes.get(spu)
        # stock_note 优先; 若该 SPU 从未出现在 stock_note, 则回退到 report_data 中携带的 note_fields (历史继承值)
        # 合并历史继承值(note_fields)与实时编辑值(spuNotes): spuNotes 优先, 逐字段覆盖
        # 修复: 之前二选一, 只要该 SPU 在 spuNotes 出现过就丢弃 note_fields 里已继承的其他字段
        _nf = p.get('note_fields')
        note = {**(_nf if isinstance(_nf, dict) else {}), **(sn_rec if isinstance(sn_rec, dict) else {})}

        # 异常处理过滤: 非管理员看不到 abnormal 记录
        abnormal_status = note.get('abnormal_status', 'normal')
        if not is_admin_session and abnormal_status == 'abnormal':
            continue

        # 可编辑字段覆盖默认值
        category_val = note.get('operator_group', p.get('category', ''))
        operator_val = note.get('operator', p.get('operator', ''))
        if not operator_val:
            operator_val = '未分配运营'
        launch_store_val = note.get('launch_store', '') or '-'
        launch_completed_val = completed_map.get(note.get('launch_completed', 'pending'), '未完成')

        # 预计到仓时间: 已到仓显示"已到仓"
        inv_status_val = p.get('spu_inventory_status', '')
        eta_val = '已到仓' if inv_status_val == 'in_stock' else (p.get('earliest_eta', '') or '')

        writer.writerow([
            abnormal_map.get(abnormal_status, '正常'),
            spu,
            '待上架' if p['source'] == 'blue_pending' else '新增',
            category_val,
            operator_val,
            p.get('multi_package_display', ''),
            p.get('spu_package_type', ''),
            inv_map.get(inv_status_val, ''),
            eta_val,
            note.get('launch_ddl') or p.get('launch_ddl', ''),
            need_launch_text(p),
            launch_store_val,
            launch_completed_val,
            p.get('launch_reason', ''),
            p.get('sku_count_total', len(p.get('skus', []))),
            ', '.join(a.get('sku', '') for a in p.get('sku_anomalies', [])) or '',
            ', '.join(a.get('sku', '') for a in p.get('combo_skus', [])) or '',
            ', '.join(a.get('sku', '') for a in p.get('invalid_skus', [])) or ''
        ])

    from urllib.parse import quote
    csv_filename = quote('北美市场事业部新品跟进.csv')
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f"attachment; filename*=UTF-8''{csv_filename}"}
    )


@bp.route('/download/excel')
@auth.role_required('sub')
def download_excel():
    """导出Excel数据: SPU主行汇总 + SKU明细行平铺"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    import io

    data = load_report_data_with_confirmations()
    if data is None:
        return "数据不存在", 404

    inv_map = {'in_stock': '已到仓', 'in_transit': '在途', 'no_record': '无记录'}
    abnormal_map = {'normal': '正常', 'abnormal': '异常（非上架新品）'}
    completed_map = {'pending': '未完成', 'done': '已完成', 'not_needed': '无需上架'}

    # 读取 stock_note (含可编辑字段覆盖)
    spu_notes = load_stock_note()
    is_admin_session = is_admin()

    wb = Workbook()
    ws = wb.active
    ws.title = "新品跟进明细"

    # 表头: SPU信息 + SKU明细
    headers = [
        '异常处理', 'SPU', '来源', '运营组', '运营', 'SPU FedEx类型', 'SPU包裹情况',
        'SPU库存状态', '预计到仓时间', '上架DDL', 'SPU是否需要上架', '上架店铺', '是否完成上架',
        '上架说明',
        '别名', '通知上架时间', '产品线', '候选来源',
        'SKU', 'SKU FedEx类型', '重量(lb)', '最长边(in)', '第二长边(in)',
        '长+周长(in)', '体积(cu.in)', 'SKU库存状态', '在库量', '在途信息', 'SKU异常记录',
        '组合SKU', '无效SKU'
    ]

    ws.append(headers)

    # 表头样式
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1a237e")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = thin_border

    # 数据行: 每个SKU一行, SPU信息重复填充
    for p in data.get('products', []):
        spu = p.get('spu', '')
        sn_rec = spu_notes.get(spu)
        # stock_note 优先; 若该 SPU 从未出现在 stock_note, 回退到 report_data.note_fields (历史继承值)
        # 合并历史继承值(note_fields)与实时编辑值(spuNotes): spuNotes 优先, 逐字段覆盖
        # 修复: 之前二选一, 只要该 SPU 在 spuNotes 出现过就丢弃 note_fields 里已继承的其他字段
        _nf = p.get('note_fields')
        note = {**(_nf if isinstance(_nf, dict) else {}), **(sn_rec if isinstance(sn_rec, dict) else {})}

        # 异常处理过滤: 非管理员看不到 abnormal 记录
        abnormal_status = note.get('abnormal_status', 'normal')
        if not is_admin_session and abnormal_status == 'abnormal':
            continue

        # 可编辑字段覆盖默认值
        abnormal_text = abnormal_map.get(abnormal_status, '正常')
        category = note.get('operator_group', p.get('category', ''))
        operator = note.get('operator', p.get('operator', ''))
        launch_store = note.get('launch_store', '') or '-'
        launch_completed = completed_map.get(note.get('launch_completed', 'pending'), '未完成')

        source = '待上架' if p.get('source') == 'blue_pending' else '新增'
        spu_pkg_type = p.get('spu_package_type', '')
        multi_pkg = p.get('multi_package_display', '')
        spu_inv = inv_map.get(p.get('spu_inventory_status', ''), '')
        # 预计到仓时间: 已到仓显示"已到仓"
        earliest_eta = '已到仓' if p.get('spu_inventory_status') == 'in_stock' else (p.get('earliest_eta') or '')
        launch_ddl = note.get('launch_ddl') or p.get('launch_ddl') or ''
        launch_status = need_launch_text(p)
        launch_reason = p.get('launch_reason', '')
        aliases = ', '.join(p.get('aliases', []))
        notify_time = p.get('notify_time', '')
        lines = ', '.join(p.get('lines', []))
        candidate_sources = ', '.join(p.get('candidate_sources', []))

        skus = p.get('skus', [])
        anomaly_text = ', '.join(a.get('sku', '') for a in p.get('sku_anomalies', [])) or ''
        combo_text = ', '.join(a.get('sku', '') for a in p.get('combo_skus', [])) or ''
        invalid_text = ', '.join(a.get('sku', '') for a in p.get('invalid_skus', [])) or ''
        if not skus:
            # 没有SKU时仍输出SPU一行
            ws.append([
                abnormal_text, spu, source, category, operator, spu_pkg_type, multi_pkg,
                spu_inv, earliest_eta, launch_ddl, launch_status,
                launch_store, launch_completed,
                launch_reason,
                aliases, notify_time, lines, candidate_sources,
                '', '', '', '', '', '', '', '', '', '', '', anomaly_text,
                combo_text, invalid_text
            ])
            continue

        first_row = True
        for s in skus:
            pd = s.get('package_details', {}) or {}
            transit_info = []
            for t in s.get('in_transit', []):
                wh = t.get('warehouse', '') or ''
                qty = t.get('quantity', '') or ''
                eta = t.get('arrival_date', '') or t.get('eta', '') or ''
                transit_info.append(f"{wh}: {qty} {eta}".strip())
            transit_str = '; '.join(transit_info)

            ws.append([
                abnormal_text, spu, source, category, operator, spu_pkg_type, multi_pkg,
                spu_inv, earliest_eta, launch_ddl, launch_status,
                launch_store, launch_completed,
                launch_reason,
                aliases, notify_time, lines, candidate_sources,
                s.get('sku', ''),
                s.get('package_type', ''),
                pd.get('weight_lb', ''),
                pd.get('longest_in', ''),
                pd.get('second_longest_in', ''),
                pd.get('length_plus_girth_in', ''),
                pd.get('volume_cu_in', ''),
                inv_map.get(s.get('inventory_status', ''), ''),
                s.get('in_stock', 0),
                transit_str,
                anomaly_text if first_row else '',
                combo_text if first_row else '',
                invalid_text if first_row else ''
            ])
            first_row = False

    # 自适应列宽
    for col in ws.columns:
        max_length = 0
        col_letter = col[0].column_letter
        for cell in col:
            try:
                val = cell.value
                if val is not None:
                    max_length = max(max_length, len(str(val)))
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = min(max_length + 2, 50)

    # 冻结首行
    ws.freeze_panes = 'A2'

    # 保存到内存
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name="北美市场事业部新品跟进.xlsx"
    )


@bp.route('/download/nc-excel')
@auth.role_required('sub')
def download_nc_excel():
    """新色跟进 Excel 导出 (清晰版)

    解决「下载的表格看不懂哪些是需要上架的新色」:
      Sheet1「需上架新色」= new_colors 列表本身 → 直接给出待上架新色清单
      Sheet2「SPU×店铺汇总」= 有新色的 SPU×店铺 (含 新色列表)
      Sheet3「全部SKU明细」= 上述记录的全部 SKU 明细, 含「是否需上架新色」标志
    颜色: 在库(已到仓)=浅橙(可立即上架); 在途=浅黄; 「需上架新色=是」行=浅黄
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    import io

    nc_path = OUTPUT_DIR / "new_color_data.json"
    if not nc_path.exists():
        if cloud_store.is_cloud():
            cloud_store.download_to_local(
                cloud_store.BUCKET_OUTPUTS, "new_color_data.json", nc_path
            )
    if not nc_path.exists():
        return "新色数据不存在, 请先处理数据", 404

    with open(nc_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    nc_notes = load_nc_note()
    spu_meta = {}
    spu_only = {}          # 兜底: 老版数据结构里 spu_rows 的「店铺」为空 (一行=一SPU多店铺)
    for r in data.get('spu_rows', []):
        key = (str(r.get('SPU', '')), str(r.get('店铺', '')))
        spu_meta[key] = r
        spu_only.setdefault(str(r.get('SPU', '')), r)

    def spu_meta_for(spu, store):
        """优先按 (SPU, 店铺) 精确取; 取不到时按 SPU 兜底 (兼容老结构)"""
        return spu_meta.get((str(spu), str(store))) or spu_only.get(str(spu), {})

    wb = Workbook()

    # ---- 通用样式 ----
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="C00000")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    _thin = Side(style='thin')
    thin_border = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
    fill_instock = PatternFill("solid", fgColor="FCE4D6")   # 已到仓(在库) → 可立即上架
    fill_transit = PatternFill("solid", fgColor="FFF2CC")   # 在途
    fill_newflag = PatternFill("solid", fgColor="FFF2CC")   # 需上架新色标记

    def style_header(ws, ncols):
        for c in range(1, ncols + 1):
            cell = ws.cell(row=1, column=c)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
            cell.border = thin_border

    def autosize(ws, ncols, maxw=52):
        for i in range(1, ncols + 1):
            letter = ws.cell(row=1, column=i).column_letter
            best = 0
            for row in ws.iter_rows(min_row=1, min_col=i, max_col=i):
                v = row[0].value
                if v is not None:
                    best = max(best, len(str(v)))
            ws.column_dimensions[letter].width = min(best + 2, maxw)

    def fill_row(ws, row_idx, ncols, fill):
        for c in range(1, ncols + 1):
            ws.cell(row=row_idx, column=c).fill = fill

    def note_val(spu, key, default=''):
        n = nc_notes.get(spu) or {}
        return n.get(key) or default

    # ================= Sheet1: 需上架新色 =================
    ws1 = wb.active
    ws1.title = "需上架新色"
    h1 = ['SPU', '店铺', '运营组', '运营', '已上架店铺', '新色', '库存状态',
          '数量', '在库库存', '在途库存', '在途最早预计到仓', '库存SKU',
          '是否完成上架', '特殊备注']
    ws1.append(h1)
    nc_rows = sorted(data.get('new_colors', []),
                     key=lambda r: (str(r.get('SPU', '')), str(r.get('店铺', ''))))
    for r in nc_rows:
        spu = r.get('SPU', '')
        m = spu_meta_for(spu, r.get('店铺', ''))
        st = str(r.get('库存状态') or '')
        ws1.append([
            spu,
            r.get('店铺', ''),
            note_val(spu, 'operator_group') or m.get('运营组', ''),
            note_val(spu, 'operator') or m.get('运营', ''),
            m.get('已上架店铺', '') or r.get('店铺', ''),
            r.get('新色', ''),
            st,
            r.get('数量', ''),
            r.get('在库库存', ''),
            r.get('在途库存', ''),
            r.get('在途最早预计到仓', '') or r.get('预计到仓', ''),
            r.get('库存SKU', ''),
            note_val(spu, 'launch_completed', '未完成'),
            note_val(spu, 'content'),
        ])
        _f = fill_transit if '在途' in st else (fill_instock if '在库' in st else None)
        if _f:
            fill_row(ws1, ws1.max_row, len(h1), _f)
    style_header(ws1, len(h1))
    autosize(ws1, len(h1))
    ws1.freeze_panes = 'A2'
    ws1.auto_filter.ref = ws1.dimensions

    # ================= Sheet2: SPU×店铺 汇总 =================
    ws2 = wb.create_sheet("SPU×店铺汇总")
    # 2026-10-27: 与新色页同步新增三列 (SPU包裹情况/预计到仓时间/新色是否需要上架), 口径见 _build_na_spu_map
    na_map = _build_na_spu_map()
    h2 = ['SPU', '店铺', '运营组', '运营', '已上架店铺', '新色数', '新色列表',
          '待上架SKU数', 'SPU包裹情况', '预计到仓时间', '新色是否需要上架',
          '是否完成上架', '特殊备注']
    ws2.append(h2)
    for r in data.get('spu_rows', []):
        spu = r.get('SPU', '')
        _rec = na_map.get(spu)
        _se = (data.get('spu_eval') or {}).get(spu)
        if _rec and (_rec.get('pkg') or _rec.get('launch')):
            _pkg = _rec.get('pkg') or ''
            _eta = _rec.get('eta') or ''
        else:
            # 新品报表没有 → 用新色管线自评估 (2026-10-27 需求①: 不再显示 '-')
            _pkg = (_se or {}).get('pkg') or ''
            _eta = _nc_eta_from_sku_rows(data.get('sku_rows'), spu)
        ws2.append([
            spu,
            r.get('店铺', ''),
            note_val(spu, 'operator_group') or r.get('运营组', ''),
            note_val(spu, 'operator') or r.get('运营', ''),
            r.get('已上架店铺', ''),
            r.get('新色数', ''),
            r.get('新色列表', ''),
            r.get('待上架SKU数', ''),
            _pkg,
            _eta,
            nc_launch_text(spu, na_map, nc_notes.get(spu), data.get('spu_eval')),
            note_val(spu, 'launch_completed', '未完成'),
            note_val(spu, 'content'),
        ])
    style_header(ws2, len(h2))
    autosize(ws2, len(h2))
    ws2.freeze_panes = 'A2'
    ws2.auto_filter.ref = ws2.dimensions

    # ================= Sheet3: 全部SKU明细(含是否需上架新色) =================
    ws3 = wb.create_sheet("全部SKU明细")
    # 只保留「是否需上架新色」一个标志列 (原 上架状态 / 是否完成上架 两列移除):
    #   是 = 上架状态为未上架 且 该SPU的是否完成上架备注不是「无需上架」; 其余一律 否
    h3 = ['SPU', '店铺', '上架SKU', '库存SKU', '颜色', '颜色判定',
          '是否需上架新色', '转单覆盖', '库存状态', '在库库存', '在途库存',
          '在途最早预计到仓', '特殊备注']
    ws3.append(h3)
    # 明细范围与 Sheet1 同源: 所有「有新色的 SPU×店铺」
    # new_colors 的 (SPU,店铺) 是权威键; 老结构里 spu_rows 店铺为空, 需跳过这类空店铺键
    need_keys = {(str(r.get('SPU', '')), str(r.get('店铺', '')))
                 for r in data.get('new_colors', [])}
    for _spu, _store in spu_meta:
        if _store:
            need_keys.add((_spu, _store))
    for r in data.get('sku_rows', []):
        spu = r.get('SPU', '')
        key = (str(spu), str(r.get('店铺', '')))
        if key not in need_keys:
            continue
        status = r.get('上架状态', '')
        lc_note = note_val(spu, 'launch_completed', '未完成')
        is_new = '是' if (status == '未上架' and lc_note != '无需上架') else '否'
        ws3.append([
            spu,
            r.get('店铺', ''),
            r.get('上架SKU', ''),
            r.get('库存SKU', ''),
            r.get('颜色', ''),
            r.get('颜色判定', ''),
            is_new,
            r.get('转单覆盖', ''),
            r.get('库存状态', ''),
            r.get('在库库存', ''),
            r.get('在途库存', ''),
            r.get('在途最早预计到仓', '') or r.get('预计到仓', ''),
            note_val(spu, 'content'),
        ])
        if is_new == '是':
            fill_row(ws3, ws3.max_row, len(h3), fill_newflag)
    style_header(ws3, len(h3))
    autosize(ws3, len(h3))
    ws3.freeze_panes = 'A2'
    ws3.auto_filter.ref = ws3.dimensions

    generated = str(data.get('generated_at', ''))[:10].replace('-', '')
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=f"新色跟进_需上架新色清单_{generated or 'report'}.xlsx"
    )


# ============================================================
# 主页面模板 (单页应用)
# ============================================================