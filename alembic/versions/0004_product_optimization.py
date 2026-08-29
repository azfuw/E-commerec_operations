"""add product optimization persistence

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-29
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_workflow_type_check = (
    "((workflow_type = 'analysis' AND start_date IS NOT NULL AND end_date IS NOT NULL "
    "AND start_date <= end_date AND status IN ('accepted', 'processing', 'awaiting_selection', 'completed', 'failed')) "
    "OR (workflow_type = 'optimization' AND start_date IS NULL AND end_date IS NULL "
    "AND status IN ('accepted', 'processing', 'draft_ready', 'pending_manual', 'failed')))"
)


def upgrade() -> None:
    op.drop_constraint("ck_workflow_runs_type", "workflow_runs", type_="check")
    op.drop_constraint("ck_workflow_runs_status", "workflow_runs", type_="check")
    op.drop_constraint("ck_workflow_runs_dates", "workflow_runs", type_="check")
    op.alter_column(
        "workflow_runs",
        "workflow_type",
        existing_type=sa.String(length=32),
        type_=sa.Enum(
            "analysis",
            "optimization",
            name="workflow_type",
            native_enum=False,
            create_constraint=False,
            length=32,
        ),
        existing_nullable=False,
    )
    op.alter_column("workflow_runs", "start_date", existing_type=sa.Date(), nullable=True)
    op.alter_column("workflow_runs", "end_date", existing_type=sa.Date(), nullable=True)
    op.create_check_constraint(
        "ck_workflow_runs_type_status_dates", "workflow_runs", _workflow_type_check
    )

    op.add_column(
        "agent_calls",
        sa.Column("iteration", sa.Integer(), server_default="0", nullable=True),
    )
    op.alter_column("agent_calls", "iteration", existing_type=sa.Integer(), nullable=False)
    op.drop_constraint(
        "uq_agent_calls_workflow_run_id_node_name_call_type_attempt",
        "agent_calls",
        type_="unique",
    )
    op.create_check_constraint("ck_agent_calls_iteration", "agent_calls", "iteration >= 0")
    op.create_unique_constraint(
        "uq_agent_calls_run_node_type_iteration_attempt",
        "agent_calls",
        ["workflow_run_id", "node_name", "call_type", "iteration", "attempt"],
    )

    op.create_table(
        "product_proposals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("analysis_run_id", sa.String(length=36), nullable=False),
        sa.Column("analysis_candidate_id", sa.String(length=36), nullable=False),
        sa.Column("optimization_run_id", sa.String(length=36), nullable=False),
        sa.Column("store_id", sa.String(length=36), nullable=False),
        sa.Column("product_id", sa.String(length=36), nullable=False),
        sa.Column("base_product_version", sa.Integer(), nullable=False),
        sa.Column("selection_idempotency_hash", sa.String(length=64), nullable=False),
        sa.Column("current_revision_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("base_product_version >= 1", name="ck_product_proposals_base_product_version"),
        sa.CheckConstraint(
            "length(selection_idempotency_hash) = 64",
            name="ck_product_proposals_selection_idempotency_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"], ["workflow_runs.id"], name="fk_product_proposals_analysis_run_id"
        ),
        sa.ForeignKeyConstraint(
            ["analysis_candidate_id"],
            ["analysis_candidates.id"],
            name="fk_product_proposals_analysis_candidate_id",
        ),
        sa.ForeignKeyConstraint(
            ["optimization_run_id"],
            ["workflow_runs.id"],
            name="fk_product_proposals_optimization_run_id",
        ),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], name="fk_product_proposals_store_id"),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name="fk_product_proposals_product_id"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analysis_run_id", name="uq_product_proposals_analysis_run_id"),
        sa.UniqueConstraint(
            "analysis_candidate_id", name="uq_product_proposals_analysis_candidate_id"
        ),
        sa.UniqueConstraint("optimization_run_id", name="uq_product_proposals_optimization_run_id"),
    )
    op.create_table(
        "proposal_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("proposal_id", sa.String(length=36), nullable=False),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("base_product_version", sa.Integer(), nullable=False),
        sa.Column("trusted_fact_hash", sa.String(length=64), nullable=False),
        sa.Column("proposal_output", sa.JSON(), nullable=False),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("iteration BETWEEN 0 AND 2", name="ck_proposal_revisions_iteration"),
        sa.CheckConstraint(
            "base_product_version >= 1", name="ck_proposal_revisions_base_product_version"
        ),
        sa.CheckConstraint(
            "length(trusted_fact_hash) = 64",
            name="ck_proposal_revisions_trusted_fact_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["product_proposals.id"], name="fk_proposal_revisions_proposal_id"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("proposal_id", "iteration", name="uq_proposal_revisions_proposal_id_iteration"),
    )
    op.create_table(
        "compliance_reviews",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("proposal_id", sa.String(length=36), nullable=False),
        sa.Column("proposal_revision_id", sa.String(length=36), nullable=False),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("deterministic_checks", sa.JSON(), nullable=False),
        sa.Column("semantic_review", sa.JSON(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column(
            "risk_level",
            sa.Enum(
                "low",
                "medium",
                "high",
                name="compliance_risk_level",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("required_changes", sa.JSON(), nullable=False),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
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
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("iteration BETWEEN 0 AND 2", name="ck_compliance_reviews_iteration"),
        sa.CheckConstraint(
            "risk_level IN ('low', 'medium', 'high')", name="ck_compliance_reviews_risk_level"
        ),
        sa.CheckConstraint(
            "quality_status IN ('normal', 'partial', 'degraded')",
            name="ck_compliance_reviews_quality_status",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["product_proposals.id"], name="fk_compliance_reviews_proposal_id"
        ),
        sa.ForeignKeyConstraint(
            ["proposal_revision_id"],
            ["proposal_revisions.id"],
            name="fk_compliance_reviews_proposal_revision_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "proposal_revision_id", name="uq_compliance_reviews_proposal_revision_id"
        ),
        sa.UniqueConstraint(
            "proposal_id", "iteration", name="uq_compliance_reviews_proposal_id_iteration"
        ),
    )
    op.create_foreign_key(
        "fk_product_proposals_current_revision_id",
        "product_proposals",
        "proposal_revisions",
        ["current_revision_id"],
        ["id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM workflow_runs WHERE workflow_type = 'optimization' LIMIT 1")).scalar() is not None:
        raise RuntimeError("cannot downgrade product optimization persistence with optimization workflows")
    if bind.execute(sa.text("SELECT 1 FROM product_proposals LIMIT 1")).scalar() is not None:
        raise RuntimeError("cannot downgrade product optimization persistence with product proposals")

    op.drop_constraint("fk_product_proposals_current_revision_id", "product_proposals", type_="foreignkey")
    op.drop_table("compliance_reviews")
    op.drop_table("proposal_revisions")
    op.drop_table("product_proposals")

    op.drop_constraint(
        "uq_agent_calls_run_node_type_iteration_attempt",
        "agent_calls",
        type_="unique",
    )
    op.drop_constraint("ck_agent_calls_iteration", "agent_calls", type_="check")
    op.drop_column("agent_calls", "iteration")
    op.create_unique_constraint(
        "uq_agent_calls_workflow_run_id_node_name_call_type_attempt",
        "agent_calls",
        ["workflow_run_id", "node_name", "call_type", "attempt"],
    )

    op.drop_constraint("ck_workflow_runs_type_status_dates", "workflow_runs", type_="check")
    op.alter_column("workflow_runs", "start_date", existing_type=sa.Date(), nullable=False)
    op.alter_column("workflow_runs", "end_date", existing_type=sa.Date(), nullable=False)
    op.alter_column(
        "workflow_runs",
        "workflow_type",
        existing_type=sa.Enum(
            "analysis",
            "optimization",
            name="workflow_type",
            native_enum=False,
            create_constraint=False,
            length=32,
        ),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
    op.create_check_constraint("ck_workflow_runs_type", "workflow_runs", "workflow_type = 'analysis'")
    op.create_check_constraint(
        "ck_workflow_runs_status",
        "workflow_runs",
        "status IN ('accepted', 'processing', 'awaiting_selection', 'failed')",
    )
    op.create_check_constraint("ck_workflow_runs_dates", "workflow_runs", "start_date <= end_date")
