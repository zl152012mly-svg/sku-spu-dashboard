#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把旧新品盘点(new-arrival)的阿里云凭证搬进 SKU&SPU 的 .env，
统一写成 NA_OSS_* 四个变量 —— 避免「部分认到、部分认不到」的半通状态。

特性：
  * 自适应识别老配置里各种命名（ALIYUN_OSS_* / ALIYUN_ACCESS_KEY_* / OSS_*）
  * 幂等：反复跑不会产生重复行（会先清掉已有的 NA_OSS_*）
  * 绝不打印密钥原文，只显示遮掩后的前后各 3 位
  * 写前自动备份 /opt/sku-spu/.env -> .env.bak.<时间戳>

用法（ECS 上）：
  sudo /opt/sku-spu/venv/bin/python na_oss_migrate.py
"""
import os, re, shutil, time, sys

SRC = os.environ.get("NA_OLD_ENV", "/home/newarrival/new-arrival/.env")
DST = os.environ.get("NA_TARGET_ENV", "/opt/sku-spu/.env")

CANDIDATES = {
    "NA_OSS_ENDPOINT": ["ALIYUN_OSS_ENDPOINT", "OSS_ENDPOINT", "ENDPOINT"],
    "NA_OSS_BUCKET": ["ALIYUN_OSS_BUCKET", "OSS_BUCKET", "BUCKET"],
    "NA_OSS_ACCESS_KEY_ID": ["ALIYUN_ACCESS_KEY_ID", "ALIYUN_OSS_ACCESS_KEY_ID",
                             "OSS_ACCESS_KEY_ID", "ACCESS_KEY_ID"],
    "NA_OSS_ACCESS_KEY_SECRET": ["ALIYUN_ACCESS_KEY_SECRET", "ALIYUN_OSS_ACCESS_KEY_SECRET",
                                 "OSS_ACCESS_KEY_SECRET", "ACCESS_KEY_SECRET"],
}


def mask(v):
    if len(v) <= 8:
        return "*" * len(v)
    return "%s***%s (len=%d)" % (v[:3], v[-3:], len(v))


def parse_env(path):
    """返回 dict；容忍 'export ' 前缀、等号周围空格、# 注释、引号包裹。"""
    d = {}
    if not os.path.exists(path):
        return d
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("export "):
                s = s[7:].strip()
            if "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            if k:
                d[k] = v
    return d


def main():
    print("源文件  :", SRC)
    print("目标文件:", DST)
    if not os.path.exists(SRC):
        print("[!! ] 找不到源 .env，请把正确路径传给 NA_OLD_ENV 环境变量")
        return 1
    if not os.path.exists(DST):
        print("[!! ] 找不到目标 .env")
        return 1

    src = parse_env(SRC)
    print("\n---- 老配置里识别到的相关变量名（不含值）----")
    hits = [k for k in src if re.search(r"(OSS|ALIYUN|ACCESS_KEY|ENDPOINT|BUCKET|STORAGE_MODE)", k)]
    for k in sorted(hits):
        print("   ", k)

    resolved, missing = {}, []
    for target, keys in CANDIDATES.items():
        got = None
        for k in keys:
            if src.get(k, "").strip():
                got = (k, src[k].strip())
                break
        if got:
            resolved[target] = got
        else:
            missing.append(target)

    print("\n---- 将要写入（值已遮掩）----")
    for t, (k, v) in resolved.items():
        print("    %-26s <- %-26s %s" % (t, k, mask(v)))
    if missing:
        print("\n[!! ] 以下变量找不到来源，必须补：")
        for m in missing:
            print("      -", m)
        print("      请在 %s 里补上后再重跑本脚本" % SRC)
        if missing:
            return 2

    # 备份目标文件
    bak = DST + ".bak." + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(DST, bak)
    print("\n已备份:", bak)

    with open(DST, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()

    # 幂等：清掉目标里已有的 NA_OSS_* / ALIYUN_OSS_* / ALIYUN_ACCESS_KEY_* 行
    # （线上曾因重复执行追加命令堆出 24 行重复配置，这里统一剥离后重写规范的 4 行）
    strip_re = re.compile(r"^\s*(export\s+)?(NA_OSS_|ALIYUN_OSS_|ALIYUN_ACCESS_KEY_)")
    kept = [l for l in lines if not strip_re.match(l)]
    removed = len(lines) - len(kept)
    if removed:
        print("已剥离目标里的旧/重复配置行: %d 行" % removed)

    extra = [
        "",
        "# ---- 新品盘点(NA) 独立 OSS 桶，由 na_oss_migrate.py 于 %s 写入 ----"
        % time.strftime("%Y-%m-%d %H:%M:%S"),
    ]
    for t, (k, v) in resolved.items():
        extra.append("%s=%s" % (t, v))
    extra.append("")

    with open(DST, "w", encoding="utf-8") as fh:
        fh.write("\n".join(kept + extra))

    print("\n[OK ] 写入完成。请执行：")
    print("      sudo systemctl restart sku-spu.service")
    print("      然后复核：grep -cE '^NA_OSS_' %s   （应为 4）" % DST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
