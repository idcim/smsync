FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 非 root 运行；/data 为卷挂载点（数据库与录音）
RUN useradd --system --uid 10001 --home /app smsync \
    && mkdir -p /data \
    && chown -R smsync:smsync /data
COPY --chown=smsync:smsync . .
USER smsync

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
