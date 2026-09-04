import pytest

from backend.audit_events import add_audit_event
from backend.common import (
    AuditEventType,
    AuditOutcome,
    EvaluationAgentType,
    EvaluationRunStatus,
)
from backend.models import AuditEvent, EvaluationCase, EvaluationResult, EvaluationRun


def _constraint_names(model: type[object]) -> set[str | None]:
    return {constraint.name for constraint in model.__table__.constraints}  # type: ignore[attr-defined]


def test_evaluation_models_declare_immutable_identity_and_query_indexes() -> None:
    assert {item.value for item in EvaluationAgentType} == {
        "analysis",
        "optimization",
        "compliance",
        "knowledge_retrieval",
    }
    assert {item.value for item in EvaluationRunStatus} == {"completed", "failed"}
    assert "uq_evaluation_cases_agent_key_version" in _constraint_names(EvaluationCase)
    assert "uq_evaluation_results_run_case_agent" in _constraint_names(EvaluationResult)
    assert {
        "ix_evaluation_runs_agent_created",
        "ix_evaluation_runs_store_created",
        "ix_evaluation_runs_status_created",
    } <= {index.name for index in EvaluationRun.__table__.indexes}


def test_audit_event_accepts_only_complete_resource_pairs() -> None:
    assert AuditEvent.__table__.c.store_id.nullable is True
    assert "ck_audit_events_resource_pair" in _constraint_names(AuditEvent)
    assert {
        AuditEventType.KNOWLEDGE_DOCUMENT_CREATED,
        AuditEventType.KNOWLEDGE_VERSION_CREATED,
        AuditEventType.KNOWLEDGE_DOCUMENT_DISABLED,
        AuditEventType.EVALUATION_RUN_PERSISTED,
        AuditEventType.ADMIN_USER_UPDATED,
        AuditEventType.ADMIN_USER_SCOPES_REPLACED,
        AuditEventType.ADMIN_STORE_UPDATED,
    } <= set(AuditEventType)


async def test_add_audit_event_accepts_global_phase_nine_fact_and_rejects_unsafe_detail(
    session,
) -> None:
    created = add_audit_event(
        session,
        event_type=AuditEventType.EVALUATION_RUN_PERSISTED,
        outcome=AuditOutcome.SUCCESS,
        store_id=None,
        resource_type="evaluation_run",
        resource_id="evaluation-run-1",
        details={
            "evaluation_agent_type": "analysis",
            "evaluation_status": "completed",
            "case_count": 1,
        },
    )
    assert (created.store_id, created.resource_type, created.resource_id) == (
        None,
        "evaluation_run",
        "evaluation-run-1",
    )
    with pytest.raises(ValueError, match="unsafe audit details"):
        add_audit_event(
            session,
            event_type=AuditEventType.EVALUATION_RUN_PERSISTED,
            outcome=AuditOutcome.SUCCESS,
            store_id=None,
            resource_type="evaluation_run",
            resource_id="evaluation-run-2",
            details={"provider_response": "never persist"},
        )


def test_evaluation_run_status_is_terminal_contract() -> None:
    assert "ck_evaluation_runs_status" in _constraint_names(EvaluationRun)
    assert "ck_evaluation_runs_completed_at" in _constraint_names(EvaluationRun)


@pytest.mark.parametrize("details", [{"scope_count": 0}, {"case_count": 0}])
async def test_zero_counts_are_valid_audit_facts(session, details) -> None:
    event = add_audit_event(session, event_type=AuditEventType.ADMIN_USER_SCOPES_REPLACED,
                            outcome=AuditOutcome.SUCCESS, store_id=None, details=details)
    assert event.details == details


@pytest.mark.parametrize("details", [{"to_role": []}, {"scope_count": -1},
                                      {"case_count": True}, {"to_role": "owner"}])
async def test_invalid_audit_detail_rejected_before_add(session, details) -> None:
    with pytest.raises(ValueError, match="unsafe audit details"):
        add_audit_event(session, event_type=AuditEventType.ADMIN_USER_UPDATED,
                        outcome=AuditOutcome.SUCCESS, store_id=None, details=details)
    assert not session.new


@pytest.mark.parametrize("value", [None, {}, [], "raw"])
async def test_evaluation_case_requires_nonempty_json_object(session, value) -> None:
    from sqlalchemy.exc import IntegrityError
    session.add(EvaluationCase(id='case', agent_type=EvaluationAgentType.ANALYSIS,
        case_key='case', case_version=1, fixture=value, expected={'valid': True}))
    with pytest.raises(IntegrityError):
        await session.flush()


def test_result_has_closed_code_nonempty_metrics_and_finite_latency() -> None:
    constraints = _constraint_names(EvaluationResult)
    assert {'ck_evaluation_results_result_code', 'ck_evaluation_results_metrics_nonempty'} <= constraints
