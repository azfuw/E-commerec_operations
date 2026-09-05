"""add phase nine management console persistence

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_AGENT_TYPES = "'analysis', 'optimization', 'compliance', 'knowledge_retrieval'"
_PHASE_NINE_AUDIT_EVENTS = (
    "'knowledge_document_created', 'knowledge_version_created', "
    "'knowledge_document_disabled', 'evaluation_run_persisted', "
    "'admin_user_updated', 'admin_user_scopes_replaced', 'admin_store_updated'"
)
_AUDIT_EVENT_TYPES = (
    "'manual_revision_created', 'manual_review_claimed', 'manual_review_completed', "
    "'manual_review_failed', 'proposal_submitted', 'proposal_approved', "
    "'proposal_rejected', 'proposal_changes_requested', 'simulated_publish_completed', "
    f"'authorization_denied', {_PHASE_NINE_AUDIT_EVENTS}"
)
_AUDIT_EVENT_TYPES_0005 = (
    "'manual_revision_created', 'manual_review_claimed', 'manual_review_completed', "
    "'manual_review_failed', 'proposal_submitted', 'proposal_approved', "
    "'proposal_rejected', 'proposal_changes_requested', 'simulated_publish_completed', "
    "'authorization_denied'"
)


def _agent_type_enum() -> sa.Enum:
    return sa.Enum(
        "analysis",
        "optimization",
        "compliance",
        "knowledge_retrieval",
        name="evaluation_agent_type",
        native_enum=False,
        create_constraint=False,
        length=32,
    )


def _run_status_enum() -> sa.Enum:
    return sa.Enum(
        "completed",
        "failed",
        name="evaluation_run_status",
        native_enum=False,
        create_constraint=False,
        length=16,
    )


def _guard_phase_nine_downgrade(bind: sa.Connection) -> None:
    checks = (
        "SELECT 1 FROM evaluation_cases LIMIT 1",
        "SELECT 1 FROM evaluation_runs LIMIT 1",
        "SELECT 1 FROM evaluation_results LIMIT 1",
        f"SELECT 1 FROM audit_events WHERE event_type IN ({_PHASE_NINE_AUDIT_EVENTS}) LIMIT 1",
        "SELECT 1 FROM audit_events WHERE store_id IS NULL LIMIT 1",
    )
    if any(bind.execute(sa.text(statement)).scalar() is not None for statement in checks):
        raise RuntimeError("cannot downgrade phase-nine management console with facts")


def upgrade() -> None:
    op.alter_column(
        "audit_events",
        "store_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )
    op.add_column("audit_events", sa.Column("resource_type", sa.String(length=32)))
    op.add_column("audit_events", sa.Column("resource_id", sa.String(length=36)))
    op.drop_constraint("ck_audit_events_event_type", "audit_events", type_="check")
    op.create_check_constraint(
        "ck_audit_events_event_type",
        "audit_events",
        f"event_type IN ({_AUDIT_EVENT_TYPES})",
    )
    op.create_check_constraint(
        "ck_audit_events_resource_pair",
        "audit_events",
        "(resource_type IS NULL AND resource_id IS NULL) OR "
        "(resource_type IS NOT NULL AND resource_id IS NOT NULL "
        "AND resource_type IN ('user', 'store', 'knowledge_document', 'knowledge_version', 'evaluation_run') "
        "AND length(resource_id) BETWEEN 1 AND 36)",
    )
    op.create_index(
        "ix_audit_events_event_created", "audit_events", ["event_type", "created_at", "id"]
    )
    op.create_index(
        "ix_audit_events_outcome_created", "audit_events", ["outcome", "created_at", "id"]
    )

    op.create_table(
        "evaluation_cases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_type", _agent_type_enum(), nullable=False),
        sa.Column("case_key", sa.String(length=64), nullable=False),
        sa.Column("case_version", sa.Integer(), nullable=False),
        sa.Column("fixture", sa.JSON(), nullable=False),
        sa.Column("expected", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"agent_type IN ({_AGENT_TYPES})", name="ck_evaluation_cases_agent_type"),
        sa.CheckConstraint("case_version >= 1", name="ck_evaluation_cases_version"),
        sa.CheckConstraint("trim(CAST(fixture AS TEXT), ' \t\r\n') LIKE '{%}' AND trim(CAST(fixture AS TEXT), '{} \t\r\n') <> ''", name="ck_evaluation_cases_fixture_nonempty"),
        sa.CheckConstraint("trim(CAST(expected AS TEXT), ' \t\r\n') LIKE '{%}' AND trim(CAST(expected AS TEXT), '{} \t\r\n') <> ''", name="ck_evaluation_cases_expected_nonempty"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_type",
            "case_key",
            "case_version",
            name="uq_evaluation_cases_agent_key_version",
        ),
    )
    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_type", _agent_type_enum(), nullable=False),
        sa.Column("store_id", sa.String(length=36)),
        sa.Column("suite_version", sa.String(length=64), nullable=False),
        sa.Column("runner_version", sa.String(length=64), nullable=False),
        sa.Column("dataset_version", sa.String(length=64), nullable=False),
        sa.Column("execution_mode", sa.String(length=32), nullable=False),
        sa.Column("status", _run_status_enum(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=64)),
        sa.Column("created_by", sa.String(length=36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"agent_type IN ({_AGENT_TYPES})", name="ck_evaluation_runs_agent_type"),
        sa.CheckConstraint("execution_mode = 'offline_fixture'", name="ck_evaluation_runs_execution_mode"),
        sa.CheckConstraint("status IN ('completed', 'failed')", name="ck_evaluation_runs_status"),
        sa.CheckConstraint("completed_at >= started_at", name="ck_evaluation_runs_completed_at"),
        sa.CheckConstraint(
            "agent_type = 'knowledge_retrieval' OR store_id IS NOT NULL",
            name="ck_evaluation_runs_store_requirement",
        ),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], name="fk_evaluation_runs_store_id"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_evaluation_runs_created_by"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_evaluation_runs_agent_created", "evaluation_runs", ["agent_type", "created_at", "id"]
    )
    op.create_index(
        "ix_evaluation_runs_store_created", "evaluation_runs", ["store_id", "created_at", "id"]
    )
    op.create_index(
        "ix_evaluation_runs_status_created", "evaluation_runs", ["status", "created_at", "id"]
    )
    op.create_table(
        "evaluation_results",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("evaluation_run_id", sa.String(length=36), nullable=False),
        sa.Column("evaluation_case_id", sa.String(length=36), nullable=False),
        sa.Column("agent_type", _agent_type_enum(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("result_code", sa.String(length=64), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"agent_type IN ({_AGENT_TYPES})", name="ck_evaluation_results_agent_type"),
        sa.CheckConstraint("outcome IN ('passed', 'failed')", name="ck_evaluation_results_outcome"),
        sa.CheckConstraint("trim(CAST(metrics AS TEXT), ' \t\r\n') LIKE '{%}' AND trim(CAST(metrics AS TEXT), '{} \t\r\n') <> ''", name="ck_evaluation_results_metrics_nonempty"),
        sa.CheckConstraint("result_code IN ('EVALUATION_PASSED', 'EVALUATION_EXPECTATION_MISMATCH', 'EVALUATION_INPUT_INVALID', 'EVALUATION_VALIDATION_FAILED', 'EVALUATION_RUNNER_FAILED')", name="ck_evaluation_results_result_code"),
        sa.CheckConstraint("latency_ms >= 0 AND latency_ms <= 1.7976931348623157e308", name="ck_evaluation_results_latency_ms"),
        sa.ForeignKeyConstraint(
            ["evaluation_run_id"], ["evaluation_runs.id"], name="fk_evaluation_results_run_id"
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_case_id"], ["evaluation_cases.id"], name="fk_evaluation_results_case_id"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "evaluation_run_id",
            "evaluation_case_id",
            "agent_type",
            name="uq_evaluation_results_run_case_agent",
        ),
    )


def downgrade() -> None:
    _guard_phase_nine_downgrade(op.get_bind())

    op.drop_table("evaluation_results")
    op.drop_index("ix_evaluation_runs_status_created", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_store_created", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_agent_created", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
    op.drop_table("evaluation_cases")

    op.drop_index("ix_audit_events_outcome_created", table_name="audit_events")
    op.drop_index("ix_audit_events_event_created", table_name="audit_events")
    op.drop_constraint("ck_audit_events_resource_pair", "audit_events", type_="check")
    op.drop_constraint("ck_audit_events_event_type", "audit_events", type_="check")
    op.drop_column("audit_events", "resource_id")
    op.drop_column("audit_events", "resource_type")
    op.alter_column(
        "audit_events",
        "store_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_audit_events_event_type",
        "audit_events",
        f"event_type IN ({_AUDIT_EVENT_TYPES_0005})",
    )
