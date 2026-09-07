from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from backend.common import (
    AgentCallType,
    ApprovalActionType,
    AuditEventType,
    AuditOutcome,
    ComplianceRiskLevel,
    EvaluationAgentType,
    EvaluationRunStatus,
    KnowledgeVersionStatus,
    OrderStatus,
    PlatformDeliveryStatus,
    ProposalRevisionOrigin,
    RefundStatus,
    UserRole,
    UserStatus,
    WorkflowQuality,
    WorkflowStatus,
    WorkflowType,
    utc_now,
)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('operator', 'supervisor', 'admin')", name="user_role"),
        CheckConstraint("status IN ('active', 'disabled')", name="user_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(
            UserRole,
            name="user_role",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    status: Mapped[UserStatus] = mapped_column(
        Enum(
            UserStatus,
            name="user_status",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        default=lambda: UserStatus.ACTIVE,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=lambda: True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class UserStoreScope(Base):
    __tablename__ = "user_store_scopes"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), primary_key=True)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("store_id", "code", name="uq_products_store_id_code"),
        CheckConstraint("current_version >= 1", name="ck_products_current_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    brand: Mapped[str] = mapped_column(String(128), default=lambda: "", nullable=False)
    selling_points: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    description: Mapped[str] = mapped_column(Text, default=lambda: "", nullable=False)
    search_keywords: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    attributes: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, default=lambda: 1, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=lambda: True, nullable=False)


class ProductSku(Base):
    __tablename__ = "product_skus"
    __table_args__ = (
        UniqueConstraint("product_id", "code", name="uq_product_skus_product_id_code"),
        CheckConstraint("price >= 0", name="ck_product_skus_price"),
        CheckConstraint("current_stock >= 0", name="ck_product_skus_current_stock"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    spec: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    current_stock: Mapped[int] = mapped_column(Integer, nullable=False)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("total_amount >= 0", name="ck_orders_total_amount"),
        CheckConstraint(
            "status IN ('paid', 'shipped', 'completed', 'cancelled')", name="order_status"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False)
    ordered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(
            OrderStatus,
            name="order_status",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_quantity"),
        CheckConstraint("unit_price >= 0", name="ck_order_items_unit_price"),
        CheckConstraint(
            "refund_status IN ('none', 'requested', 'refunded', 'returned')",
            name="refund_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), nullable=False)
    sku_id: Mapped[str] = mapped_column(ForeignKey("product_skus.id"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    refund_status: Mapped[RefundStatus] = mapped_column(
        Enum(
            RefundStatus,
            name="refund_status",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )


class TrafficDaily(Base):
    __tablename__ = "traffic_daily"
    __table_args__ = (
        UniqueConstraint("store_id", "product_id", "metric_date", name="uq_traffic_daily_store_product_date"),
        CheckConstraint("impressions >= 0", name="ck_traffic_daily_impressions"),
        CheckConstraint("clicks >= 0", name="ck_traffic_daily_clicks"),
        CheckConstraint("visitors >= 0", name="ck_traffic_daily_visitors"),
        CheckConstraint("add_to_carts >= 0", name="ck_traffic_daily_add_to_carts"),
        CheckConstraint("clicks <= impressions", name="ck_traffic_daily_clicks_impressions"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), nullable=False)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    impressions: Mapped[int] = mapped_column(Integer, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False)
    visitors: Mapped[int] = mapped_column(Integer, nullable=False)
    add_to_carts: Mapped[int] = mapped_column(Integer, nullable=False)


class InventorySnapshot(Base):
    __tablename__ = "inventory_snapshots"
    __table_args__ = (
        UniqueConstraint("store_id", "sku_id", "snapshot_date", name="uq_inventory_store_sku_date"),
        CheckConstraint("on_hand >= 0", name="ck_inventory_snapshots_on_hand"),
        CheckConstraint("inbound >= 0", name="ck_inventory_snapshots_inbound"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False)
    sku_id: Mapped[str] = mapped_column(ForeignKey("product_skus.id"), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False)
    inbound: Mapped[int] = mapped_column(Integer, nullable=False)


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        CheckConstraint(
            "((workflow_type = 'analysis' AND start_date IS NOT NULL AND end_date IS NOT NULL "
            "AND start_date <= end_date AND status IN ('accepted', 'processing', 'awaiting_selection', 'completed', 'failed')) "
            "OR (workflow_type = 'optimization' AND start_date IS NULL AND end_date IS NULL "
            "AND status IN ('accepted', 'processing', 'draft_ready', 'pending_manual', "
            "'pending_approval', 'completed', 'rejected', 'failed')) "
            "OR (workflow_type = 'manual_review' AND start_date IS NULL AND end_date IS NULL "
            "AND status IN ('accepted', 'processing', 'completed', 'failed')))",
            name="ck_workflow_runs_type_status_dates",
        ),
        CheckConstraint(
            "quality_status IN ('normal', 'partial', 'degraded')",
            name="ck_workflow_runs_quality_status",
        ),
        CheckConstraint(
            "attempt_count BETWEEN 0 AND 3", name="ck_workflow_runs_attempt_count"
        ),
        CheckConstraint(
            "(status = 'processing' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status != 'processing' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="ck_workflow_runs_lease_state",
        ),
        Index(
            "ix_workflow_runs_type_status_lease_created",
            "workflow_type",
            "status",
            "lease_expires_at",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    workflow_type: Mapped[WorkflowType] = mapped_column(
        Enum(
            WorkflowType,
            name="workflow_type",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
            length=32,
        ),
        nullable=False,
    )
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[WorkflowStatus] = mapped_column(
        Enum(
            WorkflowStatus,
            name="workflow_status",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    quality_status: Mapped[WorkflowQuality] = mapped_column(
        Enum(
            WorkflowQuality,
            name="workflow_quality",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=lambda: 0, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_step: Mapped[str | None] = mapped_column(String(64))
    input: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    output: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    quality: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class AnalysisCandidate(Base):
    __tablename__ = "analysis_candidates"
    __table_args__ = (
        UniqueConstraint(
            "workflow_run_id",
            "product_id",
            name="uq_analysis_candidates_workflow_run_id_product_id",
        ),
        UniqueConstraint(
            "workflow_run_id",
            "rank",
            name="uq_analysis_candidates_workflow_run_id_rank",
        ),
        CheckConstraint("rank >= 1", name="ck_analysis_candidates_rank"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_analysis_candidates_confidence",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    workflow_run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    product_code: Mapped[str] = mapped_column(String(64), nullable=False)
    anomaly_types: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    metrics: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    business_impact: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    evidence: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    impact_explanation: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_action: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class AgentCall(Base):
    __tablename__ = "agent_calls"
    __table_args__ = (
        UniqueConstraint(
            "workflow_run_id",
            "node_name",
            "call_type",
            "iteration",
            "attempt",
            name="uq_agent_calls_run_node_type_iteration_attempt",
        ),
        CheckConstraint(
            "call_type IN ('primary', 'schema_repair')",
            name="ck_agent_calls_call_type",
        ),
        CheckConstraint("attempt >= 0", name="ck_agent_calls_attempt"),
        CheckConstraint("iteration >= 0", name="ck_agent_calls_iteration"),
        CheckConstraint("prompt_tokens >= 0", name="ck_agent_calls_prompt_tokens"),
        CheckConstraint("completion_tokens >= 0", name="ck_agent_calls_completion_tokens"),
        CheckConstraint("total_tokens >= 0", name="ck_agent_calls_total_tokens"),
        CheckConstraint("duration_ms >= 0", name="ck_agent_calls_duration_ms"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    workflow_run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False)
    node_name: Mapped[str] = mapped_column(String(64), nullable=False)
    call_type: Mapped[AgentCallType] = mapped_column(
        Enum(
            AgentCallType,
            name="agent_call_type",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    iteration: Mapped[int] = mapped_column(
        Integer, default=lambda: 0, server_default="0", nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class EvaluationCase(Base):
    __tablename__ = "evaluation_cases"
    __table_args__ = (
        UniqueConstraint(
            "agent_type",
            "case_key",
            "case_version",
            name="uq_evaluation_cases_agent_key_version",
        ),
        CheckConstraint("agent_type IN ('analysis', 'optimization', 'compliance', 'knowledge_retrieval')", name="ck_evaluation_cases_agent_type"),
        CheckConstraint("case_version >= 1", name="ck_evaluation_cases_version"),
        CheckConstraint("trim(CAST(fixture AS TEXT), ' \t\r\n') LIKE '{%}' AND trim(CAST(fixture AS TEXT), '{} \t\r\n') <> ''", name="ck_evaluation_cases_fixture_nonempty"),
        CheckConstraint("trim(CAST(expected AS TEXT), ' \t\r\n') LIKE '{%}' AND trim(CAST(expected AS TEXT), '{} \t\r\n') <> ''", name="ck_evaluation_cases_expected_nonempty"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    agent_type: Mapped[EvaluationAgentType] = mapped_column(
        Enum(
            EvaluationAgentType,
            name="evaluation_agent_type",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
            length=32,
        ),
        nullable=False,
    )
    case_key: Mapped[str] = mapped_column(String(64), nullable=False)
    case_version: Mapped[int] = mapped_column(Integer, nullable=False)
    fixture: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    expected: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=lambda: True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    __table_args__ = (
        CheckConstraint("agent_type IN ('analysis', 'optimization', 'compliance', 'knowledge_retrieval')", name="ck_evaluation_runs_agent_type"),
        CheckConstraint("execution_mode = 'offline_fixture'", name="ck_evaluation_runs_execution_mode"),
        CheckConstraint("status IN ('completed', 'failed')", name="ck_evaluation_runs_status"),
        CheckConstraint("completed_at >= started_at", name="ck_evaluation_runs_completed_at"),
        CheckConstraint(
            "agent_type = 'knowledge_retrieval' OR store_id IS NOT NULL",
            name="ck_evaluation_runs_store_requirement",
        ),
        Index("ix_evaluation_runs_agent_created", "agent_type", "created_at", "id"),
        Index("ix_evaluation_runs_store_created", "store_id", "created_at", "id"),
        Index("ix_evaluation_runs_status_created", "status", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    agent_type: Mapped[EvaluationAgentType] = mapped_column(
        Enum(
            EvaluationAgentType,
            name="evaluation_agent_type",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
            length=32,
        ),
        nullable=False,
    )
    store_id: Mapped[str | None] = mapped_column(ForeignKey("stores.id"))
    suite_version: Mapped[str] = mapped_column(String(64), nullable=False)
    runner_version: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[EvaluationRunStatus] = mapped_column(
        Enum(
            EvaluationRunStatus,
            name="evaluation_run_status",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
            length=16,
        ),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    summary: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class EvaluationResult(Base):
    __tablename__ = "evaluation_results"
    __table_args__ = (
        UniqueConstraint(
            "evaluation_run_id",
            "evaluation_case_id",
            "agent_type",
            name="uq_evaluation_results_run_case_agent",
        ),
        CheckConstraint("agent_type IN ('analysis', 'optimization', 'compliance', 'knowledge_retrieval')", name="ck_evaluation_results_agent_type"),
        CheckConstraint("outcome IN ('passed', 'failed')", name="ck_evaluation_results_outcome"),
        CheckConstraint("trim(CAST(metrics AS TEXT), ' \t\r\n') LIKE '{%}' AND trim(CAST(metrics AS TEXT), '{} \t\r\n') <> ''", name="ck_evaluation_results_metrics_nonempty"),
        CheckConstraint("result_code IN ('EVALUATION_PASSED', 'EVALUATION_EXPECTATION_MISMATCH', 'EVALUATION_INPUT_INVALID', 'EVALUATION_VALIDATION_FAILED', 'EVALUATION_RUNNER_FAILED')", name="ck_evaluation_results_result_code"),
        CheckConstraint("latency_ms >= 0 AND latency_ms <= 1.7976931348623157e308", name="ck_evaluation_results_latency_ms"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    evaluation_run_id: Mapped[str] = mapped_column(ForeignKey("evaluation_runs.id"), nullable=False)
    evaluation_case_id: Mapped[str] = mapped_column(ForeignKey("evaluation_cases.id"), nullable=False)
    agent_type: Mapped[EvaluationAgentType] = mapped_column(
        Enum(
            EvaluationAgentType,
            name="evaluation_agent_type",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
            length=32,
        ),
        nullable=False,
    )
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    metrics: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    result_code: Mapped[str] = mapped_column(String(64), nullable=False)
    latency_ms: Mapped[float] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ProductProposal(Base):
    __tablename__ = "product_proposals"
    __table_args__ = (
        UniqueConstraint("analysis_run_id", name="uq_product_proposals_analysis_run_id"),
        UniqueConstraint("analysis_candidate_id", name="uq_product_proposals_analysis_candidate_id"),
        UniqueConstraint("optimization_run_id", name="uq_product_proposals_optimization_run_id"),
        UniqueConstraint(
            "active_manual_review_run_id",
            name="uq_product_proposals_active_manual_review_run_id",
        ),
        Index("ix_product_proposals_submitted_revision_id", "submitted_revision_id"),
        CheckConstraint("base_product_version >= 1", name="ck_product_proposals_base_product_version"),
        CheckConstraint(
            "length(selection_idempotency_hash) = 64",
            name="ck_product_proposals_selection_idempotency_hash_length",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    analysis_run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", name="fk_product_proposals_analysis_run_id"), nullable=False
    )
    analysis_candidate_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_candidates.id", name="fk_product_proposals_analysis_candidate_id"), nullable=False
    )
    optimization_run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", name="fk_product_proposals_optimization_run_id"), nullable=False
    )
    store_id: Mapped[str] = mapped_column(
        ForeignKey("stores.id", name="fk_product_proposals_store_id"), nullable=False
    )
    product_id: Mapped[str] = mapped_column(
        ForeignKey("products.id", name="fk_product_proposals_product_id"), nullable=False
    )
    base_product_version: Mapped[int] = mapped_column(Integer, nullable=False)
    selection_idempotency_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    current_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "proposal_revisions.id",
            name="fk_product_proposals_current_revision_id",
            use_alter=True,
        )
    )
    active_manual_review_run_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "manual_review_runs.id",
            name="fk_product_proposals_active_manual_review_run_id",
            use_alter=True,
        )
    )
    submitted_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "proposal_revisions.id",
            name="fk_product_proposals_submitted_revision_id",
            use_alter=True,
        )
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ProposalRevision(Base):
    __tablename__ = "proposal_revisions"
    __table_args__ = (
        UniqueConstraint("proposal_id", "iteration", name="uq_proposal_revisions_proposal_id_iteration"),
        UniqueConstraint(
            "proposal_id",
            "revision_number",
            name="uq_proposal_revisions_proposal_revision_number",
        ),
        CheckConstraint("revision_number >= 1", name="ck_proposal_revisions_revision_number"),
        CheckConstraint("origin IN ('agent', 'manual')", name="ck_proposal_revisions_origin"),
        CheckConstraint(
            "(origin = 'agent' AND iteration IS NOT NULL AND iteration BETWEEN 0 AND 2) "
            "OR (origin = 'manual' AND iteration IS NULL)",
            name="ck_proposal_revisions_origin_iteration",
        ),
        CheckConstraint(
            "(revision_number = 1 AND parent_revision_id IS NULL) "
            "OR (revision_number > 1 AND parent_revision_id IS NOT NULL)",
            name="ck_proposal_revisions_parent",
        ),
        CheckConstraint(
            "parent_revision_id IS NULL OR parent_revision_id <> id",
            name="ck_proposal_revisions_not_self_parent",
        ),
        CheckConstraint("base_product_version >= 1", name="ck_proposal_revisions_base_product_version"),
        CheckConstraint(
            "length(trusted_fact_hash) = 64", name="ck_proposal_revisions_trusted_fact_hash_length"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("product_proposals.id", name="fk_proposal_revisions_proposal_id"), nullable=False
    )
    iteration: Mapped[int | None] = mapped_column(Integer)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    origin: Mapped[ProposalRevisionOrigin] = mapped_column(
        Enum(
            ProposalRevisionOrigin,
            name="proposal_revision_origin",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
            length=16,
        ),
        nullable=False,
    )
    created_by: Mapped[str] = mapped_column(
        ForeignKey("users.id", name="fk_proposal_revisions_created_by"), nullable=False
    )
    parent_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "proposal_revisions.id",
            name="fk_proposal_revisions_parent_revision_id",
        )
    )
    base_product_version: Mapped[int] = mapped_column(Integer, nullable=False)
    trusted_fact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_output: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    citations: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ComplianceReview(Base):
    __tablename__ = "compliance_reviews"
    __table_args__ = (
        UniqueConstraint("proposal_revision_id", name="uq_compliance_reviews_proposal_revision_id"),
        UniqueConstraint("proposal_id", "iteration", name="uq_compliance_reviews_proposal_id_iteration"),
        CheckConstraint(
            "iteration IS NULL OR iteration BETWEEN 0 AND 2",
            name="ck_compliance_reviews_iteration",
        ),
        CheckConstraint(
            "risk_level IN ('low', 'medium', 'high')", name="ck_compliance_reviews_risk_level"
        ),
        CheckConstraint(
            "quality_status IN ('normal', 'partial', 'degraded')",
            name="ck_compliance_reviews_quality_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("product_proposals.id", name="fk_compliance_reviews_proposal_id"), nullable=False
    )
    proposal_revision_id: Mapped[str] = mapped_column(
        ForeignKey("proposal_revisions.id", name="fk_compliance_reviews_proposal_revision_id"),
        nullable=False,
    )
    iteration: Mapped[int | None] = mapped_column(Integer)
    deterministic_checks: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    semantic_review: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    risk_level: Mapped[ComplianceRiskLevel] = mapped_column(
        Enum(
            ComplianceRiskLevel,
            name="compliance_risk_level",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    required_changes: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    citations: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    quality_status: Mapped[WorkflowQuality] = mapped_column(
        Enum(
            WorkflowQuality,
            name="workflow_quality",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ManualReviewRun(Base):
    __tablename__ = "manual_review_runs"
    __table_args__ = (
        UniqueConstraint("workflow_run_id", name="uq_manual_review_runs_workflow_run_id"),
        UniqueConstraint(
            "proposal_revision_id", name="uq_manual_review_runs_proposal_revision_id"
        ),
        UniqueConstraint(
            "proposal_id",
            "submitted_by",
            "idempotency_key_hash",
            name="uq_manual_review_runs_proposal_actor_key",
        ),
        CheckConstraint(
            "length(idempotency_key_hash) = 64",
            name="ck_manual_review_runs_idempotency_hash_length",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_manual_review_runs_request_hash_length",
        ),
        Index(
            "ix_manual_review_runs_proposal_created",
            "proposal_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    workflow_run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", name="fk_manual_review_runs_workflow_run_id"),
        nullable=False,
    )
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("product_proposals.id", name="fk_manual_review_runs_proposal_id"),
        nullable=False,
    )
    proposal_revision_id: Mapped[str] = mapped_column(
        ForeignKey(
            "proposal_revisions.id", name="fk_manual_review_runs_proposal_revision_id"
        ),
        nullable=False,
    )
    submitted_by: Mapped[str] = mapped_column(
        ForeignKey("users.id", name="fk_manual_review_runs_submitted_by"), nullable=False
    )
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ApprovalAction(Base):
    __tablename__ = "approval_actions"
    __table_args__ = (
        UniqueConstraint(
            "proposal_id",
            "actor_id",
            "action",
            "idempotency_key_hash",
            name="uq_approval_actions_proposal_actor_action_key",
        ),
        CheckConstraint(
            "action IN ('submit', 'approve', 'reject', 'request_changes')",
            name="ck_approval_actions_action",
        ),
        CheckConstraint(
            "actor_role IN ('operator', 'supervisor', 'admin')",
            name="ck_approval_actions_actor_role",
        ),
        CheckConstraint(
            "length(idempotency_key_hash) = 64",
            name="ck_approval_actions_idempotency_hash_length",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_approval_actions_request_hash_length",
        ),
        CheckConstraint(
            "((action IN ('reject', 'request_changes') AND comment IS NOT NULL "
            "AND length(trim(comment)) BETWEEN 1 AND 500) "
            "OR (action IN ('submit', 'approve') AND comment IS NULL))",
            name="ck_approval_actions_comment",
        ),
        Index("ix_approval_actions_proposal_created", "proposal_id", "created_at", "id"),
        Index("ix_approval_actions_store_created", "store_id", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("product_proposals.id", name="fk_approval_actions_proposal_id"),
        nullable=False,
    )
    proposal_revision_id: Mapped[str] = mapped_column(
        ForeignKey(
            "proposal_revisions.id", name="fk_approval_actions_proposal_revision_id"
        ),
        nullable=False,
    )
    store_id: Mapped[str] = mapped_column(
        ForeignKey("stores.id", name="fk_approval_actions_store_id"), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", name="fk_approval_actions_actor_id"), nullable=False
    )
    actor_role: Mapped[UserRole] = mapped_column(
        Enum(
            UserRole,
            name="user_role",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
            length=16,
        ),
        nullable=False,
    )
    action: Mapped[ApprovalActionType] = mapped_column(
        Enum(
            ApprovalActionType,
            name="approval_action_type",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
            length=32,
        ),
        nullable=False,
    )
    comment: Mapped[str | None] = mapped_column(String(500))
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class PublishRecord(Base):
    __tablename__ = "publish_records"
    __table_args__ = (
        UniqueConstraint("proposal_id", name="uq_publish_records_proposal_id"),
        UniqueConstraint(
            "proposal_revision_id", name="uq_publish_records_proposal_revision_id"
        ),
        UniqueConstraint("approval_action_id", name="uq_publish_records_approval_action_id"),
        UniqueConstraint(
            "publish_idempotency_hash",
            name="uq_publish_records_publish_idempotency_hash",
        ),
        CheckConstraint("base_product_version >= 1", name="ck_publish_records_base_version"),
        CheckConstraint(
            "published_product_version = base_product_version + 1",
            name="ck_publish_records_version_increment",
        ),
        CheckConstraint(
            "length(publish_idempotency_hash) = 64",
            name="ck_publish_records_idempotency_hash_length",
        ),
        Index("ix_publish_records_store_published", "store_id", "published_at", "id"),
        Index("ix_publish_records_product_published", "product_id", "published_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("product_proposals.id", name="fk_publish_records_proposal_id"),
        nullable=False,
    )
    proposal_revision_id: Mapped[str] = mapped_column(
        ForeignKey(
            "proposal_revisions.id", name="fk_publish_records_proposal_revision_id"
        ),
        nullable=False,
    )
    product_id: Mapped[str] = mapped_column(
        ForeignKey("products.id", name="fk_publish_records_product_id"), nullable=False
    )
    store_id: Mapped[str] = mapped_column(
        ForeignKey("stores.id", name="fk_publish_records_store_id"), nullable=False
    )
    approved_by: Mapped[str] = mapped_column(
        ForeignKey("users.id", name="fk_publish_records_approved_by"), nullable=False
    )
    approval_action_id: Mapped[str] = mapped_column(
        ForeignKey("approval_actions.id", name="fk_publish_records_approval_action_id"),
        nullable=False,
    )
    publish_idempotency_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    before_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    after_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    base_product_version: Mapped[int] = mapped_column(Integer, nullable=False)
    published_product_version: Mapped[int] = mapped_column(Integer, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class PlatformDelivery(Base):
    __tablename__ = "platform_deliveries"
    __table_args__ = (
        UniqueConstraint("publish_record_id", name="uq_platform_deliveries_publish_record_id"),
        CheckConstraint("status IN ('pending', 'processing', 'succeeded', 'failed')", name="ck_platform_deliveries_status"),
        CheckConstraint("provider = 'contract_simulator'", name="ck_platform_deliveries_provider"),
        CheckConstraint("attempt_count BETWEEN 0 AND 3", name="ck_platform_deliveries_attempt_count"),
        CheckConstraint(
            "(status = 'processing' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status != 'processing' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="ck_platform_deliveries_lease",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND external_operation_id IS NOT NULL AND completed_at IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'failed' AND external_operation_id IS NULL AND completed_at IS NOT NULL AND error_code IS NOT NULL) OR "
            "(status IN ('pending', 'processing') AND external_operation_id IS NULL AND completed_at IS NULL)",
            name="ck_platform_deliveries_terminal",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ('PLATFORM_AUTH_FAILED', 'PLATFORM_FORBIDDEN', "
            "'PLATFORM_IDEMPOTENCY_CONFLICT', 'PLATFORM_RATE_LIMITED', 'PLATFORM_TIMEOUT', "
            "'PLATFORM_CONNECTION_FAILED', 'PLATFORM_SERVER_ERROR', 'PLATFORM_REQUEST_REJECTED', "
            "'PLATFORM_REQUEST_INVALID', 'PLATFORM_RESPONSE_INVALID', 'PLATFORM_SIGNATURE_INVALID', "
            "'PLATFORM_DELIVERY_FAILED')",
            name="ck_platform_deliveries_error_code",
        ),
        Index("ix_platform_deliveries_claim", "status", "next_attempt_at", "lease_expires_at", "created_at", "id"),
        Index("ix_platform_deliveries_store_created", "store_id", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    publish_record_id: Mapped[str] = mapped_column(
        ForeignKey("publish_records.id", name="fk_platform_deliveries_publish_record_id"), nullable=False
    )
    store_id: Mapped[str] = mapped_column(
        ForeignKey("stores.id", name="fk_platform_deliveries_store_id"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), default="contract_simulator", nullable=False)
    status: Mapped[PlatformDeliveryStatus] = mapped_column(
        Enum(
            PlatformDeliveryStatus,
            name="platform_delivery_status",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
            length=16,
        ),
        default=PlatformDeliveryStatus.PENDING,
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_operation_id: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlatformWebhookReceipt(Base):
    __tablename__ = "platform_webhook_receipts"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_platform_webhook_receipts_event_id"),
        CheckConstraint("event_type = 'publish.confirmed'", name="ck_platform_webhook_receipts_event_type"),
        CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name="ck_platform_webhook_receipts_payload_digest"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    platform_delivery_id: Mapped[str] = mapped_column(
        ForeignKey("platform_deliveries.id", name="fk_platform_webhook_receipts_platform_delivery_id"), nullable=False
    )
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('manual_revision_created', 'manual_review_claimed', "
            "'manual_review_completed', 'manual_review_failed', 'proposal_submitted', "
            "'proposal_approved', 'proposal_rejected', 'proposal_changes_requested', "
            "'simulated_publish_completed', 'authorization_denied', "
            "'knowledge_document_created', 'knowledge_version_created', "
            "'knowledge_document_disabled', 'evaluation_run_persisted', "
            "'admin_user_updated', 'admin_user_scopes_replaced', 'admin_store_updated', "
            "'platform_delivery_enqueued', 'platform_delivery_completed', "
            "'platform_delivery_failed', 'platform_webhook_received')",
            name="ck_audit_events_event_type",
        ),
        CheckConstraint(
            "outcome IN ('success', 'failed', 'denied')",
            name="ck_audit_events_outcome",
        ),
        CheckConstraint(
            "actor_role IS NULL OR actor_role IN ('operator', 'supervisor', 'admin')",
            name="ck_audit_events_actor_role",
        ),
        CheckConstraint(
            "(resource_type IS NULL AND resource_id IS NULL) OR "
            "(resource_type IS NOT NULL AND resource_id IS NOT NULL "
            "AND resource_type IN ('user', 'store', 'knowledge_document', 'knowledge_version', "
            "'evaluation_run', 'platform_delivery') "
            "AND length(resource_id) BETWEEN 1 AND 36)",
            name="ck_audit_events_resource_pair",
        ),
        Index("ix_audit_events_store_created", "store_id", "created_at", "id"),
        Index("ix_audit_events_proposal_created", "proposal_id", "created_at", "id"),
        Index("ix_audit_events_workflow_created", "workflow_run_id", "created_at", "id"),
        Index("ix_audit_events_actor_created", "actor_id", "created_at", "id"),
        Index("ix_audit_events_event_created", "event_type", "created_at", "id"),
        Index("ix_audit_events_outcome_created", "outcome", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    event_type: Mapped[AuditEventType] = mapped_column(
        Enum(
            AuditEventType,
            name="audit_event_type",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
            length=64,
        ),
        nullable=False,
    )
    outcome: Mapped[AuditOutcome] = mapped_column(
        Enum(
            AuditOutcome,
            name="audit_outcome",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
            length=16,
        ),
        nullable=False,
    )
    actor_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", name="fk_audit_events_actor_id")
    )
    actor_role: Mapped[UserRole | None] = mapped_column(
        Enum(
            UserRole,
            name="user_role",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
            length=16,
        )
    )
    store_id: Mapped[str | None] = mapped_column(
        ForeignKey("stores.id", name="fk_audit_events_store_id")
    )
    resource_type: Mapped[str | None] = mapped_column(String(32))
    resource_id: Mapped[str | None] = mapped_column(String(36))
    proposal_id: Mapped[str | None] = mapped_column(
        ForeignKey("product_proposals.id", name="fk_audit_events_proposal_id")
    )
    proposal_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("proposal_revisions.id", name="fk_audit_events_proposal_revision_id")
    )
    workflow_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_runs.id", name="fk_audit_events_workflow_run_id")
    )
    approval_action_id: Mapped[str | None] = mapped_column(
        ForeignKey("approval_actions.id", name="fk_audit_events_approval_action_id")
    )
    publish_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("publish_records.id", name="fk_audit_events_publish_record_id")
    )
    request_id: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, object]] = mapped_column(
        JSON, default=dict, server_default="{}", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        UniqueConstraint(
            "created_by",
            "idempotency_key",
            name="uq_knowledge_documents_created_by_idempotency_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=lambda: True, nullable=False)
    current_version_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "knowledge_document_versions.id",
            name="fk_knowledge_documents_current_version_id",
            use_alter=True,
        )
    )
    created_by: Mapped[str] = mapped_column(
        ForeignKey("users.id", name="fk_knowledge_documents_created_by"), nullable=False
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class KnowledgeDocumentVersion(Base):
    __tablename__ = "knowledge_document_versions"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "version_number",
            name="uq_knowledge_document_versions_document_id_version_number",
        ),
        UniqueConstraint(
            "document_id",
            "sha256",
            name="uq_knowledge_document_versions_document_id_sha256",
        ),
        UniqueConstraint(
            "document_id",
            "idempotency_key",
            name="uq_knowledge_document_versions_document_id_idempotency_key",
        ),
        CheckConstraint(
            "status IN ('accepted', 'processing', 'active', 'failed', 'disabled')",
            name="ck_knowledge_document_versions_status",
        ),
        CheckConstraint(
            "attempt_count BETWEEN 0 AND 3",
            name="ck_knowledge_document_versions_attempt_count",
        ),
        CheckConstraint(
            "version_number >= 1",
            name="ck_knowledge_document_versions_version_number",
        ),
        CheckConstraint(
            "(status = 'processing' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status != 'processing' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="ck_knowledge_document_versions_lease_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    document_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_documents.id", name="fk_knowledge_document_versions_document_id"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[KnowledgeVersionStatus] = mapped_column(
        Enum(
            KnowledgeVersionStatus,
            name="knowledge_version_status",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        default=lambda: KnowledgeVersionStatus.ACCEPTED,
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=lambda: 0, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parser_version: Mapped[str | None] = mapped_column(String(64))
    chunker_version: Mapped[str | None] = mapped_column(String(64))
    embedding_version: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint(
            "version_id",
            "chunk_index",
            name="uq_knowledge_chunks_version_id_chunk_index",
        ),
        CheckConstraint("chunk_index >= 0", name="ck_knowledge_chunks_chunk_index"),
        CheckConstraint("token_count > 0", name="ck_knowledge_chunks_token_count"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_document_versions.id", name="fk_knowledge_chunks_version_id"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_text: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_metadata: Mapped[dict[str, object]] = mapped_column("metadata", JSON, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
