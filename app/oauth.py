"""Z.AI OAuth 登录流程（授权码模式，与 zcode.z.ai 网页端同款）。

真实流程（逆向自 zcode.z.ai 前端）：
1. 构造授权链接 https://chat.z.ai/api/oauth/authorize?...，用户在浏览器登录 Z.AI；
2. 登录成功后浏览器携带 code/state 重定向回 redirect_uri。该公开 client_id 仅注册了
   https://zcode.z.ai/login（实测网页端发起登录时使用的回跳地址），其他地址会被
   Z.AI 以「此客户端未注册重定向 URI」拒绝；
3. 服务端 POST https://zcode.z.ai/api/v1/oauth/token 兑换凭证：
   data.token = Coding Plan JWT（上游对话用），data.zai.access_token = 业务 token。
"""

from __future__ import annotations

import base64
import json
import uuid
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

AUTHORIZE_URL = "https://chat.z.ai/api/oauth/authorize"
TOKEN_URL = "https://zcode.z.ai/api/v1/oauth/token"
# zcode.z.ai 网页端内置的公开 client_id
CLIENT_ID = "client_P8X5CMWmlaRO9gyO-KSqtg"


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


class ZaiAuthFlow:
    def __init__(self, redirect_uri: str) -> None:
        self.redirect_uri = redirect_uri
        self.nonce = str(uuid.uuid4())
        # state 为 base64url(JSON)，字段与网页端实测一致（nonce + app_return_to + redirect_uri）
        self.state = _b64url(
            json.dumps({
                "nonce": self.nonce,
                "app_return_to": redirect_uri,
                "redirect_uri": redirect_uri,
            }, separators=(",", ":")).encode()
        )

    def authorize_url(self) -> str:
        return f"{AUTHORIZE_URL}?" + urlencode({
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "client_id": CLIENT_ID,
            "state": self.state,
        })

    async def exchange(self, code: str, state: str) -> dict:
        """授权码 → {token(=Coding Plan JWT), zai.access_token, user, expires_in}。"""
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                TOKEN_URL,
                headers={"Content-Type": "application/json"},
                json={"code": code, "redirect_uri": self.redirect_uri, "state": state},
            )
        body = {}
        try:
            body = res.json()
        except Exception:  # noqa: BLE001 - 非 JSON 响应退回 HTTP 状态码
            pass
        if not body and not res.is_success:
            res.raise_for_status()
        if body.get("code") != 0:
            raise RuntimeError(body.get("msg") or f"token 兑换失败 (HTTP {res.status_code})")
        data = body.get("data") or {}
        if not data.get("token"):
            raise RuntimeError("返回数据中不含 Coding Plan JWT")
        return data

    async def exchange_api_key(self, access_token: str) -> str:
        """OAuth access_token → 业务 token → 机构/项目 → API Key。"""
        async with httpx.AsyncClient(timeout=30) as client:
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
