from __future__ import annotations

from app.settings import _bounded_int, _int


def test_bounded_int_clamps_invalid_extremes(monkeypatch) -> None:
    monkeypatch.setenv("TEST_SETTING_LOW", "-10")
    monkeypatch.setenv("TEST_SETTING_HIGH", "999")
    monkeypatch.setenv("TEST_SETTING_BAD", "not-an-int")

    assert _bounded_int("TEST_SETTING_LOW", 5, 0, 10) == 0
    assert _bounded_int("TEST_SETTING_HIGH", 5, 0, 10) == 10
    assert _bounded_int("TEST_SETTING_BAD", 5, 0, 10) == 5


def test_platform_port_is_honored(monkeypatch) -> None:
    """Railway / Zeabur 注入的 $PORT 必须生效（ZCODE_PORT 仅在显式设置时优先）。

    回归保护：Dockerfile 曾因 ENV ZCODE_PORT=3000 硬编码覆盖平台 PORT，
    导致 Railway 健康检查打到错误端口而部署失败。
    """
    import importlib

    import app.settings as settings

    monkeypatch.setenv("PORT", "8080")
    monkeypatch.delenv("ZCODE_PORT", raising=False)
    importlib.reload(settings)
    try:
        assert settings.PORT == 8080
    finally:
        monkeypatch.delenv("PORT", raising=False)
        importlib.reload(settings)

    # 显式 ZCODE_PORT 仍然优先（本地自定义场景）
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setenv("ZCODE_PORT", "4000")
    importlib.reload(settings)
    try:
        assert settings.PORT == 4000
    finally:
        monkeypatch.delenv("PORT", raising=False)
        monkeypatch.delenv("ZCODE_PORT", raising=False)
        importlib.reload(settings)


def test_port_defaults_to_3000_when_unset(monkeypatch) -> None:
    import importlib

    import app.settings as settings

    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("ZCODE_PORT", raising=False)
    importlib.reload(settings)
    try:
        assert settings.PORT == 3000
        assert _int("PORT", 3000) == 3000
    finally:
        importlib.reload(settings)
