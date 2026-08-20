from __future__ import annotations

import asyncio

from app.models import Account
from app.routes import admin_api


def test_editing_jwt_refreshes_quota_immediately(monkeypatch) -> None:
    account = Account.create("zai", "editable", "old-api-key")
    refreshed = []

    async def _refresh(updated):
        refreshed.append(updated)
        updated.plan = {"status": "active"}
        return {"quota_limit": {"ok": True}}

    monkeypatch.setattr(admin_api.store, "find_any", lambda _account_id: account)
    monkeypatch.setattr(admin_api.store, "update_account", lambda _account: None)
    monkeypatch.setattr(admin_api, "fetch_quota", _refresh)

    response = asyncio.run(admin_api.edit_account(
        account.id,
        {"token": "header.payload.signature", "name": "renamed"},
    ))

    assert response["ok"] is True
    assert response["refreshed"] is True
    assert refreshed == [account]
    assert account.mode == "jwt"
    assert account.api_key is None
    assert account.name == "renamed"
    assert response["account"]["plan"]["status"] == "active"

