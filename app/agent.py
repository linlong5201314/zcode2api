"""上游请求构建。

负责根据账号凭证选择端点、组装请求头。实际发送与流式透传在 routes/gateway.py。
"""

from __future__ import annotations

from . import settings
from .models import Account

# 只透传不会携带凭证、连接状态或路由控制信息的客户端 header。
# 以前采用黑名单会把 Cookie、Forwarded、Origin 等请求头原样带给上游，
# 也允许客户端覆盖网关生成的 Authorization / anthropic-version。这里改成
# 明确的白名单，并在末尾再次写入网关托管的关键 header。
_FORWARD_HEADERS = {
    "accept-language",
    "cache-control",
    "anthropic-beta",
    "anthropic-dangerous-direct-browser-access",
    "traceparent",
    "tracestate",
    "x-client-request-id",
    "x-stainless-helper-method",
    "x-stainless-lang",
    "x-stainless-os",
    "x-stainless-package-version",
    "x-stainless-retry-count",
    "x-stainless-runtime",
    "x-stainless-runtime-version",
}


def _safe_forward_headers(incoming_headers: dict | None) -> dict[str, str]:
    """Return a small, lower-cased set of safe client metadata headers."""
    forwarded: dict[str, str] = {}
    for key, value in (incoming_headers or {}).items():
        lower = str(key).strip().lower()
        if lower not in _FORWARD_HEADERS:
            continue
        text = str(value)
        # Reject header values containing control characters before httpx sees them.
        if any(ord(char) < 32 and char not in "\t" for char in text):
            continue
        if len(text) > 4_096:
            continue
        forwarded[lower] = text
    return forwarded


def build_request(
    account: Account,
    body: dict,
    verify_param: str | None,
    incoming_headers: dict | None = None,
    use_fallback: bool = False,
) -> tuple[str, dict]:
    """返回 (目标 URL, 请求头)。use_fallback=True 时走 API Key 回退端点（无验证码）。"""
    provider = account.provider

    if provider == "zai":
        if use_fallback and account.api_key:
            target_url = settings.UPSTREAM["zai_fallback"]
            auth = {"x-api-key": account.api_key}
        elif account.mode == "jwt" and account.jwt_token:
            target_url = settings.UPSTREAM["zai"]
            auth = {"Authorization": f"Bearer {account.jwt_token}"}
        elif account.api_key:
            target_url = settings.UPSTREAM["zai_fallback"]
            auth = {"x-api-key": account.api_key}
        else:
            raise RuntimeError("账号缺少有效凭证")
    elif provider == "bigmodel":
        target_url = settings.UPSTREAM["bigmodel"]
        if not account.api_key:
            raise RuntimeError("BigModel 账号缺少 API Key")
        auth = {"x-api-key": account.api_key}
    else:
        raise RuntimeError(f"未知提供商: {provider}")

    headers = _safe_forward_headers(incoming_headers)
    headers.update({
        "content-type": "application/json",
        "accept": "text/event-stream" if body.get("stream") else "application/json",
        **auth,
        "anthropic-version": "2023-06-01",
        "User-Agent": settings.USER_AGENT,
        "X-ZCode-App-Version": settings.ZCODE_APP_VERSION,
        "X-ZCode-Agent": "glm",
        "HTTP-Referer": "https://zcode.z.ai/",
    })
    if verify_param:
        headers["X-Aliyun-Captcha-Verify-Param"] = verify_param

    return target_url, headers
