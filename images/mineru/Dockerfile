FROM python:3.12-slim

ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    DEBIAN_FRONTEND=noninteractive \
    HF_HOME=/root/.cache/huggingface

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget git xz-utils libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
RUN pip install --no-cache-dir -U pip \
    && pip install --no-cache-dir "mineru" "fastapi" "uvicorn[standard]" "python-multipart"

COPY app.py /workspace/app.py

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
