"""Logistics facts live beside commerce orders; no commerce state is rewritten."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, CheckConstraint, DDL, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from backend.common import utc_now
from backend.models import Base


class Shipment(Base):
    __tablename__ = "logistics_shipments"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_logistics_shipment_order"),
        UniqueConstraint("carrier", "tracking_no", name="uq_logistics_shipment_tracking"),
        CheckConstraint("status IN ('pending_dispatch', 'in_transit', 'delivered')", name="ck_logistics_shipment_status"),
        CheckConstraint("expected_delivery_at >= dispatch_due_at", name="ck_logistics_shipment_schedule"),
        CheckConstraint("(status = 'pending_dispatch' AND dispatched_at IS NULL AND delivered_at IS NULL) OR (status = 'in_transit' AND dispatched_at IS NOT NULL AND delivered_at IS NULL) OR (status = 'delivered' AND dispatched_at IS NOT NULL AND delivered_at IS NOT NULL AND delivered_at >= dispatched_at)", name="ck_logistics_shipment_progress"),
        Index("ix_logistics_shipments_store_status", "store_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False)
    carrier: Mapped[str] = mapped_column(String(64), nullable=False)
    tracking_no: Mapped[str] = mapped_column(String(100), nullable=False)
    destination: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending_dispatch", nullable=False)
    dispatch_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expected_delivery_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_location: Mapped[str] = mapped_column(String(128), default="待仓库发货", nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __mapper_args__ = {"version_id_col": version}


class ReturnCase(Base):
    __tablename__ = "logistics_returns"
    __table_args__ = (
        UniqueConstraint("shipment_id", name="uq_logistics_return_shipment"),
        CheckConstraint("status IN ('requested', 'approved', 'in_transit', 'received', 'closed', 'rejected')", name="ck_logistics_return_status"),
        CheckConstraint("expected_return_at >= requested_at", name="ck_logistics_return_schedule"),
        CheckConstraint("received_at IS NULL OR received_at >= requested_at", name="ck_logistics_return_received"),
        CheckConstraint("status NOT IN ('received', 'closed') OR received_at IS NOT NULL", name="ck_logistics_return_received_state"),
        CheckConstraint("status != 'closed' OR (received_at IS NOT NULL AND closed_at IS NOT NULL AND closed_at >= received_at)", name="ck_logistics_return_closed"),
        Index("ix_logistics_returns_store_status", "store_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    shipment_id: Mapped[str] = mapped_column(ForeignKey("logistics_shipments.id"), nullable=False)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="requested", nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    carrier: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    tracking_no: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    expected_return_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __mapper_args__ = {"version_id_col": version}


class ShipmentEvent(Base):
    __tablename__ = "logistics_events"
    __table_args__ = (
        CheckConstraint("source IN ('manual', 'demo', 'agent')", name="ck_logistics_event_source"),
        Index("ix_logistics_events_shipment_time", "shipment_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    shipment_id: Mapped[str] = mapped_column(ForeignKey("logistics_shipments.id"), nullable=False)
    return_id: Mapped[str | None] = mapped_column(ForeignKey("logistics_returns.id"))
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    location: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    actor_name: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ExceptionTask(Base):
    __tablename__ = "logistics_exceptions"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_logistics_exception_dedupe"),
        CheckConstraint("kind IN ('dispatch_overdue', 'no_movement', 'delivery_overdue', 'return_overdue')", name="ck_logistics_exception_kind"),
        CheckConstraint("severity IN ('medium', 'high')", name="ck_logistics_exception_severity"),
        CheckConstraint("status IN ('open', 'in_progress', 'resolved')", name="ck_logistics_exception_status"),
        CheckConstraint("status != 'resolved' OR (resolved_at IS NOT NULL AND length(resolution) > 0)", name="ck_logistics_exception_resolution"),
        Index("ix_logistics_exceptions_store_status", "store_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    shipment_id: Mapped[str] = mapped_column(ForeignKey("logistics_shipments.id"), nullable=False)
    return_id: Mapped[str | None] = mapped_column(ForeignKey("logistics_returns.id"))
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_action: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    assignee_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str] = mapped_column(Text, default="", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __mapper_args__ = {"version_id_col": version}


class AgentRun(Base):
    __tablename__ = "logistics_agent_runs"
    __table_args__ = (Index("ix_logistics_agent_runs_actor_time", "created_by", "started_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    scope_store_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="completed", nullable=False)
    mode: Mapped[str] = mapped_column(String(32), default="deterministic_rules", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scanned_shipments: Mapped[int] = mapped_column(Integer, nullable=False)
    created_exceptions: Mapped[int] = mapped_column(Integer, nullable=False)
    existing_exceptions: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)


# Both schema-create demo databases and migrated PostgreSQL protect the audit trail.
for operation in ("UPDATE", "DELETE"):
    event.listen(ShipmentEvent.__table__, "after_create", DDL(
        f"CREATE TRIGGER logistics_events_no_{operation.lower()} BEFORE {operation} ON logistics_events "
        "BEGIN SELECT RAISE(ABORT, 'logistics events are append-only'); END"
    ).execute_if(dialect="sqlite"))

event.listen(ShipmentEvent.__table__, "after_create", DDL("""
    CREATE OR REPLACE FUNCTION logistics_events_immutable() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'logistics events are append-only'; END; $$ LANGUAGE plpgsql
""").execute_if(dialect="postgresql"))
event.listen(ShipmentEvent.__table__, "after_create", DDL("""
    CREATE TRIGGER logistics_events_immutable BEFORE UPDATE OR DELETE ON logistics_events
    FOR EACH ROW EXECUTE FUNCTION logistics_events_immutable()
""").execute_if(dialect="postgresql"))
