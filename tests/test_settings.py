from __future__ import annotations

from app.settings import _bounded_int


def test_bounded_int_clamps_invalid_extremes(monkeypatch) -> None:
    monkeypatch.setenv("TEST_SETTING_LOW", "-10")
    monkeypatch.setenv("TEST_SETTING_HIGH", "999")
    monkeypatch.setenv("TEST_SETTING_BAD", "not-an-int")

    assert _bounded_int("TEST_SETTING_LOW", 5, 0, 10) == 0
    assert _bounded_int("TEST_SETTING_HIGH", 5, 0, 10) == 10
    assert _bounded_int("TEST_SETTING_BAD", 5, 0, 10) == 5
