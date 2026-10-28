"""Read-only background refreshes for persisted decision records.

Refresh jobs only read market evidence and append one immutable input version.
They never evaluate the active strategy and never call a trading endpoint.
Job status is intentionally process-local: a restart drops in-memory progress,
so a UI can offer a retry, while records and prior versions remain unchanged.
"""

from __future__ import annotations

import copy
import math
import re
import threading
import time
from collections import deque
from typing import Any, Optional

from app import decision_records
from app.decision_versions import rules_version
from utils.time import parse_steam_history_date

_ACTIVE_STATUSES = frozenset({"queued", "running"})
_TERMINAL_STATUSES = frozenset({"succeeded", "failed"})
_KINDS = frozenset({"buy", "sell"})
_SCHEMA_VERSION = 1
_MAX_FINISHED_JOBS = 200
_JOBS: dict[str, dict[str, Any]] = {}
_FINISHED_JOBS: deque[tuple[str, object]] = deque()
_JOBS_LOCK = threading.RLock()
_clock = time.time
_SENSITIVE = re.compile(
    r"(?i)\b(cookie|token|secret|authorization|password|"
    r"steamloginsecure|sessionid|session)\b\s*[=:]\s*[^;\s]+"
)


class _RefreshFailure(Exception):
    """An expected failure whose message is safe to return to the client."""


def _record_id(value: Any) -> str:
    return str(value or "").strip()


def _kind(record: dict[str, Any]) -> str:
    return str(record.get("kind") or "").strip().lower()


def _record_identity(record: dict[str, Any]) -> tuple[Any, ...]:
    """Identify the exact immutable record observed before the network read."""
    item = record.get("item") if isinstance(record.get("item"), dict) else {}
    strategy = record.get("strategy") if isinstance(record.get("strategy"), dict) else {}
    return (
        _record_id(record.get("id")),
        _kind(record),
        str(record.get("account_id") or "").strip(),
        record.get("evaluated_at"),
        record.get("created_at"),
        record.get("schema_version"),
        record.get("rules_version"),
        str(item.get("market_hash_name") or item.get("name") or ""),
        str(item.get("goods_id") or ""),
        str(item.get("assetid") or ""),
        str(strategy.get("id") or strategy.get("strategy_id") or ""),
    )


def _public_snapshot(record_id: str, job: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": record_id,
        "status": str(job.get("status") or "queued"),
    }
    if job.get("error"):
        result["error"] = str(job["error"])
    if job.get("version_id") is not None:
        result["version_id"] = job["version_id"]
    return result


def _drop_finished_locked(record_id: str) -> None:
    retained = [entry for entry in _FINISHED_JOBS if entry[0] != record_id]
    _FINISHED_JOBS.clear()
    _FINISHED_JOBS.extend(retained)


def _record_finished_locked(record_id: str, job: dict[str, Any]) -> None:
    token = job["token"]
    # Replacing a terminal job starts a new history entry.  Drop stale entries
    # first so the bound always counts real retained completed jobs.
    retained = [
        entry
        for entry in _FINISHED_JOBS
        if (
            (existing := _JOBS.get(entry[0])) is not None
            and existing.get("token") is entry[1]
            and existing.get("status") in _TERMINAL_STATUSES
        )
    ]
    _FINISHED_JOBS.clear()
    _FINISHED_JOBS.extend(retained)
    _FINISHED_JOBS.append((record_id, token))
    while len(_FINISHED_JOBS) > _MAX_FINISHED_JOBS:
        old_id, old_token = _FINISHED_JOBS.popleft()
        existing = _JOBS.get(old_id)
        if existing is not None and existing.get("token") is old_token:
            _JOBS.pop(old_id, None)


def _set_job_state(
    record_id: str,
    token: object,
    status: str,
    *,
    error: Optional[str] = None,
    version_id: Optional[Any] = None,
) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(record_id)
        if job is None or job.get("token") is not token:
            return
        job["status"] = status
        job["error"] = error
        if version_id is not None:
            job["version_id"] = version_id
        if status in _TERMINAL_STATUSES:
            job["finished_at"] = _clock()
            _record_finished_locked(record_id, job)


def _discard_job(record_id: str, token: object) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(record_id)
        if job is not None and job.get("token") is token:
            _JOBS.pop(record_id, None)
            _drop_finished_locked(record_id)


def _store_failed(
    record_id: str,
    error: str,
    *,
    record_identity: Optional[tuple[Any, ...]] = None,
) -> dict[str, Any]:
    job = {
        "token": object(),
        "status": "failed",
        "error": error,
        "version_id": None,
        "record_identity": record_identity,
        "thread": None,
        "created_at": _clock(),
        "finished_at": _clock(),
    }
    with _JOBS_LOCK:
        existing = _JOBS.get(record_id)
        if existing is not None and existing.get("status") in _ACTIVE_STATUSES:
            return _public_snapshot(record_id, existing)
        _JOBS[record_id] = job
        _record_finished_locked(record_id, job)
    return _public_snapshot(record_id, job)


def _record_is_gone(record_id: str) -> bool:
    try:
        decision_records.get_record(record_id)
    except decision_records.DecisionError:
        return True
    except Exception:
        return False
    return False


def _job_record_is_current(
    record_id: str, expected_identity: Optional[tuple[Any, ...]]
) -> bool:
    """Check that a retained job still belongs to the current persisted record."""
    try:
        record = decision_records.get_record(record_id)
    except decision_records.DecisionError:
        return False
    except Exception:
        # A transient storage failure must not erase an otherwise useful status.
        return True
    if not isinstance(record, dict):
        return False
    return expected_identity is None or _record_identity(record) == expected_identity


def _validate_record(record: dict[str, Any], record_id: str) -> None:
    if not isinstance(record, dict):
        raise _RefreshFailure("决策记录不可用")
    if _record_id(record.get("id")) != record_id:
        raise _RefreshFailure("决策记录标识不一致")
    if _kind(record) not in _KINDS:
        raise _RefreshFailure("仅支持买入或普通上架记录的主动刷新")
    if record.get("schema_version") != _SCHEMA_VERSION:
        raise _RefreshFailure("决策记录数据版本暂不支持刷新")
    if record.get("rules_version") != rules_version(_kind(record)):
        raise _RefreshFailure("规则代码已更新或记录缺少规则版本，请等待新的正式检查；原解释仍可查看")
    if not isinstance(record.get("inputs"), dict):
        raise _RefreshFailure("决策记录缺少可刷新输入")
    if not isinstance(record.get("item"), dict):
        raise _RefreshFailure("决策记录缺少商品信息")
    if not isinstance(record.get("strategy"), dict):
        raise _RefreshFailure("决策记录缺少策略快照")
    if not str(record.get("account_id") or "").strip():
        raise _RefreshFailure("决策记录缺少账号信息")


def _account_guard(record: dict[str, Any]) -> None:
    """Fail closed unless the persisted account still owns the session."""
    expected = str(record.get("account_id") or "").strip()
    if not expected:
        raise _RefreshFailure("决策记录缺少账号信息")

    from app import accounts
    from app.config_loader import get_steam_credentials
    from app.services.steam_auth import steam_id_from_cookie_str

    account = accounts.get_account(expected)
    current_id = str(accounts.get_current_id() or "").strip()
    current = accounts.get_current_account() or {}
    if not account or current_id != expected or str(current.get("id") or "") != expected:
        raise _RefreshFailure("原记录账号不是当前账号，已停止刷新；不会切换账号")

    credentials = get_steam_credentials() or {}
    cookies = str(credentials.get("cookies") or "").strip()
    if not cookies:
        raise _RefreshFailure("原记录账号的 Steam 会话不可用，已停止刷新")

    expected_steam_id = str(account.get("steam_id") or "").strip()
    if not expected_steam_id:
        return
    actual_steam_id = str(credentials.get("steam_id") or "").strip()
    if actual_steam_id and actual_steam_id != expected_steam_id:
        raise _RefreshFailure("Steam 会话与记录账号不一致，已停止刷新；不会切换账号")
    cookie_steam_id = str(steam_id_from_cookie_str(cookies) or "").strip()
    if cookie_steam_id and cookie_steam_id != expected_steam_id:
        raise _RefreshFailure("Steam Cookie 与记录账号不一致，已停止刷新；不会切换账号")


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, _RefreshFailure):
        message = str(exc)
    else:
        message = f"刷新失败（{type(exc).__name__}），请稍后重试；原检查结果未改变"
    return _SENSITIVE.sub(r"\1=[已隐藏]", message)[:200]


def _run_refresh(
    record_id: str,
    token: object,
    expected_identity: Optional[tuple[Any, ...]],
) -> None:
    _set_job_state(record_id, token, "running")
    try:
        try:
            record = decision_records.get_record(record_id)
        except decision_records.DecisionError:
            _discard_job(record_id, token)
            return

        _validate_record(record, record_id)
        if expected_identity is not None and _record_identity(record) != expected_identity:
            _discard_job(record_id, token)
            return
        _account_guard(record)
        identity = _record_identity(record)
        sources = _fetch_sources(record)

        # A long request may outlive a record cleanup/account switch.
        _account_guard(record)
        try:
            current = decision_records.get_record(record_id)
        except decision_records.DecisionError:
            _discard_job(record_id, token)
            return
        _validate_record(current, record_id)
        if _record_identity(current) != identity:
            # Cleanup may make an id reusable after the network read.  Never
            # append the old job's evidence to a different replacement record.
            _discard_job(record_id, token)
            return

        evaluated_at = float(_clock())
        inputs = _build_refreshed_inputs(record, sources, evaluated_at)
        try:
            version = decision_records.add_version(record_id, inputs, evaluated_at)
        except decision_records.DecisionError:
            if _record_is_gone(record_id):
                _discard_job(record_id, token)
                return
            raise _RefreshFailure("新数据版本保存失败，原检查结果未改变") from None
        if not isinstance(version, dict) or version.get("id") is None:
            raise _RefreshFailure("新数据版本保存结果无效，原检查结果未改变")
        _set_job_state(record_id, token, "succeeded", version_id=version["id"])
    except Exception as exc:
        if _record_is_gone(record_id):
            _discard_job(record_id, token)
            return
        _set_job_state(record_id, token, "failed", error=_safe_error(exc))


def start_refresh(record_id: Any) -> dict[str, Any]:
    """Queue one read-only refresh, deduplicating all active work by record id."""
    normalized_id = _record_id(record_id)
    if not normalized_id:
        return {"id": normalized_id, "status": "failed", "error": "决策记录标识无效"}

    # Observe active work before touching account state.  A later account
    # switch belongs to the running job's post-fetch guard; a duplicate click
    # must not replace that job with a second status entry.
    with _JOBS_LOCK:
        existing = _JOBS.get(normalized_id)
        if existing is not None and existing.get("status") in _ACTIVE_STATUSES:
            return _public_snapshot(normalized_id, existing)

    try:
        record = decision_records.get_record(normalized_id)
    except decision_records.DecisionError:
        with _JOBS_LOCK:
            stale = _JOBS.get(normalized_id)
            if stale is not None:
                _discard_job(normalized_id, stale.get("token"))
        return {"id": normalized_id, "status": "failed", "error": "决策记录不存在或已被清理"}
    except Exception:
        return {"id": normalized_id, "status": "failed", "error": "决策记录存储暂不可用"}

    try:
        _validate_record(record, normalized_id)
        _account_guard(record)
    except Exception as exc:
        identity = _record_identity(record) if isinstance(record, dict) else None
        return _store_failed(
            normalized_id, _safe_error(exc), record_identity=identity
        )

    with _JOBS_LOCK:
        existing = _JOBS.get(normalized_id)
        if existing is not None and existing.get("status") in _ACTIVE_STATUSES:
            return _public_snapshot(normalized_id, existing)

        token = object()
        identity = _record_identity(record)
        job = {
            "token": token,
            "status": "queued",
            "error": None,
            "version_id": None,
            "record_identity": identity,
            "thread": None,
            "created_at": _clock(),
            "finished_at": None,
        }
        _drop_finished_locked(normalized_id)
        _JOBS[normalized_id] = job
        thread = threading.Thread(
            target=_run_refresh,
            args=(normalized_id, token, identity),
            name=f"decision-refresh-{normalized_id[:24]}",
            daemon=True,
        )
        job["thread"] = thread
        thread.start()
        return _public_snapshot(normalized_id, job)


def refresh_status(record_id: Any) -> Optional[dict[str, Any]]:
    """Return current status, or ``None`` for unknown/cleaned/lost jobs."""
    normalized_id = _record_id(record_id)
    if not normalized_id:
        return None
    with _JOBS_LOCK:
        job = _JOBS.get(normalized_id)
        if job is None:
            return None
        snapshot = _public_snapshot(normalized_id, job)
        token = job.get("token")
        identity = job.get("record_identity")
    if not _job_record_is_current(normalized_id, identity):
        _discard_job(normalized_id, token)
        return None
    return snapshot


def _strategy_params(record: dict[str, Any]) -> dict[str, Any]:
    # Snapshots contain hydrated step params, never the live pipeline config.
    params = {}
    for step in record["strategy"].get("steps", []):
        if step.get("enabled") is not False:
            params.update(step.get("params") or {})
    return params


def _item_for(record: dict[str, Any]) -> dict[str, Any]:
    item = record.get("item")
    if isinstance(item, dict):
        return item
    inputs = record.get("inputs") or {}
    nested = inputs.get("item") if isinstance(inputs, dict) else None
    return nested if isinstance(nested, dict) else {}


def _market_name(record: dict[str, Any]) -> str:
    item = _item_for(record)
    return str(item.get("market_hash_name") or item.get("name") or "").strip()


def _app_id(record: dict[str, Any]) -> int:
    item = _item_for(record)
    try:
        value = int(item.get("appid") or item.get("app_id") or 730)
    except (TypeError, ValueError):
        return 730
    return value if value > 0 else 730


def _unit_price(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return round(price, 6) if math.isfinite(price) and price > 0 else None


def _quantity(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        quantity = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return quantity if quantity > 0 else None


def _normalise_orders(orders: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for order in orders or []:
        if isinstance(order, dict):
            price = _unit_price(order.get("price"))
            quantity = _quantity(order.get("quantity", order.get("num")))
        elif isinstance(order, (list, tuple)) and len(order) >= 2:
            price = _unit_price(order[0])
            quantity = _quantity(order[1])
        else:
            continue
        if price is not None and quantity is not None:
            result.append({"price": price, "quantity": quantity})
    result.sort(key=lambda row: row["price"])
    return result


def _history_timestamp(value: Any) -> float:
    if isinstance(value, str):
        parsed = parse_steam_history_date(value)
        if parsed is None:
            raise ValueError("invalid Steam history date")
        return parsed.timestamp()
    return float(value)


def _normalise_history_rows(rows: Any) -> list[list[Any]]:
    result: list[list[Any]] = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        try:
            stamp = _history_timestamp(row[0])
            price = float(row[1])
            volume = float(row[2])
        except (TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(value) for value in (stamp, price, volume)):
            continue
        if stamp <= 0 or price <= 0 or volume < 0:
            continue
        result.append([row[0], round(price, 6), int(volume) if volume.is_integer() else volume])
    result.sort(key=lambda row: _history_timestamp(row[0]))
    return result


def _safe_source_reason(reason: Any, fallback: str) -> str:
    text = _SENSITIVE.sub(r"\1=[已隐藏]", str(reason or fallback).strip())
    return text[:180] or fallback


def _fetch_steam_market(record: dict[str, Any]) -> dict[str, Any]:
    name = _market_name(record)
    if not name:
        return {"available": False, "reason": "Steam 市场名为空", "sell_orders": []}

    from app.config_loader import get_steam_credentials
    from steam.market_orders import compute_smart_list_price, get_sell_orders_cny
    from steam.session import create_market_session

    credentials = (record.get("_refresh_credentials") or {}).get("steam")
    if credentials is None:
        credentials = get_steam_credentials() or {}
    cookies = str(credentials.get("cookies") or "")
    steam_id = str(credentials.get("steam_id") or "")
    if not cookies or not steam_id:
        return {"available": False, "reason": "Steam 会话不可用", "sell_orders": []}

    pipeline = _strategy_params(record)
    inputs = record.get("inputs") or {}
    valuation = inputs.get("valuation") if isinstance(inputs, dict) else {}
    try:
        usd_to_cny = float(
            (valuation or {}).get("usd_to_cny")
            or pipeline.get("usd_to_cny")
            or 7.2
        )
    except (TypeError, ValueError):
        usd_to_cny = 7.2

    session = None
    try:
        session = create_market_session(cookies, steam_id)
        result, reason = get_sell_orders_cny(
            session,
            name,
            app_id=_app_id(record),
            request_delay=1.0,
            use_cache=False,
            return_error=True,
            usd_to_cny_rate=usd_to_cny,
        )
    except Exception as exc:
        raise _RefreshFailure(f"Steam 行情获取失败（{type(exc).__name__}）") from None
    finally:
        if session is not None:
            session.close()

    reason = _safe_source_reason(reason, "Steam 行情不可用")
    if "429" in reason or "限流" in reason:
        raise _RefreshFailure("Steam 行情接口受限，请稍后重试")
    orders = _normalise_orders((result or {}).get("sell_orders") if isinstance(result, dict) else None)
    if not orders:
        return {"available": False, "reason": reason, "sell_orders": []}

    pricing_defaults = {"sell_price_wall_volume": 20, "sell_price_max_ignore_volume": 4}
    policy = inputs.get("policy") or {}
    pricing_params = {
        key: pipeline.get(key, policy.get(key, value))
        for key, value in pricing_defaults.items()
    }
    pricing_params["defaulted"] = [
        key for key in pricing_defaults if key not in pipeline and key not in policy
    ]
    smart_price = None
    try:
        smart_price, _ = compute_smart_list_price(
            [(row["price"], row["quantity"]) for row in orders],
            wall_volume_threshold=int(pricing_params["sell_price_wall_volume"]),
            max_ignore_volume=int(pricing_params["sell_price_max_ignore_volume"]),
            min_step=0,
            offset=0,
        )
    except Exception:
        smart_price = None
    return {
        "available": True,
        "reason": None,
        "smart_price": smart_price,
        "currency": "CNY",
        "pricing_params": pricing_params,
        "sell_orders": orders,
    }


def _fetch_buff_market(record: dict[str, Any]) -> dict[str, Any]:
    item = _item_for(record)
    try:
        goods_id = int(item.get("goods_id") or 0)
    except (TypeError, ValueError):
        goods_id = 0
    if goods_id <= 0:
        return {"available": False, "reason": "BUFF 商品标识为空", "orders": []}

    from app.config_loader import get_buff_credentials
    from app.services.buff_client import BuffClient
    from buff import BuffRateLimited, BuffRequestBlocked

    credentials = (record.get("_refresh_credentials") or {}).get("buff")
    if credentials is None:
        credentials = get_buff_credentials() or {}
    cookies = str(credentials.get("cookies") or "").strip()
    if not cookies:
        return {"available": False, "reason": "BUFF 会话不可用", "orders": []}

    client = BuffClient(
        cookies,
        user_agent=str(credentials.get("user_agent") or "").strip() or None,
    )
    try:
        orders = client.get_sell_orders(goods_id, "csgo")
    except BuffRateLimited:
        raise _RefreshFailure("BUFF 行情接口受限，请稍后重试") from None
    except BuffRequestBlocked:
        raise _RefreshFailure("BUFF 会话需要验证或受到风控限制") from None
    except Exception as exc:
        raise _RefreshFailure(f"BUFF 行情获取失败（{type(exc).__name__}）") from None
    finally:
        client.close()

    # BUFF returns one asset per sell order, not aggregated depth tiers.
    normalized = _normalise_orders([
        {"price": order.get("price"), "quantity": 1}
        for order in orders or [] if isinstance(order, dict)
    ])
    if not normalized:
        return {"available": False, "reason": "BUFF 卖单不可用", "orders": []}
    return {"available": True, "reason": None, "orders": normalized}


def _fetch_steam_history(record: dict[str, Any]) -> dict[str, Any]:
    name = _market_name(record)
    if not name:
        return {"available": False, "reason": "Steam 市场名为空", "rows": [], "currency": None}

    from app.services.steam_client import SteamClient

    client = SteamClient(cache_ttl=0)
    try:
        credentials = (record.get("_refresh_credentials") or {}).get("steam")
        kwargs = {"credentials": credentials} if credentials is not None else {}
        result = client.fetch_history(name, app_id=_app_id(record), return_currency=True, **kwargs)
    except Exception as exc:
        raise _RefreshFailure(f"Steam 历史行情获取失败（{type(exc).__name__}）") from None
    if not result:
        try:
            from app.services.steam_client import _history_cooldown

            if _history_cooldown.remaining() > 0:
                raise _RefreshFailure("Steam 历史行情接口受限，请稍后重试")
        except ImportError:
            pass
        return {"available": False, "reason": "Steam 历史行情不可用", "rows": [], "currency": None}
    if isinstance(result, dict):
        rows = result.get("history")
        currency = result.get("currency")
    else:
        rows = result
        currency = None
    normalized = _normalise_history_rows(rows)
    if not normalized:
        return {
            "available": False,
            "reason": "Steam 历史行情为空或无法解析",
            "rows": [],
            "currency": currency,
        }
    return {"available": True, "reason": None, "rows": normalized, "currency": currency}


def _fetch_sources(record: dict[str, Any]) -> dict[str, Any]:
    """Pin credentials for the entire read; never mix sessions between sources."""
    from app.config_loader import get_buff_credentials, get_steam_credentials
    _account_guard(record)
    credentials = {
        "steam": copy.deepcopy(get_steam_credentials() or {}),
        "buff": copy.deepcopy(get_buff_credentials() or {}),
    }
    _account_guard(record)
    # Verify the captured Steam identity itself, not only today's account.
    from app import accounts
    from app.services.steam_auth import steam_id_from_cookie_str
    account = accounts.get_account(record["account_id"]) or {}
    expected = str(account.get("steam_id") or "")
    steam = credentials["steam"]
    identities = (str(steam.get("steam_id") or ""), steam_id_from_cookie_str(steam.get("cookies") or ""))
    if expected and any(identity and str(identity) != expected for identity in identities):
        raise _RefreshFailure("刷新会话与原记录账号不一致，已停止刷新")
    request_record = {**record, "_refresh_credentials": credentials}
    sources = {}
    for key, fetch in (("steam", _fetch_steam_market), ("history", _fetch_steam_history)):
        _account_guard(record)
        sources[key] = fetch(request_record)
        _account_guard(record)
    if _kind(record) == "buy":
        sources["buff"] = _fetch_buff_market(request_record)
        _account_guard(record)
    return sources


def _source_state(source: Any) -> str:
    return "available" if isinstance(source, dict) and source.get("available") is True else "unavailable"


def _buy_market_sources(sources: dict[str, Any], evaluated_at: float) -> dict[str, Any]:
    buff = sources.get("buff") if isinstance(sources.get("buff"), dict) else {}
    steam = sources.get("steam") if isinstance(sources.get("steam"), dict) else {}
    history = sources.get("history") if isinstance(sources.get("history"), dict) else {}
    buff_orders = _normalise_orders(buff.get("orders"))
    steam_orders = _normalise_orders(steam.get("sell_orders"))
    steam_smart_price = _unit_price(steam.get("smart_price"))
    rows = _normalise_history_rows(history.get("rows"))
    lowest = min((row["price"] for row in buff_orders), default=None)
    count_at_lowest = sum(1 for row in buff_orders if row["price"] == lowest) if lowest is not None else 0
    history_snapshot: dict[str, Any] = {
        "available": bool(history.get("available") is True and rows),
        "reason": None if history.get("available") is True and rows else _safe_source_reason(
            history.get("reason"), "历史行情不可用"
        ),
        "source": "steam_history",
        "captured_at": evaluated_at,
        "currency": history.get("currency"),
        "rows": rows,
    }
    if isinstance(history.get("adjustment_rows"), list):
        history_snapshot["adjustment_rows"] = _normalise_history_rows(history["adjustment_rows"])
    return {
        "buff": {
            "available": bool(buff.get("available") is True and buff_orders),
            "reason": None if buff.get("available") is True and buff_orders else _safe_source_reason(
                buff.get("reason"), "BUFF 行情不可用"
            ),
            "source": "buff_market",
            "captured_at": evaluated_at,
            "lowest_price": lowest,
            "count_at_lowest": count_at_lowest,
            "orders": buff_orders,
        },
        "steam": {
            "available": bool(
                steam.get("available") is True
                and steam_orders
                and steam_smart_price is not None
            ),
            "reason": None if (
                steam.get("available") is True
                and steam_orders
                and steam_smart_price is not None
            ) else _safe_source_reason(
                steam.get("reason"), "Steam 智能价或卖单不可用"
            ),
            "source": "steam_market",
            "captured_at": evaluated_at,
            "smart_price": steam_smart_price,
            "currency": "CNY",
            "pricing_params": copy.deepcopy(steam.get("pricing_params")),
            "sell_orders": steam_orders,
        },
        "history": history_snapshot,
    }


def _sell_market_sources(
    record: dict[str, Any], sources: dict[str, Any], evaluated_at: float
) -> tuple[dict[str, Any], dict[str, Any]]:
    steam = sources.get("steam") if isinstance(sources.get("steam"), dict) else {}
    history = sources.get("history") if isinstance(sources.get("history"), dict) else {}
    orders = _normalise_orders(steam.get("sell_orders"))
    rows = _normalise_history_rows(history.get("rows"))
    pipeline = _strategy_params(record)
    try:
        window_days = int(pipeline.get("sell_trend_days", 7) or 7)
    except (TypeError, ValueError):
        window_days = 7
    # Keep all observations so each parameter set can select its own window.
    points = [
        {"captured_at": _history_timestamp(row[0]), "price": row[1], "volume": row[2]}
        for row in rows
    ]
    market = {
        "source": "steam_market",
        "captured_at": evaluated_at,
        "currency": "CNY",
        "available": bool(steam.get("available") is True and orders),
        "sell_orders": orders,
        "smart_price": steam.get("smart_price"),
        "error": None if steam.get("available") is True and orders else _safe_source_reason(
            steam.get("reason"), "Steam 行情不可用"
        ),
    }
    trend = {
        "source": "steam_history",
        "captured_at": evaluated_at,
        "window_days": window_days,
        "available": bool(history.get("available") is True and rows),
        "points": points,
        "error": None if history.get("available") is True and rows else _safe_source_reason(
            history.get("reason"), "历史行情不可用"
        ),
        "currency": history.get("currency"),
    }
    return market, trend


def _build_refreshed_inputs(
    record: dict[str, Any], sources: dict[str, Any], evaluated_at: float
) -> dict[str, Any]:
    """Build one common input snapshot for both original and draft parameters."""
    inputs = copy.deepcopy(record.get("inputs") or {})
    if not isinstance(inputs, dict):
        inputs = {}

    input_item = inputs.get("item")
    item = copy.deepcopy(input_item) if isinstance(input_item, dict) else {}
    # The record keeps safe identifiers, while inputs may also hold rule
    # context such as min_price/daily_volume/ratio.  Merge identities without
    # dropping those comparison inputs.
    item.update(copy.deepcopy(_item_for(record)))
    inputs["item"] = item

    if _kind(record) == "buy":
        market = _buy_market_sources(sources, evaluated_at)
        inputs["market"] = market
        snapshot_kind = "buy"
        preserved = [key for key in ("account", "valuation") if key in inputs]
        preserved.extend(
            f"item.{key}" for key in ("daily_volume", "ratio") if key in item
        )
        updated = ["market"]
        buff_lowest = _unit_price(market["buff"].get("lowest_price"))
        if buff_lowest is not None:
            # The buy evaluator treats item.min_price as the current BUFF
            # purchase price.  Keep inferred/source context such as daily
            # volume and ratio explicit rather than silently refreshing it.
            item["min_price"] = buff_lowest
            updated.insert(0, "item.min_price")
        else:
            preserved.append("item.min_price")
    else:
        inputs["market"], inputs["trend"] = _sell_market_sources(record, sources, evaluated_at)
        snapshot_kind = "sell"
        preserved = [
            key
            for key in ("item", "inventory", "ownership", "listing", "currency")
            if key in inputs
        ]
        updated = ["market", "trend"]

    source_states = {
        name: _source_state(sources.get(name))
        for name in ("steam", "history", "buff")
        if name in sources
    }
    inputs["data_version"] = f"refresh:{evaluated_at:g}"
    inputs["refresh_meta"] = {
        "kind": snapshot_kind,
        "refreshed_at": evaluated_at,
        "record_evaluated_at": record.get("evaluated_at"),
        "record_schema_version": record.get("schema_version"),
        "sources": source_states,
        "preserved": preserved,
        "updated": updated,
    }
    return inputs
