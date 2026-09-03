from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

from backend.common import (
    ApprovalActionType,
    AuditEventType,
    AuditOutcome,
    ComplianceRiskLevel,
    ProposalRevisionOrigin,
    UserRole,
    WorkflowQuality,
    WorkflowStatus,
    WorkflowType,
)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class AccessToken(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


class CurrentUserView(BaseModel):
    id: str
    username: str
    role: UserRole


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


class CanonicalRuleCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(max_length=36)
    version_id: str = Field(max_length=36)
    chunk_id: str = Field(max_length=128)
    document_name: str = Field(max_length=255)
    version_number: int
    category: str = Field(max_length=64)
    canonical_text: str = Field(max_length=12000)
    active: bool
    applicable: bool


class TrustedProductSku(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=36)
    code: str = Field(max_length=64)
    spec: dict[str, str] = Field(max_length=50)
    price: Decimal
    stock: int


class TrustedOptimizationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(max_length=36)
    product_id: str = Field(max_length=36)
    base_product_version: int
    title: str = Field(max_length=512)
    category: str = Field(max_length=128)
    brand: str = Field(max_length=128)
    selling_points: list[str] = Field(max_length=20)
    description: str = Field(max_length=8000)
    search_keywords: list[str] = Field(max_length=100)
    attributes: dict[str, str] = Field(max_length=50)
    skus: list[TrustedProductSku] = Field(max_length=100)
    candidate_metrics: ProductMetrics
    candidate_evidence: list[str] = Field(max_length=50)
    rag_quality: Literal["normal", "zero_hit", "low_confidence"]
    canonical_rule_citations: list[CanonicalRuleCitation] = Field(max_length=50)


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["fact", "citation"]
    value: str = Field(min_length=1, max_length=128)


class OutputCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(min_length=1, max_length=128)


class DescriptionSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heading: str = Field(max_length=256)
    body: str = Field(max_length=4000)
    evidence: list[EvidenceRef] = Field(max_length=20)


class OptimizationChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: Literal["title", "selling_points", "description", "keywords"]
    current_value: str | list[str] | list[DescriptionSection]
    suggested_value: str | list[str] | list[DescriptionSection]
    reason: str = Field(max_length=1024)
    evidence: list[EvidenceRef] = Field(max_length=20)


class AttributeCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_attribute: str = Field(min_length=1, max_length=64)
    current_value: str | None = Field(max_length=512)
    suggested_value: str = Field(max_length=512)
    reason: str = Field(max_length=1024)
    evidence: list[EvidenceRef] = Field(max_length=20)


class PriceSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_sku_id: str = Field(min_length=1, max_length=36)
    current_price: Decimal
    suggested_price: Decimal
    reason: str = Field(max_length=1024)
    evidence: list[EvidenceRef] = Field(max_length=20)


class SkuSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_sku_id: str = Field(min_length=1, max_length=36)
    current_code: str = Field(max_length=64)
    current_spec: dict[str, str] = Field(max_length=50)
    suggested_code: str = Field(max_length=64)
    suggested_spec: dict[str, str] = Field(max_length=50)
    reason: str = Field(max_length=1024)
    evidence: list[EvidenceRef] = Field(max_length=20)


class OptimizationProposalOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=512)
    selling_points: list[str] = Field(max_length=20)
    description: list[DescriptionSection] = Field(max_length=20)
    keywords: list[str] = Field(max_length=100)
    attribute_completions: list[AttributeCompletion] = Field(max_length=50)
    changes: list[OptimizationChange] = Field(max_length=50)
    citations: list[OutputCitation] = Field(max_length=50)
    price_suggestions: list[PriceSuggestion] = Field(max_length=50)
    sku_suggestions: list[SkuSuggestion] = Field(max_length=50)


class ManualRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_revision_id: str = Field(min_length=1, max_length=36)
    base_product_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=60)
    selling_points: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        min_length=1, max_length=5
    )
    description: list[DescriptionSection] = Field(min_length=1, max_length=10)
    keywords: list[Annotated[str, Field(min_length=1, max_length=32)]] = Field(
        min_length=1, max_length=20
    )
    attribute_completions: list[AttributeCompletion] = Field(max_length=20)
    changes: list[OptimizationChange] = Field(max_length=4)

    @model_validator(mode="after")
    def validate_manual_content(self) -> "ManualRevisionRequest":
        if not any("\u4e00" <= character <= "\u9fff" for character in self.title):
            raise ValueError("title must contain Chinese text")
        if any(
            not 1 <= len(section.heading) <= 40 or not 1 <= len(section.body) <= 1000
            for section in self.description
        ):
            raise ValueError("description section exceeds manual revision limits")
        return self


class ManualRevisionAccepted(BaseModel):
    revision_id: str
    manual_review_workflow_run_id: str
    status: Literal["accepted"]


class ProposalActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: str = Field(min_length=1, max_length=36)


class ProposalCommentActionRequest(ProposalActionRequest):
    comment: str

    @field_validator("comment")
    @classmethod
    def validate_comment(cls, value: str) -> str:
        normalized = value.strip()
        if not 1 <= len(normalized) <= 500 or any(
            unicodedata.category(character).startswith("C") for character in normalized
        ):
            raise ValueError("invalid approval comment")
        return normalized


class ApprovalActionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    proposal_id: str
    proposal_revision_id: str
    actor_id: str
    actor_role: UserRole
    action: ApprovalActionType
    comment: str | None
    created_at: datetime

    @field_serializer("created_at", when_used="json")
    def serialize_created_at(self, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class PublishRecordView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    proposal_id: str
    proposal_revision_id: str
    product_id: str
    store_id: str
    approved_by: str
    approval_action_id: str
    before_snapshot: dict[str, object]
    after_snapshot: dict[str, object]
    base_product_version: int
    published_product_version: int
    published_at: datetime

    @field_serializer("published_at", when_used="json")
    def serialize_published_at(self, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class ApprovalListItem(BaseModel):
    proposal_id: str
    proposal_revision_id: str
    revision_number: int
    store_id: str
    product_id: str
    submitted_by: str
    status: Literal["pending_approval"]
    submitted_at: datetime

    @field_serializer("submitted_at", when_used="json")
    def serialize_submitted_at(self, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class ApprovalListView(BaseModel):
    items: list[ApprovalListItem]
    page: int
    page_size: int
    total: int


class AuditEventView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    event_type: AuditEventType
    outcome: AuditOutcome
    actor_id: str | None
    actor_role: UserRole | None
    store_id: str
    proposal_id: str | None
    proposal_revision_id: str | None
    workflow_run_id: str | None
    approval_action_id: str | None
    publish_record_id: str | None
    request_id: str | None
    error_code: str | None
    details: dict[str, object]
    created_at: datetime

    @field_serializer("created_at", when_used="json")
    def serialize_created_at(self, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class AuditEventListView(BaseModel):
    items: list[AuditEventView]
    page: int
    page_size: int
    total: int


class ValidatedRequiredChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_track: Literal["deterministic", "semantic"]
    source_violation_code: Literal[
        "MISSING_TRUSTED_FACT",
        "OUTPUT_BUSINESS_LENGTH",
        "TITLE_LANGUAGE",
        "RESTRICTED_PHRASE",
        "CHANGE_TARGET_DUPLICATE",
        "CHANGE_CURRENT_MISMATCH",
        "CHANGE_SUGGESTED_MISMATCH",
        "OUTPUT_CHANGE_UNDECLARED",
        "EVIDENCE_MISSING",
        "EVIDENCE_FACT_PATH_INVALID",
        "EVIDENCE_CITATION_INVALID",
        "CITATION_DUPLICATE",
        "CITATION_UNKNOWN",
        "CITATION_EVIDENCE_UNLISTED",
        "ATTRIBUTE_TARGET_DUPLICATE",
        "ATTRIBUTE_CURRENT_MISMATCH",
        "ATTRIBUTE_SOURCE_MISSING",
        "SKU_TARGET_DUPLICATE",
        "SKU_UNKNOWN",
        "SKU_CURRENT_MISMATCH",
        "PRICE_TARGET_DUPLICATE",
        "PRICE_SKU_UNKNOWN",
        "PRICE_CURRENT_MISMATCH",
        "PRICE_CURRENT_NONPOSITIVE",
        "PRICE_NONPOSITIVE",
        "PRICE_PRECISION",
        "PRICE_RANGE",
        "RAG_QUALITY_INSUFFICIENT",
        "EXAGGERATION",
        "MEDICALIZATION",
        "MISLEADING",
        "SEMANTIC_CONTRADICTION",
        "UNPROVABLE_PROMISE",
        "INSUFFICIENT_EVIDENCE",
    ]
    field: str = Field(min_length=1, max_length=64)
    instruction: str = Field(min_length=1, max_length=240)
    citation_chunk_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        max_length=20
    )

    @model_validator(mode="after")
    def validate_contract(self) -> "ValidatedRequiredChange":
        deterministic_codes = {
            "MISSING_TRUSTED_FACT",
            "OUTPUT_BUSINESS_LENGTH",
            "TITLE_LANGUAGE",
            "RESTRICTED_PHRASE",
            "CHANGE_TARGET_DUPLICATE",
            "CHANGE_CURRENT_MISMATCH",
            "CHANGE_SUGGESTED_MISMATCH",
            "OUTPUT_CHANGE_UNDECLARED",
            "EVIDENCE_MISSING",
            "EVIDENCE_FACT_PATH_INVALID",
            "EVIDENCE_CITATION_INVALID",
            "CITATION_DUPLICATE",
            "CITATION_UNKNOWN",
            "CITATION_EVIDENCE_UNLISTED",
            "ATTRIBUTE_TARGET_DUPLICATE",
            "ATTRIBUTE_CURRENT_MISMATCH",
            "ATTRIBUTE_SOURCE_MISSING",
            "SKU_TARGET_DUPLICATE",
            "SKU_UNKNOWN",
            "SKU_CURRENT_MISMATCH",
            "PRICE_TARGET_DUPLICATE",
            "PRICE_SKU_UNKNOWN",
            "PRICE_CURRENT_MISMATCH",
            "PRICE_CURRENT_NONPOSITIVE",
            "PRICE_NONPOSITIVE",
            "PRICE_PRECISION",
            "PRICE_RANGE",
            "RAG_QUALITY_INSUFFICIENT",
        }
        if (self.source_track == "deterministic") != (
            self.source_violation_code in deterministic_codes
        ):
            raise ValueError("source track and violation code mismatch")
        if len(self.citation_chunk_ids) != len(set(self.citation_chunk_ids)):
            raise ValueError("citation chunk IDs must be unique")
        if not any("\u4e00" <= character <= "\u9fff" for character in self.instruction):
            raise ValueError("instruction must contain Chinese text")
        if any(unicodedata.category(character).startswith("C") for character in self.instruction):
            raise ValueError("instruction contains a control character")
        return self


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


class WorkbenchTaskView(BaseModel):
    id: str
    kind: Literal["analysis", "proposal"]
    store_id: str
    product_id: str | None
    analysis_run_id: str
    proposal_id: str | None
    workflow_run_id: str
    workflow_type: WorkflowType
    status: WorkflowStatus
    quality_status: WorkflowQuality
    current_step: str | None
    action_required: Literal[
        "wait",
        "select_product",
        "edit_proposal",
        "submit_proposal",
        "review_approval",
        "view_result",
        "resolve_failure",
    ]
    requires_current_user_action: bool
    created_by: str
    updated_at: datetime

    @field_serializer("updated_at", when_used="json")
    def serialize_updated_at(self, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class WorkbenchTaskListView(BaseModel):
    items: list[WorkbenchTaskView]
    page: int
    page_size: int
    total: int


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
    model_config = ConfigDict(from_attributes=True)

    id: str
    iteration: int | None
    revision_number: int
    origin: ProposalRevisionOrigin
    created_by: str
    parent_revision_id: str | None
    base_product_version: int
    proposal_output: dict[str, object]
    citations: list[dict[str, object]]


class ComplianceReviewView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    iteration: int | None
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


class ManualReviewSummary(BaseModel):
    manual_review_run_id: str
    workflow_run_id: str
    proposal_revision_id: str
    status: WorkflowStatus
    quality_status: WorkflowQuality
    current_step: str | None
    error_code: str | None


class ProposalDetailView(BaseModel):
    proposal: ProposalView
    optimization_run: OptimizationWorkflowSummary
    current_revision: ProposalRevisionView | None
    current_review: ComplianceReviewView | None
    active_manual_review: ManualReviewSummary | None
    submitted_revision: ProposalRevisionView | None
    latest_action: ApprovalActionView | None
    publish_record: PublishRecordView | None


class AnalysisCandidateView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
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
