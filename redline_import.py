# -*- coding: utf-8 -*-
"""redline_import.py —— 把旧资料库导出的 redline_cloud_export.json 导入 OSS。

用法（在 ECS 上，先加载 .env 让 OSS 凭证生效）：
    set -a; source .env; set +a
    venv/bin/python redline_import.py redline_cloud_export.json

导出方式见 redline_app/README_MIGRATION.md（在旧 workbuddy.link 页面控制台执行一段脚本）。
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import redline_store as store  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print('用法: python redline_import.py <redline_cloud_export.json>')
        return 2
    path = sys.argv[1]
    if not os.path.exists(path):
        print('文件不存在: %s' % path)
        return 2
    with open(path, encoding='utf-8') as f:
        data = json.load(f)

    print('存储后端: %s' % store.backend_name())
    sources = data.get('sources') or []
    periods = data.get('periods') or []
    print('待导入: sources=%d, periods=%d' % (len(sources), len(periods)))

    ns = 0
    for i, s in enumerate(sources, 1):
        okk = store.save_source(s.get('src') or '', s.get('store') or '', s.get('file') or '',
                                s.get('chunks') or [], s.get('updated_at') or '')
        ns += 1 if okk else 0
        print('  [%d/%d] %s / %s / %s -> %s (%d 块)' % (
            i, len(sources), s.get('src'), s.get('store') or '-', s.get('file') or '-',
            'OK' if okk else 'FAIL', len(s.get('chunks') or [])))

    np = store.sync_periods(periods) if periods else 0
    print('导入完成：sources=%d/%d, periods=%d' % (ns, len(sources), np))
    return 0 if ns == len(sources) else 1


if __name__ == '__main__':
    sys.exit(main())
