from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.common import ComplianceRiskLevel, WorkflowQuality, WorkflowStatus, WorkflowType


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
    workflow_type: WorkflowType
    store_id: str
    start_date: date | None
    end_date: date | None
    status: WorkflowStatus
    quality_status: WorkflowQuality
    current_step: str | None
    attempt_count: int
    candidates_ready: bool
    error_code: str | None


class ProductSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1, max_length=36)


class ProductSelectionView(BaseModel):
    proposal_id: str
    optimization_workflow_run_id: str
    status: Literal["accepted"]


class OptimizationWorkflowSummary(BaseModel):
    id: str
    workflow_type: WorkflowType
    status: WorkflowStatus
    quality_status: WorkflowQuality
    error_code: str | None


class ProposalRevisionView(BaseModel):
    id: str
    iteration: int
    base_product_version: int
    proposal_output: dict[str, object]
    citations: list[dict[str, object]]


class ComplianceReviewView(BaseModel):
    id: str
    iteration: int
    deterministic_checks: dict[str, object]
    semantic_review: dict[str, object]
    passed: bool
    risk_level: ComplianceRiskLevel
    quality_status: WorkflowQuality
    required_changes: list[dict[str, object]]
    citations: list[dict[str, object]]
    error_code: str | None


class ProposalView(BaseModel):
    id: str
    analysis_run_id: str
    analysis_candidate_id: str
    optimization_run_id: str
    store_id: str
    product_id: str
    base_product_version: int
    current_revision_id: str | None
    created_at: datetime
    updated_at: datetime


class ProposalDetailView(BaseModel):
    proposal: ProposalView
    optimization_run: OptimizationWorkflowSummary
    current_revision: ProposalRevisionView | None
    current_review: ComplianceReviewView | None


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


class TrustedAnalysisCandidate(BaseModel):
    product_id: str
    product_code: str
    anomaly_types: list[str]
    metrics: ProductMetrics
    business_impact: Decimal
    evidence: list[str]


class AnalysisFacts(BaseModel):
    store_summary: StoreMetrics
    candidates: list[TrustedAnalysisCandidate]


class AgentCandidateDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str
    rank: int
    impact_explanation: str
    reason: str
    recommended_action: str
    confidence: Decimal = Field(ge=0, le=1)


class AgentAnalysisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[AgentCandidateDraft]


class KnowledgeError(BaseModel):
    category: Literal[
        "timeout", "dependency_error", "validation_error", "authorization_error", "not_found"
    ]
    code: str
    message: str


class KnowledgeEnvelope(BaseModel):
    request_id: str
    status: Literal["accepted", "success", "error"]
    data: dict[str, object] | None
    quality: dict[str, str] | None
    error: KnowledgeError | None


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    categories: list[str] | None = Field(default=None, max_length=20)
    top_k: int = Field(default=10, ge=1, le=20)


class KnowledgeCitation(BaseModel):
    chunk_id: str
    document_name: str
    version_number: int
    category: str
    canonical_text: str
    chunk_metadata: dict[str, object]
    dense_score: float | None
    sparse_score: float | None
    fusion_score: float | None
    reranker_score: float | None
    final_score: float
