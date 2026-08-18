"""核心网关：兼容 Anthropic Messages 协议的 /v1/messages + OpenAI 兼容 /v1/chat/completions。

实现多账号轮询 + 额度用完自动换号 + 阿里无痕验证自动续期。
验证码求解失败时自动降级：不带验证参数直连 → API Key 回退端点。
"""

from __future__ import annotations

import asyncio
import json
import math
import secrets
import time

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import logs, settings
from ..agent import build_request
from ..auth_admin import verify_gateway_key
from ..captcha import captcha_manager
from ..models import Account, Status
from ..proxy import mask_proxy, upstream_proxy
from ..quota import fetch_quota
from ..store import store

router = APIRouter()

MAX_CAPTCHA_RETRIES = 3
MAX_ACCOUNT_ATTEMPTS = 5

# Z.AI 上游模型名大小写敏感
MODEL_NAME_MAP = {
    "glm-5.3": "GLM-5.3",
    "glm-5.2": "GLM-5.2",
    "glm-5-turbo": "GLM-5-Turbo",
    "glm-turbo": "GLM-5-Turbo",
    "glm-5.1": "GLM-5.1",
    "glm-4.7": "GLM-4.7",
}

# /v1/models 对外公布的可用模型（由 settings.ZCODE_MODELS 配置）。
AVAILABLE_MODELS = list(settings.MODEL_ALLOWLIST)

MAX_MODEL_NAME_LENGTH = 200
MAX_MESSAGES = 1_000
MAX_MESSAGE_CONTENT_LENGTH = 2_000_000
MAX_MAX_TOKENS = 1_000_000


def _fix_thinking(body: dict) -> dict:
    """GLM-5.3 强制思考模式：客户端未显式开启时自动注入官方 thinking 格式。"""
    model = str(body.get("model") or "")
    if "5.3" not in model:
        return body
    thinking = body.get("thinking")
    if not isinstance(thinking, dict) or thinking.get("type") != "enabled":
        thinking = {"type": "enabled", "budget_tokens": _thinking_budget(body)}
    elif not thinking.get("budget_tokens"):
        thinking = {**thinking, "budget_tokens": _thinking_budget(body)}
    body["thinking"] = thinking
    if "reasoning_effort" not in body:
        body["reasoning_effort"] = settings.REASONING_EFFORT
    return body


def _thinking_budget(body: dict) -> int:
    try:
        max_tokens = int(body.get("max_tokens") or 4096)
    except (TypeError, ValueError):
        max_tokens = 4096
    max_tokens = max(1024, max_tokens)
    try:
        configured = int(settings.THINKING_BUDGET_TOKENS or 8192)
    except (TypeError, ValueError):
        configured = 8192
    configured = max(1024, configured)
    return max(1024, min(configured, max_tokens - 1024))


def _validate_messages_body(body: object) -> dict:
    """Validate the small common subset required by both public protocols.

    FastAPI's JSON decoder only guarantees a Python value; callers can still
    send arrays, null, or deeply malformed message blocks.  Failing here keeps
    those cases as a deterministic 400 instead of an internal 500 while the
    request is being normalized or logged.
    """
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON object")

    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")
    if len(model) > MAX_MODEL_NAME_LENGTH:
        raise ValueError("model is too long")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must contain at least one message")
    if len(messages) > MAX_MESSAGES:
        raise ValueError("messages contains too many items")

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError(f"messages[{index}] must be an object")
        if message.get("role") not in ("user", "assistant"):
            raise ValueError(f"messages[{index}].role must be user or assistant")
        content = message.get("content")
        if not isinstance(content, (str, list)):
            raise ValueError(f"messages[{index}].content must be string or array")
        if isinstance(content, str) and len(content) > MAX_MESSAGE_CONTENT_LENGTH:
            raise ValueError(f"messages[{index}].content is too long")
        if isinstance(content, list):
            for block_index, block in enumerate(content):
                if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                    raise ValueError(
                        f"messages[{index}].content[{block_index}] must be a typed object"
                    )
            try:
                serialized_length = len(json.dumps(content, ensure_ascii=False))
            except (TypeError, ValueError) as err:
                raise ValueError(f"messages[{index}].content is not JSON serializable") from err
            if serialized_length > MAX_MESSAGE_CONTENT_LENGTH:
                raise ValueError(f"messages[{index}].content is too long")

    if "max_tokens" in body:
        max_tokens = body["max_tokens"]
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            raise ValueError("max_tokens must be an integer")
        if not 1 <= max_tokens <= MAX_MAX_TOKENS:
            raise ValueError("max_tokens must be between 1 and 1000000")
    if "stream" in body and not isinstance(body["stream"], bool):
        raise ValueError("stream must be a boolean")
    for field, minimum, maximum in (("temperature", 0.0, 2.0), ("top_p", 0.0, 1.0)):
        if field not in body:
            continue
        value = body[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field} must be a number")
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"{field} must be between {minimum:g} and {maximum:g}")
    return body

_NEXT_ACCOUNT = object()


# ── 转发结果信号 ─────────────────────────────────────────────────────────────
class _UpstreamError(Exception):
    """上游返回的最终错误响应（直接回传客户端）。"""

    def __init__(self, response) -> None:
        self.response = response


class _CaptchaRejected(Exception):
    """上游要求人机校验而当前路径无法满足，尝试下一条转发路径。"""


class _AccountBad(Exception):
    """账号不可用（已标记状态），切换下一个账号。"""


def _detect_provider(body: dict, headers) -> str:
    if not isinstance(body, dict):
        return "zai"
    model = body.get("model") or ""
    if (
        (isinstance(model, str) and model.startswith("bigmodel/"))
        or (headers or {}).get("x-provider") == "bigmodel"
    ):
        return "bigmodel"
    return "zai"


def _normalize_body(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON object")
    model = body.get("model")
    if isinstance(model, str) and "/" in model:
        model = "/".join(model.split("/")[1:])
    if isinstance(model, str):
        model = model.strip()
        model = MODEL_NAME_MAP.get(model.lower(), model)
        body["model"] = model

    body.setdefault("max_tokens", 4096)
    _fix_thinking(body)

    messages = body.get("messages")
    if isinstance(messages, list):
        bridged = []
        for msg in messages:
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                bridged.append({**msg, "content": [{"type": "text", "text": msg["content"]}]})
            else:
                bridged.append(msg)
        body["messages"] = bridged
    return body


def _is_captcha_error(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in (
        "captcha",
        "verify token",
        "verify failed",
        "human verification",
        "verifycode",
    ))


def _is_exhausted(status_code: int, text: str) -> bool:
    # 402 是最可靠的信号；部分 Z.AI 兼容端点返回结构化的余额错误并使用
    # 400/403，因此只匹配明确的“余额不足/资源包耗尽”短语，避免把普通
    # quota 参数校验误判为账号耗尽。
    if status_code == 402:
        return True
    low = (text or "").lower()
    return any(marker in low for marker in (
        "insufficient balance",
        "insufficient funds",
        "no resource package",
        "resource package exhausted",
        "quota exceeded",
        "余额不足",
        "额度已用完",
    ))


def _mark(account: Account, status_value: str, error: str | None = None,
          cooling_seconds: float | None = None) -> None:
    account.status = status_value
    account.last_error = error
    if status_value == Status.COOLING:
        account.cooling_until = time.time() + (cooling_seconds or settings.COOLING_SECONDS)
    store.update_account(account)


def _last_user_text(body: dict) -> str:
    for msg in reversed(body.get("messages") or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    return part.get("text", "")
    return ""


def _invalid_request(message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": str(message)[:500], "type": "invalid_request_error"}},
        status_code=400,
    )


def _safe_error_text(error: Exception, limit: int = 500) -> str:
    """Keep proxy credentials and oversized exception details out of logs/API errors."""
    text = str(error)
    proxy = upstream_proxy()
    if proxy:
        text = text.replace(proxy, mask_proxy(proxy))
    return text[:limit]


async def _read_json_body(request: Request) -> tuple[object | None, JSONResponse | None]:
    """Read and size-limit a request before handing it to protocol adapters."""
    declared = request.headers.get("content-length")
    try:
        if declared is not None and int(declared) > settings.MAX_REQUEST_BYTES:
            return None, JSONResponse(
                {"error": {"message": "请求体过大", "type": "invalid_request_error"}},
                status_code=413,
            )
    except (TypeError, ValueError):
        return None, _invalid_request("Content-Length 无效")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        if not isinstance(chunk, bytes):
            chunk = bytes(chunk)
        size += len(chunk)
        if size > settings.MAX_REQUEST_BYTES:
            return None, JSONResponse(
                {"error": {"message": "请求体过大", "type": "invalid_request_error"}},
                status_code=413,
            )
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        return json.loads(raw), None
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None, _invalid_request("请求体不是合法 JSON")


# ── 模型列表 ─────────────────────────────────────────────────────────────────
@router.get("/v1/models", dependencies=[Depends(verify_gateway_key)])
async def list_models():
    """列出可用模型（Anthropic /v1/models 风格）。"""
    return {
        "object": "list",
        "data": [
            {"id": i, "type": "model", "display_name": i, "created_at": "2025-01-01T00:00:00Z"}
            for i in AVAILABLE_MODELS
        ],
    }


# ── Anthropic /v1/messages ───────────────────────────────────────────────────
@router.post("/v1/messages", dependencies=[Depends(verify_gateway_key)])
async def messages(request: Request):
    body, error = await _read_json_body(request)
    if error is not None:
        return error

    incoming_headers = dict(request.headers)
    try:
        provider = _detect_provider(body, incoming_headers)
        body = _normalize_body(body)
        _validate_messages_body(body)
    except (TypeError, ValueError) as err:
        return _invalid_request(str(err))
    port = request.url.port or settings.PORT

    req_id = getattr(request.state, "request_id", None) or secrets.token_hex(3)
    logs.req(req_id, str(body.get("model") or "-"), bool(body.get("stream")), _last_user_text(body))
    return await _relay(req_id, body, incoming_headers, port, provider=provider)


# 别名：客户端 base_url 误带 /v1、或路径带尾部斜杠时兼容
@router.post("/v1/messages/", include_in_schema=False, dependencies=[Depends(verify_gateway_key)])
async def messages_slash(request: Request):
    return await messages(request)


@router.post("/v1/v1/messages", include_in_schema=False, dependencies=[Depends(verify_gateway_key)])
async def messages_v1_alias(request: Request):
    return await messages(request)


@router.get("/v1/v1/models", include_in_schema=False, dependencies=[Depends(verify_gateway_key)])
async def models_v1_alias():
    return await list_models()


# ── OpenAI 兼容 /v1/chat/completions ─────────────────────────────────────────
@router.post("/v1/chat/completions", dependencies=[Depends(verify_gateway_key)])
async def chat_completions(request: Request):
    body, error = await _read_json_body(request)
    if error is not None:
        return error

    incoming_headers = dict(request.headers)
    try:
        if not isinstance(body, dict):
            raise ValueError("请求体必须是 JSON object")
        if "stream" in body and not isinstance(body["stream"], bool):
            raise ValueError("stream must be a boolean")
        provider = _detect_provider(body, incoming_headers)
        anthropic_body = _openai_to_anthropic(body)
        anthropic_body["stream"] = True  # 内部一律流式，按需聚合
        anthropic_body = _normalize_body(anthropic_body)
        _validate_messages_body(anthropic_body)
    except (TypeError, ValueError) as err:
        return _invalid_request(str(err))
    port = request.url.port or settings.PORT

    req_id = getattr(request.state, "request_id", None) or secrets.token_hex(3)
    logs.req(req_id, str(anthropic_body.get("model") or "-"), bool(body.get("stream")),
             _last_user_text(anthropic_body))
    resp = await _relay(req_id, anthropic_body, incoming_headers, port, provider=provider)

    if not isinstance(resp, StreamingResponse) or resp.status_code >= 400:
        return _openai_error(resp)
    if body.get("stream"):
        return StreamingResponse(
            _openai_sse(resp.body_iterator, str(body.get("model") or "gpt-4o")),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )
    text, thinking, usage = await _collect_anthropic(resp.body_iterator)
    return JSONResponse(_openai_response(str(body.get("model") or "gpt-4o"), text, thinking, usage))


@router.post("/v1/v1/chat/completions", include_in_schema=False, dependencies=[Depends(verify_gateway_key)])
async def chat_completions_v1_alias(request: Request):
    return await chat_completions(request)


def _openai_to_anthropic(body: dict) -> dict:
    """OpenAI Chat Completions 请求体 → Anthropic Messages 请求体。"""
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON object")
    model = body.get("model") or "GLM-5.3"
    messages: list[dict] = []
    system_parts: list[str] = []
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role in ("system", "developer"):
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text")
                        if isinstance(text, str):
                            system_parts.append(text)
            continue
        if role == "tool":
            tool_id = str(msg.get("tool_call_id") or "")
            if not tool_id:
                continue
            result_content = content if isinstance(content, (str, list)) else str(content or "")
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result_content,
                }],
            })
            continue
        if role not in ("user", "assistant"):
            continue

        blocks: list[dict] = []
        if isinstance(content, str):
            blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    blocks.append({"type": "text", "text": part.get("text") or ""})
                elif part.get("type") == "image_url":
                    image_url = part.get("image_url") or {}
                    url = image_url.get("url") if isinstance(image_url, dict) else ""
                    if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
                        header, data = url.split(";base64,", 1)
                        mime = header[5:] or "image/png"
                        blocks.append({
                            "type": "image",
                            "source": {"type": "base64", "media_type": mime, "data": data},
                        })
        if role == "assistant":
            for call in msg.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                if not isinstance(function, dict) or not function.get("name"):
                    continue
                raw_arguments = function.get("arguments") or "{}"
                try:
                    tool_input = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                except (TypeError, ValueError):
                    tool_input = {"_raw_arguments": str(raw_arguments)}
                if not isinstance(tool_input, dict):
                    tool_input = {"value": tool_input}
                blocks.append({
                    "type": "tool_use",
                    "id": str(call.get("id") or secrets.token_hex(8)),
                    "name": str(function["name"]),
                    "input": tool_input,
                })
        if blocks:
            item = {"role": role, "content": blocks}
            if role == "assistant" and msg.get("name"):
                item["name"] = str(msg["name"])
            messages.append(item)

    out: dict = {"model": model, "messages": messages}
    if system_parts:
        out["system"] = "\n\n".join(system_parts)
    if "max_tokens" in body:
        out["max_tokens"] = body["max_tokens"]
    elif "max_completion_tokens" in body:
        out["max_tokens"] = body["max_completion_tokens"]
    else:
        out["max_tokens"] = 4096
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]
    stop = body.get("stop")
    if stop:
        out["stop_sequences"] = stop if isinstance(stop, list) else [stop]

    tools = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(function, dict) or not function.get("name"):
            continue
        tools.append({
            "name": str(function["name"]),
            "description": str(function.get("description") or ""),
            "input_schema": function.get("parameters")
            if isinstance(function.get("parameters"), dict)
            else {"type": "object", "properties": {}},
        })
    if tools:
        out["tools"] = tools

    choice = body.get("tool_choice")
    if choice == "auto":
        out["tool_choice"] = {"type": "auto"}
    elif choice == "required":
        out["tool_choice"] = {"type": "any"}
    elif isinstance(choice, dict):
        function = choice.get("function") or {}
        if isinstance(function, dict) and function.get("name"):
            out["tool_choice"] = {"type": "tool", "name": str(function["name"])}
    return out


def _openai_finish(stop_reason: str | None) -> str:
    if stop_reason == "max_tokens":
        return "length"
    if stop_reason == "tool_use":
        return "tool_calls"
    return "stop"


def _usage_count(value) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _openai_response(model: str, text: str, thinking: str, usage: dict) -> dict:
    now = int(time.time())
    input_tokens = _usage_count(usage.get("input_tokens"))
    output_tokens = _usage_count(usage.get("output_tokens"))
    message: dict = {"role": "assistant", "content": text}
    if thinking:
        message["reasoning_content"] = thinking  # DeepSeek 风格，兼容支持思考展示的客户端
    tool_calls = usage.get("tool_calls") or []
    if tool_calls:
        message["content"] = None
        message["tool_calls"] = [
            {
                "id": call.get("id") or f"call_{secrets.token_hex(8)}",
                "type": "function",
                "function": {
                    "name": call.get("name") or "tool",
                    "arguments": json.dumps(call.get("input") or {}, ensure_ascii=False),
                },
            }
            for call in tool_calls
        ]
    return {
        "id": f"chatcmpl-{secrets.token_hex(12)}",
        "object": "chat.completion",
        "created": now,
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": _openai_finish(usage.get("stop_reason")),
        }],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


def _openai_error(resp) -> JSONResponse:
    """把网关/上游的 Anthropic 风格错误转成 OpenAI 风格错误。"""
    status = getattr(resp, "status_code", 500)
    message = "upstream error"
    if isinstance(resp, JSONResponse):
        try:
            payload = json.loads(resp.body)
            message = (payload.get("error") or {}).get("message") or payload.get("detail") or message
        except (json.JSONDecodeError, ValueError):
            pass
    return JSONResponse(
        {"error": {"message": str(message)[:500], "type": "api_error", "code": status}},
        status_code=status,
    )


async def _collect_anthropic(body_iter) -> tuple[str, str, dict]:
    """聚合 Anthropic SSE 流：返回 (正文, 思考内容, 用量信息)。"""
    parts: list[str] = []
    thinking: list[str] = []
    usage: dict = {}
    tool_calls: list[dict] = []
    active_tool: dict | None = None
    async for event, data in _iter_anthropic_events(body_iter):
        if event == "content_block_start":
            block = data.get("content_block") or {}
            if block.get("type") == "tool_use":
                active_tool = {
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": block.get("input") if isinstance(block.get("input"), dict) else {},
                    "_json": "",
                }
                tool_calls.append(active_tool)
            continue
        delta = data.get("delta") or {}
        if event == "content_block_delta":
            if delta.get("type") == "text_delta":
                parts.append(delta.get("text") or "")
            elif delta.get("type") == "thinking_delta":
                thinking.append(delta.get("thinking") or "")
            elif delta.get("type") == "input_json_delta" and active_tool is not None:
                active_tool["_json"] += delta.get("partial_json") or ""
        elif event == "message_start":
            usage["input_tokens"] = ((data.get("message") or {}).get("usage") or {}).get("input_tokens", 0)
        elif event == "message_delta":
            usage["output_tokens"] = (data.get("usage") or {}).get("output_tokens", 0)
            usage["stop_reason"] = (data.get("delta") or {}).get("stop_reason")
    for call in tool_calls:
        raw_input = call.pop("_json", "")
        if raw_input:
            try:
                parsed = json.loads(raw_input)
                if isinstance(parsed, dict):
                    call["input"] = parsed
            except (TypeError, ValueError):
                pass
    if tool_calls:
        usage["tool_calls"] = tool_calls
    return "".join(parts), "".join(thinking), usage


async def _openai_sse(body_iter, model: str):
    """把 Anthropic SSE 流转换为 OpenAI chat.completion.chunk 流。"""
    now = int(time.time())
    cid = f"chatcmpl-{secrets.token_hex(12)}"
    stop_reason: str | None = None
    first = True
    tool_indices: dict[int, int] = {}
    next_tool_index = 0

    def _chunk(delta: dict, finish: str | None) -> bytes:
        payload = {
            "id": cid, "object": "chat.completion.chunk", "created": now, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()

    async for event, data in _iter_anthropic_events(body_iter):
        if event == "content_block_start":
            block = data.get("content_block") or {}
            if block.get("type") == "tool_use":
                source_index = int(data.get("index") or 0)
                tool_index = next_tool_index
                next_tool_index += 1
                tool_indices[source_index] = tool_index
                if first:
                    first = False
                    yield _chunk({"role": "assistant", "content": None}, None)
                yield _chunk({
                    "tool_calls": [{
                        "index": tool_index,
                        "id": block.get("id") or f"call_{secrets.token_hex(8)}",
                        "type": "function",
                        "function": {"name": block.get("name") or "", "arguments": ""},
                    }]
                }, None)
        elif event == "content_block_delta":
            delta = data.get("delta") or {}
            if delta.get("type") == "text_delta":
                if first:
                    first = False
                    yield _chunk({"role": "assistant", "content": ""}, None)
                yield _chunk({"content": delta.get("text") or ""}, None)
            elif delta.get("type") == "thinking_delta":
                if first:
                    first = False
                    yield _chunk({"role": "assistant", "content": ""}, None)
                yield _chunk({"reasoning_content": delta.get("thinking") or ""}, None)
            elif delta.get("type") == "input_json_delta":
                source_index = int(data.get("index") or 0)
                tool_index = tool_indices.get(source_index, 0)
                yield _chunk({
                    "tool_calls": [{
                        "index": tool_index,
                        "function": {"arguments": delta.get("partial_json") or ""},
                    }]
                }, None)
        elif event == "message_delta":
            stop_reason = (data.get("delta") or {}).get("stop_reason") or stop_reason

    if first:  # 上游没有任何文本增量（罕见），补发 role 块
        yield _chunk({"role": "assistant", "content": ""}, None)
    yield _chunk({}, _openai_finish(stop_reason) or "stop")
    yield b"data: [DONE]\n\n"


def _parse_anthropic_sse(text: str):
    """解析 Anthropic SSE 片段，产出 (event, data) 序列。"""
    for block in text.split("\n\n"):
        event = data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if event and data:
            try:
                payload = json.loads(data)
                if isinstance(payload, dict):
                    yield event, payload
            except (json.JSONDecodeError, ValueError):
                continue


async def _iter_anthropic_events(body_iter):
    """Yield complete SSE events even when HTTP chunks split a frame."""
    buffer = ""
    async for raw in body_iter:
        if isinstance(raw, bytes):
            buffer += raw.decode("utf-8", "ignore")
        else:
            buffer += str(raw)
        # Keep both LF and CRLF delimiters in the buffer.  Searching the raw
        # text avoids losing an event when a network chunk ends between the
        # `\r` and `\n` of a CRLF sequence.
        while True:
            candidates = [
                (buffer.find("\n\n"), 2),
                (buffer.find("\r\n\r\n"), 4),
            ]
            candidates = [(index, width) for index, width in candidates if index >= 0]
            if not candidates:
                break
            index, width = min(candidates)
            block, buffer = buffer[:index], buffer[index + width:]
            for event in _parse_anthropic_sse(block + "\n\n"):
                yield event
    if buffer.strip():
        for event in _parse_anthropic_sse(buffer):
            yield event


# ── 转发核心 ─────────────────────────────────────────────────────────────────
async def _relay(req_id: str, body: dict, incoming_headers: dict, port: int,
                 provider: str | None = None):
    """选账号并按降级链转发，返回 Response。"""
    provider = provider or _detect_provider(body, incoming_headers)
    payload = json.dumps(body).encode("utf-8")
    tried: set[str] = set()
    reasons: list[str] = []

    for _ in range(MAX_ACCOUNT_ATTEMPTS):
        account = store.select(provider, skip_ids=tried)
        if account is None:
            break
        tried.add(account.id)
        needs_captcha = provider == "zai" and account.mode == "jwt"

        result = await _try_account(req_id, account, body, payload, incoming_headers, port,
                                    needs_captcha, reasons)
        if result is _NEXT_ACCOUNT:
            continue
        return result

    detail = "；".join(dict.fromkeys(reasons)) if reasons else ""  # 去重保序，展示完整失败链
    if len(detail) > 400:
        detail = detail[:400] + "…"
    logs.req_err(req_id, f"无可用账号 / 额度均已耗尽（{detail}）")
    return JSONResponse(
        {"error": {"message": f"所有账号均不可用或额度已用完，请在后台检查账号状态"
                              + (f"（最近失败原因: {detail}）" if detail else ""),
                   "type": "no_available_account"}},
        status_code=503,
    )


async def _try_account(req_id, account, body, payload, incoming_headers, port, needs_captcha,
                       reasons: list[str] | None = None):
    """尝试用单个账号转发。

    降级链：JWT + 验证码 → JWT 不带验证参数直连 → API Key 回退端点（无需验证码）。
    """
    reasons = reasons if reasons is not None else []

    def _note(msg: str) -> None:
        reasons.append(f"{account.name}: {msg}")

    if needs_captcha:
        try:
            verify_param = await captcha_manager.get_verify_param(port)
        except Exception as err:  # noqa: BLE001
            verify_param = None
            _note(f"人机校验求解失败: {_safe_error_text(err, 180)}")
        try:
            return await _forward_once(req_id, account, body, payload, incoming_headers,
                                       verify_param, use_fallback=False, retries=MAX_CAPTCHA_RETRIES)
        except _CaptchaRejected:
            _note("带验证码请求被上游拒绝")
        except _UpstreamError as err:
            return err.response
        except _AccountBad:
            _note(f"账号不可用: {account.last_error or account.status}")
            return _NEXT_ACCOUNT

    # JWT 不带验证参数直连（上游可能已放宽人机校验）
    try:
        return await _forward_once(req_id, account, body, payload, incoming_headers,
                                   None, use_fallback=False, retries=1)
    except _CaptchaRejected:
        _note("不带验证码被上游拒绝（captcha required）")
    except _UpstreamError as err:
        return err.response
    except _AccountBad:
        _note(f"账号不可用: {account.last_error or account.status}")
        return _NEXT_ACCOUNT

    # API Key 回退端点（api.z.ai），无需验证码
    if account.api_key:
        try:
            return await _forward_once(req_id, account, body, payload, incoming_headers,
                                       None, use_fallback=True, retries=1)
        except _UpstreamError as err:
            return err.response
        except _AccountBad:
            _note(f"API Key 回退失败: {account.last_error or account.status}")
            return _NEXT_ACCOUNT
        except _CaptchaRejected:  # noqa: BLE001
            _note("API Key 回退被拒（captcha required）")
    else:
        _note("无 API Key 可回退")

    logs.req_err(req_id, f"账号 {account.name} 所有转发路径均失败")
    return _NEXT_ACCOUNT


async def _forward_once(req_id, account, body, payload, incoming_headers, verify_param, use_fallback, retries):
    """单条路径转发。成功 → StreamingResponse；普通上游错误 → _UpstreamError；
    验证码被拒 → _CaptchaRejected；账号不可用 → _AccountBad。"""
    for attempt in range(retries):
        try:
            url, headers = build_request(account, body, verify_param, incoming_headers, use_fallback)
        except RuntimeError as err:
            _mark(account, Status.INVALID, str(err))
            logs.warn(req_id, f"账号 {account.name} 凭证无效，切换下一个")
            raise _AccountBad from err

        try:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=30.0, read=None, write=120.0, pool=30.0),
                proxy=upstream_proxy(),
            )
        except (TypeError, ValueError) as err:
            _mark(account, Status.COOLING, f"上游客户端配置无效: {type(err).__name__}")
            raise _AccountBad from err
        try:
            cm = client.stream("POST", url, headers=headers, content=payload)
            resp = await cm.__aenter__()
        except (httpx.HTTPError, OSError, ValueError, RuntimeError) as err:
            await client.aclose()
            _mark(account, Status.COOLING, f"连接失败: {_safe_error_text(err)}")
            logs.warn(req_id, f"账号 {account.name} 连接失败，切换下一个")
            raise _AccountBad from err

        status_code = resp.status_code

        if status_code >= 400:
            response_headers = {
                key: resp.headers[key]
                for key in ("retry-after", "x-request-id", "request-id")
                if key in resp.headers
            }
            try:
                text = (await resp.aread()).decode("utf-8", "ignore")
            except (httpx.HTTPError, OSError) as err:
                text = f"上游响应读取失败: {type(err).__name__}"
            finally:
                await cm.__aexit__(None, None, None)
                await client.aclose()

            if _is_captcha_error(text) and status_code in (400, 401, 403):
                if verify_param:
                    captcha_manager.invalidate()
                    logs.warn(req_id, f"账号 {account.name} 验证码失效，刷新重试")
                    continue  # 同路径重试（换新验证码）
                raise _CaptchaRejected

            if status_code in (401, 403):
                _mark(account, Status.INVALID, f"鉴权失败 HTTP {status_code}")
                logs.warn(req_id, f"账号 {account.name} 鉴权失败 {status_code}，切换下一个")
                raise _AccountBad

            if status_code == 429:
                # 限流多为瞬时（RPM 峰值），短冷却即可，避免单账号被长时间雪藏
                _mark(account, Status.COOLING, f"上游限流 429", cooling_seconds=60)
                logs.warn(req_id, f"账号 {account.name} 被限流 429，切换下一个")
                raise _AccountBad

            if _is_exhausted(status_code, text):
                _mark(account, Status.EXHAUSTED, "额度已用完")
                logs.warn(req_id, f"账号 {account.name} 额度用完，切换下一个")
                asyncio.create_task(_safe_refresh(account))
                raise _AccountBad

            # 其它错误：直接回传客户端
            account.fail_count += 1
            store.update_account(account)
            logs.req_err(req_id, f"上游错误 HTTP {status_code}（账号 {account.name}）")
            error_response = JSONResponse(
                _safe_json(text) or {"error": {"message": text[:500], "type": "upstream_error"}},
                status_code=status_code,
                headers=response_headers,
            )
            raise _UpstreamError(error_response)

        # 成功：记录用量并流式透传
        account.use_count += 1
        account.last_used_at = time.time()
        if account.status in (Status.COOLING, Status.EXHAUSTED):
            account.status = Status.ACTIVE
        store.update_account(account)
        asyncio.create_task(_safe_refresh(account))

        content_type = resp.headers.get("content-type", "application/json")

        async def _body_iter():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
                logs.req_ok(req_id)
            except Exception as err:  # noqa: BLE001
                logs.req_err(req_id, f"流传输中断: {err}")
            finally:
                await cm.__aexit__(None, None, None)
                await client.aclose()

        out_headers = {"Cache-Control": "no-cache"}
        for key in ("x-request-id", "request-id"):
            if key in resp.headers:
                out_headers[key] = resp.headers[key]
        return StreamingResponse(_body_iter(), status_code=status_code,
                                 media_type=content_type, headers=out_headers)

    raise _CaptchaRejected  # 验证码重试次数用尽


def _safe_json(text: str):
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


async def _safe_refresh(account: Account) -> None:
    try:
        if account.provider == "zai" and account.mode == "jwt":
            await fetch_quota(account)
    except Exception:  # noqa: BLE001
        pass
