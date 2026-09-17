"""Persist logistics operations, evidence events, and deterministic patrol runs.

Revision ID: 0008
Revises: 0007
"""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "logistics_shipments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("order_id", sa.String(36), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("store_id", sa.String(36), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("carrier", sa.String(64), nullable=False),
        sa.Column("tracking_no", sa.String(100), nullable=False),
        sa.Column("destination", sa.String(128), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("dispatch_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_delivery_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("last_event_at", sa.DateTime(timezone=True)),
        sa.Column("last_location", sa.String(128), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("order_id", name="uq_logistics_shipment_order"),
        sa.UniqueConstraint("carrier", "tracking_no", name="uq_logistics_shipment_tracking"),
        sa.CheckConstraint("status IN ('pending_dispatch', 'in_transit', 'delivered')", name="ck_logistics_shipment_status"),
        sa.CheckConstraint("expected_delivery_at >= dispatch_due_at", name="ck_logistics_shipment_schedule"),
        sa.CheckConstraint("(status = 'pending_dispatch' AND dispatched_at IS NULL AND delivered_at IS NULL) OR (status = 'in_transit' AND dispatched_at IS NOT NULL AND delivered_at IS NULL) OR (status = 'delivered' AND dispatched_at IS NOT NULL AND delivered_at IS NOT NULL AND delivered_at >= dispatched_at)", name="ck_logistics_shipment_progress"),
    )
    op.create_index("ix_logistics_shipments_store_status", "logistics_shipments", ["store_id", "status"])
    op.create_table(
        "logistics_returns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("shipment_id", sa.String(36), sa.ForeignKey("logistics_shipments.id"), nullable=False),
        sa.Column("store_id", sa.String(36), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("carrier", sa.String(64), nullable=False),
        sa.Column("tracking_no", sa.String(100), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_return_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("shipment_id", name="uq_logistics_return_shipment"),
        sa.CheckConstraint("status IN ('requested', 'approved', 'in_transit', 'received', 'closed', 'rejected')", name="ck_logistics_return_status"),
        sa.CheckConstraint("expected_return_at >= requested_at", name="ck_logistics_return_schedule"),
        sa.CheckConstraint("received_at IS NULL OR received_at >= requested_at", name="ck_logistics_return_received"),
        sa.CheckConstraint("status NOT IN ('received', 'closed') OR received_at IS NOT NULL", name="ck_logistics_return_received_state"),
        sa.CheckConstraint("status != 'closed' OR (received_at IS NOT NULL AND closed_at IS NOT NULL AND closed_at >= received_at)", name="ck_logistics_return_closed"),
    )
    op.create_index("ix_logistics_returns_store_status", "logistics_returns", ["store_id", "status"])
    op.create_table(
        "logistics_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("shipment_id", sa.String(36), sa.ForeignKey("logistics_shipments.id"), nullable=False),
        sa.Column("return_id", sa.String(36), sa.ForeignKey("logistics_returns.id")),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("location", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("users.id")),
        sa.Column("actor_name", sa.String(64), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source IN ('manual', 'demo', 'agent')", name="ck_logistics_event_source"),
    )
    op.create_index("ix_logistics_events_shipment_time", "logistics_events", ["shipment_id", "occurred_at"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""CREATE OR REPLACE FUNCTION logistics_events_immutable() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'logistics events are append-only'; END; $$ LANGUAGE plpgsql""")
        op.execute("""CREATE TRIGGER logistics_events_immutable BEFORE UPDATE OR DELETE ON logistics_events
            FOR EACH ROW EXECUTE FUNCTION logistics_events_immutable()""")
    elif op.get_bind().dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER logistics_events_no_{operation.lower()} BEFORE {operation} ON logistics_events BEGIN SELECT RAISE(ABORT, 'logistics events are append-only'); END")
    op.create_table(
        "logistics_exceptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("shipment_id", sa.String(36), sa.ForeignKey("logistics_shipments.id"), nullable=False),
        sa.Column("return_id", sa.String(36), sa.ForeignKey("logistics_returns.id")),
        sa.Column("store_id", sa.String(36), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("recommended_action", sa.Text(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("dedupe_key", sa.String(64), nullable=False),
        sa.Column("assignee_id", sa.String(36), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolution", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("dedupe_key", name="uq_logistics_exception_dedupe"),
        sa.CheckConstraint("kind IN ('dispatch_overdue', 'no_movement', 'delivery_overdue', 'return_overdue')", name="ck_logistics_exception_kind"),
        sa.CheckConstraint("severity IN ('medium', 'high')", name="ck_logistics_exception_severity"),
        sa.CheckConstraint("status IN ('open', 'in_progress', 'resolved')", name="ck_logistics_exception_status"),
        sa.CheckConstraint("status != 'resolved' OR (resolved_at IS NOT NULL AND length(resolution) > 0)", name="ck_logistics_exception_resolution"),
    )
    op.create_index("ix_logistics_exceptions_store_status", "logistics_exceptions", ["store_id", "status"])
    op.create_table(
        "logistics_agent_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id")),
        sa.Column("scope_store_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scanned_shipments", sa.Integer(), nullable=False),
        sa.Column("created_exceptions", sa.Integer(), nullable=False),
        sa.Column("existing_exceptions", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
    )
    op.create_index("ix_logistics_agent_runs_actor_time", "logistics_agent_runs", ["created_by", "started_at"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = ["logistics_agent_runs", "logistics_exceptions", "logistics_events", "logistics_returns", "logistics_shipments"]
    if any(bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() for table in tables):
        raise RuntimeError("cannot downgrade logistics with persisted facts")
    for table in tables:
        op.drop_table(table)
    if bind.dialect.name == "postgresql":
        op.execute("DROP FUNCTION logistics_events_immutable()")
