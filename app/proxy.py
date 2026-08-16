"""上游代理配置：一键导入本机代理、端口探测、订阅解析、连通性测试。

代理解析优先级：数据库设置（后台「代理设置」页） > 环境变量 ZCODE_UPSTREAM_PROXY > 直连。

机场订阅里的 ss / vmess / vless / trojan / hysteria2 等协议 Python 无法直连，
必须经本机 Clash / sing-box 内核暴露的 mixed / http / socks 本地端口转译
（与 OmniRoute 的「本地内核端点」方案一致，且仅允许 127.0.0.1 回环地址）。
因此「一键导入」读取的是系统代理 / 本机内核端口，而非订阅节点本身。
"""

from __future__ import annotations

import asyncio
import base64
import re
import subprocess
import sys

import httpx

from . import settings
from .store import store

# 本机常见代理内核监听端口（Clash Verge / Clash for Windows / v2rayN / sing-box）
PROBE_PORTS = (7897, 7890, 7891, 7899, 1080, 10808, 2080, 8889, 8118)

_PORT_LABELS = {
    7897: "Clash Verge 混合端口（常见默认）",
    7890: "Clash 混合端口（常见默认）",
    7891: "Clash HTTP 端口",
    7899: "Clash Verge 备用端口",
    1080: "SOCKS5 通用端口",
    10808: "v2rayN SOCKS 端口",
    2080: "sing-box 混合端口",
    8889: "HTTP 代理通用端口",
    8118: "Privoxy HTTP 端口",
}

# 订阅节点协议分类（与 OmniRoute 的 DIRECT_TYPES / NEEDS_CORE_PROTOCOLS 一致）
DIRECT_TYPES = {"http", "https", "socks5", "socks"}
NEEDS_CORE_TYPES = {"ss", "ssr", "vmess", "vless", "trojan", "tuic",
                    "hysteria", "hysteria2", "wireguard", "snell"}

_IP_APIS = (
    "https://api.ipify.org/?format=json",
    "https://ipinfo.io/json",
    "https://myip.ipip.net/json",
)


# ── 生效代理解析 ─────────────────────────────────────────────────────────────
def upstream_proxy() -> str | None:
    """当前生效的上游代理：后台设置优先，其次环境变量，均无则直连。"""
    stored = (store.get_setting("upstream_proxy", "") or "").strip()
    return stored or settings.UPSTREAM_PROXY or None


def proxy_source() -> str:
    """当前代理来源：db（后台设置）/ env（环境变量）/ none（直连）。"""
    if (store.get_setting("upstream_proxy", "") or "").strip():
        return "db"
    if settings.UPSTREAM_PROXY:
        return "env"
    return "none"


def set_upstream_proxy(url: str) -> str | None:
    """保存（或清空）后台代理设置，返回生效值。"""
    url = (url or "").strip()
    store.set_setting("upstream_proxy", url)
    return upstream_proxy()


def mask_proxy(url: str | None) -> str:
    """脱敏展示代理地址（隐藏用户名密码）。"""
    if not url:
        return ""
    if "@" in url:
        scheme, _, rest = url.partition("://")
        creds, _, host = rest.rpartition("@")
        masked = creds[:2] + "***" if creds else "***"
        return f"{scheme}://{masked}@{host}"
    return url


# ── 系统代理探测 ─────────────────────────────────────────────────────────────
def detect_system_proxy() -> dict:
    """读取操作系统代理设置（Windows 注册表 / macOS scutil / Linux 环境变量）。"""
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            ) as key:
                enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
                server = ""
                try:
                    server, _ = winreg.QueryValueEx(key, "ProxyServer")
                except OSError:
                    pass
        except OSError:
            return {"enabled": False, "url": None}
        if not enable or not server:
            return {"enabled": False, "url": None}
        # ProxyServer 可能是 "host:port" 或 "http=...;https=...;socks=..." 形式
        if "=" in server:
            parts = dict(
                p.split("=", 1) for p in server.split(";") if "=" in p
            )
            server = parts.get("https") or parts.get("http") or next(iter(parts.values()), "")
        if server and "://" not in server:
            server = f"http://{server}"
        return {"enabled": True, "url": server or None}

    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["scutil", "--proxy"], capture_output=True, text=True, timeout=5
            ).stdout
            info = dict(
                line.split(":", 1) for line in out.splitlines() if ":" in line
            )
            if info.get("HTTPEnable", "").strip() == "1":
                host = info.get("HTTPProxy", "").strip()
                port = info.get("HTTPPort", "").strip()
                if host and port:
                    return {"enabled": True, "url": f"http://{host}:{port}"}
            if info.get("SOCKSEnable", "").strip() == "1":
                host = info.get("SOCKSProxy", "").strip()
                port = info.get("SOCKSPort", "").strip()
                if host and port:
                    return {"enabled": True, "url": f"socks5://{host}:{port}"}
        except (OSError, subprocess.SubprocessError):
            pass
        return {"enabled": False, "url": None}

    # Linux：读取桌面环境环境变量
    import os

    for var in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        value = (os.getenv(var) or "").strip()
        if value:
            return {"enabled": True, "url": value}
    return {"enabled": False, "url": None}


# ── 本机端口探测 ─────────────────────────────────────────────────────────────
async def probe_local_ports() -> list[dict]:
    """并发探测本机常见代理内核端口，返回开放端口列表。"""

    async def _open(port: int) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=0.5
            )
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True

    results = await asyncio.gather(*[_open(p) for p in PROBE_PORTS])
    return [
        {
            "url": f"http://127.0.0.1:{port}",
            "port": port,
            "label": _PORT_LABELS.get(port, "本机代理端口"),
        }
        for port, ok in zip(PROBE_PORTS, results)
        if ok
    ]


# ── 连通性 / 出口 IP 测试 ────────────────────────────────────────────────────
async def _fetch_exit_ip(client: httpx.AsyncClient) -> str | None:
    for api in _IP_APIS:
        try:
            res = await client.get(api, timeout=10)
            if not res.is_success:
                continue
            data = res.json()
            ip = data.get("ip") or (data.get("data") or [None])[0]
            if ip:
                return str(ip)
        except (httpx.HTTPError, ValueError, IndexError):
            continue
    return None


async def test_proxy(proxy_url: str | None = None) -> dict:
    """测试代理连通性并返回出口 IP（附带直连 IP 对比）。"""
    import time as _time

    proxy_url = proxy_url or upstream_proxy()
    result: dict = {"proxy": mask_proxy(proxy_url), "ok": False}

    # 直连基线禁用环境变量代理，保证对比结果真实
    async with httpx.AsyncClient(timeout=15, trust_env=False) as direct:
        result["direct_ip"] = await _fetch_exit_ip(direct)

    if not proxy_url:
        result["ok"] = result["direct_ip"] is not None
        result["message"] = "当前为直连（未启用代理）"
        return result

    t0 = _time.monotonic()
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=15, trust_env=False) as via:
            ip = await _fetch_exit_ip(via)
    except (httpx.HTTPError, OSError) as err:
        result["message"] = f"代理不可用: {err}"
        return result
    result["elapsed"] = round(_time.monotonic() - t0, 2)
    if not ip:
        result["message"] = "代理可连接，但无法通过它访问外网"
        return result
    result["ok"] = True
    result["exit_ip"] = ip
    result["changed"] = bool(result.get("direct_ip") and ip != result["direct_ip"])
    result["message"] = (
        f"出口 IP 已切换为 {ip}" if result["changed"] else f"出口 IP: {ip}（与直连相同）"
    )
    return result


# ── 订阅链接解析 ─────────────────────────────────────────────────────────────
def _count_types_by_regex(text: str) -> dict:
    """无 PyYAML 时按行统计 Clash YAML 节点协议分布。"""
    types: dict[str, int] = {}
    for match in re.finditer(r"type:\s*([a-z0-9\-]+)", text):
        t = match.group(1)
        types[t] = types.get(t, 0) + 1
    return types


def _parse_clash_yaml(text: str) -> dict:
    local_ports: dict = {}
    for key in ("mixed-port", "port", "socks-port"):
        m = re.search(rf"^{key}:\s*(\d+)", text, re.MULTILINE)
        if m:
            local_ports[key] = int(m.group(1))

    try:
        import yaml  # type: ignore

        doc = yaml.safe_load(text) or {}
        proxies = doc.get("proxies") or doc.get("outbounds") or []
        nodes = [
            {"name": str(p.get("name") or ""), "type": str(p.get("type") or "")}
            for p in proxies
            if isinstance(p, dict) and p.get("type") not in (None, "direct", "block", "dns", "selector", "urltest", "fallback", "relay")
        ]
    except ImportError:
        nodes = []
        types = _count_types_by_regex(text)
        for t, count in types.items():
            nodes.extend([{"name": "", "type": t}] * count)
        return {"local_ports": local_ports, "nodes": nodes, "parser": "regex"}

    return {"local_ports": local_ports, "nodes": nodes, "parser": "yaml"}


def _parse_uri_lines(text: str) -> list[dict]:
    nodes = []
    for line in text.splitlines():
        m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*)://", line.strip())
        if m:
            nodes.append({"name": line.strip()[:60], "type": m.group(1).lower()})
    return nodes


def _try_base64(text: str) -> str:
    stripped = "".join(text.split())
    if len(stripped) < 16 or not re.fullmatch(r"[A-Za-z0-9+/=_\-]+", stripped):
        return text
    try:
        decoded = base64.b64decode(stripped + "=" * (-len(stripped) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return text
    if decoded.count("://") >= 1 and "�" not in decoded:
        return decoded
    return text


def parse_subscription_body(body: str) -> dict:
    """解析订阅内容（Clash YAML / base64 节点列表 / URI 行），返回统计摘要。"""
    if re.search(r"^\s*(proxies|proxy-providers|outbounds):", body, re.MULTILINE):
        parsed = _parse_clash_yaml(body)
    else:
        decoded = _try_base64(body)
        nodes = _parse_uri_lines(decoded) or _parse_uri_lines(body)
        parsed = {"local_ports": {}, "nodes": nodes, "parser": "uri"}

    nodes = parsed["nodes"]
    types: dict[str, int] = {}
    for node in nodes:
        t = node["type"]
        types[t] = types.get(t, 0) + 1

    usable = [n for n in nodes if n["type"] in DIRECT_TYPES]
    needs_core = [n for n in nodes if n["type"] in NEEDS_CORE_TYPES]

    if not nodes:
        advice = "未能解析出节点，请确认这是 Clash / V2Ray 订阅链接"
    elif needs_core and not usable:
        advice = (
            "订阅节点均为需要本地内核的协议（如 hysteria2 / vmess / trojan），"
            "程序无法直连这些节点。请保持本机 Clash Verge 开启，"
            "在上方「一键导入」选择它的本地端口（如 http://127.0.0.1:7897）即可经代理出口。"
        )
    else:
        advice = "订阅包含可直接使用的 http/socks5 节点，可将其地址填入手动配置"

    return {
        "ok": bool(nodes),
        "format": "clash-yaml" if parsed["parser"] in ("yaml", "regex") else "uri-list",
        "parser": parsed["parser"],
        "node_count": len(nodes),
        "types": types,
        "direct_usable": len(usable),
        "needs_core": len(needs_core),
        "needs_core_types": sorted({n["type"] for n in needs_core}),
        "sample_names": [n["name"] for n in nodes if n["name"]][:8],
        "local_ports": parsed["local_ports"],
        "advice": advice,
    }


async def fetch_subscription(url: str) -> dict:
    """拉取并解析订阅链接（以 Clash 客户端 UA 请求，返回 Clash YAML）。"""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        res = await client.get(
            url,
            headers={"User-Agent": "clash-verge/v2.2.0"},  # 头部须 ASCII，带 Clash UA 才返回 YAML
        )
        res.raise_for_status()
        return parse_subscription_body(res.text)
