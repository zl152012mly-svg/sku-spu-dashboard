# -*- coding: utf-8 -*-
"""
gunicorn 配置 —— ECS 直接部署（非 Docker）时用：
    gunicorn -c gunicorn.conf.py wsgi:app

Docker 部署走 Dockerfile 的 CMD，不依赖本文件。
"""
import os

port = int(os.environ.get('PORT', '8092'))
bind = '0.0.0.0:%d' % port
workers = 1          # 单 worker + 多线程，保证异常确认行级 version 一致性
threads = 4
timeout = 180
graceful_timeout = 30
keepalive = 5
accesslog = 'logs/access.log'
errorlog = 'logs/error.log'
