"""核心网关：兼容 Anthropic Messages 协议的 /v1/messages + OpenAI 兼容 /v1/chat/completions。

实现多账号轮询 + 额度用完自动换号 + 阿里无痕验证自动续期。
验证码求解失败时自动降级：不带验证参数直连 → API Key 回退端点。
"""

from __future__ import annotations

import asyncio
import json
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

# /v1/models 对外公布的可用模型（GLM-5.3 排第一作为客户端默认）
AVAILABLE_MODELS = ["GLM-5.3", "GLM-5.2", "GLM-5-Turbo"]

# 命中以下信号则认为账号额度用完
_EXHAUST_KEYWORDS = ("quota", "insufficient", "balance", "exhaust", "额度", "余额不足")

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
    model = body.get("model") or ""
    if model.startswith("bigmodel/") or headers.get("x-provider") == "bigmodel":
        return "bigmodel"
    return "zai"


def _normalize_body(body: dict) -> dict:
    model = body.get("model")
    if isinstance(model, str) and "/" in model:
        model = "/".join(model.split("/")[1:])
    if isinstance(model, str):
        model = MODEL_NAME_MAP.get(model.lower(), model)
        body["model"] = model

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
    low = text.lower()
    return "captcha" in low or "verify token" in low or "verify failed" in low


def _is_exhausted(status_code: int, text: str) -> bool:
    if status_code in (402,):
        return True
    low = text.lower()
    return any(k in low for k in _EXHAUST_KEYWORDS)


def _mark(account: Account, status_value: str, error: str | None = None) -> None:
    account.status = status_value
    account.last_error = error
    if status_value == Status.COOLING:
        account.cooling_until = time.time() + settings.COOLING_SECONDS
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
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": {"message": "请求体不是合法 JSON", "type": "invalid_request"}}, status_code=400)

    incoming_headers = dict(request.headers)
    body = _normalize_body(body)
    port = request.url.port or settings.PORT

    req_id = secrets.token_hex(3)
    logs.req(req_id, str(body.get("model") or "-"), bool(body.get("stream")), _last_user_text(body))
    return await _relay(req_id, body, incoming_headers, port)


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
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": {"message": "请求体不是合法 JSON", "type": "invalid_request"}}, status_code=400)

    incoming_headers = dict(request.headers)
    anthropic_body = _openai_to_anthropic(body)
    anthropic_body["stream"] = True  # 内部一律流式，按需聚合
    anthropic_body = _normalize_body(anthropic_body)
    port = request.url.port or settings.PORT

    req_id = secrets.token_hex(3)
    logs.req(req_id, str(anthropic_body.get("model") or "-"), bool(body.get("stream")),
             _last_user_text(anthropic_body))
    resp = await _relay(req_id, anthropic_body, incoming_headers, port)

    if not isinstance(resp, StreamingResponse) or resp.status_code >= 400:
        return _openai_error(resp)
    if body.get("stream"):
        return StreamingResponse(
            _openai_sse(resp.body_iterator, str(body.get("model") or "gpt-4o")),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )
    text, usage = await _collect_anthropic(resp.body_iterator)
    return JSONResponse(_openai_response(str(body.get("model") or "gpt-4o"), text, usage))


@router.post("/v1/v1/chat/completions", include_in_schema=False, dependencies=[Depends(verify_gateway_key)])
async def chat_completions_v1_alias(request: Request):
    return await chat_completions(request)


def _openai_to_anthropic(body: dict) -> dict:
    """OpenAI Chat Completions 请求体 → Anthropic Messages 请求体。"""
    model = body.get("model") or "GLM-5.3"
    messages: list[dict] = []
    system_parts: list[str] = []
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        system_parts.append(part.get("text") or "")
            continue
        if role not in ("user", "assistant"):
            continue
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            blocks = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    blocks.append({"type": "text", "text": part.get("text") or ""})
                elif part.get("type") == "image_url":
                    url = ((part.get("image_url") or {}).get("url") or "")
                    if url.startswith("data:") and ";base64," in url:
                        header, data = url.split(";base64,", 1)
                        mime = header[5:] or "image/png"
                        blocks.append({
                            "type": "image",
                            "source": {"type": "base64", "media_type": mime, "data": data},
                        })
            content = blocks
        else:
            content = []
        if content:
            messages.append({"role": role, "content": content})

    out: dict = {"model": model, "messages": messages}
    if system_parts:
        out["system"] = "\n\n".join(system_parts)
    out["max_tokens"] = body.get("max_tokens") or body.get("max_completion_tokens") or 4096
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]
    stop = body.get("stop")
    if stop:
        out["stop_sequences"] = stop if isinstance(stop, list) else [stop]
    return out


def _openai_finish(stop_reason: str | None) -> str:
    if stop_reason == "max_tokens":
        return "length"
    if stop_reason == "tool_use":
        return "tool_calls"
    return "stop"


def _openai_response(model: str, text: str, usage: dict) -> dict:
    now = int(time.time())
    return {
        "id": f"chatcmpl-{secrets.token_hex(12)}",
        "object": "chat.completion",
        "created": now,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": _openai_finish(usage.get("stop_reason")),
        }],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
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


async def _collect_anthropic(body_iter) -> tuple[str, dict]:
    """聚合 Anthropic SSE 流：返回 (文本, {input_tokens, output_tokens, stop_reason})。"""
    parts: list[str] = []
    usage: dict = {}
    async for raw in body_iter:
        for event in _parse_anthropic_sse(raw.decode("utf-8", "ignore")):
            if event[0] == "content_block_delta" and (event[1].get("delta") or {}).get("type") == "text_delta":
                parts.append((event[1].get("delta") or {}).get("text") or "")
            elif event[0] == "message_start":
                usage["input_tokens"] = ((event[1].get("message") or {}).get("usage") or {}).get("input_tokens", 0)
            elif event[0] == "message_delta":
                usage["output_tokens"] = (event[1].get("usage") or {}).get("output_tokens", 0)
                usage["stop_reason"] = (event[1].get("delta") or {}).get("stop_reason")
    return "".join(parts), usage


async def _openai_sse(body_iter, model: str):
    """把 Anthropic SSE 流转换为 OpenAI chat.completion.chunk 流。"""
    now = int(time.time())
    cid = f"chatcmpl-{secrets.token_hex(12)}"
    stop_reason: str | None = None
    first = True

    def _chunk(delta: dict, finish: str | None) -> bytes:
        payload = {
            "id": cid, "object": "chat.completion.chunk", "created": now, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()

    async for raw in body_iter:
        for event, data in _parse_anthropic_sse(raw.decode("utf-8", "ignore")):
            if event == "content_block_delta" and (data.get("delta") or {}).get("type") == "text_delta":
                if first:
                    first = False
                    yield _chunk({"role": "assistant", "content": ""}, None)
                yield _chunk({"content": (data.get("delta") or {}).get("text") or ""}, None)
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
                yield event, json.loads(data)
            except (json.JSONDecodeError, ValueError):
                continue


# ── 转发核心 ─────────────────────────────────────────────────────────────────
async def _relay(req_id: str, body: dict, incoming_headers: dict, port: int):
    """选账号并按降级链转发，返回 Response。"""
    provider = _detect_provider(body, incoming_headers)
    payload = json.dumps(body).encode("utf-8")
    tried: set[str] = set()

    for _ in range(MAX_ACCOUNT_ATTEMPTS):
        account = store.select(provider, skip_ids=tried)
        if account is None:
            break
        tried.add(account.id)
        needs_captcha = provider == "zai" and account.mode == "jwt"

        result = await _try_account(req_id, account, body, payload, incoming_headers, port, needs_captcha)
        if result is _NEXT_ACCOUNT:
            continue
        return result

    logs.req_err(req_id, "无可用账号 / 额度均已耗尽")
    return JSONResponse(
        {"error": {"message": "所有账号均不可用或额度已用完，请在后台检查账号状态", "type": "no_available_account"}},
        status_code=503,
    )


async def _try_account(req_id, account, body, payload, incoming_headers, port, needs_captcha):
    """尝试用单个账号转发。

    降级链：JWT + 验证码 → JWT 不带验证参数直连 → API Key 回退端点（无需验证码）。
    """
    if needs_captcha:
        try:
            verify_param = await captcha_manager.get_verify_param(port)
        except Exception as err:  # noqa: BLE001
            verify_param = None
            logs.warn(req_id, f"人机校验求解失败，尝试不带验证参数直连: {err}")
        try:
            return await _forward_once(req_id, account, body, payload, incoming_headers,
                                       verify_param, use_fallback=False, retries=MAX_CAPTCHA_RETRIES)
        except _CaptchaRejected:
            logs.warn(req_id, f"账号 {account.name} 验证码连续失败，尝试不带验证参数直连")
        except _UpstreamError as err:
            return err.response
        except _AccountBad:
            return _NEXT_ACCOUNT

    # JWT 不带验证参数直连（上游可能已放宽人机校验）
    try:
        return await _forward_once(req_id, account, body, payload, incoming_headers,
                                   None, use_fallback=False, retries=1)
    except _CaptchaRejected:
        pass
    except _UpstreamError as err:
        return err.response
    except _AccountBad:
        return _NEXT_ACCOUNT

    # API Key 回退端点（api.z.ai），无需验证码
    if account.api_key:
        try:
            return await _forward_once(req_id, account, body, payload, incoming_headers,
                                       None, use_fallback=True, retries=1)
        except _UpstreamError as err:
            return err.response
        except _AccountBad:
            return _NEXT_ACCOUNT
        except _CaptchaRejected:  # noqa: BLE001
            pass

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

        client = httpx.AsyncClient(timeout=httpx.Timeout(connect=30.0, read=None, write=120.0, pool=30.0))
        cm = client.stream("POST", url, headers=headers, content=payload)
        try:
            resp = await cm.__aenter__()
        except httpx.HTTPError as err:
            await client.aclose()
            _mark(account, Status.COOLING, f"连接失败: {err}")
            logs.warn(req_id, f"账号 {account.name} 连接失败，切换下一个")
            raise _AccountBad from err

        status_code = resp.status_code

        if status_code >= 400:
            text = (await resp.aread()).decode("utf-8", "ignore")
            await cm.__aexit__(None, None, None)
            await client.aclose()

            if status_code == 403 and _is_captcha_error(text):
                if verify_param:
                    captcha_manager.invalidate()
                    logs.warn(req_id, f"账号 {account.name} 验证码失效，刷新重试")
                    continue  # 同路径重试（换新验证码）
                raise _CaptchaRejected

            if _is_exhausted(status_code, text):
                _mark(account, Status.EXHAUSTED, "额度已用完")
                logs.warn(req_id, f"账号 {account.name} 额度用完，切换下一个")
                asyncio.create_task(_safe_refresh(account))
                raise _AccountBad

            if status_code in (401, 403):
                _mark(account, Status.INVALID, f"鉴权失败 HTTP {status_code}")
                logs.warn(req_id, f"账号 {account.name} 鉴权失败 {status_code}，切换下一个")
                raise _AccountBad

            if status_code == 429:
                _mark(account, Status.COOLING, "上游限流 429")
                logs.warn(req_id, f"账号 {account.name} 被限流 429，切换下一个")
                raise _AccountBad

            # 其它错误：直接回传客户端
            account.fail_count += 1
            store.update_account(account)
            logs.req_err(req_id, f"上游错误 HTTP {status_code}（账号 {account.name}）")
            raise _UpstreamError(JSONResponse(
                _safe_json(text) or {"error": {"message": text[:500], "type": "upstream_error"}},
                status_code=status_code,
            ))

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
        return StreamingResponse(_body_iter(), status_code=status_code,
                                 media_type=content_type, headers=out_headers)

    raise _CaptchaRejected  # 验证码重试次数用尽


def _safe_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


async def _safe_refresh(account: Account) -> None:
    try:
        if account.provider == "zai" and account.mode == "jwt":
            await fetch_quota(account)
    except Exception:  # noqa: BLE001
        pass
