"""Readable trading records and bounded automatic-repricing settings."""
import json
import time
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select
from app.database import get_session, Purchase
from app.trading_models import TradeEvent, MarketListing, RepriceJob, TradingPolicy
from app.trading_ledger import trading_overview, add_event

router = APIRouter()


class PolicyBody(BaseModel):
    enabled: bool
    age_hours: int = Field(default=24, ge=1, le=720)
    max_loss_percent: int = Field(default=30, ge=0, le=99)
    min_change_percent: float = Field(default=1, ge=0, le=50)
    min_change_minor: int = Field(default=10, ge=1, le=10000)


@router.get("/api/trading")
def overview(days: int = Query(0, ge=0, le=3650), status: str = "", query: str = "",
             page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)):
    return trading_overview(days, status, query[:200], page, page_size)


@router.get("/api/trading/items/{purchase_id}")
def detail(purchase_id: int):
    with get_session() as s:
        p = s.get(Purchase, purchase_id)
        if not p:
            raise HTTPException(404, "购买记录不存在")
        events = s.exec(select(TradeEvent).where(TradeEvent.purchase_id == purchase_id).order_by(TradeEvent.recorded_at)).all()
        listings = s.exec(select(MarketListing).where(MarketListing.purchase_id == purchase_id).order_by(MarketListing.submitted_at)).all()
        return {"item": {"id": p.id, "name": p.name, "assetid": p.assetid},
                "events": [{**e.model_dump(), "details": json.loads(e.details)} for e in events],
                "listings": [l.model_dump() for l in listings]}


@router.put("/api/trading/policy")
def set_policy(body: PolicyBody):
    with get_session() as s:
        p = s.get(TradingPolicy, 1) or TradingPolicy()
        old = p.model_dump()
        for k, v in body.model_dump().items():
            setattr(p, k, v)
        p.next_check_at = time.time()
        s.add(p)
        if old["age_hours"] != p.age_hours:
            for listing in s.exec(select(MarketListing).where(MarketListing.status == "active")).all():
                if listing.activated_at:
                    listing.next_evaluation_at = max(time.time(), listing.activated_at + p.age_hours * 3600)
                    s.add(listing)
        add_event(s, 0, "policy_changed", "自动调价设置已更新", at=time.time(), source="user",
                  details={"old": {k: old[k] for k in body.model_dump()}, "new": body.model_dump()})
        s.commit()
        s.refresh(p)
        return {"ok": True, "policy": p.model_dump()}


@router.post("/api/trading/reconcile")
def request_reconcile():
    with get_session() as s:
        p = s.get(TradingPolicy, 1) or TradingPolicy()
        p.next_check_at = time.time()
        s.add(p)
        s.commit()
    return {"ok": True, "message": "已安排市场核实，将在约 10 秒内开始"}


@router.post("/api/trading/jobs/{job_id}/verify")
def verify_job(job_id: str):
    with get_session() as s:
        job = s.get(RepriceJob, job_id)
        if not job or job.status != "needs_review":
            return {"ok": False, "error": "该任务无需人工核实"}
        job.status = job.review_from or "awaiting_inventory"
        job.consecutive_failures = 0
        job.updated_at, job.retry_at, job.error = time.time(), 0, ""
        s.add(job)
        p = s.get(TradingPolicy, 1) or TradingPolicy()
        p.next_check_at = time.time()
        s.add(p)
        add_event(s, job.purchase_id, "verification_requested", "用户请求重新核实调价结果", at=time.time(), source="user")
        s.commit()
    return {"ok": True}
