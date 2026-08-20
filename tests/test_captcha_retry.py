"""验证码重试链路测试。

上游拒绝验证码后，网关必须失效缓存并重新求解，用新的 verify_param 重试，
而不是拿同一个已失效参数反复请求（修复前的行为）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.routes import gateway
from app.models import Account


JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV"


class _CaptchaErrorStream:
    """模拟上游 400 验证码错误响应。"""

    def __init__(self) -> None:
        self.status_code = 400
        self.headers: dict[str, str] = {}

    async def aread(self) -> bytes:
        return b'{"error":{"message":"captcha verify failed","type":"invalid_request_error"}}'

    async def aiter_bytes(self):  # pragma: no cover - 不应被读取
        raise AssertionError("错误响应不应进入流式读取")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _OkStream:
    """模拟上游 200 SSE 成功响应。"""

    def __init__(self) -> None:
        self.status_code = 200
        self.headers = {"content-type": "text/event-stream"}

    async def aread(self):  # pragma: no cover
        raise AssertionError("成功响应不应被 aread")

    async def aiter_bytes(self):
        yield b'event: message_start\r\ndata: {"message":{"usage":{"input_tokens":1}}}\r\n\r\n'
        yield b'event: content_block_delta\r\ndata: {"delta":{"type":"text_delta","text":"ok"}}\r\n\r\n'

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _Client:
    """按验证码参数决定响应：旧 param → 400 拒绝；新 param → 200 成功。"""

    instances: list["_Client"] = []

    def __init__(self, *args, **kwargs):
        self.requests: list[dict] = []
        _Client.instances.append(self)

    def stream(self, method, url, headers=None, content=None):
        self.requests.append({"url": url, "headers": dict(headers or {})})
        param = (headers or {}).get("X-Aliyun-Captcha-Verify-Param")
        if param == "fresh-param":
            return _OkStream()
        return _CaptchaErrorStream()

    async def aclose(self):
        return None


class _FakeCaptchaManager:
    """缓存失效后每次求解都返回新参数（模拟 invalidate 后重新求解）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.invalidations = 0

    def invalidate(self) -> None:
        self.invalidations += 1

    async def get_verify_param(self, port=None) -> str:
        self.calls += 1
        return "fresh-param"


def test_captcha_retry_refreshes_verify_param(monkeypatch) -> None:
    account = Account.create("zai", "test-jwt", JWT)
    monkeypatch.setattr(gateway.store, "update_account", lambda _account: None)
    monkeypatch.setattr(gateway.httpx, "AsyncClient", _Client)
    fake_captcha = _FakeCaptchaManager()
    monkeypatch.setattr(gateway, "captcha_manager", fake_captcha)

    body = {"model": "GLM-5.3", "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 64, "stream": True}

    async def run():
        return await gateway._forward_once(
            req_id="t1",
            account=account,
            body=body,
            payload=b"{}",
            incoming_headers={},
            verify_param="stale-param",
            use_fallback=False,
            retries=3,
            port=3000,
        )

    result = asyncio.run(run())

    # 旧参数被拒 → 失效缓存 → 重新求解 → 新参数成功
    assert fake_captcha.invalidations == 1
    assert fake_captcha.calls == 1, "重试时应重新求解验证码"
    assert result.status_code == 200
    # 只统计真正向上游发起过请求的 client（后台额度刷新任务不调用 stream）
    upstream_requests = [
        request
        for client in _Client.instances
        for request in client.requests
    ]
    sent_params = [r["headers"].get("X-Aliyun-Captcha-Verify-Param") for r in upstream_requests]
    assert sent_params == ["stale-param", "fresh-param"]


def test_captcha_rejection_surfaces_after_retries(monkeypatch) -> None:
    """求解持续失败时 exhaustion 后应抛 _CaptchaRejected 走降级链。"""

    class _AlwaysRejectClient(_Client):
        def stream(self, method, url, headers=None, content=None):
            self.requests.append({"url": url, "headers": dict(headers or {})})
            return _CaptchaErrorStream()

    account = Account.create("zai", "test-jwt2", JWT)
    monkeypatch.setattr(gateway.store, "update_account", lambda _account: None)
    monkeypatch.setattr(gateway.httpx, "AsyncClient", _AlwaysRejectClient)
    fake_captcha = _FakeCaptchaManager()
    monkeypatch.setattr(gateway, "captcha_manager", fake_captcha)

    body = {"model": "GLM-5.3", "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 64, "stream": True}

    async def run():
        return await gateway._forward_once(
            req_id="t2",
            account=account,
            body=body,
            payload=b"{}",
            incoming_headers={},
            verify_param="stale-param",
            use_fallback=False,
            retries=3,
            port=3000,
        )

    _Client.instances.clear()
    with pytest.raises(gateway._CaptchaRejected):
        asyncio.run(run())
    # 每次重试都重新求解新参数（而非复用失效值），次数用尽后走降级链
    assert fake_captcha.calls == 2
    assert fake_captcha.invalidations == 2
