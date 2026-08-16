"""后台管理 API：/admin/api/*（账号池、设置、用量监控、代理配置）。"""

from __future__ import annotations

import secrets
import time

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse

from .. import settings
from ..auth_admin import verify_admin_key
from ..models import PROVIDERS, Status
from ..oauth import ZaiAuthFlow, display_name, extract_code
from ..proxy import (
    detect_system_proxy,
    fetch_subscription,
    mask_proxy,
    probe_local_ports,
    proxy_source,
    set_upstream_proxy,
    test_proxy,
    upstream_proxy,
)
from ..quota import fetch_quota, refresh_accounts
from ..store import store

router = APIRouter(prefix="/admin/api", dependencies=[Depends(verify_admin_key)])

# 进行中的 OAuth 登录流程（flow_id -> ZaiAuthFlow）
_login_flows: dict[str, ZaiAuthFlow] = {}
_LOGIN_FLOW_TTL = 15 * 60  # 登录流程最长保留时长（秒）


# ── 鉴权探针 ─────────────────────────────────────────────────────────────────
@router.get("/verify")
async def verify():
    return {"status": "ok"}


@router.get("/captcha/check")
async def captcha_check():
    """诊断端点：实测一次人机校验求解，返回成功/失败原因（用于远程定位风控问题）。"""
    from ..captcha import captcha_manager

    t0 = time.monotonic()
    try:
        param = await captcha_manager.get_verify_param()
        return {
            "ok": True,
            "elapsed": round(time.monotonic() - t0, 2),
            "param_len": len(param or ""),
            "param_head": (param or "")[:40],
        }
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "elapsed": round(time.monotonic() - t0, 2), "reason": str(err)[:300]}


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
async def _import_oauth_account(data: dict) -> tuple[object, dict]:
    """把兑换结果导入账号池：保存 JWT、尝试兑换 API Key 回退、记录用户信息。"""
    zcode_jwt = data.get("token")
    access_token = (data.get("zai") or {}).get("access_token")
    user = data.get("user") or {}
    name = display_name(user) or "OAuth账号"
    account = None
    if zcode_jwt:
        account = store.add_account("zai", name, zcode_jwt)
        account.user = user
        store.update_account(account)
    if access_token:
        try:
            api_key = await ZaiAuthFlow.manual().exchange_api_key(access_token)
            if account is not None:
                account.api_key = api_key
                store.update_account(account)
            else:
                account = store.add_account("zai", name, api_key)
                account.user = user
                store.update_account(account)
        except Exception:  # noqa: BLE001 - 兑换失败不影响 JWT 已入池
            pass
    return account, user


def _login_result_json(account, user: dict, extra: dict | None = None) -> dict:
    """构造登录结果 JSON（含用户信息，便于前端展示）。"""
    payload = {
        "status": "ready",
        "account": account.public_view() if account else None,
        "user": {
            "name": display_name(user) or None,
            "email": user.get("email"),
            "user_id": user.get("user_id") or user.get("userId") or user.get("id"),
        },
    }
    if extra:
        payload.update(extra)
    return payload


@router.post("/login/start")
async def login_start(payload: dict = Body(default=None)):
    """构造授权链接。mode=loopback（默认，一键登录）回跳到网关自身自动捕获；
    mode=manual 回跳到 zcode.z.ai 登录页，由用户复制回跳地址中的 code。"""
    payload = payload or {}
    mode = payload.get("mode") or "loopback"
    now = time.time()
    for fid in [fid for fid, f in _login_flows.items() if now - f.created > _LOGIN_FLOW_TTL]:
        _login_flows.pop(fid, None)

    if mode == "manual":
        flow = ZaiAuthFlow.manual()
    else:
        flow = ZaiAuthFlow.loopback(settings.PORT)
    flow_id = secrets.token_hex(8)
    flow.created = now
    _login_flows[flow_id] = flow
    return {
        "flow_id": flow_id,
        "mode": mode,
        "authorize_url": flow.authorize_url(),
        "redirect_uri": flow.redirect_uri,
    }


@router.post("/login/poll")
async def login_poll(payload: dict = Body(...)):
    """轮询一键登录进度；回跳到达后自动兑换并导入账号池。"""
    flow = _login_flows.get(payload.get("flow_id") or "")
    if not flow:
        raise HTTPException(404, "登录会话不存在或已过期，请重新发起登录")

    result = await flow.poll()
    status = result.get("status")
    if status == "pending":
        return {"status": "pending"}
    if status == "ready":
        _login_flows.pop(payload["flow_id"], None)
        data = result.get("data") or {}
        account, user = await _import_oauth_account(data)
        if account is None:
            return {"status": "failed", "message": "未能从授权结果中获取凭证"}
        if account.mode == "jwt":
            await refresh_accounts([account])
        return _login_result_json(account, user)
    # failed / unknown
    return {"status": "failed", "message": result.get("message") or "登录会话状态异常，请重试"}


@router.post("/login/finish")
async def login_finish(payload: dict = Body(...)):
    """手动模式：用户粘贴回跳地址 / code，服务端兑换凭证并导入账号池。"""
    flow = _login_flows.get(payload.get("flow_id") or "")
    if not flow:
        raise HTTPException(404, "登录会话不存在或已过期，请重新发起登录")
    code = extract_code(payload.get("code") or "")
    if not code:
        return {"status": "failed", "message": "未识别到授权码（code），请粘贴回跳网址或 code 值"}

    try:
        data = await flow.exchange(code, flow.state)
    except Exception as err:  # noqa: BLE001
        # 授权码一次性，失败后保留会话：重新打开授权链接取新 code 即可重试（TTL 兜底清理）
        return {"status": "failed", "message": f"token 兑换失败: {err}（可重新打开授权链接获取新 code 后重试）"}

    user = data.get("user") or {}
    account, user = await _import_oauth_account(data)
    _login_flows.pop(payload["flow_id"], None)
    if account is None:
        return {"status": "failed", "message": "未能从授权结果中获取凭证"}
    if account.mode == "jwt":
        await refresh_accounts([account])
    return _login_result_json(account, user)


# ── 代理配置 ─────────────────────────────────────────────────────────────────
@router.get("/proxy")
async def get_proxy():
    """当前代理状态 + 系统代理探测 + 本机端口探测。"""
    return {
        "current": {
            "url": upstream_proxy(),
            "masked": mask_proxy(upstream_proxy()) or None,
            "source": proxy_source(),
        },
        "system": detect_system_proxy(),
        "ports": await probe_local_ports(),
    }


@router.put("/proxy")
async def put_proxy(payload: dict = Body(...)):
    """保存 / 清空上游代理（空值清除后台设置，回退环境变量）。"""
    url = (payload.get("url") or "").strip()
    if url and not url.startswith(("http://", "https://", "socks5://", "socks4://")):
        raise HTTPException(400, "代理地址须以 http:// 、https:// 或 socks5:// 开头")
    effective = set_upstream_proxy(url)
    return {
        "ok": True,
        "current": {
            "url": effective,
            "masked": mask_proxy(effective) or None,
            "source": proxy_source(),
        },
        "message": "代理已启用，上游请求与验证码求解即刻走代理" if effective else "已停用代理，恢复直连",
    }


@router.post("/proxy/test")
async def post_proxy_test(payload: dict = Body(default=None)):
    """测试代理连通性与出口 IP（未传 url 则测试当前生效代理）。"""
    payload = payload or {}
    return await test_proxy((payload.get("url") or "").strip() or None)


@router.post("/proxy/subscription")
async def post_proxy_subscription(payload: dict = Body(...)):
    """解析订阅链接：统计节点协议分布，判断是否需要本地 Clash 内核转接。"""
    url = (payload.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "订阅链接须以 http:// 或 https:// 开头")
    try:
        return await fetch_subscription(url)
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "error": f"订阅拉取失败: {str(err)[:200]}"}


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
