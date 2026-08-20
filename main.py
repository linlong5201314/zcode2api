#!/usr/bin/env python
"""ZCode2api

用法:
  python main.py serve [--port 3000]        启动网关 + 后台 UI
  python main.py login zai [--no-browser]   通过 OAuth 登录 Z.AI 并自动加入账号池
  python main.py add-account zai <name> <jwt|key>   添加轮询账号
  python main.py accounts [zai|bigmodel]    查看账号列表
  python main.py remove-account <provider> <id|name>
  python main.py quota                      查看各账号实时额度
  python main.py status                     查看配置概览
  python main.py set-admin-key <key>        设置后台密码
  python main.py export [file]              导出账号
  python main.py import <file>              导入账号
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from app import settings
from app.models import Status
from app.oauth import ZaiAuthFlow, display_name, extract_code
from app.quota import fetch_quota
from app.store import store

# Keep CLI diagnostics readable on Windows consoles using a legacy code page.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

C = {
    "reset": "\033[0m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "red": "\033[31m", "cyan": "\033[36m", "bold": "\033[1m",
}


def c(text: str, color: str) -> str:
    return f"{C[color]}{text}{C['reset']}"


def usage() -> None:
    print(__doc__)


# ── serve ────────────────────────────────────────────────────────────────────
def cmd_serve(args: list[str]) -> None:
    port = settings.PORT
    if "--port" in args:
        i = args.index("--port")
        if i + 1 < len(args):
            port = int(args[i + 1])
    settings.PORT = port
    import uvicorn

    uvicorn.run("app.main:app", host=settings.HOST, port=port, log_level="info")


# ── login ────────────────────────────────────────────────────────────────────
async def cmd_login(args: list[str]) -> None:
    if not args or args[0] != "zai":
        print(c("目前仅支持: python cli.py login zai", "red"))
        return

    flow = ZaiAuthFlow(settings.OAUTH_REDIRECT_URI)
    authorize_url = flow.authorize_url()

    print(c("\n✔ 已构造授权链接！请在浏览器中打开并完成 Z.AI 登录：", "green"))
    print(c(authorize_url, "blue"))
    print(c("登录成功后浏览器会跳转到 zcode.z.ai/login 并带有 ?code=... 参数"
            "（页面本身可能报错，可忽略），", "yellow"))
    print(c("请把跳转后的完整地址（或 code 参数值）粘贴到下面：", "yellow"))

    if "--no-browser" not in args:
        try:
            import webbrowser
            webbrowser.open(authorize_url)
        except Exception:  # noqa: BLE001
            pass

    try:
        raw = input("code / 回跳地址> ").strip()
    except (EOFError, KeyboardInterrupt):
        print(c("\n已取消登录。", "red"))
        return
    code = extract_code(raw)
    if not code:
        print(c("❌ 未识别到授权码（code），请重试。", "red"))
        return

    try:
        data = await flow.exchange(code, flow.state)
    except Exception as err:  # noqa: BLE001
        print(c(f"❌ token 兑换失败: {err}", "red"))
        return

    zcode_jwt = data.get("token")
    access_token = (data.get("zai") or {}).get("access_token")
    user = data.get("user") or {}
    name = display_name(user) or "oauth-login"
    account = None
    if zcode_jwt:
        account = store.add_account("zai", name, zcode_jwt)
        account.user = user
        store.update_account(account)
        print(c(f"\n✔ 已保存 Coding Plan JWT 账号: {account.name} ({account.id})", "green"))
    if access_token:
        try:
            key = await flow.exchange_api_key(access_token)
            if account is not None:
                # 同一 OAuth 会话：把 API Key 挂到 JWT 账号上作为回退凭证，不再单独立号
                account.api_key = key
                store.update_account(account)
            else:
                store.add_account("zai", name, key)
            print(c(f"✔ 已兑换并保存 API Key: {key[:8]}...", "green"))
        except Exception as err:  # noqa: BLE001
            print(c(f"⚠️ 兑换 API Key 失败: {err}", "yellow"))
    if not zcode_jwt:
        print(c("❌ 返回数据中不含 Coding Plan JWT", "red"))
        return

    # 登录后立即拉取额度，确保账号以「已激活」状态入池（与后台 OAuth 登录行为一致）
    if account is not None:
        print(c("\n正在拉取 Coding Plan 额度...", "cyan"))
        await fetch_quota(account)
        _print_quota(account)
        if account.status == Status.EXHAUSTED:
            print(c("⚠️ 该账号额度已用完", "yellow"))
        elif account.status == Status.INVALID:
            print(c(f"⚠️ 账号状态异常: {account.last_error or account.status}", "yellow"))


def _print_quota(a) -> None:
    """终端打印单账号额度摘要。"""
    if not a.quota:
        print("  无额度数据" + (f"（{a.last_error}）" if a.last_error else ""))
        return
    for model, q in a.quota.items():
        rem, tot = q.get("remaining") or 0, q.get("total") or 0
        pct = f" ({rem / tot * 100:.0f}%)" if tot else ""
        print(f"  {c(model, 'cyan')}: 剩余 {rem:,} / 总额 {tot:,}{pct}")


# ── 账号管理 ─────────────────────────────────────────────────────────────────
def cmd_add_account(args: list[str]) -> None:
    if len(args) < 3:
        print(c("格式: python cli.py add-account <zai|bigmodel> <name> <jwt|key>", "red"))
        return
    provider, name, secret = args[0], args[1], args[2]
    acc = store.add_account(provider, name, secret)
    print(c(f"✔ 已添加账号 {acc.name} ({acc.id}) 模式={acc.mode}", "green"))


def cmd_accounts(args: list[str]) -> None:
    provider = args[0] if args and args[0] in ("zai", "bigmodel") else None
    accounts = store.list_accounts(provider)
    if not accounts:
        print("无账号")
        return
    print(c(f"\n--- 账号列表 ({provider or '全部'}) ---", "cyan"))
    for a in accounts:
        st = a.effective_status()
        print(f"{a.id}  {a.provider}  {a.mode}  {st}  {a.name}")


def cmd_remove_account(args: list[str]) -> None:
    if len(args) < 2:
        print(c("格式: python cli.py remove-account <provider> <id|name>", "red"))
        return
    if store.remove_account(args[0], args[1]):
        print(c(f"✔ 已删除账号 {args[1]}", "green"))
    else:
        print(c("⚠️ 未找到指定账号", "yellow"))


def cmd_set_admin_key(args: list[str]) -> None:
    if not args:
        print(c("格式: python cli.py set-admin-key <key>", "red"))
        return
    store.set_setting("admin_key", args[0])
    print(c("✔ 已更新后台密码", "green"))


def cmd_status() -> None:
    print(c("\n--- zcode2api 状态 ---", "cyan"))
    print(f"数据库      : {c(str(settings.DB_PATH), 'blue')}")
    print(f"默认端口    : {c(str(settings.PORT), 'blue')}")
    print(f"后台密码    : {'已设置' if store.admin_key() else c('未设置', 'yellow')}")
    print(f"网关 API Key: {'已设置' if store.gateway_key() else '未设置（不校验）'}")
    for p in ("zai", "bigmodel"):
        accounts = store.list_accounts(p)
        active = sum(1 for a in accounts if a.is_selectable())
        print(f"{p:9s}  : {len(accounts)} 个账号，{active} 个可用")


async def cmd_quota() -> None:
    accounts = [a for a in store.list_accounts("zai") if a.mode == "jwt"]
    if not accounts:
        print(c("无 Coding Plan (JWT) 账号可查询额度。", "yellow"))
        return
    print(c("\n正在拉取各账号实时额度...", "cyan"))
    for a in accounts:
        await fetch_quota(a)
        print(c(f"\n账号: {a.name} ({a.effective_status()})", "bold"))
        _print_quota(a)


def cmd_export(args: list[str]) -> None:
    out = args[0] if args else "zcode-accounts.json"
    data = store.export()
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(c(f"✔ 已导出到 {out}", "green"))


def cmd_import(args: list[str]) -> None:
    if not args:
        print(c("格式: python cli.py import <file>", "red"))
        return
    with open(args[0], encoding="utf-8") as f:
        payload = json.load(f)
    count = store.import_accounts(payload)
    print(c(f"✔ 已导入 {count} 个账号", "green"))


# ── 分发 ─────────────────────────────────────────────────────────────────────
def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        usage()
        return
    cmd, rest = argv[0], argv[1:]

    if cmd in ("help", "-h", "--help"):
        usage()
    elif cmd == "serve":
        cmd_serve(rest)
    elif cmd == "login":
        asyncio.run(cmd_login(rest))
    elif cmd == "add-account":
        cmd_add_account(rest)
    elif cmd == "accounts":
        cmd_accounts(rest)
    elif cmd == "remove-account":
        cmd_remove_account(rest)
    elif cmd == "set-admin-key":
        cmd_set_admin_key(rest)
    elif cmd == "status":
        cmd_status()
    elif cmd == "quota":
        asyncio.run(cmd_quota())
    elif cmd == "export":
        cmd_export(rest)
    elif cmd == "import":
        cmd_import(rest)
    else:
        print(c(f"未知命令: {cmd}", "red"))
        usage()


if __name__ == "__main__":
    main()
