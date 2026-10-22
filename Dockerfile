# Dockerfile —— SKU&SPU 清洗看板（Render / Hugging Face Spaces 通用）
FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖（openpyxl 读 xlsx 不需要额外系统库；此处保持精简）
RUN pip install --no-cache-dir --upgrade pip

# 先装依赖以利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码 + 前端静态资源 + 模板
COPY app.py pipeline.py storage.py wsgi.py ./
COPY gunicorn.conf.py ./            # ECS 直接部署（非 Docker）用的 gunicorn 配置
COPY templates/ ./templates/
COPY static/ ./static/
# 随仓库带上数据源留档，供冷启动自动重建主数据
COPY reference/ ./reference/
COPY uploads/ ./uploads/

# 运行前建立可写目录（云端磁盘可能为临时卷）
RUN mkdir -p /app/data /app/uploads /app/reference /app/logs

# 端口：ECS/容器统一用 8092（Render 仍可注入 PORT 覆盖）
EXPOSE 8092

# gunicorn 绑定 0.0.0.0:$PORT（用 shell 形式让 $PORT 在运行时展开）
# 存储后端由 STORAGE_MODE 决定：supabase（海外）或 oss（阿里云，推荐国内 ECS）。
# worker=1 保证异常确认行级 version 一致性（单实例部署足够支撑内部使用）。
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:${PORT:-8092} -w 1 --threads 4 --timeout 180 wsgi:app"]
