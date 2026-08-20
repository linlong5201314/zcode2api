from __future__ import annotations

import asyncio

import pytest

from app.routes.gateway import (
    _collect_anthropic,
    _detect_provider,
    _normalize_body,
    _openai_to_anthropic,
    _openai_sse,
    _responses_response,
    _responses_sse,
    _responses_to_anthropic,
    _validate_messages_body,
)


def test_normalize_body_maps_model_and_bridges_text_content() -> None:
    body = {
        "model": "zai/glm-5.3",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 4096,
    }

    normalized = _normalize_body(body)

    assert normalized["model"] == "GLM-5.3"
    assert normalized["messages"][0]["content"] == [{"type": "text", "text": "hello"}]
    assert normalized["thinking"]["type"] == "enabled"


def test_provider_prefix_is_detected_before_model_normalization() -> None:
    assert _detect_provider({"model": "bigmodel/glm-4"}, {}) == "bigmodel"


@pytest.mark.parametrize(
    ("payload", "needle"),
    [
        (None, "JSON object"),
        ({}, "model"),
        ({"model": "glm-5.3"}, "messages"),
        ({"model": "glm-5.3", "messages": []}, "at least one message"),
        (
            {"model": "glm-5.3", "messages": [{"role": "user", "content": "x"}], "max_tokens": "oops"},
            "max_tokens",
        ),
        (
            {"model": "glm-5.3", "messages": [{"role": "user", "content": "x"}], "temperature": "hot"},
            "temperature",
        ),
    ],
)
def test_validate_messages_body_rejects_malformed_payloads(payload, needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        _validate_messages_body(payload)


def test_openai_conversion_preserves_tools_and_tool_messages() -> None:
    body = {
        "model": "glm-5.2",
        "messages": [
            {"role": "user", "content": "find the weather"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "weather", "arguments": '{"city":"上海"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "晴朗"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "weather",
                    "description": "查询天气",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "tool_choice": "auto",
    }

    converted = _openai_to_anthropic(body)

    assert converted["tools"][0]["name"] == "weather"
    assert converted["tool_choice"] == {"type": "auto"}
    assert converted["messages"][1]["content"][0]["type"] == "tool_use"
    assert converted["messages"][2]["content"][0]["type"] == "tool_result"


def test_responses_conversion_supports_string_input_and_tools() -> None:
    converted = _responses_to_anthropic({
        "model": "glm-5.3",
        "instructions": "Be concise.",
        "input": "hello",
        "max_output_tokens": 2048,
        "tools": [{"type": "function", "name": "weather", "parameters": {"type": "object"}}],
    })

    assert converted["system"] == "Be concise."
    assert converted["messages"][0]["content"][0]["text"] == "hello"
    assert converted["max_tokens"] == 2048
    assert converted["tools"][0]["name"] == "weather"


def test_responses_response_shape_is_codex_friendly() -> None:
    payload = _responses_response(
        "GLM-5.3",
        "resp_test",
        "ok",
        "",
        {"input_tokens": 2, "output_tokens": 1},
    )

    assert payload["object"] == "response"
    assert payload["status"] == "completed"
    assert payload["output"][0]["content"][0]["type"] == "output_text"
    assert payload["output_text"] == "ok"


async def _split_sse_frames():
    payload = (
        b'event: message_start\r\ndata: {"message":{"usage":{"input_tokens":3}}}\r\n\r\n'
        b'event: content_block_delta\r\ndata: {"delta":{"type":"text_delta","text":"ok"}}\r\n\r\n'
        b'event: message_delta\r\ndata: {"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\r\n\r\n'
    )
    # Deliberately split in the middle of headers and JSON, as httpx can do
    # when a network read does not align with SSE frame boundaries.
    # One-byte chunks exercise boundaries inside both JSON and CRLF pairs.
    for index in range(0, len(payload), 1):
        yield payload[index:index + 1]


def test_sse_parser_buffers_frames_split_across_http_chunks() -> None:
    text, thinking, usage = asyncio.run(_collect_anthropic(_split_sse_frames()))

    assert text == "ok"
    assert thinking == ""
    assert usage["input_tokens"] == 3
    assert usage["output_tokens"] == 2
    assert usage["stop_reason"] == "end_turn"


def test_openai_sse_keeps_events_split_across_http_chunks() -> None:
    async def collect() -> list[bytes]:
        return [chunk async for chunk in _openai_sse(_split_sse_frames(), "GLM-5.3")]

    chunks = asyncio.run(collect())
    joined = b"".join(chunks).decode()

    assert '"content": "ok"' in joined
    assert '"finish_reason": "stop"' in joined
    assert joined.endswith("data: [DONE]\n\n")


def test_openai_sse_emits_usage_chunk_when_include_usage() -> None:
    async def collect() -> list[bytes]:
        return [
            chunk async for chunk in _openai_sse(_split_sse_frames(), "GLM-5.3", True)
        ]

    chunks = asyncio.run(collect())
    joined = b"".join(chunks).decode()

    assert '"finish_reason": "stop"' in joined
    # 末尾 usage chunk：choices 为空数组，携带完整用量
    assert '"choices": []' in joined
    assert '"prompt_tokens": 3' in joined
    assert '"completion_tokens": 2' in joined
    assert '"total_tokens": 5' in joined
    assert joined.endswith("data: [DONE]\n\n")


def test_openai_sse_omits_usage_without_include_usage() -> None:
    async def collect() -> list[bytes]:
        return [chunk async for chunk in _openai_sse(_split_sse_frames(), "GLM-5.3")]

    joined = b"".join(asyncio.run(collect())).decode()

    assert "prompt_tokens" not in joined
    assert '"choices": []' not in joined


def test_responses_sse_emits_standard_text_events() -> None:
    async def collect() -> list[bytes]:
        return [chunk async for chunk in _responses_sse(_split_sse_frames(), "GLM-5.3", "resp_test")]

    joined = b"".join(asyncio.run(collect())).decode()

    assert "event: response.created" in joined
    assert "event: response.output_text.delta" in joined
    assert '"delta": "ok"' in joined
    assert "event: response.completed" in joined
