"""后台管理 API：/admin/api/*（账号池、设置、用量监控）。"""

from __future__ import annotations

import os
import secrets
import time

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..auth_admin import verify_admin_key
from ..models import PROVIDERS, Status
from ..oauth import ZaiAuthFlow
from ..quota import fetch_quota, refresh_accounts
from ..store import store

router = APIRouter(prefix="/admin/api", dependencies=[Depends(verify_admin_key)])
# OAuth 回调是浏览器从 Z.AI 跳回来的落地页，无法携带后台密钥，
# 安全性由 state 中的随机 nonce 保证，因此挂在无鉴权路由上。
public_router = APIRouter(prefix="/admin/api")

# 进行中的 OAuth 登录流程（flow_id -> ZaiAuthFlow），回调写入结果、轮询读取结果
_login_flows: dict[str, ZaiAuthFlow] = {}
_LOGIN_FLOW_TTL = 15 * 60  # 登录流程最长保留时长（秒）


def _public_base(request: Request) -> str:
    """推断网关对外可达的 base URL（Railway 等反代场景取 Host / X-Forwarded-Proto）。"""
    custom = os.getenv("ZCODE_PUBLIC_URL", "").strip()
    if custom:
        return custom.rstrip("/")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


# ── 鉴权探针 ─────────────────────────────────────────────────────────────────
@router.get("/verify")
async def verify():
    return {"status": "ok"}


# ── 账号列表 + 概览统计 ──────────────────────────────────────────────────────
@router.get("/accounts")
async def list_accounts():
    now = time.time()
    accounts = [a.public_view() for a in store.list_accounts()]
    stats = {"total": len(accounts), "active": 0, "exhausted": 0,
             "cooling": 0, "invalid": 0, "disabled": 0,
             "calls": 0, "fail": 0}
    for a in accounts:
        st = a["status"]
        if st in stats:
            stats[st] += 1
        stats["calls"] += a["use_count"]
        stats["fail"] += a["fail_count"]
    return {"accounts": accounts, "stats": stats, "providers": list(PROVIDERS), "ts": now}


@router.get("/status")
async def status_info():
    return {
        "providers": list(PROVIDERS),
        "gateway_key_set": bool(store.gateway_key()),
        "quota_pool": {
            p: sum(1 for a in store.list_accounts(p) if a.is_selectable())
            for p in PROVIDERS
        },
    }


# ── 新增账号 ─────────────────────────────────────────────────────────────────
@router.post("/accounts")
async def add_accounts(payload: dict = Body(...)):
    provider = payload.get("provider", "zai")
    if provider not in PROVIDERS:
        raise HTTPException(400, "不支持的 provider")
    tokens = payload.get("tokens") or []
    if isinstance(tokens, str):
        tokens = [t.strip() for t in tokens.splitlines() if t.strip()]
    tokens = [t.strip() for t in tokens if t and t.strip()]
    if not tokens:
        raise HTTPException(400, "请输入至少一个 Token / API Key")

    added = []
    for tok in dict.fromkeys(tokens):  # 去重保序
        name = payload.get("name") or f"{provider}-{len(store.list_accounts(provider)) + 1}"
        acc = store.add_account(provider, name, tok)
        added.append(acc.id)
    # 立即刷新一次额度（仅 zai jwt）
    fresh = [a for a in store.list_accounts(provider) if a.id in added and a.mode == "jwt"]
    if fresh:
        await refresh_accounts(fresh)
    return {"count": len(added), "ids": added}


# ── 删除账号 ─────────────────────────────────────────────────────────────────
@router.delete("/accounts")
async def delete_accounts(ids: list[str] = Body(...)):
    deleted = 0
    for aid in ids:
        acc = store.find_any(aid)
        if acc and store.remove_account(acc.provider, aid):
            deleted += 1
    return {"deleted": deleted}


# ── 编辑账号 ─────────────────────────────────────────────────────────────────
@router.put("/accounts/{account_id}")
async def edit_account(account_id: str, payload: dict = Body(...)):
    acc = store.find_any(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if "name" in payload and payload["name"]:
        acc.name = payload["name"].strip()
    secret = payload.get("token") or payload.get("secret")
    if secret:
        secret = secret.strip()
        acc.mode = "jwt" if (secret.count(".") == 2 and acc.provider == "zai") else "apiKey"
        acc.jwt_token = secret if acc.mode == "jwt" else None
        acc.api_key = None if acc.mode == "jwt" else secret
        acc.status = Status.ACTIVE
        acc.last_error = None
    store.update_account(acc)
    return {"ok": True}


# ── 启用 / 禁用 ──────────────────────────────────────────────────────────────
@router.post("/accounts/{account_id}/enabled")
async def set_enabled(account_id: str, payload: dict = Body(...)):
    acc = store.find_any(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    enabled = bool(payload.get("enabled", True))
    store.set_enabled(acc.provider, account_id, enabled)
    return {"ok": True}


# ── 刷新额度（实时用量监控）─────────────────────────────────────────────────
@router.post("/accounts/refresh")
async def refresh(payload: dict = Body(default=None)):
    payload = payload or {}
    if payload.get("all"):
        targets = [a for a in store.list_accounts("zai") if a.mode == "jwt"]
    else:
        ids = set(payload.get("ids") or [])
        targets = [a for a in store.list_accounts() if a.id in ids and a.mode == "jwt"]
    summary = await refresh_accounts(targets)
    return {"summary": summary, "count": len(targets)}


@router.post("/accounts/{account_id}/refresh")
async def refresh_one(account_id: str):
    acc = store.find_any(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if acc.mode != "jwt":
        return {"ok": False, "message": "仅 Coding Plan (JWT) 账号支持额度查询"}
    res = await fetch_quota(acc)
    return {"ok": "error" not in res, "result": res, "account": acc.public_view()}


# ── OAuth 登录（Z.AI）────────────────────────────────────────────────────────
@router.post("/login/start")
async def login_start(request: Request):
    """构造授权链接（授权码模式），回调地址为网关自身的 /admin/api/login/callback。"""
    now = time.time()
    for fid in [fid for fid, f in _login_flows.items() if now - f.created > _LOGIN_FLOW_TTL]:
        _login_flows.pop(fid, None)

    flow = ZaiAuthFlow(f"{_public_base(request)}/admin/api/login/callback")
    flow_id = secrets.token_hex(8)
    flow.created = now
    flow.status = "pending"
    _login_flows[flow_id] = flow
    return {"flow_id": flow_id, "authorize_url": flow.authorize_url()}


@public_router.get("/login/callback")
async def login_callback(code: str = "", state: str = "", error: str = ""):
    """Z.AI 授权后的浏览器落地页：兑换凭证、导入账号池并展示结果。"""
    flow = next((f for f in _login_flows.values() if f.state == state), None)
    if not flow:
        return HTMLResponse("<h3>登录会话不存在或已过期，请回到后台重新发起登录。</h3>", status_code=404)

    if error:
        flow.status, flow.message = "failed", f"授权失败: {error}"
    elif not code:
        flow.status, flow.message = "failed", "回调中缺少授权码"
    else:
        try:
            data = await flow.exchange(code, state)
        except Exception as err:  # noqa: BLE001
            flow.status, flow.message = "failed", f"token 兑换失败: {err}"

        if flow.status != "failed":
            # 授权成功：保存 Coding Plan JWT，并尝试兑换 API Key 作为同账号回退
            zcode_jwt = data.get("token")
            access_token = (data.get("zai") or {}).get("access_token")
            account = None
            if zcode_jwt:
                account = store.add_account("zai", "oauth-login", zcode_jwt)
            if access_token:
                try:
                    api_key = await flow.exchange_api_key(access_token)
                    if account is not None:
                        account.api_key = api_key
                        store.update_account(account)
                    else:
                        account = store.add_account("zai", "oauth-login", api_key)
                except Exception:  # noqa: BLE001 - 兑换失败不影响 JWT 已入池
                    pass
            if account is None:
                flow.status, flow.message = "failed", "未能从授权结果中获取凭证"
            else:
                if account.mode == "jwt":
                    await refresh_accounts([account])
                flow.status, flow.account = "ready", account.public_view()

    ok = flow.status == "ready"
    return HTMLResponse(
        "<html><head><meta charset='utf-8'><title>zcode2api 登录</title></head>"
        f"<body style='font-family:sans-serif;text-align:center;padding-top:80px'>"
        f"<h2>{'✅ 登录成功，已导入账号池' if ok else '❌ ' + (flow.message or '登录失败')}</h2>"
        "<p>请返回 zcode2api 后台页面查看。</p></body></html>",
        status_code=200,
    )


@router.get("/login/poll/{flow_id}")
async def login_poll(flow_id: str):
    """前端轮询授权状态；结果已由 /login/callback 写入。"""
    flow = _login_flows.get(flow_id)
    if not flow:
        raise HTTPException(404, "登录会话不存在或已过期")
    if flow.status == "pending":
        return {"status": "pending"}
    _login_flows.pop(flow_id, None)
    if flow.status == "ready":
        return {"status": "ready", "account": getattr(flow, "account", None)}
    return {"status": "failed", "message": getattr(flow, "message", None)}


# ── 设置 ─────────────────────────────────────────────────────────────────────
@router.get("/settings")
async def get_settings():
    return {
        "admin_key": store.admin_key(),
        "gateway_key": store.gateway_key(),
        "quota_refresh_interval": store.quota_refresh_interval(),
    }


@router.put("/settings")
async def update_settings(payload: dict = Body(...)):
    if "admin_key" in payload:
        key = (payload["admin_key"] or "").strip()
        if not key:
            raise HTTPException(400, "后台密钥不能为空")
        store.set_setting("admin_key", key)
    if "gateway_key" in payload:
        store.set_setting("gateway_key", (payload["gateway_key"] or "").strip())
    if "quota_refresh_interval" in payload:
        try:
            interval = max(0, int(payload["quota_refresh_interval"]))
        except (TypeError, ValueError):
            raise HTTPException(400, "刷新间隔必须是非负整数")
        store.set_setting("quota_refresh_interval", str(interval))
    return {"ok": True}


# ── 导入 / 导出 ─────────────────────────────────────────────────────────────
@router.get("/export")
async def export_accounts():
    return store.export()


@router.post("/import")
async def import_accounts(payload: dict = Body(...)):
    count = store.import_accounts(payload)
    return {"count": count}
