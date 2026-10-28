from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.strategy_engine import (
    StrategyError,
    activate_strategy,
    delete_strategy,
    export_strategy,
    get_strategy_payload,
    import_strategy,
    import_user_module,
    save_strategy,
    simulate_strategy,
)


router = APIRouter()


class StrategyBody(BaseModel):
    strategy: dict


class ActivateBody(BaseModel):
    risk_confirmed: bool = False


class ImportBody(BaseModel):
    strategy: dict


class ModuleImportBody(BaseModel):
    manifest: dict


class PreviewCopyBody(BaseModel):
    name: str
    params: dict = Field(default_factory=dict)


class SimulateBody(BaseModel):
    record_id: str = ""
    version_id: str = "original"
    params: dict = Field(default_factory=dict)
    strategy_id: str = ""
    strategy: dict = Field(default_factory=dict)


def _error(exc: Exception):
    return {"ok": False, "error": str(exc)}


@router.get("/api/strategies")
def api_strategies():
    return {"ok": True, **get_strategy_payload(), "runtime": api_strategy_runtime()}


@router.get("/api/strategies/runtime")
def api_strategy_runtime():
    from app.pipeline import get_buy_policy_status, is_pipeline_running
    return {"pipeline_active": is_pipeline_running(), "buy": get_buy_policy_status()}


@router.post("/api/strategies")
def api_save_strategy(body: StrategyBody):
    try:
        return {"ok": True, "strategy": save_strategy(body.strategy)}
    except StrategyError as exc:
        return _error(exc)


@router.delete("/api/strategies/{strategy_id}")
def api_delete_strategy(strategy_id: str):
    try:
        delete_strategy(strategy_id)
        return {"ok": True}
    except StrategyError as exc:
        return _error(exc)


@router.post("/api/strategies/{strategy_id}/activate")
def api_activate_strategy(strategy_id: str, body: ActivateBody):
    try:
        return activate_strategy(strategy_id, risk_confirmed=body.risk_confirmed)
    except StrategyError as exc:
        return _error(exc)


@router.post("/api/strategies/import")
def api_import_strategy(body: ImportBody):
    try:
        return {"ok": True, "strategy": import_strategy(body.strategy)}
    except StrategyError as exc:
        return _error(exc)


@router.get("/api/strategies/{strategy_id}/export")
def api_export_strategy(strategy_id: str):
    try:
        return {"ok": True, "strategy": export_strategy(strategy_id)}
    except StrategyError as exc:
        return _error(exc)


@router.post("/api/strategy-modules/import")
def api_import_strategy_module(body: ModuleImportBody):
    try:
        return {"ok": True, "module": import_user_module(body.manifest)}
    except StrategyError as exc:
        return _error(exc)


@router.post("/api/strategies/simulate")
def api_simulate_strategy(body: SimulateBody):
    try:
        payload = body.model_dump()
        return simulate_strategy(payload)
    except (StrategyError, ValueError) as exc:
        return _error(exc)
    except Exception:
        return _error(ValueError("预演不可用，请检查记录的数据和版本"))


@router.get("/api/strategy-decisions")
def api_decision_list(kind: str = "", name: str = "", status: str = "", page: int = 1, page_size: int = 20):
    from app.decision_records import list_records
    try:
        return list_records(kind=kind, name=name, status=status, page=page, page_size=page_size)
    except ValueError as exc:
        return _error(exc)
    except Exception:
        return _error(ValueError("检查记录读取失败，请稍后重试"))


@router.get("/api/strategy-decisions/{record_id}")
def api_decision_detail(record_id: str):
    from app.decision_records import get_record, list_versions
    try:
        from app.decision_refresh import refresh_status
        return {"ok": True, "record": get_record(record_id), "versions": list_versions(record_id), "refresh": refresh_status(record_id)}
    except ValueError as exc:
        return _error(exc)
    except Exception:
        return _error(ValueError("检查记录读取失败，请稍后重试"))


@router.post("/api/strategy-decisions/{record_id}/copy")
def api_decision_copy(record_id: str, body: PreviewCopyBody):
    from app.decision_preview import save_copy
    try:
        return save_copy(record_id, body.name, body.params)
    except (StrategyError, ValueError) as exc:
        return _error(exc)
    except Exception:
        return _error(ValueError("保存副本失败，正式策略未改变"))


@router.post("/api/strategy-decisions/{record_id}/refresh")
def api_decision_refresh(record_id: str):
    from app.decision_refresh import start_refresh
    try:
        return {"ok": True, "refresh": start_refresh(record_id)}
    except ValueError as exc:
        return _error(exc)
    except Exception:
        return _error(ValueError("行情刷新不可用，请稍后重试"))
