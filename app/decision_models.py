"""Independent, additive tables for strategy decision evidence."""
from sqlmodel import Field, SQLModel


class StrategyDecision(SQLModel, table=True):
    id: str = Field(primary_key=True)
    kind: str = Field(index=True)
    account_id: str = Field(default="", index=True)
    name: str = Field(default="", index=True)
    status: str = Field(default="unavailable", index=True)
    evaluated_at: float = Field(index=True)
    created_at: float = Field(index=True)
    schema_version: int = 1
    payload: str


class StrategyDecisionVersion(SQLModel, table=True):
    id: str = Field(primary_key=True)
    record_id: str = Field(foreign_key="strategydecision.id", index=True)
    created_at: float = Field(index=True)
    evaluated_at: float
    payload: str
