# zcode2api — Python(FastAPI) + Google Chrome（无痕验证求解器）
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ZCODE_HOST=0.0.0.0 \
    ZCODE_DATA_DIR=/data \
    ZCODE_CAPTCHA_BROWSER_CHANNEL=chrome

# 注意：不要在此固定 ZCODE_PORT。Railway / Zeabur / Render 等平台会注入
# $PORT，应用必须监听该端口才能通过平台健康检查；本地未注入时 settings.py
# 自动回落到 3000（ZCODE_PORT → PORT → 3000）。

WORKDIR /app

# ── Python 依赖（独立分层，便于缓存）────────────────────────────────────────
COPY requirements.txt ./
RUN pip install -r requirements.txt

# ── 系统依赖（真实 Chrome 所需的运行库，仅装依赖不下载浏览器）──────────────
RUN playwright install-deps chromium

# ── Google Chrome（无痕验证必须用真实 Chrome 二进制：
#    Playwright 自带 Chromium 会被阿里云风控识破，返回 verifyCode=F001）───────
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# ── 应用源码 ────────────────────────────────────────────────────────────────
COPY . .

# 账号 / 设置持久化目录（建议挂载到宿主机卷；不使用 VOLUME 指令，Railway 等平台不支持）
RUN mkdir -p /data
EXPOSE 3000

# 让编排平台能区分“容器已启动”和“网关已就绪”。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; p=os.getenv('ZCODE_PORT') or os.getenv('PORT') or '3000'; urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=3)"

CMD ["python", "main.py", "serve"]
