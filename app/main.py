"""FastAPI 应用工厂 + 生命周期。"""

from __future__ import annotations

import sys
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import settings
from . import logs
from .captcha import captcha_manager
from .quota import monitor
from .routes import admin_api, gateway, pages
from .store import store

# 修正 Windows 中文控制台可能出现的乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def _display_host() -> str:
    # 0.0.0.0 / 空地址在浏览器中不可直接访问，展示为 127.0.0.1
    host = (settings.HOST or "").strip()
    return "127.0.0.1" if host in ("", "0.0.0.0", "::") else host


@asynccontextmanager
async def lifespan(app: FastAPI):
    monitor.start()
    base = f"http://{_display_host()}:{settings.PORT}"
    logs.banner([
        f"{logs._B}{logs._MAG}zcode2api{logs._R} {logs._DIM}v{settings.APP_VERSION} · Python{logs._R}",
        f"{logs._DIM}后台管理{logs._R}  {logs._C}{base}/admin/login{logs._R}",
        f"{logs._DIM}对话端点{logs._R}  {logs._C}{base}/v1/messages{logs._R}",
    ])
    try:
        yield
    finally:
        await monitor.stop()
        await captcha_manager.close()


def create_app() -> FastAPI:
    app = FastAPI(title="zcode2api", version=settings.APP_VERSION, lifespan=lifespan)

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        # Keep a bounded, printable request id for correlating gateway logs.
        supplied = request.headers.get("x-request-id", "")
        request_id = supplied if re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", supplied) else None
        if not request_id:
            import secrets

            request_id = secrets.token_hex(8)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception as err:  # noqa: BLE001 - do not leak stack traces to clients
            logs.err(request_id, f"未处理异常: {type(err).__name__}")
            response = JSONResponse(
                {"error": {"message": "服务器内部错误", "type": "internal_error"}},
                status_code=500,
            )
        response.headers.setdefault("x-request-id", request_id)
        response.headers.setdefault("x-content-type-options", "nosniff")
        response.headers.setdefault("x-frame-options", "DENY")
        response.headers.setdefault("referrer-policy", "no-referrer")
        return response

    app.mount("/static", StaticFiles(directory=str(settings.STATIC_DIR)), name="static")

    @app.get("/healthz", include_in_schema=True)
    async def healthz():
        """Liveness probe: the process and event loop are serving requests."""
        return {"status": "ok", "service": "zcode2api", "version": settings.APP_VERSION}

    @app.get("/readyz", include_in_schema=True)
    async def readyz():
        """Readiness probe without exposing account credentials or pool contents."""
        checks = {
            "database": settings.DB_PATH.exists(),
            "admin_key": bool(store.admin_key()),
        }
        ready = all(checks.values())
        payload = {
            "status": "ready" if ready else "not_ready",
            "service": "zcode2api",
            "version": settings.APP_VERSION,
            "checks": checks,
        }
        return JSONResponse(payload, status_code=200 if ready else 503)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc_handler(request: Request, exc: StarletteHTTPException):
        # 404 时回显请求路径，帮助客户端定位 base_url 配置问题
        if exc.status_code == 404:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": f"Not Found: 未注册的路径 {request.method} {request.url.path}",
                    "supported": [
                        "POST /v1/messages          (Anthropic Messages 协议)",
                        "POST /v1/chat/completions  (OpenAI 兼容)",
                        "GET  /v1/models",
                    ],
                    "hint": "Anthropic/OpenAI 客户端的 base_url 应填网关根地址（结尾不要带 /v1）；"
                            "如已带 /v1，网关会自动兼容 /v1/v1/* 路径。",
                },
            )
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc.detail)})

    app.include_router(pages.router)
    app.include_router(admin_api.router)
    app.include_router(gateway.router)
    return app


app = create_app()
