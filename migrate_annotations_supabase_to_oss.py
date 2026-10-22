# -*- coding: utf-8 -*-
"""
migrate_annotations_supabase_to_oss.py
========================================
历史异常确认（sku_annotations）从 Supabase 迁移到阿里云 OSS。

背景：2026-10-22 起 SKU&SPU 看板生产环境全面切到 阿里云 ECS + OSS，
Render + Supabase 退役。本脚本把 Supabase 里的人工异常确认标注搬到 OSS
（meta/annotations.json），否则在阿里云看板上这些标注会丢失。

用法（在 ECS 上，已配好 .env 含 OSS_*）：
    cd /opt/sku-spu
    set -a; source .env; set +a
    export SUPABASE_URL=https://xxxx.supabase.co
    export SUPABASE_KEY=eyJ...（service_role 或可读 key）
    source venv/bin/activate
    pip install supabase -q        # 仅需一次性装，跑完可不动
    python migrate_annotations_supabase_to_oss.py

要点：
  - 读取 Supabase sku_annotations（id_key/status/note/version）；
  - 与 OSS 已有标注合并（Supabase 优先，避免覆盖阿里云端手动新增的）；
  - 一次性原子写回 OSS meta/annotations.json，保留原 version；
  - 不删 Supabase 任何数据（迁完由用户在控制台确认后再退役 Supabase）。
跑完记得 `systemctl restart sku-spu` 让线上服务加载新标注。
"""
import os
import json
import sys

from supabase import create_client
import storage


def main():
    url = os.environ.get('SUPABASE_URL', '').strip()
    key = os.environ.get('SUPABASE_KEY', '').strip()
    if not (url and key):
        raise SystemExit('❌ 请先 export SUPABASE_URL / SUPABASE_KEY')
    # OSS 凭证必须存在（来自 .env）
    if not (storage._OSS_ID and storage._OSS_SECRET and storage._OSS_BUCKET):
        raise SystemExit('❌ 未检测到 OSS 凭证（OSS_ACCESS_KEY_ID/SECRET/BUCKET），请先 source .env')

    print('连接 Supabase:', url)
    client = create_client(url, key)
    table = client.table(storage._TABLE_ANNOT)

    # 分页读取全部标注（每页 1000 行，防止大表截断）
    mig = {}
    offset = 0
    page = 1000
    while True:
        res = table.select('id_key,status,note,version').range(offset, offset + page - 1).execute()
        rows = getattr(res, 'data', None) or []
        if not rows:
            break
        for r in rows:
            ik = (r.get('id_key') or '').strip()
            if not ik:
                continue
            mig[ik] = {
                'status': (r.get('status') or '') if r.get('status') is not None else '',
                'note': (r.get('note') or '') if r.get('note') is not None else '',
                'version': int(r.get('version') or 1),
            }
        if len(rows) < page:
            break
        offset += page

    print('从 Supabase 读取标注: %d 条' % len(mig))

    # 合并到 OSS 已有标注（Supabase 优先，避免覆盖用户在阿里云端手动新增的）
    existing = storage._oss_annot_get() or {}
    print('OSS 已有标注: %d 条' % len(existing))
    existing.update(mig)
    print('合并后 OSS 总数: %d 条' % len(existing))

    ok = storage._oss_put_bytes(
        storage._OSS_ANNOT_KEY,
        json.dumps(existing, ensure_ascii=False).encode('utf-8'))
    if not ok:
        raise SystemExit('❌ 写入 OSS 失败: ' + (storage.last_error() or '未知'))

    # 回读校验
    back = storage._oss_annot_get() or {}
    print('✅ 写入 OSS meta/annotations.json 成功，回读校验条数: %d' % len(back))

    # 给出几个样例，便于肉眼核对
    sample = list(back.items())[:3]
    if sample:
        print('样例:', json.dumps(sample, ensure_ascii=False))


if __name__ == '__main__':
    main()
