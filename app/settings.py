"""运行期配置：环境变量 + 默认值。

所有可调参数集中在此。账号与凭证不在此处，而是持久化到 data/ 目录（见 store.py）。
"""

from __future__ import annotations

import os
from pathlib import Path

try:  # dotenv is convenient but should not make API-key-only deployments fail
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - exercised in minimal production images
    def load_dotenv(*_args, **_kwargs) -> bool:
        """No-op fallback when python-dotenv is intentionally not installed."""
        return False

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


def _bounded_int(env_name: str, default: int, minimum: int, maximum: int) -> int:
    """Read an integer environment setting without allowing unsafe extremes."""
    return min(max(_int(env_name, default), minimum), maximum)


def _csv(env_name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(env_name)
    values = tuple(item.strip() for item in (raw or "").split(",") if item.strip())
    return values or default


# ── 目录 ─────────────────────────────────────────────────────────────────────
DATA_DIR = _resolve_path("ZCODE_DATA_DIR", "data")
# 账号与设置持久化到本地 SQLite（与 grok2api 的 local 后端一致）
DB_PATH = DATA_DIR / "accounts.db"
STATIC_DIR = Path(__file__).resolve().parent / "statics"

# ── 服务 ─────────────────────────────────────────────────────────────────────
PORT = _bounded_int("ZCODE_PORT", 3000, 0, 65535)
HOST = os.getenv("ZCODE_HOST", "0.0.0.0")

# ── 鉴权 ─────────────────────────────────────────────────────────────────────
# 后台管理密码默认值，首次启动写入 data/accounts.db，之后以数据库（meta 表）为准。
DEFAULT_ADMIN_KEY = os.getenv("ZCODE_ADMIN_KEY", "zcode")

# OAuth 授权码回跳地址。Z.AI 服务端按 client_id 校验重定向 URI 白名单，
# 该公开 client_id 仅注册了 zcode.z.ai 的登录页 /login（与网页端实测一致），
# 因此授权后需用户从地址栏复制 code 交给网关兑换。
OAUTH_REDIRECT_URI = os.getenv("ZCODE_OAUTH_REDIRECT_URI", "https://zcode.z.ai/login")

# ── 验证码缓存 ───────────────────────────────────────────────────────────────
CAPTCHA_CACHE_TTL = _bounded_int(
    "CAPTCHA_CACHE_TTL", 45_000, 0, 86_400_000
)  # ms
CAPTCHA_CONFIG_CACHE_TTL = _bounded_int(
    "CAPTCHA_CONFIG_CACHE_TTL", 600_000, 0, 86_400_000
)  # ms
# TTL 过期后的宽限期：期间直接返回旧参数并后台刷新，避免请求同步等待求解
CAPTCHA_STALE_GRACE = _bounded_int(
    "ZCODE_CAPTCHA_STALE_GRACE", 300_000, 0, 86_400_000
)  # ms
# 求解失败结果也缓存一段时间，避免每个请求都重复跑注定失败的求解（数据中心 IP 被风控拒绝时尤其重要）
CAPTCHA_FAIL_CACHE_TTL = _bounded_int(
    "ZCODE_CAPTCHA_FAIL_TTL", 60_000, 0, 86_400_000
)  # ms

# 验证码求解（Playwright 无头浏览器运行阿里云无痕 SDK）
CAPTCHA_SOLVE_RETRIES = _bounded_int("ZCODE_CAPTCHA_RETRIES", 4, 1, 10)
CAPTCHA_SOLVE_TIMEOUT = _bounded_int("ZCODE_CAPTCHA_TIMEOUT", 40, 1, 300)  # 每次求解超时（秒）
# 留空使用 Playwright 自带 Chromium（会被阿里云风控识破，仅限调试）；
# 生产环境必须用真实 Chrome：Docker 镜像已内置 Google Chrome 并默认 "chrome"，
# 本机运行可设 "msedge" / "chrome" 复用系统浏览器。
CAPTCHA_BROWSER_CHANNEL = os.getenv("ZCODE_CAPTCHA_BROWSER_CHANNEL", "") or None

# ── 用量监控 ─────────────────────────────────────────────────────────────────
# 后台自动刷新账号额度的间隔（秒）。0 表示关闭后台轮询，仅按需刷新。
QUOTA_REFRESH_INTERVAL = _bounded_int("ZCODE_QUOTA_REFRESH_INTERVAL", 60, 0, 86_400)
# 限流（cooling）冷却时长（秒）
COOLING_SECONDS = _bounded_int("ZCODE_COOLING_SECONDS", 300, 1, 86_400)

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
# 新版 ZCode 客户端把 Coding Plan 的订阅状态与配额拆到 api.z.ai。
# 保留环境变量覆盖，便于 BigModel/私有兼容端点或离线测试使用。
ZCODE_SUBSCRIPTION_URL = os.getenv(
    "ZCODE_SUBSCRIPTION_URL",
    "https://api.z.ai/api/biz/subscription/list",
).strip().rstrip("/")
ZCODE_QUOTA_LIMIT_URL = os.getenv(
    "ZCODE_QUOTA_LIMIT_URL",
    "https://api.z.ai/api/monitor/usage/quota/limit",
).strip().rstrip("/")
ZCODE_QUOTA_TIMEOUT = _bounded_int("ZCODE_QUOTA_TIMEOUT", 20, 3, 120)

# 可选：为上游请求与验证码求解统一走住宅代理（数据中心 IP 会被阿里云风控 F001 拒绝）。
# 形如 http://user:pass@host:port 或 socks5://host:port；留空直连。
UPSTREAM_PROXY = os.getenv("ZCODE_UPSTREAM_PROXY", "") or None

# ── GLM-5.3 强制思考模式 ─────────────────────────────────────────────────────
# 客户端未显式开启 thinking 时，网关自动注入 {"type":"enabled","budget_tokens":N}。
# 上游仅接受 enabled（不允许禁用思考），思考深度由 reasoning_effort 控制（low/high/max）。
THINKING_BUDGET_TOKENS = _bounded_int("ZCODE_THINKING_BUDGET", 8192, 1_024, 1_000_000)
REASONING_EFFORT = os.getenv("ZCODE_REASONING_EFFORT", "max")

# 可通过环境变量扩展 / 收窄公开模型清单。默认覆盖当前 Coding Plan 常见模型，
# 同时保留原有前三项的顺序，避免客户端默认模型发生变化。
MODEL_ALLOWLIST = _csv(
    "ZCODE_MODELS",
    (
        "GLM-5.3",
        "GLM-5.2",
        "GLM-5-Turbo",
        "GLM-4.7",
        "GLM-4.6",
        "GLM-4.5",
        "GLM-4.5-Air",
        "GLM-4.5V",
        "GLM-4.5-Flash",
    ),
)

# 上游/客户端请求体保护，防止无界 JSON 消耗内存。
MAX_REQUEST_BYTES = _bounded_int(
    "ZCODE_MAX_REQUEST_BYTES", 8 * 1024 * 1024, 1_024, 64 * 1024 * 1024
)

# 对齐 ZCode 桌面端版本号（上游 configs / 请求头均校验版本）
ZCODE_APP_VERSION = os.getenv("ZCODE_APP_VERSION", "3.7.7")
USER_AGENT = os.getenv("UPSTREAM_USER_AGENT", f"ZCode/{ZCODE_APP_VERSION}")
APP_VERSION = "2.0.0"
