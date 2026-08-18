from __future__ import annotations

import pytest

from app.proxy import parse_subscription_body


def test_parse_subscription_body_handles_base64_uri_list() -> None:
    import base64

    raw = "http://127.0.0.1:8080\nsocks5://127.0.0.1:1080"
    encoded = base64.b64encode(raw.encode()).decode()

    parsed = parse_subscription_body(encoded)

    assert parsed["ok"] is True
    assert parsed["node_count"] == 2
    assert parsed["direct_usable"] == 2


def test_parse_subscription_body_rejects_unknown_content_gracefully() -> None:
    parsed = parse_subscription_body("not a proxy subscription")

    assert parsed["ok"] is False
    assert parsed["node_count"] == 0
    assert "未能解析" in parsed["advice"]
