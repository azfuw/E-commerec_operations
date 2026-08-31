from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from backend.common import (
    AgentCallType,
    ComplianceRiskLevel,
    ProposalRevisionOrigin,
    UserRole,
    UserStatus,
    WorkflowQuality,
    WorkflowStatus,
    WorkflowType,
)
from backend.models import (
    AgentCall,
    AnalysisCandidate,
    ComplianceReview,
    Product,
    ProductProposal,
    ProposalRevision,
    Store,
    User,
    WorkflowRun,
)


async def _add_references(session) -> None:
    store = Store(id="store-1", name="旗舰店", code="flagship")
    user = User(
        id="user-1",
        username="operator",
        password_hash="hash",
        role=UserRole.OPERATOR,
        status=UserStatus.ACTIVE,
    )
    product = Product(
        id="product-1",
        store_id=store.id,
        code="FLAGSHIP-001",
        title="商品一",
        category="数码",
        current_version=7,
    )
    session.add_all([store, user])
    await session.flush()
    session.add(product)
    await session.flush()


def _analysis_run(run_id: str, **changes: object) -> WorkflowRun:
    values: dict[str, object] = {
        "id": run_id,
        "workflow_type": WorkflowType.ANALYSIS,
        "store_id": "store-1",
        "created_by": "user-1",
        "start_date": date(2026, 8, 1),
        "end_date": date(2026, 8, 2),
        "status": WorkflowStatus.AWAITING_SELECTION,
        "quality_status": WorkflowQuality.NORMAL,
    }
    values.update(changes)
    return WorkflowRun(**values)


def _optimization_run(run_id: str, **changes: object) -> WorkflowRun:
    values: dict[str, object] = {
        "id": run_id,
        "workflow_type": WorkflowType.OPTIMIZATION,
        "store_id": "store-1",
        "created_by": "user-1",
        "start_date": None,
        "end_date": None,
        "status": WorkflowStatus.ACCEPTED,
        "quality_status": WorkflowQuality.NORMAL,
    }
    values.update(changes)
    return WorkflowRun(**values)


def _candidate(candidate_id: str, run_id: str) -> AnalysisCandidate:
    return AnalysisCandidate(
        id=candidate_id,
        workflow_run_id=run_id,
        product_id="product-1",
        rank=1,
        product_code="FLAGSHIP-001",
        anomaly_types=["low_conversion"],
        metrics={"product_id": "product-1"},
        business_impact=Decimal("1.00"),
        evidence=["clicks=300"],
        impact_explanation="影响说明",
        reason="原因",
        recommended_action="建议",
        confidence=Decimal("0.8000"),
    )


def _proposal(proposal_id: str, analysis: WorkflowRun, candidate: AnalysisCandidate, optimization: WorkflowRun, **changes: object) -> ProductProposal:
    values: dict[str, object] = {
        "id": proposal_id,
        "analysis_run_id": analysis.id,
        "analysis_candidate_id": candidate.id,
        "optimization_run_id": optimization.id,
        "store_id": "store-1",
        "product_id": "product-1",
        "base_product_version": 7,
        "selection_idempotency_hash": "a" * 64,
    }
    values.update(changes)
    return ProductProposal(**values)


def _revision(revision_id: str, proposal_id: str, iteration: int = 0, **changes: object) -> ProposalRevision:
    values: dict[str, object] = {
        "id": revision_id,
        "proposal_id": proposal_id,
        "iteration": iteration,
        "revision_number": iteration + 1,
        "origin": ProposalRevisionOrigin.AGENT,
        "created_by": "user-1",
        "parent_revision_id": None,
        "base_product_version": 7,
        "trusted_fact_hash": "b" * 64,
        "proposal_output": {"title": "商品"},
        "citations": [],
    }
    values.update(changes)
    return ProposalRevision(**values)


def _review(review_id: str, proposal_id: str, revision_id: str, iteration: int, **changes: object) -> ComplianceReview:
    values: dict[str, object] = {
        "id": review_id,
        "proposal_id": proposal_id,
        "proposal_revision_id": revision_id,
        "iteration": iteration,
        "deterministic_checks": {"passed": True},
        "semantic_review": {"passed": True},
        "passed": True,
        "risk_level": ComplianceRiskLevel.LOW,
        "required_changes": [],
        "citations": [],
        "quality_status": WorkflowQuality.NORMAL,
    }
    values.update(changes)
    return ComplianceReview(**values)


def _agent_call(call_id: str, run_id: str, **changes: object) -> AgentCall:
    values: dict[str, object] = {
        "id": call_id,
        "workflow_run_id": run_id,
        "node_name": "call_analysis_agent",
        "call_type": AgentCallType.PRIMARY,
        "iteration": 0,
        "attempt": 1,
        "model": "deepseek-v4-flash",
        "prompt_version": "v1",
        "status": "success",
        "input_hash": "0" * 64,
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
        "duration_ms": 100,
    }
    values.update(changes)
    return AgentCall(**values)


async def _selection_facts(session, suffix: str) -> tuple[WorkflowRun, AnalysisCandidate, WorkflowRun]:
    analysis = _analysis_run(f"analysis-{suffix}")
    optimization = _optimization_run(f"optimization-{suffix}")
    session.add_all([analysis, optimization])
    await session.flush()
    candidate = _candidate(f"candidate-{suffix}", analysis.id)
    session.add(candidate)
    await session.flush()
    return analysis, candidate, optimization


async def _selection_graph(session, suffix: str) -> tuple[WorkflowRun, AnalysisCandidate, WorkflowRun, ProductProposal]:
    analysis, candidate, optimization = await _selection_facts(session, suffix)
    proposal = _proposal(f"proposal-{suffix}", analysis, candidate, optimization)
    session.add(proposal)
    await session.flush()
    return analysis, candidate, optimization, proposal


async def _assert_integrity_error(session, row: object) -> None:
    async with session.begin_nested():
        session.add(row)
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_typed_workflows_proposal_revision_and_review_persist(session) -> None:
    await _add_references(session)
    analysis, candidate, optimization = await _selection_facts(session, "one")
    proposal = _proposal("proposal-one", analysis, candidate, optimization)
    session.add(proposal)
    await session.flush()
    revision = _revision("revision-one", proposal.id)
    session.add(revision)
    await session.flush()
    proposal.current_revision_id = revision.id
    review = _review("review-one", proposal.id, revision.id, revision.iteration)
    session.add(review)
    await session.flush()

    assert analysis.workflow_type is WorkflowType.ANALYSIS
    assert optimization.workflow_type is WorkflowType.OPTIMIZATION
    assert proposal.current_revision_id == revision.id
    assert (revision.revision_number, revision.origin, revision.created_by, revision.parent_revision_id) == (
        1,
        ProposalRevisionOrigin.AGENT,
        "user-1",
        None,
    )
    assert review.risk_level is ComplianceRiskLevel.LOW
    assert set(ProductProposal.__table__.columns).isdisjoint({"status", "quality_status", "error_code"})


async def test_type_aware_workflow_dates_and_states_reject_invalid_combinations(session) -> None:
    await _add_references(session)
    invalid_runs = [
        _analysis_run("analysis-no-start", start_date=None),
        _analysis_run("analysis-no-end", end_date=None),
        _optimization_run("optimization-start", start_date=date(2026, 8, 1)),
        _optimization_run("optimization-end", end_date=date(2026, 8, 2)),
        _analysis_run("invalid-workflow-type", workflow_type="other"),
        _analysis_run("analysis-draft-ready", status=WorkflowStatus.DRAFT_READY),
        _optimization_run("optimization-awaiting-selection", status=WorkflowStatus.AWAITING_SELECTION),
    ]

    for run in invalid_runs:
        await _assert_integrity_error(session, run)


async def test_product_proposals_reject_duplicate_keys_and_invalid_versions_or_hashes(session) -> None:
    await _add_references(session)
    analysis_one, candidate_one, optimization_one, _ = await _selection_graph(session, "one")
    analysis_two, candidate_two, optimization_two, _ = await _selection_graph(session, "two")
    analysis_three, candidate_three, optimization_three = await _selection_facts(session, "three")

    await _assert_integrity_error(
        session,
        _proposal("duplicate-analysis", analysis_one, candidate_two, optimization_two),
    )
    await _assert_integrity_error(
        session,
        _proposal("duplicate-candidate", analysis_two, candidate_one, optimization_two),
    )
    await _assert_integrity_error(
        session,
        _proposal("duplicate-optimization", analysis_two, candidate_two, optimization_one),
    )
    for proposal_id, changes in [
        ("base-version-zero", {"base_product_version": 0}),
        ("empty-selection-hash", {"selection_idempotency_hash": ""}),
        ("short-selection-hash", {"selection_idempotency_hash": "a" * 63}),
    ]:
        await _assert_integrity_error(
            session,
            _proposal(proposal_id, analysis_three, candidate_three, optimization_three, **changes),
        )


async def test_proposal_revisions_reject_invalid_iterations_versions_hashes_and_duplicate_iteration(session) -> None:
    await _add_references(session)
    _, _, _, proposal = await _selection_graph(session, "one")
    revision = _revision("revision-zero", proposal.id)
    session.add(revision)
    await session.flush()

    for revision_id, iteration, changes in [
        ("revision-too-high", 3, {}),
        ("revision-base-version-zero", 1, {"base_product_version": 0}),
        ("revision-empty-hash", 1, {"trusted_fact_hash": ""}),
        ("revision-short-hash", 1, {"trusted_fact_hash": "b" * 63}),
        ("revision-duplicate", 0, {}),
    ]:
        await _assert_integrity_error(
            session,
            _revision(revision_id, proposal.id, iteration, **changes),
        )


async def test_compliance_reviews_reject_duplicate_and_invalid_typed_values(session) -> None:
    await _add_references(session)
    _, _, _, proposal = await _selection_graph(session, "one")
    revisions = [_revision("revision-0", proposal.id)]
    session.add(revisions[0])
    await session.flush()
    for iteration in (1, 2):
        revisions.append(
            _revision(
                f"revision-{iteration}",
                proposal.id,
                iteration,
                parent_revision_id=revisions[-1].id,
            )
        )
        session.add(revisions[-1])
        await session.flush()
    session.add(_review("review-zero", proposal.id, revisions[0].id, 0))
    await session.flush()

    for review_id, revision_id, iteration, changes in [
        ("review-duplicate-revision", revisions[0].id, 1, {}),
        ("review-duplicate-proposal-iteration", revisions[1].id, 0, {}),
        ("review-too-high", revisions[2].id, 3, {}),
        ("review-invalid-risk", revisions[2].id, 2, {"risk_level": "invalid"}),
        ("review-invalid-quality", revisions[2].id, 2, {"quality_status": "invalid"}),
    ]:
        await _assert_integrity_error(
            session,
            _review(review_id, proposal.id, revision_id, iteration, **changes),
        )


async def test_agent_calls_use_iteration_in_their_unique_key_and_reject_negative_values(session) -> None:
    await _add_references(session)
    run = _analysis_run("analysis-one")
    session.add(run)
    await session.flush()
    calls = [_agent_call("call-zero", run.id, iteration=0), _agent_call("call-one", run.id, iteration=1)]
    session.add_all(calls)
    await session.flush()

    assert [call.iteration for call in calls] == [0, 1]
    await _assert_integrity_error(session, _agent_call("call-duplicate", run.id, iteration=1))
    await _assert_integrity_error(
        session,
        _agent_call("call-negative-iteration", run.id, node_name="negative-iteration", iteration=-1),
    )
