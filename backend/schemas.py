from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


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
