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
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.common import (
    AgentCallType,
    OrderStatus,
    RefundStatus,
    UserRole,
    UserStatus,
    WorkflowQuality,
    WorkflowStatus,
    utc_now,
)
from backend.database import Base


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
        CheckConstraint("workflow_type = 'analysis'", name="ck_workflow_runs_type"),
        CheckConstraint(
            "status IN ('accepted', 'processing', 'awaiting_selection', 'failed')",
            name="ck_workflow_runs_status",
        ),
        CheckConstraint(
            "quality_status IN ('normal', 'partial', 'degraded')",
            name="ck_workflow_runs_quality_status",
        ),
        CheckConstraint("start_date <= end_date", name="ck_workflow_runs_dates"),
        CheckConstraint(
            "attempt_count BETWEEN 0 AND 3", name="ck_workflow_runs_attempt_count"
        ),
        CheckConstraint(
            "(status = 'processing' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status != 'processing' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="ck_workflow_runs_lease_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    workflow_type: Mapped[str] = mapped_column(String(32), nullable=False)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
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
            "attempt",
            name="uq_agent_calls_workflow_run_id_node_name_call_type_attempt",
        ),
        CheckConstraint(
            "call_type IN ('primary', 'schema_repair')",
            name="ck_agent_calls_call_type",
        ),
        CheckConstraint("attempt >= 0", name="ck_agent_calls_attempt"),
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
