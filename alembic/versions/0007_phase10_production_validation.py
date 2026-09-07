"""add phase ten production validation persistence

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-07
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PHASE_TEN_AUDIT_EVENTS = (
    "'platform_delivery_enqueued', 'platform_delivery_completed', "
    "'platform_delivery_failed', 'platform_webhook_received'"
)
_AUDIT_EVENT_TYPES_0006 = (
    "'manual_revision_created', 'manual_review_claimed', 'manual_review_completed', "
    "'manual_review_failed', 'proposal_submitted', 'proposal_approved', "
    "'proposal_rejected', 'proposal_changes_requested', 'simulated_publish_completed', "
    "'authorization_denied', 'knowledge_document_created', 'knowledge_version_created', "
    "'knowledge_document_disabled', 'evaluation_run_persisted', 'admin_user_updated', "
    "'admin_user_scopes_replaced', 'admin_store_updated'"
)
_AUDIT_RESOURCE_PAIR_0006 = (
    "(resource_type IS NULL AND resource_id IS NULL) OR "
    "(resource_type IS NOT NULL AND resource_id IS NOT NULL "
    "AND resource_type IN ('user', 'store', 'knowledge_document', 'knowledge_version', 'evaluation_run') "
    "AND length(resource_id) BETWEEN 1 AND 36)"
)
_ERROR_CODES = (
    "'PLATFORM_AUTH_FAILED', 'PLATFORM_FORBIDDEN', 'PLATFORM_IDEMPOTENCY_CONFLICT', "
    "'PLATFORM_RATE_LIMITED', 'PLATFORM_TIMEOUT', 'PLATFORM_CONNECTION_FAILED', "
    "'PLATFORM_SERVER_ERROR', 'PLATFORM_REQUEST_REJECTED', 'PLATFORM_REQUEST_INVALID', "
    "'PLATFORM_RESPONSE_INVALID', 'PLATFORM_SIGNATURE_INVALID', 'PLATFORM_DELIVERY_FAILED'"
)


def _guard_phase_ten_downgrade(bind: sa.Connection) -> None:
    checks = (
        "SELECT 1 FROM platform_webhook_receipts LIMIT 1",
        "SELECT 1 FROM platform_deliveries LIMIT 1",
        f"SELECT 1 FROM audit_events WHERE event_type IN ({_PHASE_TEN_AUDIT_EVENTS}) LIMIT 1",
    )
    if any(bind.execute(sa.text(statement)).scalar() is not None for statement in checks):
        raise RuntimeError("cannot downgrade phase-ten production validation with facts")


def upgrade() -> None:
    op.drop_constraint("ck_audit_events_event_type", "audit_events", type_="check")
    op.drop_constraint("ck_audit_events_resource_pair", "audit_events", type_="check")
    op.create_check_constraint(
        "ck_audit_events_event_type",
        "audit_events",
        f"event_type IN ({_AUDIT_EVENT_TYPES_0006}, {_PHASE_TEN_AUDIT_EVENTS})",
    )
    op.create_check_constraint(
        "ck_audit_events_resource_pair",
        "audit_events",
        "(resource_type IS NULL AND resource_id IS NULL) OR "
        "(resource_type IS NOT NULL AND resource_id IS NOT NULL "
        "AND resource_type IN ('user', 'store', 'knowledge_document', 'knowledge_version', "
        "'evaluation_run', 'platform_delivery') AND length(resource_id) BETWEEN 1 AND 36)",
    )

    op.create_table(
        "platform_deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("publish_record_id", sa.String(length=36), nullable=False),
        sa.Column("store_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("lease_owner", sa.String(length=128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("external_operation_id", sa.String(length=128)),
        sa.Column("error_code", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('pending', 'processing', 'succeeded', 'failed')", name="ck_platform_deliveries_status"),
        sa.CheckConstraint("provider = 'contract_simulator'", name="ck_platform_deliveries_provider"),
        sa.CheckConstraint("attempt_count BETWEEN 0 AND 3", name="ck_platform_deliveries_attempt_count"),
        sa.CheckConstraint(
            "(status = 'processing' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status != 'processing' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="ck_platform_deliveries_lease",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND external_operation_id IS NOT NULL AND completed_at IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'failed' AND external_operation_id IS NULL AND completed_at IS NOT NULL AND error_code IS NOT NULL) OR "
            "(status IN ('pending', 'processing') AND external_operation_id IS NULL AND completed_at IS NULL)",
            name="ck_platform_deliveries_terminal",
        ),
        sa.CheckConstraint(f"error_code IS NULL OR error_code IN ({_ERROR_CODES})", name="ck_platform_deliveries_error_code"),
        sa.ForeignKeyConstraint(["publish_record_id"], ["publish_records.id"], name="fk_platform_deliveries_publish_record_id"),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], name="fk_platform_deliveries_store_id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("publish_record_id", name="uq_platform_deliveries_publish_record_id"),
    )
    op.create_index(
        "ix_platform_deliveries_claim",
        "platform_deliveries",
        ["status", "next_attempt_at", "lease_expires_at", "created_at", "id"],
    )
    op.create_index(
        "ix_platform_deliveries_store_created",
        "platform_deliveries",
        ["store_id", "created_at", "id"],
    )

    op.create_table(
        "platform_webhook_receipts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("platform_delivery_id", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("event_type = 'publish.confirmed'", name="ck_platform_webhook_receipts_event_type"),
        sa.CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name="ck_platform_webhook_receipts_payload_digest"),
        sa.ForeignKeyConstraint(
            ["platform_delivery_id"], ["platform_deliveries.id"],
            name="fk_platform_webhook_receipts_platform_delivery_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_platform_webhook_receipts_event_id"),
    )


def downgrade() -> None:
    _guard_phase_ten_downgrade(op.get_bind())
    op.drop_table("platform_webhook_receipts")
    op.drop_index("ix_platform_deliveries_store_created", table_name="platform_deliveries")
    op.drop_index("ix_platform_deliveries_claim", table_name="platform_deliveries")
    op.drop_table("platform_deliveries")
    op.drop_constraint("ck_audit_events_resource_pair", "audit_events", type_="check")
    op.drop_constraint("ck_audit_events_event_type", "audit_events", type_="check")
    op.create_check_constraint(
        "ck_audit_events_event_type",
        "audit_events",
        f"event_type IN ({_AUDIT_EVENT_TYPES_0006})",
    )
    op.create_check_constraint(
        "ck_audit_events_resource_pair", "audit_events", _AUDIT_RESOURCE_PAIR_0006
    )
