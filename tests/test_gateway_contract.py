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


def test_responses_sse_sequence_numbers_are_strictly_increasing() -> None:
    async def collect() -> list[int]:
        sequence: list[int] = []
        async for chunk in _responses_sse(_split_sse_frames(), "GLM-5.3", "resp_test"):
            for line in chunk.decode().splitlines():
                if line.startswith("data: ") and not line.startswith("data: ["):
                    import json as _json
                    payload = _json.loads(line[6:])
                    if isinstance(payload, dict) and "sequence_number" in payload:
                        sequence.append(payload["sequence_number"])
        return sequence

    sequence = asyncio.run(collect())

    assert sequence, "至少应产出携带 sequence_number 的事件"
    assert sequence == sorted(sequence), "sequence_number 必须单调递增"
    assert len(set(sequence)) == len(sequence), "sequence_number 不得重复"


async def _tool_call_frames():
    """一个带 tool_use 块的 Anthropic SSE 流（模拟上游返回工具调用）。"""
    payload = (
        b'event: message_start\r\ndata: {"message":{"usage":{"input_tokens":5}}}\r\n\r\n'
        b'event: content_block_start\r\ndata: {"index":0,"content_block":{"type":"text","text":""}}\r\n\r\n'
        b'event: content_block_delta\r\ndata: {"index":0,"delta":{"type":"text_delta","text":"calling"}}\r\n\r\n'
        b'event: content_block_stop\r\ndata: {"index":0}\r\n\r\n'
        b'event: content_block_start\r\ndata: {"index":1,"content_block":{"type":"tool_use","id":"toolu_01","name":"shell","input":{}}}\r\n\r\n'
        b'event: content_block_delta\r\ndata: {"index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"cmd\\":"}}\r\n\r\n'
        b'event: content_block_delta\r\ndata: {"index":1,"delta":{"type":"input_json_delta","partial_json":"\\"ls\\"}"}}\r\n\r\n'
        b'event: content_block_stop\r\ndata: {"index":1}\r\n\r\n'
        b'event: message_delta\r\ndata: {"delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":9}}\r\n\r\n'
    )
    for index in range(0, len(payload), 7):
        yield payload[index:index + 7]


def test_responses_sse_converts_tool_use_to_function_call_events() -> None:
    async def collect() -> list[bytes]:
        return [chunk async for chunk in _responses_sse(_tool_call_frames(), "GLM-5.3", "resp_tool")]

    joined = b"".join(asyncio.run(collect())).decode()

    # function_call 输出项生命周期完整
    assert '"type": "function_call"' in joined
    assert "event: response.function_call_arguments.delta" in joined
    assert "event: response.function_call_arguments.done" in joined
    assert '"name": "shell"' in joined
    assert '"call_id": "toolu_01"' in joined
    # 参数分片按顺序到达并完成聚合
    assert '"delta": "{\\"cmd\\":"' in joined
    assert '"arguments": "{\\"cmd\\":\\"ls\\"}"' in joined
    # 文本部分保留
    assert '"delta": "calling"' in joined
    # completed 事件中携带工具调用与用量
    assert "event: response.completed" in joined
    assert '"input_tokens": 5' in joined
    assert '"output_tokens": 9' in joined


def test_openai_sse_streams_tool_calls() -> None:
    async def collect() -> list[bytes]:
        return [chunk async for chunk in _openai_sse(_tool_call_frames(), "GLM-5.3")]

    joined = b"".join(asyncio.run(collect())).decode()

    assert '"name": "shell"' in joined
    assert '"arguments": "{\\"cmd\\":"' in joined
    assert '"finish_reason": "tool_calls"' in joined
    assert joined.endswith("data: [DONE]\n\n")
