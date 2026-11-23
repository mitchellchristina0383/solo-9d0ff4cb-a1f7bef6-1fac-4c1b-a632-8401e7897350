FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

RUN chmod +x scripts/entrypoint.sh

# 数据（中心 bare 仓库、SQLite、worktrees）挂载到 /data
VOLUME ["/data"]
EXPOSE 8000

ENTRYPOINT ["scripts/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
