FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com

WORKDIR /workspace
RUN pip install --no-cache-dir -U pip \
    && pip install --no-cache-dir "argilla-server" "argilla" "click<8.2"

# argilla-server 2.8.0 的 console script 元数据为空，统一走模块入口
CMD ["python", "-m", "argilla_server", "start", "--db-url", "sqlite:////data/argilla/argilla.db", "--host", "0.0.0.0", "--port", "3000"]
