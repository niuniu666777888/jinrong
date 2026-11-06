FROM python:3.12-slim

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY run.py .
COPY app ./app

ENV MVP_HOST=0.0.0.0 \
    MVP_PORT=8080 \
    MVP_DB=/data/mvp.db \
    PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).status==200 else 1)"

CMD ["python", "run.py"]
