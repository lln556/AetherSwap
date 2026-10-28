"""Durable decision evidence. Reads never consult a live trading account.

Only call capture_decision with explicitly selected business inputs. Runtime
configuration and platform responses must never be passed as inputs.
"""
import copy
import json
import logging
import math
import re
import threading
import time
import uuid

from sqlalchemy import delete, func
from sqlmodel import Session, select

from app import database
from app.decision_models import StrategyDecision, StrategyDecisionVersion

SCHEMA_VERSION = 1
RETENTION_DAYS = 7
MAX_RECORDS = 10_000
_lock = threading.RLock()
_recording_error = ""
_sensitive = re.compile(r"cookie|token|secret|password|passwd|session|credential|api.?key|private.?key|public.?key|access.?key|^auth$|payment_url|pay_url|pay_link|payment_link|authorization|headers|^config$", re.I)


class DecisionError(ValueError):
    pass


def _safe(value):
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items() if not _sensitive.search(str(k))}
    if isinstance(value, (tuple, list)):
        return [_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str):
        # Links are not required by rule calculations; never persist checkout
        # URLs or signed platform URLs hidden inside otherwise innocuous text.
        return re.sub(r"https?://[^\s<>\"']+", "[链接已隐藏]", value, flags=re.I)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    raise DecisionError("检查输入包含不支持的数据类型")


def snapshot_strategy(config, kind):
    from app import strategy_engine as se
    strategy_type = "buy" if kind == "buy" else "sell"
    runtime = (config.get("_strategy_runtime") or {}).get(strategy_type) or {}
    if runtime.get("strategy"):
        return copy.deepcopy(runtime["strategy"]), copy.deepcopy(runtime.get("modules") or {})
    # Older callers can still supply a resolved config without a runtime snapshot.
    strategy = se.get_strategy(runtime.get("active_strategy_id") or se.get_active_strategy_ids(config)[strategy_type])
    if not strategy:
        raise DecisionError("检查使用的策略不可用")
    strategy = se._hydrate_strategy_params(strategy, config)
    modules = se._module_by_id()
    return strategy, {s["module_id"]: copy.deepcopy(modules.get(s["module_id"]) or {}) for s in strategy["steps"]}


def _encode(value):
    return json.dumps(_safe(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _cleanup(session, now):
    expired = select(StrategyDecision.id).where(StrategyDecision.created_at <= now - RETENTION_DAYS * 86400)
    session.exec(delete(StrategyDecisionVersion).where(StrategyDecisionVersion.record_id.in_(expired)))
    session.exec(delete(StrategyDecision).where(StrategyDecision.created_at <= now - RETENTION_DAYS * 86400))
    excess = session.exec(select(StrategyDecision.id).order_by(StrategyDecision.created_at.desc(), StrategyDecision.id.desc()).offset(MAX_RECORDS)).all()
    # Chunk to keep SQLite's bound variable limit predictable.
    for offset in range(0, len(excess), 500):
        ids = excess[offset:offset + 500]
        session.exec(delete(StrategyDecisionVersion).where(StrategyDecisionVersion.record_id.in_(ids)))
        session.exec(delete(StrategyDecision).where(StrategyDecision.id.in_(ids)))


def report_recording_failure():
    global _recording_error
    _recording_error = "检查记录写入失败，本次决策解释不可用；正式规则判断未改变"
    logging.getLogger(__name__).warning(_recording_error)


def capture_decision(config, kind, item, inputs, result, evaluated_at=None):
    """Best effort: recording can never authorize or abort a real transaction."""
    global _recording_error
    try:
        from app.accounts import get_current_account
        if kind not in {"buy", "sell", "reprice"}:
            raise DecisionError("未知检查类型")
        now = time.time()
        strategy, modules = snapshot_strategy(config, kind)
        account = get_current_account() or {}
        account_id = str(config.get("_decision_account_id", account.get("id") or ""))
        safe_item = {k: item[k] for k in ("name", "market_hash_name", "goods_id", "assetid", "app_id") if k in item}
        rid = uuid.uuid4().hex
        from app.decision_versions import rules_version
        payload = {"item": safe_item, "strategy": strategy, "modules": modules, "inputs": inputs, "result": result,
                   "rules_version": rules_version(kind)}
        row = StrategyDecision(id=rid, kind=kind, account_id=account_id,
                               name=str(safe_item.get("name") or safe_item.get("market_hash_name") or ""),
                               status=result.get("status", "unavailable"), evaluated_at=now if evaluated_at is None else evaluated_at,
                               created_at=now, schema_version=SCHEMA_VERSION, payload=_encode(payload))
        with _lock, Session(database.get_engine()) as session:
            session.add(row)
            session.flush()
            _cleanup(session, now)
            session.commit()
        _recording_error = ""
        return rid
    except Exception:
        # Do not surface exceptions that might contain platform credentials.
        report_recording_failure()
        return None


def _payload(row):
    return {**json.loads(row.payload), "id": row.id, "kind": row.kind, "account_id": row.account_id,
            "evaluated_at": row.evaluated_at, "created_at": row.created_at, "schema_version": row.schema_version}


def _require(session, record_id):
    row = session.exec(select(StrategyDecision).where(StrategyDecision.id == record_id).with_for_update()).first()
    if row is None:
        session.commit()  # Keep retention cleanup even when this lookup expired.
        raise DecisionError("检查记录不存在或已过期清理，无法使用")
    return row


def get_record(record_id):
    with _lock, Session(database.get_engine()) as session:
        _cleanup(session, time.time())
        row = _require(session, record_id)
        payload = _payload(row)
        session.commit()
        return payload


def list_records(*, kind="", name="", status="", page=1, page_size=20):
    if kind and kind not in {"buy", "sell", "reprice"}:
        raise DecisionError("未知检查类型")
    page, page_size = max(1, page), max(1, min(100, page_size))
    with _lock, Session(database.get_engine()) as session:
        _cleanup(session, time.time())
        conditions = []
        if kind:
            conditions.append(StrategyDecision.kind == kind)
        if name:
            conditions.append(StrategyDecision.name.contains(name, autoescape=True))
        if status:
            conditions.append(StrategyDecision.status == status)
        total = session.exec(select(func.count()).select_from(StrategyDecision).where(*conditions)).one()
        rows = session.exec(select(StrategyDecision).where(*conditions).order_by(StrategyDecision.created_at.desc(), StrategyDecision.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
        items = [{k: v for k, v in _payload(row).items() if k not in {"inputs", "modules"}} for row in rows]
        session.commit()
    return {"ok": True, "items": items, "total": total, "page": page, "page_size": page_size,
            "retention": {"days": RETENTION_DAYS, "max_records": MAX_RECORDS}, "recording_error": _recording_error}


def list_versions(record_id):
    with _lock, Session(database.get_engine()) as session:
        _cleanup(session, time.time())
        _require(session, record_id)
        rows = session.exec(select(StrategyDecisionVersion).where(StrategyDecisionVersion.record_id == record_id).order_by(StrategyDecisionVersion.created_at)).all()
        result = [{"id": row.id, "created_at": row.created_at, "evaluated_at": row.evaluated_at, "inputs": json.loads(row.payload)} for row in rows]
        session.commit()
        return result


def add_version(record_id, inputs, evaluated_at):
    now = time.time()
    with _lock, Session(database.get_engine()) as session:
        _cleanup(session, now)
        _require(session, record_id)
        row = StrategyDecisionVersion(id=uuid.uuid4().hex, record_id=record_id, created_at=now, evaluated_at=evaluated_at, payload=_encode(inputs))
        session.add(row)
        session.flush()
        result = {"id": row.id, "created_at": now, "evaluated_at": evaluated_at, "inputs": json.loads(row.payload)}
        session.commit()
        return result
