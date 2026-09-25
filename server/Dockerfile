FROM python:3.11-slim

# 构建时由 deploy.sh 注入当前 git 提交号，作为"已部署版本"的权威标记
ARG GIT_COMMIT=unknown
LABEL git_commit=$GIT_COMMIT

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
