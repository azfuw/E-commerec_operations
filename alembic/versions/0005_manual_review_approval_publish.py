"""add manual review approval publish persistence

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-31
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_workflow_type_check = (
    "((workflow_type = 'analysis' AND start_date IS NOT NULL AND end_date IS NOT NULL "
    "AND start_date <= end_date AND status IN ('accepted', 'processing', 'awaiting_selection', 'completed', 'failed')) "
    "OR (workflow_type = 'optimization' AND start_date IS NULL AND end_date IS NULL "
    "AND status IN ('accepted', 'processing', 'draft_ready', 'pending_manual', "
    "'pending_approval', 'completed', 'rejected', 'failed')) "
    "OR (workflow_type = 'manual_review' AND start_date IS NULL AND end_date IS NULL "
    "AND status IN ('accepted', 'processing', 'completed', 'failed')))"
)
_workflow_type_check_0004 = (
    "((workflow_type = 'analysis' AND start_date IS NOT NULL AND end_date IS NOT NULL "
    "AND start_date <= end_date AND status IN ('accepted', 'processing', 'awaiting_selection', 'completed', 'failed')) "
    "OR (workflow_type = 'optimization' AND start_date IS NULL AND end_date IS NULL "
    "AND status IN ('accepted', 'processing', 'draft_ready', 'pending_manual', 'failed')))"
)


def _backfill_agent_revisions(bind: sa.Connection) -> None:
    malformed = bind.execute(
        sa.text(
            "SELECT r.id FROM proposal_revisions r "
            "LEFT JOIN product_proposals p ON p.id = r.proposal_id "
            "LEFT JOIN workflow_runs w ON w.id = p.optimization_run_id "
            "WHERE r.iteration NOT BETWEEN 0 AND 2 OR p.id IS NULL OR w.id IS NULL "
            "OR w.workflow_type <> 'optimization' LIMIT 1"
        )
    ).first()
    duplicate = bind.execute(
        sa.text(
            "SELECT proposal_id, iteration FROM proposal_revisions "
            "GROUP BY proposal_id, iteration HAVING count(*) <> 1 LIMIT 1"
        )
    ).first()
    missing_parent = bind.execute(
        sa.text(
            "SELECT r.id FROM proposal_revisions r WHERE r.iteration > 0 AND "
            "(SELECT count(*) FROM proposal_revisions p "
            "WHERE p.proposal_id = r.proposal_id AND p.iteration = r.iteration - 1) <> 1 LIMIT 1"
        )
    ).first()
    if malformed is not None or duplicate is not None or missing_parent is not None:
        raise RuntimeError("cannot backfill proposal revisions from malformed automatic history")

    bind.execute(
        sa.text(
            "UPDATE proposal_revisions SET revision_number = iteration + 1, origin = 'agent', "
            "created_by = (SELECT w.created_by FROM product_proposals p "
            "JOIN workflow_runs w ON w.id = p.optimization_run_id "
            "WHERE p.id = proposal_revisions.proposal_id)"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE proposal_revisions SET parent_revision_id = "
            "(SELECT p.id FROM proposal_revisions p "
            "WHERE p.proposal_id = proposal_revisions.proposal_id "
            "AND p.iteration = proposal_revisions.iteration - 1) "
            "WHERE iteration > 0"
        )
    )
    invalid = bind.execute(
        sa.text(
            "SELECT r.id FROM proposal_revisions r "
            "LEFT JOIN proposal_revisions p ON p.id = r.parent_revision_id "
            "WHERE r.revision_number IS NULL OR r.origin <> 'agent' OR r.created_by IS NULL "
            "OR (r.iteration = 0 AND r.parent_revision_id IS NOT NULL) "
            "OR (r.iteration > 0 AND (p.id IS NULL OR p.proposal_id <> r.proposal_id "
            "OR p.iteration <> r.iteration - 1)) LIMIT 1"
        )
    ).first()
    duplicate_number = bind.execute(
        sa.text(
            "SELECT proposal_id, revision_number FROM proposal_revisions "
            "GROUP BY proposal_id, revision_number HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if invalid is not None or duplicate_number is not None:
        raise RuntimeError("cannot backfill proposal revisions from malformed automatic history")


def _guard_stage_five_downgrade(bind: sa.Connection) -> None:
    checks = (
        "SELECT 1 FROM proposal_revisions WHERE origin = 'manual' LIMIT 1",
        "SELECT 1 FROM workflow_runs WHERE workflow_type = 'manual_review' LIMIT 1",
        "SELECT 1 FROM manual_review_runs LIMIT 1",
        "SELECT 1 FROM approval_actions LIMIT 1",
        "SELECT 1 FROM publish_records LIMIT 1",
        "SELECT 1 FROM audit_events LIMIT 1",
    )
    if any(bind.execute(sa.text(statement)).scalar() is not None for statement in checks):
        raise RuntimeError("cannot downgrade manual review approval publish with stage-five facts")


def upgrade() -> None:
    op.drop_constraint("ck_workflow_runs_type_status_dates", "workflow_runs", type_="check")
    op.create_check_constraint(
        "ck_workflow_runs_type_status_dates", "workflow_runs", _workflow_type_check
    )
    op.create_index(
        "ix_workflow_runs_type_status_lease_created",
        "workflow_runs",
        ["workflow_type", "status", "lease_expires_at", "created_at", "id"],
    )

    op.add_column("proposal_revisions", sa.Column("revision_number", sa.Integer()))
    op.add_column(
        "proposal_revisions",
        sa.Column(
            "origin",
            sa.Enum(
                "agent",
                "manual",
                name="proposal_revision_origin",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
        ),
    )
    op.add_column("proposal_revisions", sa.Column("created_by", sa.String(length=36)))
    op.add_column("proposal_revisions", sa.Column("parent_revision_id", sa.String(length=36)))
    _backfill_agent_revisions(op.get_bind())
    op.alter_column("proposal_revisions", "revision_number", existing_type=sa.Integer(), nullable=False)
    op.alter_column(
        "proposal_revisions",
        "origin",
        existing_type=sa.Enum(
            "agent",
            "manual",
            name="proposal_revision_origin",
            native_enum=False,
            create_constraint=False,
            length=16,
        ),
        nullable=False,
    )
    op.alter_column(
        "proposal_revisions", "created_by", existing_type=sa.String(length=36), nullable=False
    )
    op.drop_constraint("ck_proposal_revisions_iteration", "proposal_revisions", type_="check")
    op.alter_column("proposal_revisions", "iteration", existing_type=sa.Integer(), nullable=True)
    op.create_unique_constraint(
        "uq_proposal_revisions_proposal_revision_number",
        "proposal_revisions",
        ["proposal_id", "revision_number"],
    )
    op.create_check_constraint(
        "ck_proposal_revisions_revision_number", "proposal_revisions", "revision_number >= 1"
    )
    op.create_check_constraint(
        "ck_proposal_revisions_origin",
        "proposal_revisions",
        "origin IN ('agent', 'manual')",
    )
    op.create_check_constraint(
        "ck_proposal_revisions_origin_iteration",
        "proposal_revisions",
        "(origin = 'agent' AND iteration IS NOT NULL AND iteration BETWEEN 0 AND 2) "
        "OR (origin = 'manual' AND iteration IS NULL)",
    )
    op.create_check_constraint(
        "ck_proposal_revisions_parent",
        "proposal_revisions",
        "(revision_number = 1 AND parent_revision_id IS NULL) "
        "OR (revision_number > 1 AND parent_revision_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_proposal_revisions_not_self_parent",
        "proposal_revisions",
        "parent_revision_id IS NULL OR parent_revision_id <> id",
    )
    op.create_foreign_key(
        "fk_proposal_revisions_created_by",
        "proposal_revisions",
        "users",
        ["created_by"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_proposal_revisions_parent_revision_id",
        "proposal_revisions",
        "proposal_revisions",
        ["parent_revision_id"],
        ["id"],
    )

    op.drop_constraint("ck_compliance_reviews_iteration", "compliance_reviews", type_="check")
    op.alter_column("compliance_reviews", "iteration", existing_type=sa.Integer(), nullable=True)
    op.create_check_constraint(
        "ck_compliance_reviews_iteration",
        "compliance_reviews",
        "iteration IS NULL OR iteration BETWEEN 0 AND 2",
    )

    op.add_column(
        "product_proposals", sa.Column("active_manual_review_run_id", sa.String(length=36))
    )
    op.add_column("product_proposals", sa.Column("submitted_revision_id", sa.String(length=36)))

    op.create_table(
        "manual_review_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workflow_run_id", sa.String(length=36), nullable=False),
        sa.Column("proposal_id", sa.String(length=36), nullable=False),
        sa.Column("proposal_revision_id", sa.String(length=36), nullable=False),
        sa.Column("submitted_by", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(idempotency_key_hash) = 64",
            name="ck_manual_review_runs_idempotency_hash_length",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64", name="ck_manual_review_runs_request_hash_length"
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"],
            ["workflow_runs.id"],
            name="fk_manual_review_runs_workflow_run_id",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["product_proposals.id"],
            name="fk_manual_review_runs_proposal_id",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_revision_id"],
            ["proposal_revisions.id"],
            name="fk_manual_review_runs_proposal_revision_id",
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by"], ["users.id"], name="fk_manual_review_runs_submitted_by"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_run_id", name="uq_manual_review_runs_workflow_run_id"),
        sa.UniqueConstraint(
            "proposal_revision_id", name="uq_manual_review_runs_proposal_revision_id"
        ),
        sa.UniqueConstraint(
            "proposal_id",
            "submitted_by",
            "idempotency_key_hash",
            name="uq_manual_review_runs_proposal_actor_key",
        ),
    )
    op.create_index(
        "ix_manual_review_runs_proposal_created",
        "manual_review_runs",
        ["proposal_id", "created_at", "id"],
    )

    op.create_table(
        "approval_actions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("proposal_id", sa.String(length=36), nullable=False),
        sa.Column("proposal_revision_id", sa.String(length=36), nullable=False),
        sa.Column("store_id", sa.String(length=36), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=False),
        sa.Column(
            "actor_role",
            sa.Enum(
                "operator",
                "supervisor",
                "admin",
                name="user_role",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "action",
            sa.Enum(
                "submit",
                "approve",
                "reject",
                "request_changes",
                name="approval_action_type",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("comment", sa.String(length=500)),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('submit', 'approve', 'reject', 'request_changes')",
            name="ck_approval_actions_action",
        ),
        sa.CheckConstraint(
            "actor_role IN ('operator', 'supervisor', 'admin')",
            name="ck_approval_actions_actor_role",
        ),
        sa.CheckConstraint(
            "length(idempotency_key_hash) = 64",
            name="ck_approval_actions_idempotency_hash_length",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64", name="ck_approval_actions_request_hash_length"
        ),
        sa.CheckConstraint(
            "((action IN ('reject', 'request_changes') AND comment IS NOT NULL "
            "AND length(trim(comment)) BETWEEN 1 AND 500) "
            "OR (action IN ('submit', 'approve') AND comment IS NULL))",
            name="ck_approval_actions_comment",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["product_proposals.id"], name="fk_approval_actions_proposal_id"
        ),
        sa.ForeignKeyConstraint(
            ["proposal_revision_id"],
            ["proposal_revisions.id"],
            name="fk_approval_actions_proposal_revision_id",
        ),
        sa.ForeignKeyConstraint(
            ["store_id"], ["stores.id"], name="fk_approval_actions_store_id"
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], name="fk_approval_actions_actor_id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "proposal_id",
            "actor_id",
            "action",
            "idempotency_key_hash",
            name="uq_approval_actions_proposal_actor_action_key",
        ),
    )
    op.create_index(
        "ix_approval_actions_proposal_created",
        "approval_actions",
        ["proposal_id", "created_at", "id"],
    )
    op.create_index(
        "ix_approval_actions_store_created",
        "approval_actions",
        ["store_id", "created_at", "id"],
    )

    op.create_table(
        "publish_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("proposal_id", sa.String(length=36), nullable=False),
        sa.Column("proposal_revision_id", sa.String(length=36), nullable=False),
        sa.Column("product_id", sa.String(length=36), nullable=False),
        sa.Column("store_id", sa.String(length=36), nullable=False),
        sa.Column("approved_by", sa.String(length=36), nullable=False),
        sa.Column("approval_action_id", sa.String(length=36), nullable=False),
        sa.Column("publish_idempotency_hash", sa.String(length=64), nullable=False),
        sa.Column("before_snapshot", sa.JSON(), nullable=False),
        sa.Column("after_snapshot", sa.JSON(), nullable=False),
        sa.Column("base_product_version", sa.Integer(), nullable=False),
        sa.Column("published_product_version", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("base_product_version >= 1", name="ck_publish_records_base_version"),
        sa.CheckConstraint(
            "published_product_version = base_product_version + 1",
            name="ck_publish_records_version_increment",
        ),
        sa.CheckConstraint(
            "length(publish_idempotency_hash) = 64",
            name="ck_publish_records_idempotency_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["product_proposals.id"], name="fk_publish_records_proposal_id"
        ),
        sa.ForeignKeyConstraint(
            ["proposal_revision_id"],
            ["proposal_revisions.id"],
            name="fk_publish_records_proposal_revision_id",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name="fk_publish_records_product_id"
        ),
        sa.ForeignKeyConstraint(
            ["store_id"], ["stores.id"], name="fk_publish_records_store_id"
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"], ["users.id"], name="fk_publish_records_approved_by"
        ),
        sa.ForeignKeyConstraint(
            ["approval_action_id"],
            ["approval_actions.id"],
            name="fk_publish_records_approval_action_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("proposal_id", name="uq_publish_records_proposal_id"),
        sa.UniqueConstraint(
            "proposal_revision_id", name="uq_publish_records_proposal_revision_id"
        ),
        sa.UniqueConstraint("approval_action_id", name="uq_publish_records_approval_action_id"),
        sa.UniqueConstraint(
            "publish_idempotency_hash", name="uq_publish_records_publish_idempotency_hash"
        ),
    )
    op.create_index(
        "ix_publish_records_store_published",
        "publish_records",
        ["store_id", "published_at", "id"],
    )
    op.create_index(
        "ix_publish_records_product_published",
        "publish_records",
        ["product_id", "published_at", "id"],
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum(
                "manual_revision_created",
                "manual_review_claimed",
                "manual_review_completed",
                "manual_review_failed",
                "proposal_submitted",
                "proposal_approved",
                "proposal_rejected",
                "proposal_changes_requested",
                "simulated_publish_completed",
                "authorization_denied",
                name="audit_event_type",
                native_enum=False,
                create_constraint=False,
                length=64,
            ),
            nullable=False,
        ),
        sa.Column(
            "outcome",
            sa.Enum(
                "success",
                "failed",
                "denied",
                name="audit_outcome",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("actor_id", sa.String(length=36)),
        sa.Column(
            "actor_role",
            sa.Enum(
                "operator",
                "supervisor",
                "admin",
                name="user_role",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
        ),
        sa.Column("store_id", sa.String(length=36), nullable=False),
        sa.Column("proposal_id", sa.String(length=36)),
        sa.Column("proposal_revision_id", sa.String(length=36)),
        sa.Column("workflow_run_id", sa.String(length=36)),
        sa.Column("approval_action_id", sa.String(length=36)),
        sa.Column("publish_record_id", sa.String(length=36)),
        sa.Column("request_id", sa.String(length=64)),
        sa.Column("error_code", sa.String(length=64)),
        sa.Column("details", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('manual_revision_created', 'manual_review_claimed', "
            "'manual_review_completed', 'manual_review_failed', 'proposal_submitted', "
            "'proposal_approved', 'proposal_rejected', 'proposal_changes_requested', "
            "'simulated_publish_completed', 'authorization_denied')",
            name="ck_audit_events_event_type",
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'failed', 'denied')", name="ck_audit_events_outcome"
        ),
        sa.CheckConstraint(
            "actor_role IS NULL OR actor_role IN ('operator', 'supervisor', 'admin')",
            name="ck_audit_events_actor_role",
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], name="fk_audit_events_actor_id"),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], name="fk_audit_events_store_id"),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["product_proposals.id"], name="fk_audit_events_proposal_id"
        ),
        sa.ForeignKeyConstraint(
            ["proposal_revision_id"],
            ["proposal_revisions.id"],
            name="fk_audit_events_proposal_revision_id",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"], ["workflow_runs.id"], name="fk_audit_events_workflow_run_id"
        ),
        sa.ForeignKeyConstraint(
            ["approval_action_id"],
            ["approval_actions.id"],
            name="fk_audit_events_approval_action_id",
        ),
        sa.ForeignKeyConstraint(
            ["publish_record_id"],
            ["publish_records.id"],
            name="fk_audit_events_publish_record_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_events_store_created", "audit_events", ["store_id", "created_at", "id"]
    )
    op.create_index(
        "ix_audit_events_proposal_created",
        "audit_events",
        ["proposal_id", "created_at", "id"],
    )
    op.create_index(
        "ix_audit_events_workflow_created",
        "audit_events",
        ["workflow_run_id", "created_at", "id"],
    )
    op.create_index(
        "ix_audit_events_actor_created", "audit_events", ["actor_id", "created_at", "id"]
    )

    op.create_foreign_key(
        "fk_product_proposals_active_manual_review_run_id",
        "product_proposals",
        "manual_review_runs",
        ["active_manual_review_run_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_product_proposals_active_manual_review_run_id",
        "product_proposals",
        ["active_manual_review_run_id"],
    )
    op.create_foreign_key(
        "fk_product_proposals_submitted_revision_id",
        "product_proposals",
        "proposal_revisions",
        ["submitted_revision_id"],
        ["id"],
    )
    op.create_index(
        "ix_product_proposals_submitted_revision_id",
        "product_proposals",
        ["submitted_revision_id"],
    )


def downgrade() -> None:
    _guard_stage_five_downgrade(op.get_bind())

    op.drop_index("ix_product_proposals_submitted_revision_id", table_name="product_proposals")
    op.drop_constraint(
        "fk_product_proposals_submitted_revision_id", "product_proposals", type_="foreignkey"
    )
    op.drop_constraint(
        "uq_product_proposals_active_manual_review_run_id", "product_proposals", type_="unique"
    )
    op.drop_constraint(
        "fk_product_proposals_active_manual_review_run_id",
        "product_proposals",
        type_="foreignkey",
    )

    op.drop_table("audit_events")
    op.drop_table("publish_records")
    op.drop_table("approval_actions")
    op.drop_table("manual_review_runs")
    op.drop_column("product_proposals", "submitted_revision_id")
    op.drop_column("product_proposals", "active_manual_review_run_id")

    op.drop_constraint("ck_compliance_reviews_iteration", "compliance_reviews", type_="check")
    op.alter_column("compliance_reviews", "iteration", existing_type=sa.Integer(), nullable=False)
    op.create_check_constraint(
        "ck_compliance_reviews_iteration", "compliance_reviews", "iteration BETWEEN 0 AND 2"
    )

    op.drop_constraint(
        "fk_proposal_revisions_parent_revision_id", "proposal_revisions", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_proposal_revisions_created_by", "proposal_revisions", type_="foreignkey"
    )
    op.drop_constraint(
        "uq_proposal_revisions_proposal_revision_number", "proposal_revisions", type_="unique"
    )
    for name in (
        "ck_proposal_revisions_not_self_parent",
        "ck_proposal_revisions_parent",
        "ck_proposal_revisions_origin_iteration",
        "ck_proposal_revisions_origin",
        "ck_proposal_revisions_revision_number",
    ):
        op.drop_constraint(name, "proposal_revisions", type_="check")
    op.alter_column("proposal_revisions", "iteration", existing_type=sa.Integer(), nullable=False)
    op.drop_column("proposal_revisions", "parent_revision_id")
    op.drop_column("proposal_revisions", "created_by")
    op.drop_column("proposal_revisions", "origin")
    op.drop_column("proposal_revisions", "revision_number")
    op.create_check_constraint(
        "ck_proposal_revisions_iteration", "proposal_revisions", "iteration BETWEEN 0 AND 2"
    )

    op.drop_index("ix_workflow_runs_type_status_lease_created", table_name="workflow_runs")
    op.drop_constraint("ck_workflow_runs_type_status_dates", "workflow_runs", type_="check")
    op.create_check_constraint(
        "ck_workflow_runs_type_status_dates", "workflow_runs", _workflow_type_check_0004
    )
