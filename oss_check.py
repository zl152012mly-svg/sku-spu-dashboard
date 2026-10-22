# -*- coding: utf-8 -*-
"""
OSS 连通性自测（在 ECS 或本地运行，需先设好 OSS 环境变量）。
用法：
  export OSS_ENDPOINT=oss-cn-hangzhou.aliyuncs.com
  export OSS_ACCESS_KEY_ID=<你的子账号 AccessKeyId>
  export OSS_ACCESS_KEY_SECRET=<你的子账号 AccessKeySecret>
  export OSS_BUCKET=sku-spu-data
  python oss_check.py
特点：不打印任何密钥；只输出后端模式 + put/get/exists 的 PASS/FAIL，并清理测试对象。
用途：迁阿里云前先验证「子账号 + bucket 权限」是否配通，避免部署后才发现 403。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import storage


def main():
    print('STORAGE_MODE =', storage.MODE or '(空 → 按凭证自动探测)')
    print('backend      =', storage._backend())
    print('has_cloud    =', storage.has_cloud())
    if storage._backend() != 'oss':
        print('❌ 未进入 oss 模式：请确认 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET / '
              'OSS_BUCKET 已设置（或显式 STORAGE_MODE=oss）。')
        return 1

    test_key = 'meta/_selftest_%s.txt' % os.getpid()
    data = ('oss-selftest-ok-' + str(os.getpid())).encode('utf-8')

    print('→ 测试 put_object ...')
    ok = storage.ds_file_upload(test_key, data)
    print(('✅ put' if ok else '❌ put') + ('' if ok else '  err=' + (storage.last_error() or '')))

    got = storage.ds_file_download(test_key)
    print('✅ get 命中且内容一致' if got == data else '❌ get 不匹配或未取到: %r' % (got,))

    ex = storage.ds_file_exists(test_key)
    print('✅ exists 命中' if ex else '❌ exists 误判（应为 True）')

    # 清理测试对象，避免污染 bucket
    try:
        storage._oss_client().delete_object(storage._oss_key(test_key))
        print('✅ 已清理测试对象')
    except Exception as e:  # noqa
        print('⚠️ 清理失败（可手动删 meta/_selftest_*）: %r' % (e,))

    passed = bool(ok) and (got == data) and bool(ex)
    print('\n' + ('OSS_SELFTEST_PASS' if passed else 'OSS_SELFTEST_FAIL'))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
