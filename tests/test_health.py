from __future__ import annotations

from fastapi.testclient import TestClient

from app import settings
from app.main import create_app


def test_health_and_readiness_endpoints_are_public() -> None:
    with TestClient(create_app()) as client:
        health = client.get("/healthz")
        ready = client.get("/readyz")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert ready.status_code in (200, 503)
    assert ready.json()["status"] in ("ready", "not_ready")


def test_gateway_rejects_non_object_and_missing_messages_with_400() -> None:
    with TestClient(create_app()) as client:
        non_object = client.post("/v1/messages", json=[])
        missing = client.post("/v1/messages", json={"model": "GLM-5.2"})
        bad_model = client.post(
            "/v1/messages",
            json={"model": 123, "messages": [{"role": "user", "content": "hi"}]},
        )

    assert non_object.status_code == 400
    assert missing.status_code == 400
    assert bad_model.status_code == 400
    assert non_object.json()["error"]["type"] == "invalid_request_error"


def test_gateway_enforces_request_size_before_json_parsing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "MAX_REQUEST_BYTES", 32)
    with TestClient(create_app()) as client:
        response = client.post("/v1/messages", content=b"{" + b"x" * 64)

    assert response.status_code == 413
