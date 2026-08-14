"""运行期配置：环境变量 + 默认值。

所有可调参数集中在此。账号与凭证不在此处，而是持久化到 data/ 目录（见 store.py）。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# 项目根目录
ROOT_DIR = Path(__file__).resolve().parents[1]


def _resolve_path(env_name: str, default: str) -> Path:
    raw = (os.getenv(env_name, default) or default).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path


def _int(env_name: str, default: int) -> int:
    try:
        return int(os.getenv(env_name, str(default)))
    except (TypeError, ValueError):
        return default


# ── 目录 ─────────────────────────────────────────────────────────────────────
DATA_DIR = _resolve_path("ZCODE_DATA_DIR", "data")
# 账号与设置持久化到本地 SQLite（与 grok2api 的 local 后端一致）
DB_PATH = DATA_DIR / "accounts.db"
STATIC_DIR = Path(__file__).resolve().parent / "statics"

# ── 服务 ─────────────────────────────────────────────────────────────────────
PORT = _int("ZCODE_PORT", 3000)
HOST = os.getenv("ZCODE_HOST", "0.0.0.0")

# ── 鉴权 ─────────────────────────────────────────────────────────────────────
# 后台管理密码默认值，首次启动写入 data/accounts.db，之后以数据库（meta 表）为准。
DEFAULT_ADMIN_KEY = os.getenv("ZCODE_ADMIN_KEY", "zcode")

# OAuth 授权码回跳地址。Z.AI 服务端按 client_id 校验重定向 URI 白名单，
# 该公开 client_id 仅注册了 zcode.z.ai 的登录页 /login（与网页端实测一致），
# 因此授权后需用户从地址栏复制 code 交给网关兑换。
OAUTH_REDIRECT_URI = os.getenv("ZCODE_OAUTH_REDIRECT_URI", "https://zcode.z.ai/login")

# ── 验证码缓存 ───────────────────────────────────────────────────────────────
CAPTCHA_CACHE_TTL = _int("CAPTCHA_CACHE_TTL", 45_000)          # ms
CAPTCHA_CONFIG_CACHE_TTL = _int("CAPTCHA_CONFIG_CACHE_TTL", 600_000)  # ms

# 验证码求解（Playwright 无头浏览器运行阿里云无痕 SDK）
CAPTCHA_SOLVE_RETRIES = _int("ZCODE_CAPTCHA_RETRIES", 4)
CAPTCHA_SOLVE_TIMEOUT = _int("ZCODE_CAPTCHA_TIMEOUT", 40)  # 每次求解超时（秒）
# 留空使用 Playwright 自带 Chromium（会被阿里云风控识破，仅限调试）；
# 生产环境必须用真实 Chrome：Docker 镜像已内置 Google Chrome 并默认 "chrome"，
# 本机运行可设 "msedge" / "chrome" 复用系统浏览器。
CAPTCHA_BROWSER_CHANNEL = os.getenv("ZCODE_CAPTCHA_BROWSER_CHANNEL", "") or None

# ── 用量监控 ─────────────────────────────────────────────────────────────────
# 后台自动刷新账号额度的间隔（秒）。0 表示关闭后台轮询，仅按需刷新。
QUOTA_REFRESH_INTERVAL = _int("ZCODE_QUOTA_REFRESH_INTERVAL", 60)
# 限流（cooling）冷却时长（秒）
COOLING_SECONDS = _int("ZCODE_COOLING_SECONDS", 300)

# ── 上游端点 ─────────────────────────────────────────────────────────────────
UPSTREAM = {
    "zai": os.getenv(
        "ZAI_UPSTREAM_URL",
        "https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages",
    ),
    "zai_fallback": os.getenv(
        "ZAI_FALLBACK_URL",
        "https://api.z.ai/api/anthropic/v1/messages",
    ),
    "bigmodel": os.getenv(
        "BIGMODEL_UPSTREAM_URL",
        "https://open.bigmodel.cn/api/anthropic/v1/messages",
    ),
}

# ZCode 计费 / 额度查询端点
ZCODE_BILLING_BASE = os.getenv(
    "ZAI_BILLING_URL",
    "https://zcode.z.ai/api/v1/zcode-plan",
).rstrip("/")

# ── GLM-5.3 强制思考模式 ─────────────────────────────────────────────────────
# 客户端未显式开启 thinking 时，网关自动注入 {"type":"enabled","budget_tokens":N}。
# 上游仅接受 enabled（不允许禁用思考），思考深度由 reasoning_effort 控制（low/high/max）。
THINKING_BUDGET_TOKENS = _int("ZCODE_THINKING_BUDGET", 8192)
REASONING_EFFORT = os.getenv("ZCODE_REASONING_EFFORT", "max")

# 对齐 ZCode 桌面端版本号（上游 configs / 请求头均校验版本）
ZCODE_APP_VERSION = os.getenv("ZCODE_APP_VERSION", "3.7.7")
USER_AGENT = os.getenv("UPSTREAM_USER_AGENT", f"ZCode/{ZCODE_APP_VERSION}")
APP_VERSION = "2.0.0"
