from __future__ import annotations

import asyncio

import httpx

from app import quota
from app.models import Account, Status


class _FailingClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        raise httpx.ConnectError("offline")

    async def __aexit__(self, *args):
        return None


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


class _Client:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, headers):
        if url.endswith("/billing/current"):
            return _Response({"data": {"plans": [{"name": "pro"}]}})
        if url.endswith("/billing/balance"):
            return _Response({"data": {"balances": {"GLM-5.2": {"total": "10", "remaining": "0"}}}})
        return _Response({"data": {"requests": 3}})


class _LimitsClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, headers):
        payload = {
            "code": 200,
            "msg": "操作成功",
            "data": {
                "limits": [
                    {
                        "type": "TOKENS_LIMIT",
                        "usage": 100,
                        "currentValue": 20,
                        "remaining": 80,
                        "percentage": 20,
                        "nextResetTime": 1767373239187,
                    }
                ],
                "level": "pro",
            },
            "success": True,
        }
        if url.endswith("/billing/current"):
            return _Response(payload)
        if url.endswith("/billing/balance"):
            return _Response(payload)
        return _Response({"data": {"requests": 3}})


def test_fetch_quota_returns_error_on_transport_failure(monkeypatch) -> None:
    account = Account.create("zai", "offline", "api-key")
    monkeypatch.setattr(quota.httpx, "AsyncClient", _FailingClient)
    monkeypatch.setattr(quota.store, "update_account", lambda _account: None)

    result = asyncio.run(quota.fetch_quota(account))

    assert "error" in result
    assert account.last_checked_at is not None
    assert account.status == Status.ACTIVE


def test_fetch_quota_normalizes_string_balances_and_marks_exhausted(monkeypatch) -> None:
    account = Account.create("zai", "empty", "api-key")
    monkeypatch.setattr(quota.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(quota.store, "update_account", lambda _account: None)

    result = asyncio.run(quota.fetch_quota(account))

    assert "balance" in result
    assert account.quota["GLM-5.2"]["remaining"] == 0
    assert account.status == Status.EXHAUSTED


def test_fetch_quota_restores_active_from_limits_payload(monkeypatch) -> None:
    account = Account.create("zai", "restored", "api-key")
    account.status = Status.EXHAUSTED
    monkeypatch.setattr(quota.httpx, "AsyncClient", _LimitsClient)
    monkeypatch.setattr(quota.store, "update_account", lambda _account: None)

    result = asyncio.run(quota.fetch_quota(account))

    assert "balance" in result
    assert account.quota["TOKENS_LIMIT"]["remaining"] == 80
    assert account.status == Status.ACTIVE
    assert account.last_error is None
