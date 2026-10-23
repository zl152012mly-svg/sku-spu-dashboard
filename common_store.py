# -*- coding: utf-8 -*-
"""
common_store.py —— 合并后统一存储层（SKU&SPU 清洗看板 × 新品盘点系统）

两个命名空间共用一个模块：
  ▸ SKU&SPU 命名空间：annot_* / ds_* / ds_file_* / has_cloud / last_error / OBJ_*
     —— 1:1 委托给已验证的 storage.py（OSS>Supabase>本地 三态，行为完全不变，
     确保线上 68378 行 + 27 条标注零风险迁移；键布局保持原样，不引入前缀）。
  ▸ 新品盘点命名空间：find_file_info / ensure_data_file_local / upload_local_file /
     upload_bytes / download_bytes / read_json / write_json / file_exists /
     delete_matching / invalidate_list_cache
     —— 指向**独立** OSS/Supabase 桶组（决策③：各系统独立桶），
     NA_OSS_* 环境变量（兼容旧 ALIYUN_OSS_*）。OSS 原生 UTF-8 键，无需 safe_key/manifest。

环境变量（SKU&SPU 侧，同 storage.py）：
  STORAGE_MODE / OSS_ENDPOINT / OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET /
  OSS_BUCKET / OSS_PREFIX(可选) / SUPABASE_URL / SUPABASE_KEY
环境变量（新品盘点侧，独立桶）：
  NA_OSS_ENDPOINT / NA_OSS_ACCESS_KEY_ID / NA_OSS_ACCESS_KEY_SECRET / NA_OSS_BUCKET
  （缺省回落到 ALIYUN_OSS_*，再回落到 SKU&SPU 的 OSS_* —— 单桶开发时可用）
"""

import os
import io
import json
import time
import fnmatch
import datetime
from pathlib import Path

# ============================ SKU&SPU 命名空间（委托 storage.py） ============================
# 显式再导出，行为 100% 与 storage.py 一致 —— 线上零风险。
from storage import (  # noqa: F401
    annot_get, annot_update,
    ds_update, ds_get,
    ds_file_upload, ds_file_download, ds_file_exists,
    has_cloud, last_error,
    OBJ_RAW, OBJ_REF, OBJ_LX, OBJ_WM, OBJ_ADMIN, OBJ_TEMU, OBJ_TEMU_A, OBJ_TEMU_B, OBJ_OPS,
    MODE,
)
from storage import _backend, _use_supabase  # noqa: F401  供 routes 层 introspect

__all__ = [
    'annot_get', 'annot_update', 'ds_update', 'ds_get',
    'ds_file_upload', 'ds_file_download', 'ds_file_exists',
    'has_cloud', 'last_error',
    'OBJ_RAW', 'OBJ_REF', 'OBJ_LX', 'OBJ_WM', 'OBJ_ADMIN', 'OBJ_TEMU', 'OBJ_TEMU_A', 'OBJ_TEMU_B', 'OBJ_OPS',
    'MODE',
# 新品盘点命名空间
'find_file_info', 'ensure_data_file_local', 'upload_local_file', 'upload_bytes',
'download_bytes', 'read_json', 'write_json', 'file_exists', 'delete_matching',
'invalidate_list_cache', 'BUCKET_UPLOADS', 'BUCKET_OUTPUTS',
# 新品盘点兼容 shim（对齐原 cloud_store.py 的对外 API）
'NA_DATA_DIR', 'is_cloud', 'download_to_local', 'ensure_all_data_files_local',
'ensure_report_data_local', 'get_mode_str',
]


# ============================ 新品盘点命名空间（独立桶组） ============================
BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 新品盘点的**本地磁盘根目录**独立成 na_data/，避免数据文件散落在代码根、
# 也避免 delete_matching('*Listing*.xlsx') 之类的通配误伤 SKU&SPU 侧文件。
NA_DATA_DIR = BASE_DIR / "na_data"
NA_DATA_DIR.mkdir(parents=True, exist_ok=True)

BUCKET_UPLOADS = "uploads"    # 原始数据源（上品通知表.xlsx 等）
BUCKET_OUTPUTS = "outputs"    # report_data.json / stock_note.json / color_dict.json 等

# --- 独立桶组解析（决策③：各系统独立 OSS 桶）---
_NA_ENDPOINT = os.environ.get('NA_OSS_ENDPOINT', os.environ.get('ALIYUN_OSS_ENDPOINT', '')).strip()
_NA_BUCKET   = os.environ.get('NA_OSS_BUCKET', os.environ.get('ALIYUN_OSS_BUCKET', '')).strip()
_NA_AK       = os.environ.get('NA_OSS_ACCESS_KEY_ID', os.environ.get('ALIYUN_ACCESS_KEY_ID', '')).strip()
_NA_SK       = os.environ.get('NA_OSS_ACCESS_KEY_SECRET', os.environ.get('ALIYUN_ACCESS_KEY_SECRET', '')).strip()
_NA_SUPABASE_URL = os.environ.get('NA_SUPABASE_URL', os.environ.get('SUPABASE_URL', '')).strip()
_NA_SUPABASE_KEY = os.environ.get('NA_SUPABASE_SERVICE_KEY', os.environ.get('SUPABASE_SERVICE_KEY', '')).strip()

NA_USE_OSS = bool(_NA_ENDPOINT and _NA_BUCKET and _NA_AK and _NA_SK)
NA_USE_SUPABASE = bool(_NA_SUPABASE_URL) and bool(_NA_SUPABASE_KEY)

_NA_OSS = None


def _na_oss():
    """延迟创建 NA 桶 OSS 客户端（仅 NA_USE_OSS 且真正读写时 import oss2）。"""
    global _NA_OSS
    if _NA_OSS is None and NA_USE_OSS:
        import oss2
        auth = oss2.Auth(_NA_AK, _NA_SK)
        _NA_OSS = oss2.Bucket(auth, _NA_ENDPOINT, _NA_BUCKET)
    return _NA_OSS


def _na_oss_key(bucket, key):
    """逻辑桶名 -> 单桶下的前缀路径。key 可含中文（OSS 原生支持）。"""
    return f"{bucket}/{key}"


# 短 TTL 缓存：数据管理页一次查多个数据源，避免抖动误判「未找到」(同 cloud_store)。
_LIST_CACHE = {}
_LIST_CACHE_TTL = 10.0


def invalidate_list_cache():
    """上传/删除后主动失效，保证随后读到的状态是最新的。"""
    _LIST_CACHE.clear()


def _na_oss_list(bucket):
    ck = ('na', bucket)
    now = time.time()
    hit = _LIST_CACHE.get(ck)
    if hit and (now - hit[0]) < _LIST_CACHE_TTL:
        return hit[1]
    b = _na_oss()
    out = []
    for obj in b.list_objects(prefix=f"{bucket}/", max_keys=1000).object_list:
        logical = obj.key.split("/", 1)[1] if "/" in obj.key else obj.key
        mod = datetime.datetime.fromtimestamp(obj.last_modified).strftime("%Y-%m-%dT%H:%M:%S")
        out.append({"name": logical, "original_name": logical,
                    "size": obj.size, "updated_at": mod})
    _LIST_CACHE[ck] = (now, out)
    return out


def _na_oss_upload(bucket, key, data, content_type="application/octet-stream"):
    _na_oss().put_object(_na_oss_key(bucket, key), data,
                         headers={"Content-Type": content_type})


def _na_oss_download(bucket, key):
    import oss2
    try:
        resp = _na_oss().get_object(_na_oss_key(bucket, key))
        return resp.read()
    except oss2.exceptions.NoSuchKey:
        raise FileNotFoundError(f"NA OSS 404: {bucket}/{key}")


def _na_oss_exists(bucket, key):
    try:
        return _na_oss().object_exists(_na_oss_key(bucket, key))
    except Exception:
        return False


def _na_oss_delete(bucket, key):
    try:
        _na_oss().delete_object(_na_oss_key(bucket, key))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 新品盘点公共 API
# ---------------------------------------------------------------------------

def find_file_info(pattern, bucket=BUCKET_UPLOADS):
    """查找匹配 glob 的文件。返回 {found,name,size_mb,modified,error} 或 {found:False}。"""
    if NA_USE_OSS:
        try:
            items = _na_oss_list(bucket)
            matched = [f for f in items
                       if fnmatch.fnmatch(f.get("original_name", ""), pattern)]
            if matched:
                f = matched[-1]
                return {
                    "found": True,
                    "name": f.get("original_name", f.get("name", "")),
                    "size_mb": round(f.get("size", 0) / 1048576, 2),
                    "modified": f.get("updated_at", "")[:16].replace("T", " "),
                    "error": "",
                }
            return {"found": False, "name": "", "size_mb": 0, "modified": "", "error": ""}
        except Exception as e:
            return {"found": False, "name": "", "size_mb": 0, "modified": "",
                    "error": f"{type(e).__name__}: {str(e)[:160]}"}
    # 本地模式
    matches = sorted(NA_DATA_DIR.glob(pattern))
    if matches:
        f = matches[-1]
        st = f.stat()
        return {"found": True, "name": f.name,
                "size_mb": round(st.st_size / 1048576, 2),
                "modified": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "error": ""}
    return {"found": False, "name": "", "size_mb": 0, "modified": "", "error": ""}


def ensure_data_file_local(pattern, bucket=BUCKET_UPLOADS):
    """确保匹配文件在本地磁盘可用；云端模式则先下载。返回本地 Path 或 None。"""
    matches = sorted(NA_DATA_DIR.glob(pattern))
    if matches:
        return matches[-1]
    if NA_USE_OSS:
        try:
            items = _na_oss_list(bucket)
            for item in items:
                original_name = item.get("original_name", item.get("name", ""))
                if fnmatch.fnmatch(original_name, pattern):
                    local_path = NA_DATA_DIR / original_name
                    data = _na_oss_download(bucket, original_name)
                    with open(local_path, "wb") as out:
                        out.write(data)
                    return local_path
        except Exception as e:
            print(f"[common_store] ensure_data_file_local(OSS) failed for {pattern}: {e}")
    return None


def upload_local_file(local_path, bucket, key, content_type="application/octet-stream"):
    """上传本地文件到存储。key 为逻辑名（可含中文）。本地模式 no-op。"""
    if NA_USE_OSS:
        with open(Path(local_path), "rb") as f:
            _na_oss_upload(bucket, key, f.read(), content_type)
        invalidate_list_cache()
        return
    # 本地模式：文件已在磁盘


def upload_bytes(bucket, key, data, content_type="application/octet-stream"):
    """上传原始字节。本地模式：调用方自行写盘。"""
    if NA_USE_OSS:
        _na_oss_upload(bucket, key, data, content_type)
        return
    # 本地模式：no-op


def download_bytes(bucket, key):
    """下载原始字节。本地模式：从 BASE_DIR/key 读。找不到抛 FileNotFoundError。"""
    if NA_USE_OSS:
        return _na_oss_download(bucket, key)
    local_path = NA_DATA_DIR / key if not key.startswith("/") else Path(key)
    with open(local_path, "rb") as f:
        return f.read()


def read_json(bucket, key, local_path=None):
    """读 JSON；找不到/非法返回 None。"""
    if NA_USE_OSS:
        try:
            return json.loads(_na_oss_download(bucket, key))
        except FileNotFoundError:
            return None
        except Exception as e:
            print(f"[common_store] read_json(OSS) error for {bucket}/{key}: {e}")
            return None
    path = Path(local_path) if local_path else NA_DATA_DIR / key
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_json(bucket, key, data, local_path=None):
    """写 JSON 到存储 + 本地磁盘。云端失败返回 False（数据仍落本地）。"""
    content = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    path = Path(local_path) if local_path else NA_DATA_DIR / key
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if NA_USE_OSS:
        try:
            _na_oss_upload(bucket, key, content.encode("utf-8"),
                           content_type="application/json")
            return True
        except Exception as e:
            print(f"[common_store] write_json(OSS) upload failed for {bucket}/{key}: {e}")
            return False
    return True


def file_exists(bucket, key, local_path=None):
    """检查文件是否存在。"""
    if NA_USE_OSS:
        return _na_oss_exists(bucket, key)
    path = Path(local_path) if local_path else NA_DATA_DIR / key
    return path.exists()


def delete_matching(pattern, bucket=BUCKET_UPLOADS, keep_name=None):
    """删除匹配 glob 的所有文件（云端 + 本地）。keep_name 保留不被删。"""
    for f in NA_DATA_DIR.glob(pattern):
        if keep_name and f.name == keep_name:
            continue
        try:
            f.unlink()
        except Exception:
            pass
    if NA_USE_OSS:
        try:
            items = _na_oss_list(bucket)
            for item in items:
                original_name = item.get("original_name", item.get("name", ""))
                if keep_name and original_name == keep_name:
                    continue
                if fnmatch.fnmatch(original_name, pattern):
                    _na_oss_delete(bucket, original_name)
        except Exception:
            pass
        finally:
            invalidate_list_cache()


# ---------------------------------------------------------------------------
# 兼容层：对齐新品盘点原 cloud_store.py 的对外 API
#   （web_app.py / process_script.py / new_color_module.py 原样拷贝后仍用旧名调用）
# ---------------------------------------------------------------------------

def is_cloud():
    """是否处于云端存储模式（OSS 或 Supabase）。

    ⚠ 与原 cloud_store 的差别：原实现「本地模式」也返回 False，语义一致；
    本地模式下 is_cloud()=False 时，web_app 会跳过 _所有_ 云同步分支，行为不变。
    """
    return bool(NA_USE_OSS or NA_USE_SUPABASE)


def download_to_local(bucket, key, local_path):
    """把云端对象下载到 local_path。本地模式：文件已在该路径，存在即返回。"""
    p = Path(local_path)
    if is_cloud():
        try:
            data = download_bytes(bucket, key)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
        except Exception as e:
            print(f"[common_store] download_to_local failed {bucket}/{key}: {e}")
    return p


def ensure_all_data_files_local(patterns, bucket=BUCKET_UPLOADS):
    """批量确保本地有对应数据源文件。返回 {pattern: Path|None}。"""
    out = {}
    for pat in patterns:
        try:
            out[pat] = ensure_data_file_local(pat, bucket)
        except Exception as e:
            print(f"[common_store] ensure_all_data_files_local({pat}) err: {e}")
            out[pat] = None
    return out


def ensure_report_data_local(local_path=None, bucket=BUCKET_OUTPUTS):
    """确保 report_data.json 在本地可用。返回本地 Path 或 None。"""
    p = Path(local_path) if local_path else (NA_DATA_DIR / "output" / "report_data.json")
    if p.exists():
        return p
    if is_cloud():
        try:
            if file_exists(bucket, "report_data.json"):
                download_to_local(bucket, "report_data.json", p)
                return p
        except Exception as e:
            print(f"[common_store] ensure_report_data_local err: {e}")
    return None


def get_mode_str():
    """人类可读的存储模式串（启动日志/排障用）。"""
    if NA_USE_OSS:
        return f"阿里云OSS(独立桶 {_NA_BUCKET})"
    if NA_USE_SUPABASE:
        return f"Supabase({_NA_SUPABASE_URL.split('//')[-1][:24]})"
    return f"本地磁盘({NA_DATA_DIR})"
