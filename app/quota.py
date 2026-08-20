"""ZCode 额度 / 余额 / 用量查询，以及账号状态判定。

在查询基础上提供「额度用完自动标记 exhausted」的监控能力。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable

import httpx

from . import logs, settings
from .models import Account, Status
from .proxy import upstream_proxy
from .store import store


def _auth_headers(account: Account) -> dict:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": settings.USER_AGENT,
        "X-ZCode-App-Version": settings.ZCODE_APP_VERSION,
    }
    if account.mode == "jwt" and account.jwt_token:
        headers["Authorization"] = f"Bearer {account.jwt_token}"
    elif account.api_key:
        headers["x-api-key"] = account.api_key
    return headers


def _as_number(value):
    """Coerce the several numeric encodings used by billing endpoints."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        raw = value.strip().replace(",", "")
        if not raw:
            return None
        try:
            return float(raw) if "." in raw else int(raw)
        except ValueError:
            return None
    return None


def _json_payload(response) -> dict | list | None:
    """Decode a successful response, treating malformed JSON as unavailable."""
    if response is None or getattr(response, "status_code", 0) != 200:
        return None
    try:
        payload = response.json()
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, (dict, list)) else None


def _find_key(payload, key: str, depth: int = 0):
    """Find a key in the variably wrapped JSON returned by ZCode."""
    if depth > 6:
        return None
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = _find_key(value, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_key(value, key, depth + 1)
            if found is not None:
                return found
    return None


def _record_items(raw, fields: tuple[str, ...]) -> list[dict]:
    """Normalize a list, one record, or a {name: record} mapping."""
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if not isinstance(raw, dict):
        return []
    if any(field in raw for field in fields):
        for nested_key in ("balances", "limits", "quotas", "items", "values", "list", "limit"):
            nested = raw.get(nested_key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        return [raw]
    items = []
    for key, value in raw.items():
        if isinstance(value, dict):
            item = dict(value)
            item.setdefault("model", key)
        else:
            item = {"model": key, "remaining": value}
        items.append(item)
    return items


_QUOTA_FIELDS = (
    "total_units", "used_units", "available_units", "remaining_units",
    "total", "used", "remaining", "available", "usage", "limit",
    "currentValue",
)


def _quota_items(data) -> list[dict]:
    """Read old balances and current top-level/data.limits payloads."""
    if not isinstance(data, (dict, list)):
        return []
    for key in ("balances", "limits", "quotas", "quota", "items", "balance", "limit"):
        items = _record_items(_find_key(data, key), _QUOTA_FIELDS)
        if items:
            return items
    if isinstance(data, dict) and any(key in data for key in _QUOTA_FIELDS):
        return [data]
    return []


def _plan_items(data) -> list[dict]:
    """Read billing plans or subscription-list records."""
    if not isinstance(data, (dict, list)):
        return []
    for key in ("plans", "subscriptions", "subscription", "entitlements"):
        items = _record_items(
            _find_key(data, key),
            ("status", "state", "productName", "planName", "name"),
        )
        if items:
            return items
    # Current subscription/list response is commonly {data: [{...}]}.
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return [item for item in data["data"] if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _number(item: dict, *keys: str):
    sources = [item]
    for nested_key in ("quota", "usage", "limit", "balance"):
        nested = item.get(nested_key)
        if isinstance(nested, dict):
            sources.append(nested)
    for source in sources:
        for key in keys:
            value = _as_number(source.get(key))
            if value is not None:
                return value
    return None


def _expiry(value):
    """Normalize millisecond Unix timestamps while preserving ISO strings."""
    numeric = _as_number(value)
    if numeric is not None and numeric > 100_000_000_000:
        return numeric / 1000
    return value


def _normalise_quota(item: dict) -> dict:
    total = _number(item, "total_units", "total", "limit", "total_limit", "usage")
    used = _number(item, "used_units", "used", "consumed", "currentValue")
    remaining = _number(
        item,
        "available_units",
        "remaining_units",
        "remaining",
        "available",
    )
    if remaining is None and total is not None and used is not None:
        remaining = total - used
    if total is None and used is not None and remaining is not None:
        total = used + remaining
    return {
        "total": total,
        "used": used,
        "remaining": remaining,
        "expires_at": _expiry(
            item.get("expires_at")
            or item.get("expire_at")
            or item.get("period_end")
            or item.get("ends_at")
            or item.get("nextResetTime")
            or item.get("next_reset_time")
            or item.get("reset_at")
        ),
    }


def _quota_name(item: dict) -> str:
    return str(
        item.get("show_name")
        or item.get("model")
        or item.get("model_name")
        or item.get("model_id")
        or item.get("entitlement_id")
        or item.get("quota_type")
        or item.get("name")
        or item.get("type")
        or "model"
    )


def _merge_quota(target: dict, data) -> None:
    for item in _quota_items(data):
        values = _normalise_quota(item)
        if not any(value is not None for value in values.values()):
            continue
        current = target.setdefault(_quota_name(item), {})
        for key, value in values.items():
            if value is not None:
                current[key] = value


def _active_plan(plan: dict) -> bool:
    status = str(plan.get("status") or plan.get("state") or "").strip().lower()
    return (
        status in {"active", "valid", "enabled", "paid", "trial", "current"}
        or plan.get("valid") is True
        or plan.get("active") is True
        or plan.get("inCurrentPeriod") is True
        or plan.get("in_current_period") is True
    )


def _select_plan(sources: Iterable[tuple[str, object]]) -> dict:
    candidates: list[tuple[int, str, dict]] = []
    for source, payload in sources:
        for plan in _plan_items(payload):
            score = 100 if _active_plan(plan) else 0
            status = str(plan.get("status") or "").lower()
            if status in {"active", "valid"}:
                score += 10
            name = " ".join(str(plan.get(k) or "") for k in (
                "productName", "product_name", "planName", "plan_name", "name"
            )).lower()
            if any(word in name for word in ("coding", "pro", "max", "lite", "start")):
                score += 5
            candidates.append((score, source, plan))
    if not candidates:
        return {}
    _, source, plan = max(candidates, key=lambda item: item[0])
    selected = dict(plan)
    selected.setdefault("source", source)
    selected.setdefault("active", _active_plan(selected))
    return selected


def _response_reason(payload) -> str:
    for key in ("reason", "missingReason", "status", "code", "errorCode", "message", "msg"):
        value = _find_key(payload, key)
        if isinstance(value, str):
            value = value.strip().lower().replace("-", "_").replace(" ", "_")
            if any(marker in value for marker in ("coding_plan", "not_entitled", "auth_failed", "not_auth")):
                return value
    return ""


async def fetch_quota(account: Account) -> dict:
    """拉取单个账号的 方案 / 余额 / 用量，写回账号状态并持久化。

    返回结构: {"billing":..., "balance":..., "usage":..., "error":...}
    """
    if not account.secret:
        account.last_checked_at = time.time()
        account.status = Status.INVALID
        account.last_error = "账号缺少有效凭证"
        store.update_account(account)
        return {"error": account.last_error}

    headers = _auth_headers(account)
    base = settings.ZCODE_BILLING_BASE.rstrip("/")
    endpoints: dict[str, tuple[str, dict | None]] = {
        # app_version is required by current clients.  Passing it as params
        # keeps the URL readable and has a plain-URL fallback for old mocks.
        "billing": (f"{base}/billing/current", {"app_version": settings.ZCODE_APP_VERSION}),
        "balance": (f"{base}/billing/balance", None),
        "usage": (f"{base}/usage", None),
    }
    if account.provider == "zai" and account.mode == "jwt":
        subscription_url = getattr(settings, "ZCODE_SUBSCRIPTION_URL", "").strip()
        quota_url = getattr(settings, "ZCODE_QUOTA_LIMIT_URL", "").strip()
        if subscription_url:
            endpoints["subscription"] = (subscription_url, None)
        if quota_url:
            endpoints["quota_limit"] = (quota_url, None)

    responses: dict[str, object | None] = {}
    try:
        async with httpx.AsyncClient(
            timeout=getattr(settings, "ZCODE_QUOTA_TIMEOUT", 20),
            proxy=upstream_proxy(),
        ) as client:
            async def _get(url: str, params: dict | None = None):
                try:
                    if params:
                        try:
                            return await client.get(url, headers=headers, params=params)
                        except TypeError:
                            # Tiny test doubles and older wrappers may not expose
                            # httpx's params keyword; the server accepts the legacy
                            # URL without it as a compatibility fallback.
                            return await client.get(url, headers=headers)
                    return await client.get(url, headers=headers)
                except (httpx.HTTPError, OSError, TypeError, ValueError):
                    return None

            values = await asyncio.gather(*[
                _get(url, params) for url, params in endpoints.values()
            ])
            responses = dict(zip(endpoints, values))
    except (httpx.HTTPError, OSError, ValueError, TypeError) as err:
        account.last_checked_at = time.time()
        account.last_error = f"额度查询失败: {type(err).__name__}"
        store.update_account(account)
        return {"error": account.last_error}

    now = time.time()
    account.last_checked_at = now
    result: dict = {}
    payloads: dict[str, dict | list] = {}
    for label, response in responses.items():
        payload = _json_payload(response)
        if payload is not None:
            payloads[label] = payload
            result[label] = payload

    # A 401/403 from the legacy endpoint must not hide a successful response
    # from the current subscription/quota endpoints.
    auth_labels = ["billing", "subscription", "quota_limit", "balance"]
    auth_statuses = [
        getattr(responses[label], "status_code", None)
        for label in auth_labels
        if responses.get(label) is not None
    ]
    if auth_statuses and not any(status == 200 for status in auth_statuses):
        if all(status in (401, 403) for status in auth_statuses):
            account.status = Status.INVALID
            account.last_error = f"鉴权失败 HTTP {auth_statuses[0]}"
            store.update_account(account)
            return {"error": account.last_error}

    plan = _select_plan((
        ("billing", payloads.get("billing")),
        ("subscription", payloads.get("subscription")),
        ("quota", payloads.get("quota_limit")),
    ))
    reasons = [
        _response_reason(payloads.get(label))
        for label in ("subscription", "quota_limit", "billing")
        if label in payloads
    ]
    reason = next((value for value in reasons if value), "")
    if plan:
        account.plan = plan
    elif "not_entitled" in reason:
        account.plan = {"status": "not_entitled", "active": False, "source": "subscription"}
    elif any(label in payloads for label in ("billing", "subscription")):
        # Replace an old plan snapshot after a successful response that no
        # longer contains a plan; otherwise a cancelled plan would look active.
        account.plan = {}

    quota_map: dict = {}
    # Legacy balance first, then the current quota endpoint so newer values win.
    for label in ("balance", "quota_limit", "billing"):
        if label in payloads:
            _merge_quota(quota_map, payloads[label])
            # Some legacy billing responses put the units directly on a plan
            # object instead of exposing a balances array.
            for plan_item in _plan_items(payloads[label]):
                values = _normalise_quota(plan_item)
                if any(value is not None for value in values.values()):
                    current = quota_map.setdefault(_quota_name(plan_item), {})
                    for key, value in values.items():
                        if value is not None:
                            current[key] = value
    if quota_map:
        account.quota = quota_map
        remainings = [
            q.get("remaining") for q in quota_map.values() if q.get("remaining") is not None
        ]
        if remainings and all((remaining or 0) <= 0 for remaining in remainings):
            account.status = Status.EXHAUSTED
            account.last_error = "额度已用完"
        elif account.status in (Status.EXHAUSTED, Status.COOLING, Status.INVALID):
            account.status = Status.ACTIVE
            account.last_error = None
            account.cooling_until = None
        else:
            account.last_error = None
    elif _active_plan(account.plan):
        # An active plan with no model-level balance is still a valid account;
        # do not leave a previous exhausted/invalid flag stuck forever.
        if account.status in (Status.EXHAUSTED, Status.COOLING, Status.INVALID):
            account.status = Status.ACTIVE
            account.cooling_until = None
        account.last_error = None
    elif "not_entitled" in reason:
        # The credential itself may still be valid, but without an active plan
        # it should not stay in the selectable pool until the next refresh.
        account.status = Status.INVALID
        account.last_error = "Coding Plan 未激活（coding_plan_not_entitled）"
    elif any(marker in reason for marker in ("auth_failed", "not_auth")):
        account.status = Status.INVALID
        account.last_error = "Coding Plan 凭证无效"

    usage_payload = payloads.get("usage")
    if usage_payload is not None:
        if isinstance(usage_payload, dict):
            usage_data = usage_payload.get("data")
            account.usage = usage_data if isinstance(usage_data, dict) else usage_payload
        else:
            account.usage = {"items": usage_payload}
        result["usage"] = account.usage

    if not result:
        statuses = [
            f"{label}={getattr(responses.get(label), 'status_code', 'unavailable')}"
            for label in endpoints
        ]
        account.last_error = "额度接口无可用数据（" + ", ".join(statuses) + ")"
        store.update_account(account)
        return {"error": account.last_error}

    store.update_account(account)
    return result


async def refresh_accounts(accounts: list[Account]) -> dict:
    """并发刷新一批账号，返回汇总。"""
    if not accounts:
        return {"ok": 0, "fail": 0}
    sem = asyncio.Semaphore(8)

    async def _one(acc: Account) -> bool:
        async with sem:
            res = await fetch_quota(acc)
            return "error" not in res

    results = await asyncio.gather(*[_one(a) for a in accounts], return_exceptions=True)
    ok = sum(1 for r in results if r is True)
    return {"ok": ok, "fail": len(accounts) - ok}


class QuotaMonitor:
    """后台周期性刷新可管理账号的额度，实现实时用量监控。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def _loop(self) -> None:
        # 启动后先等几秒，避免与服务启动争抢
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass

        while not self._stop.is_set():
            interval = store.quota_refresh_interval()  # 实时读取设置，改后即生效
            if interval > 0:
                try:
                    accounts = [
                        a for a in store.list_accounts("zai")
                        if a.mode == "jwt" and a.status != Status.DISABLED
                    ]
                    if accounts:
                        await refresh_accounts(accounts)
                except Exception as err:  # noqa: BLE001 - 后台任务需吞掉异常继续运行
                    logs.err("quota", f"后台刷新出错: {err}")
            # interval<=0 视为关闭：仍周期性回看设置，便于随时启用
            wait = interval if interval > 0 else 30
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                continue

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


monitor = QuotaMonitor()
