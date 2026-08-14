"""FastAPI 应用工厂 + 生命周期。"""

from __future__ import annotations

import sys
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

    app.mount("/static", StaticFiles(directory=str(settings.STATIC_DIR)), name="static")

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
