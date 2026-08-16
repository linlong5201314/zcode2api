"""验证码求解（无头 Chrome）。

Z.AI 上游按环境做人机校验（阿里云无痕验证），需要 X-Aliyun-Captcha-Verify-Param。
原 Node + jsdom 模拟浏览器环境的方案已被风控识破（verifyCode=F001 环境风险拒绝），
现改为 Playwright 无头浏览器运行官方无痕 SDK 求解，且优先复用本机安装的真实
Chrome / Edge（自带 Chromium 同样会被风控识破）。

- 缓存：求得的 verifyParam 在 TTL 内复用；TTL 过期后先返回旧值并后台刷新（宽限期）
- 并发：同一时刻只跑一个求解进程，其余请求等待后命中缓存
- 重试：单次求解偶发失败时自动重试；连续失败会短时熔断，避免每个请求都白等
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from . import logs, settings

# 无头浏览器伪装成普通 Windows Chrome，避免被风控识别为自动化环境
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _detect_browser_channel() -> str | None:
    """探测本机真实浏览器，返回 playwright channel（chrome / msedge），无则 None。"""
    if sys.platform == "win32":
        candidates = [
            (Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"), "chrome"),
            (Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"), "chrome"),
            (Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"), "msedge"),
            (Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"), "msedge"),
        ]
        for path, channel in candidates:
            if path.exists():
                return channel
        return None
    if sys.platform == "darwin":
        if Path("/Applications/Google Chrome.app").exists():
            return "chrome"
        if Path("/Applications/Microsoft Edge.app").exists():
            return "msedge"
        return None
    # Linux：PATH 中有官方 Chrome 包即用
    if shutil.which("google-chrome-stable") or shutil.which("google-chrome"):
        return "chrome"
    if shutil.which("microsoft-edge"):
        return "msedge"
    return None


# 显式配置优先，否则自动探测（真 Chrome/Edge 优先，自带 Chromium 兜底）
BROWSER_CHANNEL = settings.CAPTCHA_BROWSER_CHANNEL or _detect_browser_channel()

_SOLVER_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<script src="https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js"></script>
</head><body><div id="cap"></div><button id="btn"></button>
<script>
window.initAliyunCaptcha({
  SceneId: __SCENE__, mode: 'popup', region: __REGION__, prefix: __PREFIX__,
  element: '#cap', button: '#btn', captchaLogoImg: '', showErrorTip: false,
  getInstance: function (inst) {
    var fn = inst.startTracelessVerification || inst.show;
    try { fn.call(inst); } catch (e) {
      window.__onCaptcha(JSON.stringify({event: 'starterr', message: String(e && e.message || e)}));
    }
  },
  success: function (param) { window.__onCaptcha(JSON.stringify({event: 'success', param: param})); },
  fail: function (m) { window.__onCaptcha(JSON.stringify({event: 'fail', reason: m})); },
  onError: function (m) { window.__onCaptcha(JSON.stringify({event: 'error', reason: m})); }
});
</script></body></html>"""


class CaptchaManager:
    def __init__(self) -> None:
        self._cached: str | None = None
        self._cached_at: float = 0.0
        self._failed_at: float = 0.0
        self._failed_err: str | None = None
        self._refreshing: bool = False
        self._lock = asyncio.Lock()
        self._config_cache: dict | None = None
        self._config_cache_at: float = 0.0

    # ── 配置 ─────────────────────────────────────────────────────────────────
    async def fetch_config(self) -> dict:
        now = time.time() * 1000
        if self._config_cache and now - self._config_cache_at < settings.CAPTCHA_CONFIG_CACHE_TTL:
            return self._config_cache
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.get(
                    "https://zcode.z.ai/api/v1/client/configs"
                    f"?version={settings.ZCODE_APP_VERSION}&os=win32"
                )
            res.raise_for_status()
            captcha = ((res.json().get("data") or {}).get("configs") or {}).get("captcha")
            if captcha:
                self._config_cache = captcha
                self._config_cache_at = now
                return captcha
        except (httpx.HTTPError, ValueError) as err:
            logs.warn("captcha", f"获取配置失败，使用默认: {err}")
        return {"enabled": True, "prefix": "no8xfe", "region": "cn", "sceneId": "11xygtvd"}

    # ── 求解 ─────────────────────────────────────────────────────────────────
    async def get_verify_param(self, port: int | None = None) -> str:
        now = time.time() * 1000
        if self._cached:
            age = now - self._cached_at
            if age < settings.CAPTCHA_CACHE_TTL:
                return self._cached
            # 宽限期内先返回旧值并后台刷新，避免请求同步等待求解；
            # 若旧值被上游拒绝，转发层会走 invalidate + 重试兜底
            if age < settings.CAPTCHA_STALE_GRACE and not self._refreshing:
                self._refreshing = True
                asyncio.create_task(self._refresh_background())
                return self._cached
        # 失败也短缓存：数据中心 IP 被风控拒绝时，避免每个请求都重跑注定失败的求解
        if self._failed_at and now - self._failed_at < settings.CAPTCHA_FAIL_CACHE_TTL:
            wait = max(0, int((settings.CAPTCHA_FAIL_CACHE_TTL - (now - self._failed_at)) / 1000))
            raise RuntimeError(f"验证码求解失败（{wait}s 内不重试）: {self._failed_err}")

        async with self._lock:
            # 二次检查：等锁期间可能已被其他请求填充
            now = time.time() * 1000
            if self._cached and now - self._cached_at < settings.CAPTCHA_CACHE_TTL:
                return self._cached
            if self._failed_at and now - self._failed_at < settings.CAPTCHA_FAIL_CACHE_TTL:
                raise RuntimeError(f"验证码求解失败（短时内不重试）: {self._failed_err}")

            config = await self.fetch_config()
            if config.get("enabled") is False:
                return ""  # 上游已关闭人机校验，直接放行
            try:
                param = await self._solve(config)
            except Exception as err:  # noqa: BLE001
                self._failed_at = time.time() * 1000
                self._failed_err = str(err)[:300]
                raise
            self._cached = param
            self._cached_at = time.time() * 1000
            self._failed_at = 0.0
            return param

    async def _refresh_background(self) -> None:
        """后台刷新验证码参数（宽限期触发）。"""
        try:
            async with self._lock:
                config = await self.fetch_config()
                if config.get("enabled") is False:
                    return
                param = await self._solve(config)
                self._cached = param
                self._cached_at = time.time() * 1000
                self._failed_at = 0.0
        except Exception as err:  # noqa: BLE001
            logs.warn("captcha", f"后台刷新失败（继续用旧参数）: {str(err)[:150]}")
        finally:
            self._refreshing = False

    async def _solve(self, config: dict) -> str:
        scene = config.get("sceneId") or "11xygtvd"
        region = config.get("region") or "cn"
        prefix = config.get("prefix") or "no8xfe"

        last_err: str | None = None
        for attempt in range(1, settings.CAPTCHA_SOLVE_RETRIES + 1):
            try:
                param = await self._run_browser(scene, region, prefix)
            except Exception as err:  # noqa: BLE001
                last_err = str(err)
                param = None
            if param:
                if attempt > 1:
                    logs.ok("captcha", f"求解成功（第 {attempt} 次尝试）")
                return param
            logs.warn("captcha", f"第 {attempt}/{settings.CAPTCHA_SOLVE_RETRIES} 次求解未果，重试…")

        raise RuntimeError(f"验证码求解失败: {last_err or '多次重试无结果'}")

    async def _run_browser(self, scene: str, region: str, prefix: str) -> str:
        html = (
            _SOLVER_HTML
            .replace("__SCENE__", json.dumps(scene))
            .replace("__REGION__", json.dumps(region))
            .replace("__PREFIX__", json.dumps(prefix))
        )
        received: list[str] = []

        async with async_playwright() as pw:
            launch_kwargs = {
                "headless": True,
                "channel": BROWSER_CHANNEL,
                "args": [
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            }
            if settings.UPSTREAM_PROXY:
                launch_kwargs["proxy"] = {"server": settings.UPSTREAM_PROXY}
            browser = await pw.chromium.launch(**launch_kwargs)
            try:
                page = await browser.new_page(
                    user_agent=_CHROME_UA, viewport={"width": 1280, "height": 720}
                )
                await page.expose_function("__onCaptcha", lambda payload: received.append(payload))
                # 先落到 zcode.z.ai 同源页面再替换内容，保证 origin / localStorage 环境真实
                await page.goto("https://zcode.z.ai/", wait_until="domcontentloaded")
                await page.set_content(html)

                deadline = time.monotonic() + settings.CAPTCHA_SOLVE_TIMEOUT
                while time.monotonic() < deadline:
                    if received:
                        msg = json.loads(received[0])
                        if msg.get("event") == "success" and msg.get("param"):
                            return msg["param"]
                        raise RuntimeError(
                            f"风控拒绝: {json.dumps(msg, ensure_ascii=False)[:300]}"
                        )
                    await asyncio.sleep(0.2)
                raise TimeoutError("求解超时（无头浏览器未在时限内返回结果）")
            finally:
                await browser.close()

    def invalidate(self) -> None:
        self._cached = None
        self._cached_at = 0.0

    async def close(self) -> None:
        pass


captcha_manager = CaptchaManager()
