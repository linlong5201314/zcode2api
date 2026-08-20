from __future__ import annotations

import asyncio

from app.routes.admin_api import get_settings
from app.store import store
from app.auth_admin import _extract_bearer, _safe_token
from app.auth_admin import verify_admin_key


def test_settings_endpoint_does_not_return_secret_material(monkeypatch) -> None:
    monkeypatch.setattr(store, "admin_key", lambda: "super-secret-admin")
    monkeypatch.setattr(store, "gateway_key", lambda: "gateway-secret")
    monkeypatch.setattr(store, "quota_refresh_interval", lambda: 60)

    payload = asyncio.run(get_settings())

    assert payload["admin_key"] is None
    assert payload["gateway_key"] is None
    assert payload["admin_key_set"] is True
    assert payload["gateway_key_set"] is True
    assert "super-secret-admin" not in str(payload)
    assert "gateway-secret" not in str(payload)


def test_auth_tokens_reject_control_characters_and_extreme_length() -> None:
    assert _extract_bearer("Bearer good-token") == "good-token"
    assert _extract_bearer("Bearer bad\n-token") is None
    assert _safe_token("x" * 4097) is None


def test_admin_auth_requires_username_when_configured(monkeypatch) -> None:
    monkeypatch.setattr(store, "admin_user", lambda: "admin")
    monkeypatch.setattr(store, "admin_key", lambda: "zcode")

    asyncio.run(verify_admin_key(authorization="Bearer zcode", x_admin_user="admin"))
