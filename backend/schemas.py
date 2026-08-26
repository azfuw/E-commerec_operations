from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.common import WorkflowQuality, WorkflowStatus


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class AccessToken(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


class StoreSummary(BaseModel):
    id: str
    code: str
    name: str


class ProductSummary(BaseModel):
    id: str
    code: str
    title: str
    category: str
    current_version: int


class StoreMetrics(BaseModel):
    store_id: str
    impressions: int
    clicks: int
    orders: int
    units: int
    revenue: Decimal
    refunds: int
    ctr: Decimal
    conversion_rate: Decimal
    refund_rate: Decimal
    average_order_value: Decimal


class ProductMetrics(BaseModel):
    product_id: str | None = None
    product_code: str | None = None
    impressions: int
    clicks: int
    orders: int
    units: int
    revenue: Decimal
    refunds: int
    ctr: Decimal
    conversion_rate: Decimal
    refund_rate: Decimal
    average_order_value: Decimal

    @classmethod
    def from_totals(
        cls,
        *,
        impressions: int,
        clicks: int,
        orders: int,
        units: int,
        revenue: Decimal,
        refunds: int,
    ) -> "ProductMetrics":
        def rate(numerator: int, denominator: int) -> Decimal:
            if not denominator:
                return Decimal("0.0000")
            return (Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.0001"))

        return cls(
            impressions=impressions,
            clicks=clicks,
            orders=orders,
            units=units,
            revenue=revenue,
            refunds=refunds,
            ctr=rate(clicks, impressions),
            conversion_rate=rate(orders, clicks),
            refund_rate=rate(refunds, orders),
            average_order_value=(revenue / Decimal(orders)).quantize(Decimal("0.0001"))
            if orders
            else Decimal("0.0000"),
        )


class InventoryRisk(BaseModel):
    product_id: str
    snapshot_date: date | None
    on_hand: int
    seven_day_units_sold: int
    threshold: int
    is_at_risk: bool


class AnomalyCandidate(BaseModel):
    product_id: str
    product_code: str
    anomaly_types: tuple[str, ...]
    score: Decimal
    business_impact: Decimal
    metrics: ProductMetrics
    evidence: list[str]


class AnalysisRunRequest(BaseModel):
    store_id: str = Field(min_length=1, max_length=36)
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_duration(self) -> "AnalysisRunRequest":
        days = (self.end_date - self.start_date).days + 1
        if not 1 <= days <= 90:
            raise ValueError("date range must contain 1 to 90 days")
        return self


class AnalysisRunAccepted(BaseModel):
    workflow_run_id: str
    status: Literal["accepted"]


class WorkflowRunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    workflow_type: Literal["analysis"]
    store_id: str
    start_date: date
    end_date: date
    status: WorkflowStatus
    quality_status: WorkflowQuality
    current_step: str | None
    attempt_count: int
    candidates_ready: bool
    error_code: str | None


class AnalysisCandidateView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: str
    rank: int
    product_code: str
    anomaly_types: list[str]
    metrics: ProductMetrics
    business_impact: Decimal
    evidence: list[str]
    impact_explanation: str
    reason: str
    recommended_action: str
    confidence: Decimal
