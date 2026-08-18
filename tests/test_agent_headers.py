from __future__ import annotations

from app.agent import build_request
from app.models import Account


def test_build_request_does_not_forward_credentials_or_override_generated_headers() -> None:
    account = Account.create("zai", "demo", "header.payload.signature")
    url, headers = build_request(
        account,
        {"model": "GLM-5.2", "messages": [], "stream": True},
        None,
        {
            "authorization": "Bearer attacker-token",
            "cookie": "session=secret",
            "anthropic-version": "old",
            "x-zcode-injected": "evil",
            "x-client-request-id": "client-1",
        },
    )

    assert url.endswith("/v1/messages")
    assert headers["Authorization"] == f"Bearer {account.jwt_token}"
    assert headers["anthropic-version"] == "2023-06-01"
    assert "cookie" not in {key.lower() for key in headers}
    assert "x-zcode-injected" not in {key.lower() for key in headers}
    assert headers.get("x-client-request-id") == "client-1"
    assert headers.get("accept") == "text/event-stream"
