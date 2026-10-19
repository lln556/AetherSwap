"""Durable trading facts. Prices are currency minor units, timestamps are UTC epochs."""
import time
import uuid
from typing import Optional
from sqlmodel import SQLModel, Field


def uid():
    return uuid.uuid4().hex


class TradeEvent(SQLModel, table=True):
    id: str = Field(default_factory=uid, primary_key=True)
    dedupe_key: Optional[str] = Field(default=None, unique=True)
    purchase_id: int = Field(index=True)
    kind: str = Field(index=True)
    message: str
    occurred_at: Optional[float] = None
    recorded_at: float = Field(default_factory=time.time, index=True)
    precision: str = "exact"
    source: str = "application"
    details: str = "{}"


class MarketListing(SQLModel, table=True):
    id: str = Field(default_factory=uid, primary_key=True)
    purchase_id: int = Field(index=True)
    account_id: str = Field(default="", index=True)
    assetid: str = Field(index=True)
    steam_listing_id: Optional[str] = Field(default=None, unique=True)
    legacy_sale_id: Optional[int] = Field(default=None, unique=True)
    status: str = Field(default="submitted", index=True)
    buyer_minor: Optional[int] = None
    seller_minor: Optional[int] = None
    currency: str = "CNY"
    submitted_at: float = Field(default_factory=time.time)
    activated_at: Optional[float] = None
    activation_source: str = "unknown"
    checked_at: Optional[float] = None
    ended_at: Optional[float] = None
    next_evaluation_at: Optional[float] = Field(default=None, index=True)
    last_reason: str = ""


class TradeSettlement(SQLModel, table=True):
    purchase_id: int = Field(primary_key=True)
    external_id: Optional[str] = Field(default=None, unique=True)
    assetid: str = ""
    proceeds_minor: Optional[int] = None
    currency: str = "CNY"
    amount_source: str = "estimated_legacy"
    sold_at: Optional[float] = None
    sold_day: Optional[str] = Field(default=None, index=True)
    date_label: str = ""
    time_precision: str = "unknown"
    detected_at: float = Field(default_factory=time.time)


class RepriceJob(SQLModel, table=True):
    id: str = Field(default_factory=uid, primary_key=True)
    listing_id: str = Field(unique=True)
    purchase_id: int = Field(index=True)
    account_id: str
    old_assetid: str
    assetid: str
    old_steam_listing_id: str
    status: str = Field(default="planned", index=True)
    old_buyer_minor: int
    target_buyer_minor: int
    target_seller_minor: int
    floor_seller_minor: int
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    retry_at: float = 0
    attempts: int = 0
    error: str = ""
    review_from: str = ""
    consecutive_failures: int = 0
    last_quoted_at: float = Field(default_factory=time.time)


class TradingPolicy(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    enabled: bool = False
    age_hours: int = 24
    max_loss_percent: int = 30
    min_change_percent: float = 1.0
    min_change_minor: int = 10
    reevaluate_minutes: int = 60
    check_interval_seconds: int = 600
    last_check_at: Optional[float] = None
    next_check_at: Optional[float] = None
    last_error: str = ""
    mutation_retry_at: float = 0
    mutation_error: str = ""
