"""durable analysis persistence

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-26
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workflow_type", sa.String(length=32), nullable=False),
        sa.Column("store_id", sa.String(length=36), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "accepted",
                "processing",
                "awaiting_selection",
                "failed",
                name="workflow_status",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "quality_status",
            sa.Enum(
                "normal",
                "partial",
                "degraded",
                name="workflow_quality",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_step", sa.String(length=64), nullable=True),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("output", sa.JSON(), nullable=False),
        sa.Column("quality", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("workflow_type = 'analysis'", name="ck_workflow_runs_type"),
        sa.CheckConstraint(
            "status IN ('accepted', 'processing', 'awaiting_selection', 'failed')",
            name="ck_workflow_runs_status",
        ),
        sa.CheckConstraint(
            "quality_status IN ('normal', 'partial', 'degraded')",
            name="ck_workflow_runs_quality_status",
        ),
        sa.CheckConstraint("start_date <= end_date", name="ck_workflow_runs_dates"),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 3", name="ck_workflow_runs_attempt_count"
        ),
        sa.CheckConstraint(
            "(status = 'processing' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status != 'processing' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="ck_workflow_runs_lease_state",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "analysis_candidates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workflow_run_id", sa.String(length=36), nullable=False),
        sa.Column("product_id", sa.String(length=36), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("product_code", sa.String(length=64), nullable=False),
        sa.Column("anomaly_types", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("business_impact", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("impact_explanation", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("recommended_action", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rank >= 1", name="ck_analysis_candidates_rank"),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_analysis_candidates_confidence",
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workflow_run_id",
            "product_id",
            name="uq_analysis_candidates_workflow_run_id_product_id",
        ),
        sa.UniqueConstraint(
            "workflow_run_id",
            "rank",
            name="uq_analysis_candidates_workflow_run_id_rank",
        ),
    )
    op.create_table(
        "agent_calls",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workflow_run_id", sa.String(length=36), nullable=False),
        sa.Column("node_name", sa.String(length=64), nullable=False),
        sa.Column(
            "call_type",
            sa.Enum(
                "primary",
                "schema_repair",
                name="agent_call_type",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("estimated_cost", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "call_type IN ('primary', 'schema_repair')",
            name="ck_agent_calls_call_type",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_agent_calls_attempt"),
        sa.CheckConstraint("prompt_tokens >= 0", name="ck_agent_calls_prompt_tokens"),
        sa.CheckConstraint(
            "completion_tokens >= 0", name="ck_agent_calls_completion_tokens"
        ),
        sa.CheckConstraint("total_tokens >= 0", name="ck_agent_calls_total_tokens"),
        sa.CheckConstraint("duration_ms >= 0", name="ck_agent_calls_duration_ms"),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workflow_run_id",
            "node_name",
            "call_type",
            "attempt",
            name="uq_agent_calls_workflow_run_id_node_name_call_type_attempt",
        ),
    )


def downgrade() -> None:
    op.drop_table("agent_calls")
    op.drop_table("analysis_candidates")
    op.drop_table("workflow_runs")
