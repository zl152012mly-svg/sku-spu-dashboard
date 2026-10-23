# -*- coding: utf-8 -*-
"""
cloud_store.py —— 新品盘点侧「存储适配层」（Phase 2 Step 3）

背景
----
新品盘点的业务模块（process_script / new_color_module / web_app）是从
`D:\\WorkBuddy AI 文件\\新品盘点` **原样拷贝**过来的，内部依然写 `import cloud_store`。

本文件的作用：让这些老调用名继续可用，但真实读写统一转发到系统的
`common_store`（新品盘点命名空间 → **独立 OSS 桶** NA_OSS_*），
实现 Phase 2 决策③「各系统独立桶」+ 决策④「统一存储层」。

注意：`system/na` 会被 na_core 插入 sys.path，因此这里的 `cloud_store`
对业务模块而言就是它们熟悉的那个模块名。
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent                      # system/
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import common_store as _cs  # noqa: E402

BUCKET_UPLOADS = _cs.BUCKET_UPLOADS       # 原始数据源（上品通知表.xlsx 等）
BUCKET_OUTPUTS = _cs.BUCKET_OUTPUTS       # report_data.json / stock_note.json 等
NA_DATA_DIR = _cs.NA_DATA_DIR             # 本地镜像根目录 system/na_data


def invalidate_list_cache():
    _cs.invalidate_list_cache()


def is_cloud():
    return _cs.is_cloud()


def get_mode_str():
    return _cs.get_mode_str()


def find_file_info(pattern, bucket=BUCKET_UPLOADS):
    return _cs.find_file_info(pattern, bucket)


def ensure_data_file_local(pattern, bucket=BUCKET_UPLOADS):
    return _cs.ensure_data_file_local(pattern, bucket)


def ensure_all_data_files_local(data_source_patterns, bucket=BUCKET_UPLOADS):
    return _cs.ensure_all_data_files_local(data_source_patterns, bucket)


def ensure_report_data_local():
    return _cs.ensure_report_data_local()


def upload_local_file(local_path, bucket, key, content_type="application/octet-stream"):
    return _cs.upload_local_file(local_path, bucket, key, content_type)


def upload_bytes(bucket, key, data, content_type="application/octet-stream"):
    return _cs.upload_bytes(bucket, key, data, content_type)


def download_bytes(bucket, key):
    return _cs.download_bytes(bucket, key)


def download_to_local(bucket, key, local_path):
    return _cs.download_to_local(bucket, key, local_path)


def delete_matching(pattern, bucket=BUCKET_UPLOADS, keep_name=None):
    return _cs.delete_matching(pattern, bucket, keep_name)


def read_json(bucket, key, local_path=None):
    return _cs.read_json(bucket, key, local_path)


def write_json(bucket, key, data, local_path=None):
    return _cs.write_json(bucket, key, data, local_path)


def file_exists(bucket, key, local_path=None):
    return _cs.file_exists(bucket, key, local_path)
