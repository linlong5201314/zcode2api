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


class _CurrentClient:
    calls = []

    def __init__(self, *args, **kwargs):
        type(self).calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, headers, **kwargs):
        type(self).calls.append((url, headers, kwargs))
        if "subscription/list" in url:
            return _Response({
                "code": 200,
                "data": [{
                    "productName": "GLM Coding Max",
                    "status": "VALID",
                    "inCurrentPeriod": True,
                }],
            })
        if "quota/limit" in url:
            return _Response({
                "plans": [{"status": "active", "name": "Coding Max"}],
                "balances": [{
                    "entitlement_id": "model_usage",
                    "total_units": "100",
                    "used_units": "30",
                    "available_units": "70",
                    "period_end": 1_768_000_000,
                }],
            })
        if url.endswith("/billing/current"):
            return _Response({"data": {"plans": []}})
        if url.endswith("/billing/balance"):
            return _Response({"data": {"balances": []}})
        return _Response({"data": {"requests": 3}})


class _NotEntitledClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, headers, **kwargs):
        if "subscription/list" in url:
            return _Response({"reason": "coding_plan_not_entitled"})
        if "quota/limit" in url:
            return _Response({"msg": "coding_plan_not_entitled"})
        return _Response({"data": {"plans": []}})


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
    assert account.quota["TOKENS_LIMIT"]["expires_at"] == 1_767_373_239.187
    assert account.status == Status.ACTIVE
    assert account.last_error is None


def test_fetch_quota_uses_current_subscription_and_available_units(monkeypatch) -> None:
    account = Account.create("zai", "current", "header.payload.signature")
    account.status = Status.EXHAUSTED
    account.last_error = "stale error"
    monkeypatch.setattr(quota.httpx, "AsyncClient", _CurrentClient)
    monkeypatch.setattr(quota.store, "update_account", lambda _account: None)

    result = asyncio.run(quota.fetch_quota(account))

    assert "subscription" in result
    assert "quota_limit" in result
    assert account.plan["productName"] == "GLM Coding Max"
    assert account.plan["active"] is True
    assert account.quota["model_usage"]["remaining"] == 70
    assert account.status == Status.ACTIVE
    assert account.last_error is None
    assert any("subscription/list" in call[0] for call in _CurrentClient.calls)
    assert any(call[2].get("params", {}).get("app_version") for call in _CurrentClient.calls)


def test_fetch_quota_marks_not_entitled_as_invalid(monkeypatch) -> None:
    account = Account.create("zai", "not-entitled", "header.payload.signature")
    monkeypatch.setattr(quota.httpx, "AsyncClient", _NotEntitledClient)
    monkeypatch.setattr(quota.store, "update_account", lambda _account: None)

    result = asyncio.run(quota.fetch_quota(account))

    assert "subscription" in result
    assert account.plan["status"] == "not_entitled"
    assert account.status == Status.INVALID
    assert account.last_error == "Coding Plan 未激活（coding_plan_not_entitled）"
