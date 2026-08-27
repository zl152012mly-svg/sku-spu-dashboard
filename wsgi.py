# -*- coding: utf-8 -*-
"""
wsgi.py —— Render / Hugging Face Spaces（gunicorn）入口
======================================
gunicorn 不会执行 app.__main__，故在此恢复已持久化的异常确认状态 + 冷启动自动重建后暴露 app。
仅用于上云部署；本地 `python app.py` 仍然走 app 自身的 __main__（含单实例保护）。
"""
import app as sku_app

# 加载后恢复云端/本地已存的异常确认状态（幂等）
sku_app._apply_annotations()
# 云端冷启动自动重建数据（Render/HF 磁盘临时，重启后空看板+数据源齐备时重建）
sku_app._boot_rebuild()

# 暴露供 gunicorn 使用的 WSGI 应用
application = sku_app.app
app = sku_app.app
