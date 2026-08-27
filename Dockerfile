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
COPY templates/ ./templates/
COPY static/ ./static/
# 随仓库带上数据源留档，供冷启动自动重建主数据
COPY reference/ ./reference/
COPY uploads/ ./uploads/ 2>/dev/null || true

# 运行前建立可写目录（云端磁盘可能为临时卷）
RUN mkdir -p /app/data /app/uploads /app/reference

# 端口：Render 注入 PORT，HF 默认 7860
EXPOSE 7860

# gunicorn 绑定 0.0.0.0:$PORT（用 shell 形式让 $PORT 在运行时展开）
# 上云建议用 Supabase 模式（SUPABASE_URL+SUPABASE_KEY）且 worker=1 保证行级一致性。
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:${PORT:-7860} -w 1 --threads 4 --timeout 180 wsgi:app"]
