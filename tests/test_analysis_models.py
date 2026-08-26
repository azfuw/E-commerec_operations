from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from backend.common import AgentCallType, UserRole, WorkflowQuality, WorkflowStatus
from backend.models import AnalysisCandidate, AgentCall, Product, Store, User, WorkflowRun


async def _add_references(session) -> None:
    store = Store(id="store-1", name="旗舰店", code="flagship")
    user = User(id="user-1", username="operator", password_hash="hash", role=UserRole.OPERATOR)
    products = [
        Product(
            id="product-1",
            store_id=store.id,
            code="FLAGSHIP-001",
            title="商品一",
            category="数码",
        ),
        Product(
            id="product-2",
            store_id=store.id,
            code="FLAGSHIP-002",
            title="商品二",
            category="数码",
        ),
    ]
    session.add_all([store, user])
    await session.flush()
    session.add_all(products)
    await session.flush()


def _workflow_run(run_id: str, **changes: object) -> WorkflowRun:
    values: dict[str, object] = {
        "id": run_id,
        "workflow_type": "analysis",
        "store_id": "store-1",
        "created_by": "user-1",
        "start_date": date(2026, 7, 26),
        "end_date": date(2026, 8, 24),
        "status": WorkflowStatus.ACCEPTED,
        "quality_status": WorkflowQuality.NORMAL,
    }
    values.update(changes)
    return WorkflowRun(**values)


def _candidate(candidate_id: str, run_id: str, product_id: str, **changes: object) -> AnalysisCandidate:
    values: dict[str, object] = {
        "id": candidate_id,
        "workflow_run_id": run_id,
        "product_id": product_id,
        "rank": 1,
        "product_code": "FLAGSHIP-001",
        "anomaly_types": ["low_conversion"],
        "metrics": {},
        "business_impact": Decimal("1.00"),
        "evidence": ["clicks=300"],
        "impact_explanation": "影响说明",
        "reason": "原因",
        "recommended_action": "建议",
        "confidence": Decimal("0.8000"),
    }
    values.update(changes)
    return AnalysisCandidate(**values)


def _agent_call(call_id: str, run_id: str, **changes: object) -> AgentCall:
    values: dict[str, object] = {
        "id": call_id,
        "workflow_run_id": run_id,
        "node_name": "call_analysis_agent",
        "call_type": AgentCallType.PRIMARY,
        "attempt": 1,
        "model": "deepseek-flash",
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


async def _assert_integrity_error(session, row: object) -> None:
    async with session.begin_nested():
        session.add(row)
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_valid_analysis_persistence_rows_have_defaults(session) -> None:
    await _add_references(session)
    run = _workflow_run("run-1")
    session.add(run)
    await session.flush()
    candidate = _candidate("candidate-1", run.id, "product-1")
    call = _agent_call("call-1", run.id)
    session.add_all([candidate, call])
    await session.flush()

    assert run.attempt_count == 0
    assert run.lease_owner is None
    assert run.lease_expires_at is None
    assert run.input == {}
    assert run.output == {}
    assert run.quality == {}
    assert run.created_at.tzinfo is UTC
    assert run.updated_at.tzinfo is UTC
    assert candidate.business_impact == Decimal("1.00")
    assert candidate.confidence == Decimal("0.8000")
    assert call.estimated_cost is None
    assert call.created_at.tzinfo is UTC


async def test_candidates_reject_duplicate_keys_and_invalid_rank_or_confidence(session) -> None:
    await _add_references(session)
    run = _workflow_run("run-1")
    session.add(run)
    await session.flush()
    session.add(_candidate("candidate-1", run.id, "product-1"))
    await session.flush()

    await _assert_integrity_error(
        session, _candidate("candidate-duplicate-product", run.id, "product-1", rank=2)
    )
    await _assert_integrity_error(
        session, _candidate("candidate-duplicate-rank", run.id, "product-2", rank=1)
    )

    for run_id, candidate_id, changes in [
        ("run-rank", "candidate-rank", {"rank": 0}),
        ("run-confidence-low", "candidate-confidence-low", {"confidence": Decimal("-0.0001")}),
        ("run-confidence-high", "candidate-confidence-high", {"confidence": Decimal("1.0001")}),
    ]:
        session.add(_workflow_run(run_id))
        await session.flush()
        await _assert_integrity_error(session, _candidate(candidate_id, run_id, "product-1", **changes))


@pytest.mark.parametrize(
    ("run_id", "changes"),
    [
        ("run-type", {"workflow_type": "other"}),
        ("run-status", {"status": "unknown"}),
        ("run-quality", {"quality_status": "unknown"}),
        ("run-dates", {"start_date": date(2026, 8, 25), "end_date": date(2026, 8, 24)}),
        ("run-attempt-low", {"attempt_count": -1}),
        ("run-attempt-high", {"attempt_count": 4}),
        (
            "run-terminal-lease",
            {
                "status": WorkflowStatus.AWAITING_SELECTION,
                "lease_owner": "worker-a",
                "lease_expires_at": datetime(2026, 8, 25, tzinfo=UTC),
            },
        ),
        ("run-processing-no-expiry", {"status": WorkflowStatus.PROCESSING, "lease_owner": "worker-a"}),
        (
            "run-processing-no-owner",
            {
                "status": WorkflowStatus.PROCESSING,
                "lease_expires_at": datetime(2026, 8, 25, tzinfo=UTC),
            },
        ),
    ],
)
async def test_workflow_runs_reject_invalid_state_or_lease(session, run_id, changes) -> None:
    await _add_references(session)
    await _assert_integrity_error(session, _workflow_run(run_id, **changes))


async def test_processing_run_with_complete_lease_persists(session) -> None:
    await _add_references(session)
    run = _workflow_run(
        "run-processing",
        status=WorkflowStatus.PROCESSING,
        lease_owner="worker-a",
        lease_expires_at=datetime(2026, 8, 25, tzinfo=UTC),
    )
    session.add(run)
    await session.flush()

    assert run.status is WorkflowStatus.PROCESSING
    assert run.lease_owner == "worker-a"
    assert run.lease_expires_at == datetime(2026, 8, 25, tzinfo=UTC)


async def test_agent_calls_reject_duplicate_or_invalid_audit_values(session) -> None:
    await _add_references(session)
    run = _workflow_run("run-1")
    session.add(run)
    await session.flush()
    session.add(_agent_call("call-1", run.id))
    await session.flush()

    await _assert_integrity_error(session, _agent_call("call-duplicate", run.id))
    await _assert_integrity_error(
        session,
        _agent_call(
            "call-type",
            run.id,
            node_name="validate_and_reconcile",
            call_type="invalid",
        ),
    )
    for field in ("attempt", "prompt_tokens", "completion_tokens", "total_tokens", "duration_ms"):
        await _assert_integrity_error(
            session,
            _agent_call(
                f"call-negative-{field}",
                run.id,
                node_name=f"negative-{field}",
                **{field: -1},
            ),
        )


async def test_agent_call_attempt_zero_persists_for_degradation(session) -> None:
    await _add_references(session)
    run = _workflow_run("run-1")
    session.add(run)
    await session.flush()
    call = _agent_call(
        "call-degradation",
        run.id,
        node_name="validate_and_reconcile",
        attempt=0,
    )
    session.add(call)
    await session.flush()

    assert call.attempt == 0
