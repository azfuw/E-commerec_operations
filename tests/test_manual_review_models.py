from datetime import date
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

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
from backend.models import (
    AnalysisCandidate,
    ApprovalAction,
    AuditEvent,
    ComplianceReview,
    ManualReviewRun,
    Product,
    ProductProposal,
    ProposalRevision,
    PublishRecord,
    Store,
    User,
    WorkflowRun,
)


def _migration():
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0005_manual_review_approval_publish.py"
    spec = spec_from_file_location("manual_review_migration_0005", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _assert_integrity_error(session, row: object) -> None:
    async with session.begin_nested():
        session.add(row)
        with pytest.raises(IntegrityError):
            await session.flush()


async def _seed_proposal(session) -> tuple[ProductProposal, WorkflowRun, ProposalRevision]:
    store = Store(id="store-1", name="旗舰店", code="flagship")
    user = User(
        id="user-1",
        username="operator",
        password_hash="hash",
        role=UserRole.OPERATOR,
    )
    product = Product(
        id="product-1",
        store_id=store.id,
        code="PRODUCT-1",
        title="商品一",
        category="数码",
        current_version=7,
    )
    session.add_all([store, user])
    await session.flush()
    session.add(product)
    await session.flush()
    analysis = WorkflowRun(
        id="analysis-1",
        workflow_type=WorkflowType.ANALYSIS,
        store_id=store.id,
        created_by=user.id,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2),
        status=WorkflowStatus.COMPLETED,
        quality_status=WorkflowQuality.NORMAL,
        current_step="product_selected",
    )
    optimization = WorkflowRun(
        id="optimization-1",
        workflow_type=WorkflowType.OPTIMIZATION,
        store_id=store.id,
        created_by=user.id,
        status=WorkflowStatus.DRAFT_READY,
        quality_status=WorkflowQuality.NORMAL,
    )
    session.add_all([analysis, optimization])
    await session.flush()
    candidate = AnalysisCandidate(
        id="candidate-1",
        workflow_run_id=analysis.id,
        product_id=product.id,
        rank=1,
        product_code=product.code,
        anomaly_types=["low_conversion"],
        metrics={"product_id": product.id},
        business_impact=Decimal("1.00"),
        evidence=["clicks=10"],
        impact_explanation="影响说明",
        reason="原因",
        recommended_action="建议",
        confidence=Decimal("0.8000"),
    )
    session.add(candidate)
    await session.flush()
    proposal = ProductProposal(
        id="proposal-1",
        analysis_run_id=analysis.id,
        analysis_candidate_id=candidate.id,
        optimization_run_id=optimization.id,
        store_id=store.id,
        product_id=product.id,
        base_product_version=7,
        selection_idempotency_hash="a" * 64,
    )
    session.add(proposal)
    await session.flush()
    revision = ProposalRevision(
        id="revision-1",
        proposal_id=proposal.id,
        iteration=0,
        revision_number=1,
        origin=ProposalRevisionOrigin.AGENT,
        created_by=user.id,
        parent_revision_id=None,
        base_product_version=7,
        trusted_fact_hash="b" * 64,
        proposal_output={"title": "商品一"},
        citations=[],
    )
    session.add(revision)
    await session.flush()
    proposal.current_revision_id = revision.id
    await session.flush()
    return proposal, optimization, revision


def test_stage_five_closed_sets_are_exact() -> None:
    assert {member.value for member in ProposalRevisionOrigin} == {"agent", "manual"}
    assert {member.value for member in ApprovalActionType} == {
        "submit",
        "approve",
        "reject",
        "request_changes",
    }
    assert {member.value for member in AuditOutcome} == {"success", "failed", "denied"}
    assert {member.value for member in AuditEventType} == {
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
        "knowledge_document_created",
        "knowledge_version_created",
        "knowledge_document_disabled",
        "evaluation_run_persisted",
        "admin_user_updated",
        "admin_user_scopes_replaced",
        "admin_store_updated",
        "platform_delivery_enqueued",
        "platform_delivery_completed",
        "platform_delivery_failed",
        "platform_webhook_received",
    }
    assert WorkflowType.MANUAL_REVIEW.value == "manual_review"
    assert WorkflowStatus.PENDING_APPROVAL.value == "pending_approval"
    assert WorkflowStatus.REJECTED.value == "rejected"


async def test_manual_review_workflow_matrix_and_nullable_review_iteration(session) -> None:
    proposal, optimization, agent_revision = await _seed_proposal(session)
    optimization.status = WorkflowStatus.PENDING_APPROVAL
    manual_workflow = WorkflowRun(
        id="manual-workflow-1",
        workflow_type=WorkflowType.MANUAL_REVIEW,
        store_id=proposal.store_id,
        created_by="user-1",
        status=WorkflowStatus.ACCEPTED,
        quality_status=WorkflowQuality.NORMAL,
    )
    manual_revision = ProposalRevision(
        id="revision-2",
        proposal_id=proposal.id,
        iteration=None,
        revision_number=2,
        origin=ProposalRevisionOrigin.MANUAL,
        created_by="user-1",
        parent_revision_id=agent_revision.id,
        base_product_version=7,
        trusted_fact_hash="c" * 64,
        proposal_output={"title": "人工商品"},
        citations=[],
    )
    session.add_all([manual_workflow, manual_revision])
    await session.flush()
    review = ComplianceReview(
        id="review-2",
        proposal_id=proposal.id,
        proposal_revision_id=manual_revision.id,
        iteration=None,
        deterministic_checks={"passed": True},
        semantic_review={"passed": True},
        passed=True,
        risk_level=ComplianceRiskLevel.LOW,
        required_changes=[],
        citations=[],
        quality_status=WorkflowQuality.NORMAL,
    )
    session.add(review)
    await session.flush()

    assert review.iteration is None
    await _assert_integrity_error(
        session,
        WorkflowRun(
            id="manual-with-date",
            workflow_type=WorkflowType.MANUAL_REVIEW,
            store_id=proposal.store_id,
            created_by="user-1",
            start_date=date(2026, 8, 1),
            status=WorkflowStatus.ACCEPTED,
            quality_status=WorkflowQuality.NORMAL,
        ),
    )
    await _assert_integrity_error(
        session,
        WorkflowRun(
            id="manual-pending-approval",
            workflow_type=WorkflowType.MANUAL_REVIEW,
            store_id=proposal.store_id,
            created_by="user-1",
            status=WorkflowStatus.PENDING_APPROVAL,
            quality_status=WorkflowQuality.NORMAL,
        ),
    )


async def test_revision_origin_number_and_parent_constraints(session) -> None:
    proposal, _, first = await _seed_proposal(session)
    invalid = [
        ProposalRevision(
            id="manual-with-iteration",
            proposal_id=proposal.id,
            iteration=1,
            revision_number=2,
            origin=ProposalRevisionOrigin.MANUAL,
            created_by="user-1",
            parent_revision_id=first.id,
            base_product_version=7,
            trusted_fact_hash="c" * 64,
            proposal_output={},
            citations=[],
        ),
        ProposalRevision(
            id="agent-without-iteration",
            proposal_id=proposal.id,
            iteration=None,
            revision_number=2,
            origin=ProposalRevisionOrigin.AGENT,
            created_by="user-1",
            parent_revision_id=first.id,
            base_product_version=7,
            trusted_fact_hash="d" * 64,
            proposal_output={},
            citations=[],
        ),
        ProposalRevision(
            id="revision-number-zero",
            proposal_id=proposal.id,
            iteration=None,
            revision_number=0,
            origin=ProposalRevisionOrigin.MANUAL,
            created_by="user-1",
            parent_revision_id=first.id,
            base_product_version=7,
            trusted_fact_hash="e" * 64,
            proposal_output={},
            citations=[],
        ),
        ProposalRevision(
            id="revision-self-parent",
            proposal_id=proposal.id,
            iteration=None,
            revision_number=2,
            origin=ProposalRevisionOrigin.MANUAL,
            created_by="user-1",
            parent_revision_id="revision-self-parent",
            base_product_version=7,
            trusted_fact_hash="f" * 64,
            proposal_output={},
            citations=[],
        ),
    ]
    for row in invalid:
        await _assert_integrity_error(session, row)


def _constraint_names(table) -> set[str]:
    return {constraint.name for constraint in table.constraints if constraint.name is not None}


def _foreign_key_names(table) -> set[str]:
    return {constraint.name for constraint in table.foreign_key_constraints}


def _index_names(table) -> set[str]:
    return {index.name for index in table.indexes}


def test_all_stage_five_named_constraints_and_indexes_are_mapped() -> None:
    assert {
        "uq_proposal_revisions_proposal_revision_number",
        "ck_proposal_revisions_revision_number",
        "ck_proposal_revisions_origin",
        "ck_proposal_revisions_origin_iteration",
        "ck_proposal_revisions_parent",
        "ck_proposal_revisions_not_self_parent",
    } <= _constraint_names(ProposalRevision.__table__)
    assert {
        "fk_proposal_revisions_proposal_id",
        "fk_proposal_revisions_created_by",
        "fk_proposal_revisions_parent_revision_id",
    } == _foreign_key_names(ProposalRevision.__table__)
    assert {
        "fk_product_proposals_active_manual_review_run_id",
        "fk_product_proposals_submitted_revision_id",
    } <= _foreign_key_names(ProductProposal.__table__)
    assert "uq_product_proposals_active_manual_review_run_id" in _constraint_names(
        ProductProposal.__table__
    )
    assert "ix_product_proposals_submitted_revision_id" in _index_names(ProductProposal.__table__)
    assert "ix_workflow_runs_type_status_lease_created" in _index_names(WorkflowRun.__table__)

    expected = {
        ManualReviewRun: {
            "constraints": {
                "uq_manual_review_runs_workflow_run_id",
                "uq_manual_review_runs_proposal_revision_id",
                "uq_manual_review_runs_proposal_actor_key",
                "ck_manual_review_runs_idempotency_hash_length",
                "ck_manual_review_runs_request_hash_length",
            },
            "indexes": {"ix_manual_review_runs_proposal_created"},
            "foreign_keys": {
                "fk_manual_review_runs_workflow_run_id",
                "fk_manual_review_runs_proposal_id",
                "fk_manual_review_runs_proposal_revision_id",
                "fk_manual_review_runs_submitted_by",
            },
        },
        ApprovalAction: {
            "constraints": {
                "uq_approval_actions_proposal_actor_action_key",
                "ck_approval_actions_action",
                "ck_approval_actions_actor_role",
                "ck_approval_actions_idempotency_hash_length",
                "ck_approval_actions_request_hash_length",
                "ck_approval_actions_comment",
            },
            "indexes": {
                "ix_approval_actions_proposal_created",
                "ix_approval_actions_store_created",
            },
            "foreign_keys": {
                "fk_approval_actions_proposal_id",
                "fk_approval_actions_proposal_revision_id",
                "fk_approval_actions_store_id",
                "fk_approval_actions_actor_id",
            },
        },
        PublishRecord: {
            "constraints": {
                "uq_publish_records_proposal_id",
                "uq_publish_records_proposal_revision_id",
                "uq_publish_records_approval_action_id",
                "uq_publish_records_publish_idempotency_hash",
                "ck_publish_records_base_version",
                "ck_publish_records_version_increment",
                "ck_publish_records_idempotency_hash_length",
            },
            "indexes": {
                "ix_publish_records_store_published",
                "ix_publish_records_product_published",
            },
            "foreign_keys": {
                "fk_publish_records_proposal_id",
                "fk_publish_records_proposal_revision_id",
                "fk_publish_records_product_id",
                "fk_publish_records_store_id",
                "fk_publish_records_approved_by",
                "fk_publish_records_approval_action_id",
            },
        },
        AuditEvent: {
            "constraints": {
                "ck_audit_events_event_type",
                "ck_audit_events_outcome",
                "ck_audit_events_actor_role",
            },
            "indexes": {
                "ix_audit_events_store_created",
                "ix_audit_events_proposal_created",
                "ix_audit_events_workflow_created",
                "ix_audit_events_actor_created",
                "ix_audit_events_event_created",
                "ix_audit_events_outcome_created",
            },
            "foreign_keys": {
                "fk_audit_events_actor_id",
                "fk_audit_events_store_id",
                "fk_audit_events_proposal_id",
                "fk_audit_events_proposal_revision_id",
                "fk_audit_events_workflow_run_id",
                "fk_audit_events_approval_action_id",
                "fk_audit_events_publish_record_id",
            },
        },
    }
    for model, names in expected.items():
        assert names["constraints"] <= _constraint_names(model.__table__)
        assert names["indexes"] == _index_names(model.__table__)
        assert names["foreign_keys"] == _foreign_key_names(model.__table__)


async def test_new_rows_enforce_hash_comment_publish_and_audit_contracts(session) -> None:
    proposal, _, parent = await _seed_proposal(session)
    workflow = WorkflowRun(
        id="manual-workflow-1",
        workflow_type=WorkflowType.MANUAL_REVIEW,
        store_id=proposal.store_id,
        created_by="user-1",
        status=WorkflowStatus.ACCEPTED,
        quality_status=WorkflowQuality.NORMAL,
    )
    revision = ProposalRevision(
        id="revision-2",
        proposal_id=proposal.id,
        iteration=None,
        revision_number=2,
        origin=ProposalRevisionOrigin.MANUAL,
        created_by="user-1",
        parent_revision_id=parent.id,
        base_product_version=7,
        trusted_fact_hash="c" * 64,
        proposal_output={},
        citations=[],
    )
    session.add_all([workflow, revision])
    await session.flush()
    manual_run = ManualReviewRun(
        id="manual-run-1",
        workflow_run_id=workflow.id,
        proposal_id=proposal.id,
        proposal_revision_id=revision.id,
        submitted_by="user-1",
        idempotency_key_hash="d" * 64,
        request_hash="e" * 64,
    )
    session.add(manual_run)
    await session.flush()
    proposal.active_manual_review_run_id = manual_run.id
    proposal.submitted_revision_id = revision.id
    await session.flush()

    second_workflow = WorkflowRun(
        id="manual-workflow-2",
        workflow_type=WorkflowType.MANUAL_REVIEW,
        store_id=proposal.store_id,
        created_by="user-1",
        status=WorkflowStatus.ACCEPTED,
        quality_status=WorkflowQuality.NORMAL,
    )
    second_revision = ProposalRevision(
        id="revision-3",
        proposal_id=proposal.id,
        iteration=None,
        revision_number=3,
        origin=ProposalRevisionOrigin.MANUAL,
        created_by="user-1",
        parent_revision_id=revision.id,
        base_product_version=7,
        trusted_fact_hash="4" * 64,
        proposal_output={},
        citations=[],
    )
    session.add_all([second_workflow, second_revision])
    await session.flush()
    await _assert_integrity_error(
        session,
        ManualReviewRun(
            id="bad-manual-hash",
            workflow_run_id=second_workflow.id,
            proposal_id=proposal.id,
            proposal_revision_id=second_revision.id,
            submitted_by="user-1",
            idempotency_key_hash="d" * 63,
            request_hash="e" * 64,
        ),
    )

    await _assert_integrity_error(
        session,
        ApprovalAction(
            id="bad-comment",
            proposal_id=proposal.id,
            proposal_revision_id=revision.id,
            store_id=proposal.store_id,
            actor_id="user-1",
            actor_role=UserRole.OPERATOR,
            action=ApprovalActionType.REJECT,
            comment="   ",
            idempotency_key_hash="f" * 64,
            request_hash="0" * 64,
        ),
    )
    await _assert_integrity_error(
        session,
        ApprovalAction(
            id="bad-action-hash",
            proposal_id=proposal.id,
            proposal_revision_id=revision.id,
            store_id=proposal.store_id,
            actor_id="user-1",
            actor_role=UserRole.OPERATOR,
            action=ApprovalActionType.SUBMIT,
            comment=None,
            idempotency_key_hash="f" * 63,
            request_hash="0" * 64,
        ),
    )
    action = ApprovalAction(
        id="approve-1",
        proposal_id=proposal.id,
        proposal_revision_id=revision.id,
        store_id=proposal.store_id,
        actor_id="user-1",
        actor_role=UserRole.ADMIN,
        action=ApprovalActionType.APPROVE,
        comment=None,
        idempotency_key_hash="1" * 64,
        request_hash="2" * 64,
    )
    session.add(action)
    await session.flush()
    await _assert_integrity_error(
        session,
        PublishRecord(
            id="bad-publish",
            proposal_id=proposal.id,
            proposal_revision_id=revision.id,
            product_id=proposal.product_id,
            store_id=proposal.store_id,
            approved_by="user-1",
            approval_action_id=action.id,
            publish_idempotency_hash="3" * 64,
            before_snapshot={},
            after_snapshot={},
            base_product_version=7,
            published_product_version=9,
        ),
    )
    audit = AuditEvent(
        id="audit-1",
        event_type=AuditEventType.PROPOSAL_APPROVED,
        outcome=AuditOutcome.SUCCESS,
        actor_id="user-1",
        actor_role=UserRole.ADMIN,
        store_id=proposal.store_id,
        proposal_id=proposal.id,
        proposal_revision_id=revision.id,
        workflow_run_id=workflow.id,
        approval_action_id=action.id,
    )
    session.add(audit)
    await session.flush()
    assert audit.details == {}
    await _assert_integrity_error(
        session,
        AuditEvent(
            id="bad-audit",
            event_type="other",
            outcome=AuditOutcome.SUCCESS,
            store_id=proposal.store_id,
            details={},
        ),
    )


def _old_history_metadata() -> sa.MetaData:
    metadata = sa.MetaData()
    sa.Table(
        "workflow_runs",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workflow_type", sa.String(32), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
    )
    sa.Table(
        "product_proposals",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("optimization_run_id", sa.String(36), nullable=False),
    )
    sa.Table(
        "proposal_revisions",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("proposal_id", sa.String(36), nullable=False),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("revision_number", sa.Integer()),
        sa.Column("origin", sa.String(16)),
        sa.Column("created_by", sa.String(36)),
        sa.Column("parent_revision_id", sa.String(36)),
    )
    return metadata


def test_migration_backfills_agent_number_creator_and_exact_parent_chain() -> None:
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    metadata = _old_history_metadata()
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["workflow_runs"].insert(),
            {"id": "optimization-1", "workflow_type": "optimization", "created_by": "user-1"},
        )
        connection.execute(
            metadata.tables["product_proposals"].insert(),
            {"id": "proposal-1", "optimization_run_id": "optimization-1"},
        )
        connection.execute(
            metadata.tables["proposal_revisions"].insert(),
            [
                {"id": "revision-0", "proposal_id": "proposal-1", "iteration": 0},
                {"id": "revision-1", "proposal_id": "proposal-1", "iteration": 1},
                {"id": "revision-2", "proposal_id": "proposal-1", "iteration": 2},
            ],
        )
        migration._backfill_agent_revisions(connection)
        rows = connection.execute(
            sa.text(
                "SELECT id, iteration, revision_number, origin, created_by, parent_revision_id "
                "FROM proposal_revisions ORDER BY iteration"
            )
        ).all()
    assert rows == [
        ("revision-0", 0, 1, "agent", "user-1", None),
        ("revision-1", 1, 2, "agent", "user-1", "revision-0"),
        ("revision-2", 2, 3, "agent", "user-1", "revision-1"),
    ]


@pytest.mark.parametrize("shape", ["missing", "duplicate", "cross_proposal", "missing_creator"])
def test_migration_rejects_malformed_agent_history_before_tightening(shape: str) -> None:
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    metadata = _old_history_metadata()
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["workflow_runs"].insert(),
            {"id": "optimization-1", "workflow_type": "optimization", "created_by": "user-1"},
        )
        proposals = [{"id": "proposal-1", "optimization_run_id": "optimization-1"}]
        revisions: list[dict[str, object]]
        if shape == "missing":
            revisions = [{"id": "revision-1", "proposal_id": "proposal-1", "iteration": 1}]
        elif shape == "duplicate":
            revisions = [
                {"id": "revision-0a", "proposal_id": "proposal-1", "iteration": 0},
                {"id": "revision-0b", "proposal_id": "proposal-1", "iteration": 0},
                {"id": "revision-1", "proposal_id": "proposal-1", "iteration": 1},
            ]
        elif shape == "cross_proposal":
            proposals.append({"id": "proposal-2", "optimization_run_id": "optimization-1"})
            revisions = [
                {"id": "revision-1", "proposal_id": "proposal-1", "iteration": 1},
                {"id": "revision-0", "proposal_id": "proposal-2", "iteration": 0},
            ]
        else:
            proposals[0]["optimization_run_id"] = "missing-run"
            revisions = [{"id": "revision-0", "proposal_id": "proposal-1", "iteration": 0}]
        connection.execute(metadata.tables["product_proposals"].insert(), proposals)
        connection.execute(metadata.tables["proposal_revisions"].insert(), revisions)
        with pytest.raises(RuntimeError, match="cannot backfill proposal revisions"):
            migration._backfill_agent_revisions(connection)


def _downgrade_metadata() -> sa.MetaData:
    metadata = sa.MetaData()
    sa.Table(
        "workflow_runs",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workflow_type", sa.String(32), nullable=False),
    )
    sa.Table(
        "proposal_revisions",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("origin", sa.String(16), nullable=False),
    )
    for name in ("manual_review_runs", "approval_actions", "publish_records", "audit_events"):
        sa.Table(name, metadata, sa.Column("id", sa.String(36), primary_key=True))
    return metadata


@pytest.mark.parametrize(
    ("table", "values"),
    [
        ("proposal_revisions", {"id": "manual-revision", "origin": "manual"}),
        ("workflow_runs", {"id": "manual-workflow", "workflow_type": "manual_review"}),
        ("manual_review_runs", {"id": "manual-run"}),
        ("approval_actions", {"id": "approval"}),
        ("publish_records", {"id": "publish"}),
        ("audit_events", {"id": "audit"}),
    ],
)
def test_migration_downgrade_refuses_each_stage_five_fact(table: str, values: dict[str, str]) -> None:
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    metadata = _downgrade_metadata()
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["workflow_runs"].insert(),
            {"id": "optimization-1", "workflow_type": "optimization"},
        )
        connection.execute(
            metadata.tables["proposal_revisions"].insert(),
            {"id": "agent-revision", "origin": "agent"},
        )
        migration._guard_stage_five_downgrade(connection)
        connection.execute(metadata.tables[table].insert(), values)
        with pytest.raises(RuntimeError, match="cannot downgrade manual review approval publish"):
            migration._guard_stage_five_downgrade(connection)


def test_migration_identity_is_0005_over_0004() -> None:
    migration = _migration()
    assert (migration.revision, migration.down_revision) == ("0005", "0004")
