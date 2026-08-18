from __future__ import annotations

from app import settings
from app.models import Account
from app.store import Store


def test_store_find_any_matches_id_or_name(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "accounts.db")
    store = Store()
    account = store.add_account("zai", "friendly-name", "api-key-1")

    assert store.find_any(account.id) is account
    assert store.find_any("friendly-name") is account


def test_import_accounts_counts_only_new_valid_accounts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "accounts.db")
    store = Store()
    payload = {
        "providers": {
            "zai": [
                {"name": "one", "secret": "same-key"},
                {"name": "duplicate", "secret": "same-key"},
                {"name": "empty", "secret": ""},
            ],
            "unknown": [{"name": "ignored", "secret": "other"}],
        }
    }

    assert store.import_accounts(payload) == 1
    assert len(store.list_accounts("zai")) == 1
    assert store.import_accounts(payload) == 0


def test_add_account_with_status_distinguishes_duplicates(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "accounts.db")
    store = Store()

    first, created = store.add_account_with_status("zai", "one", "same-key")
    second, duplicate = store.add_account_with_status("zai", "two", "same-key")

    assert created is True
    assert duplicate is False
    assert first is second


def test_account_mode_detection_requires_jwt_shaped_segments() -> None:
    assert Account.create("zai", "jwt", "header.payload.signature").mode == "jwt"
    assert Account.create("zai", "key", "not.a jwt").mode == "apiKey"
    assert Account.create("bigmodel", "key", "a.b.c").mode == "apiKey"
