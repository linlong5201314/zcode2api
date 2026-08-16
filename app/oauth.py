"""Z.AI OAuth 登录流程（授权码模式，与 zcode.z.ai 网页端同款）。

支持两种回跳方式：
1. 一键登录（loopback，参考 zcode2api 生态的实测方案）：redirect_uri 使用
   Z.AI 已注册的回环地址 http://127.0.0.1:{port}/oauth/callback，浏览器授权后
   自动跳回网关本身，code 被自动捕获兑换，全程无需手动复制；
2. 手动粘贴：redirect_uri 使用 zcode.z.ai 网页端登录页（该公开 client_id 唯一
   注册的网页回跳），用户从地址栏复制回跳网址 / code 交给网关兑换。

token 兑换响应 JSON 结构：
  {"code": 0, "data": {
     "token": "<Coding Plan JWT，上游对话用>",
     "zai": {"access_token": "...", "refresh_token": "..."},
     "user": {"user_id": "...", "email": "...", ...},
     "expires_in": ...}}
"""

from __future__ import annotations

import base64
import json
import secrets
import uuid
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from . import settings
from .proxy import upstream_proxy

AUTHORIZE_URL = "https://chat.z.ai/api/oauth/authorize"
TOKEN_URL = "https://zcode.z.ai/api/v1/oauth/token"
USERINFO_URL = "https://chat.z.ai/api/oauth/userinfo"
# zcode.z.ai 网页端内置的公开 client_id
CLIENT_ID = "client_P8X5CMWmlaRO9gyO-KSqtg"
LOOPBACK_PATH = "/oauth/callback"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def extract_code(raw: str) -> str:
    """容忍用户粘贴整个回跳 URL，从中提取 code 参数。"""
    raw = (raw or "").strip().strip('"\'')
    if "code=" in raw:
        try:
            query = parse_qs(urlparse(raw).query)
            return (query.get("code") or [""])[0]
        except Exception:  # noqa: BLE001
            return ""
    return raw


def display_name(user: dict | None) -> str:
    """从 OAuth 用户信息提取展示名：昵称 → 邮箱前缀 → 手机号。"""
    if not isinstance(user, dict):
        return ""

    def _pick(*keys: str) -> str:
        for key in keys:
            value = user.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    name = _pick("name", "username", "nickName", "nickname", "displayName")
    if name:
        return name
    email = _pick("email", "mail")
    if email and "@" in email:
        return email.split("@", 1)[0]
    phone = _pick("phone", "phone_number", "phoneNumber", "mobile")
    if phone:
        return f"账号{phone}"
    return ""


# ── loopback 回调登记（由 /oauth/callback 路由写入）─────────────────────────
_callbacks: dict[str, dict] = {}


def accept_callback(state: str, *, code: str = "", error: str = "",
                    error_description: str = "") -> bool:
    """接收浏览器回跳；返回是否存在对应的待处理登录流程。"""
    state = (state or "").strip()
    if not state or state not in _callbacks:
        return False
    _callbacks[state] = {
        "code": (code or "").strip(),
        "error": (error or "").strip(),
        "error_description": (error_description or "").strip(),
    }
    return True


def callback_result(state: str) -> dict | None:
    return _callbacks.get((state or "").strip())


class ZaiAuthFlow:
    def __init__(self, redirect_uri: str, state: str | None = None) -> None:
        self.redirect_uri = redirect_uri
        if state is not None:
            # loopback 模式：state 即流程号（随机 hex），回跳据此路由
            self.nonce = state
            self.state = state
            _callbacks.setdefault(state, {})
        else:
            # 手动模式：state 为 base64url(JSON)，字段与网页端实测一致
            self.nonce = str(uuid.uuid4())
            self.state = _b64url(
                json.dumps({
                    "nonce": self.nonce,
                    "app_return_to": redirect_uri,
                    "redirect_uri": redirect_uri,
                }, separators=(",", ":")).encode()
            )
        self.created: float = 0.0  # admin_api 写入，用于 TTL 清理

    # ── 构造 ────────────────────────────────────────────────────────────────
    @classmethod
    def loopback(cls, port: int) -> "ZaiAuthFlow":
        """一键登录：回跳到网关自身监听的回环地址。"""
        flow_id = secrets.token_hex(16)
        return cls(f"http://127.0.0.1:{port}{LOOPBACK_PATH}", state=flow_id)

    @classmethod
    def manual(cls) -> "ZaiAuthFlow":
        """手动粘贴：回跳到 zcode.z.ai 登录页，由用户复制 code。"""
        return cls(settings.OAUTH_REDIRECT_URI)

    @property
    def flow_id(self) -> str:
        return self.state if self.state in _callbacks else ""

    def authorize_url(self) -> str:
        return f"{AUTHORIZE_URL}?" + urlencode({
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "client_id": CLIENT_ID,
            "state": self.state,
        })

    # ── loopback 轮询 ───────────────────────────────────────────────────────
    async def poll(self) -> dict:
        """查询回跳是否到达；到达则自动兑换并返回结果 JSON。"""
        result = callback_result(self.state)
        if result is None:
            return {"status": "unknown"}
        if result.get("error"):
            _callbacks.pop(self.state, None)
            return {
                "status": "failed",
                "message": f"授权被拒绝: {result.get('error_description') or result['error']}",
            }
        if not result.get("code"):
            return {"status": "pending"}
        code = result["code"]
        _callbacks.pop(self.state, None)
        try:
            data = await self.exchange(code, self.state)
        except Exception as err:  # noqa: BLE001
            return {"status": "failed", "message": f"token 兑换失败: {err}"}
        user = data.get("user") or {}
        if not user.get("email") and not user.get("user_id"):
            # 兑换响应缺用户信息时再补查一次 userinfo
            access = ((data.get("zai") or {}).get("access_token")) or ""
            if access:
                user = await self._fetch_userinfo(access) or user
                data["user"] = user
        return {"status": "ready", "data": data}

    async def _fetch_userinfo(self, access_token: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=15, proxy=upstream_proxy()) as client:
                res = await client.get(
                    USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
                )
            if res.is_success:
                return (res.json() or {}).get("data") or {}
        except (httpx.HTTPError, ValueError):
            pass
        return {}

    # ── 兑换 ────────────────────────────────────────────────────────────────
    async def exchange(self, code: str, state: str) -> dict:
        """授权码 → {token(=Coding Plan JWT), zai.access_token, user, expires_in}。"""
        async with httpx.AsyncClient(timeout=30, proxy=upstream_proxy()) as client:
            res = await client.post(
                TOKEN_URL,
                headers={"Content-Type": "application/json"},
                json={
                    "provider": "zai",
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                    "state": state,
                },
            )
        body = {}
        try:
            body = res.json()
        except Exception:  # noqa: BLE001 - 非 JSON 响应退回 HTTP 状态码
            pass
        if not body and not res.is_success:
            res.raise_for_status()
        if body.get("code") not in (0, None, "0", 200, "200"):
            raise RuntimeError(body.get("msg") or f"token 兑换失败 (HTTP {res.status_code})")
        data = body.get("data") or {}
        if not data.get("token"):
            raise RuntimeError("返回数据中不含 Coding Plan JWT")
        return data

    async def exchange_api_key(self, access_token: str) -> str:
        """OAuth access_token → 业务 token → 机构/项目 → API Key。"""
        async with httpx.AsyncClient(timeout=30, proxy=upstream_proxy()) as client:
            login = await client.post(
                "https://api.z.ai/api/auth/z/login",
                headers={"Content-Type": "application/json"},
                json={"token": access_token},
            )
            login.raise_for_status()
            biz = (login.json().get("data") or {})
            biz_token = biz.get("access_token") or biz.get("accessToken")
            if not biz_token:
                raise RuntimeError("返回数据中不含业务凭证")

            info = await client.get(
                "https://api.z.ai/api/biz/customer/getCustomerInfo",
                headers={"Authorization": f"Bearer {biz_token}"},
            )
            info.raise_for_status()
            orgs = (info.json().get("data") or {}).get("organizations") or []
            org = next((o for o in orgs if "默认机构" in (o.get("organizationName") or "")), None) or (orgs[0] if orgs else None)
            if not org:
                raise RuntimeError("找不到可用的机构")
            projects = org.get("projects") or []
            proj = next((p for p in projects if "默认项目" in (p.get("projectName") or "")), None) or (projects[0] if projects else None)
            if not proj:
                raise RuntimeError("找不到可用的项目")

            org_id, proj_id = org["organizationId"], proj["projectId"]
            key_url = f"https://api.z.ai/api/biz/v1/organization/{org_id}/projects/{proj_id}/api_keys"

            keys_res = await client.get(key_url, headers={"Authorization": f"Bearer {biz_token}"})
            keys_res.raise_for_status()
            keys = keys_res.json().get("data") or []
            key_obj = next((k for k in keys if k.get("name") == "zcode-api-key"), None)
            if not key_obj:
                create = await client.post(
                    key_url,
                    headers={"Authorization": f"Bearer {biz_token}", "Content-Type": "application/json"},
                    json={"name": "zcode-api-key"},
                )
                create.raise_for_status()
                key_obj = create.json().get("data")

            api_key = (key_obj or {}).get("apiKey")
            if not api_key:
                raise RuntimeError("获取 API Key 失败")

            copy = await client.get(
                f"{key_url}/copy/{api_key}",
                headers={"Authorization": f"Bearer {biz_token}"},
            )
            copy.raise_for_status()
            secret_key = (copy.json().get("data") or {}).get("secretKey")
            if not secret_key:
                raise RuntimeError("未能解密 Secret Key")
        return f"{api_key}.{secret_key}"
