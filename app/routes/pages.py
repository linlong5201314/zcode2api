"""页面路由：登录、账号管理、设置、代理设置，以及 OAuth 浏览器回跳页。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import settings
from ..oauth import accept_callback

router = APIRouter()

_TOKEN = "{{APP_VERSION}}"


def _html(name: str) -> HTMLResponse:
    path = settings.STATIC_DIR / "admin" / name
    if not path.exists():
        raise HTTPException(404, "页面不存在")
    body = path.read_text(encoding="utf-8").replace(_TOKEN, settings.APP_VERSION)
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


def _callback_page(ok: bool, message: str) -> HTMLResponse:
    """浏览器回跳结果页（极简中文提示，自动尝试关闭窗口）。"""
    color = "#16a34a" if ok else "#dc2626"
    icon = "✔" if ok else "✘"
    return HTMLResponse(
        f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>授权结果</title><style>
body{{margin:0;font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
background:#f7f7f8;color:#18181b;display:flex;align-items:center;justify-content:center;height:100vh}}
.card{{background:#fff;border:1px solid #e4e4e7;border-radius:14px;padding:40px 48px;
text-align:center;box-shadow:0 6px 24px rgba(0,0,0,.05);max-width:420px}}
.icon{{width:52px;height:52px;border-radius:50%;color:#fff;font-size:28px;line-height:52px;
margin:0 auto 16px;background:{color}}}
h1{{font-size:18px;margin:0 0 10px}}p{{font-size:13px;color:#71717a;margin:0;line-height:1.8}}
</style></head><body>
<div class="card"><div class="icon">{icon}</div>
<h1>{'授权成功' if ok else '授权未完成'}</h1>
<p>{message}</p></div>
<script>setTimeout(()=>{{try{{window.close()}}catch(e){{}}}},4000)</script>
</body></html>""",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/oauth/callback", include_in_schema=False)
async def oauth_callback(code: str = "", state: str = "",
                         error: str = "", error_description: str = ""):
    """OAuth 一键登录回跳（公开端点）：自动捕获授权码，结果页提示用户返回后台。"""
    accepted = accept_callback(
        state, code=code, error=error, error_description=error_description
    )
    if error:
        return _callback_page(False, f"授权被拒绝：{error_description or error}。请返回后台重试。")
    if not accepted:
        return _callback_page(False, "未找到对应的登录会话（可能已过期），请返回后台重新发起登录。")
    return _callback_page(
        True,
        "已收到授权码，正在自动导入账号。<b>请回到后台管理页面</b>查看结果，本窗口稍后自动关闭。",
    )


@router.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/admin")


@router.get("/admin", include_in_schema=False)
async def admin_root():
    return RedirectResponse("/admin/login")


@router.get("/admin/login", include_in_schema=False)
async def admin_login():
    return _html("login.html")


@router.get("/admin/accounts", include_in_schema=False)
async def admin_accounts():
    return _html("accounts.html")


@router.get("/admin/settings", include_in_schema=False)
async def admin_settings():
    return _html("settings.html")


@router.get("/admin/proxy", include_in_schema=False)
async def admin_proxy():
    return _html("proxy.html")


@router.get("/meta", include_in_schema=False)
async def meta():
    return {"version": settings.APP_VERSION}
