# zcode2api — Python(FastAPI) + Playwright 无头 Chromium（无痕验证求解器）
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ZCODE_HOST=0.0.0.0 \
    ZCODE_PORT=3000 \
    ZCODE_DATA_DIR=/data

WORKDIR /app

# ── Python 依赖（独立分层，便于缓存）────────────────────────────────────────
COPY requirements.txt ./
RUN pip install -r requirements.txt

# ── Chromium（无痕验证求解：Playwright 无头浏览器运行阿里云无痕 SDK）────────
RUN playwright install --with-deps chromium

# ── 应用源码 ────────────────────────────────────────────────────────────────
COPY . .

# 账号 / 设置持久化目录（建议挂载到宿主机卷；不使用 VOLUME 指令，Railway 等平台不支持）
RUN mkdir -p /data
EXPOSE 3000

CMD ["python", "main.py", "serve"]
