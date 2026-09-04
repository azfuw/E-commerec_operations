"""Fixed offline evaluation facts and safe read projections."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, UTC
import json
import math
from pathlib import Path
from typing import Literal, TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from backend.common import EvaluationAgentType, EvaluationRunStatus, UserRole, UserStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from backend.models import EvaluationRun


@dataclass
class EvaluationDomainError(Exception):
    code: str
    status_code: int = 422


@dataclass(frozen=True)
class EvaluationResultInput:
    case_id: str
    outcome: Literal['passed', 'failed']
    metrics: dict[str, object]
    result_code: str
    latency_ms: float


_METRIC_KEYS = {
    EvaluationAgentType.ANALYSIS: frozenset({'candidate_set_valid','rank_order_valid','latency_ms'}),
    EvaluationAgentType.OPTIMIZATION: frozenset({'output_schema_valid','trusted_fact_valid','citation_valid','latency_ms'}),
    EvaluationAgentType.COMPLIANCE: frozenset({'deterministic_valid','semantic_schema_valid','citation_valid','latency_ms'}),
    EvaluationAgentType.KNOWLEDGE_RETRIEVAL: frozenset({'recall_at_10','mrr','citation_document_version_accuracy','latency_ms'}),
}
RESULT_CODES = frozenset({'EVALUATION_PASSED','EVALUATION_EXPECTATION_MISMATCH',
    'EVALUATION_INPUT_INVALID','EVALUATION_VALIDATION_FAILED','EVALUATION_RUNNER_FAILED'})
FIXTURE_PATH = Path(__file__).resolve().parents[1] / 'data/evaluation/phase9-agent-cases.json'
SUITE_VERSION = 'phase9-v1'


def _invalid() -> EvaluationDomainError:
    return EvaluationDomainError('EVALUATION_INPUT_INVALID')


def _number(value: object, *, ratio: bool = False) -> bool:
    return (type(value) in (int,float) and math.isfinite(value) and value >= 0
            and (not ratio or value <= 1))


def validate_evaluation_metrics(agent_type: EvaluationAgentType, metrics: dict[str, object]) -> dict[str, bool | float]:
    if agent_type not in _METRIC_KEYS or not isinstance(metrics,dict) or set(metrics) != _METRIC_KEYS[agent_type]:
        raise _invalid()
    for key,value in metrics.items():
        if key == 'latency_ms':
            valid = _number(value)
        elif agent_type == EvaluationAgentType.KNOWLEDGE_RETRIEVAL:
            valid = _number(value,ratio=True)
        else:
            valid = type(value) is bool
        if not valid: raise _invalid()
    return dict(metrics)


def validate_evaluation_summary(agent_type: EvaluationAgentType, summary: dict[str, object]) -> dict[str, object]:
    counts = {'total_cases','passed_cases','failed_cases'}
    required = counts | {'average_latency_ms'}
    allowed = required | (_METRIC_KEYS[agent_type] - {'latency_ms'})
    if not isinstance(summary,dict) or not required <= set(summary) <= allowed:
        raise _invalid()
    if any(type(summary[k]) is not int or summary[k] < 0 for k in counts): raise _invalid()
    if summary['passed_cases'] + summary['failed_cases'] != summary['total_cases']: raise _invalid()
    if not _number(summary['average_latency_ms']): raise _invalid()
    for key in set(summary)-required:
        if not _number(summary[key],ratio=True): raise _invalid()
    return dict(summary)


async def _context(session: AsyncSession, actor_id: str, agent_type: EvaluationAgentType, store_id: str | None):
    from backend.models import User, Store
    if agent_type not in _METRIC_KEYS or not isinstance(actor_id,str) or not 1 <= len(actor_id) <= 36: raise _invalid()
    actor = await session.scalar(select(User).where(User.id == actor_id).execution_options(populate_existing=True))
    if actor is None or actor.status != UserStatus.ACTIVE or actor.role != UserRole.ADMIN:
        raise EvaluationDomainError('EVALUATION_FORBIDDEN',403)
    if store_id is None:
        if agent_type != EvaluationAgentType.KNOWLEDGE_RETRIEVAL: raise _invalid()
    elif not isinstance(store_id,str) or not 1 <= len(store_id) <= 36:
        raise _invalid()
    elif await session.scalar(select(Store.id).where(Store.id == store_id,Store.enabled.is_(True))) is None:
        raise _invalid()
    return actor


def _audit(session, run, actor):
    from backend.audit_events import add_audit_event
    from backend.common import AuditEventType, AuditOutcome
    add_audit_event(session,event_type=AuditEventType.EVALUATION_RUN_PERSISTED,
        outcome=AuditOutcome.SUCCESS if run.status == EvaluationRunStatus.COMPLETED else AuditOutcome.FAILED,
        store_id=run.store_id,actor_id=actor.id,actor_role=actor.role,
        resource_type='evaluation_run',resource_id=run.id,
        details={'evaluation_agent_type':run.agent_type.value,'evaluation_status':run.status.value,
                 'case_count':run.summary['total_cases']})


async def persist_failed_evaluation_run(session: AsyncSession, *, actor_id: str,
    agent_type: EvaluationAgentType, store_id: str | None, error_code: str,
    suite_version: str = SUITE_VERSION, runner_version: str = SUITE_VERSION,
    dataset_version: str = SUITE_VERSION) -> EvaluationRun:
    from backend.models import EvaluationRun
    from uuid import uuid4
    try:
        actor = await _context(session,actor_id,agent_type,store_id)
        if error_code not in RESULT_CODES - {'EVALUATION_PASSED'}: raise _invalid()
        now = datetime.now(UTC)
        run = EvaluationRun(id=str(uuid4()),agent_type=agent_type,store_id=store_id,
            suite_version=suite_version,runner_version=runner_version,dataset_version=dataset_version,
            execution_mode='offline_fixture',status=EvaluationRunStatus.FAILED,started_at=now,completed_at=now,
            summary={'total_cases':0,'passed_cases':0,'failed_cases':0,'average_latency_ms':0.0},
            error_code=error_code,created_by=actor_id)
        session.add(run); _audit(session,run,actor)
        await session.commit()
        return run
    except (SQLAlchemyError, EvaluationDomainError):
        await session.rollback()
        raise EvaluationDomainError('EVALUATION_RUNNER_FAILED',503) from None


async def persist_evaluation_run(session: AsyncSession, *, actor_id: str, agent_type: EvaluationAgentType,
    store_id: str | None, suite_version: str, runner_version: str, dataset_version: str,
    results: list[EvaluationResultInput]) -> EvaluationRun:
    from backend.models import EvaluationCase, EvaluationRun, EvaluationResult
    from uuid import uuid4
    # Validate identity before opening a success transaction; invalid principals cannot create failure facts.
    try:
        actor = await _context(session,actor_id,agent_type,store_id)
        for version in (suite_version,runner_version,dataset_version):
            if not isinstance(version,str) or not 1 <= len(version) <= 64 or not all(c.isalnum() or c in '-._' for c in version): raise _invalid()
    except (SQLAlchemyError,EvaluationDomainError):
        await session.rollback()
        raise _invalid() from None
    try:
        cases = list(await session.scalars(select(EvaluationCase).where(
            EvaluationCase.agent_type == agent_type,EvaluationCase.enabled.is_(True)).order_by(EvaluationCase.id).with_for_update()))
        ids = [result.case_id for result in results]
        if len(ids) != len(set(ids)) or set(ids) != {case.id for case in cases}: raise _invalid()
        for result in results:
            validate_evaluation_metrics(agent_type,result.metrics)
            if result.outcome not in {'passed','failed'} or result.result_code not in RESULT_CODES: raise _invalid()
            if (result.outcome == 'passed') != (result.result_code == 'EVALUATION_PASSED'): raise _invalid()
            if not _number(result.latency_ms) or result.latency_ms != result.metrics['latency_ms']: raise _invalid()
        total = len(results)
        passed = sum(r.outcome == 'passed' for r in results)
        summary = {'total_cases':total,'passed_cases':passed,'failed_cases':total-passed,
                   'average_latency_ms':sum(r.latency_ms for r in results)/total if total else 0.0}
        for key in _METRIC_KEYS[agent_type]-{'latency_ms'}:
            summary[key] = sum(float(r.metrics[key]) for r in results)/total if total else 0.0
        validate_evaluation_summary(agent_type,summary)
        now = datetime.now(UTC)
        run = EvaluationRun(id=str(uuid4()),agent_type=agent_type,store_id=store_id,
            suite_version=suite_version,runner_version=runner_version,dataset_version=dataset_version,
            execution_mode='offline_fixture',status=EvaluationRunStatus.COMPLETED,started_at=now,completed_at=now,
            summary=summary,error_code=None,created_by=actor_id)
        session.add(run)
        await session.flush()
        session.add_all([EvaluationResult(evaluation_run_id=run.id,evaluation_case_id=r.case_id,
            agent_type=agent_type,outcome=r.outcome,metrics=dict(r.metrics),result_code=r.result_code,
            latency_ms=r.latency_ms) for r in results])
        _audit(session,run,actor)
        await session.commit()
        return run
    except (SQLAlchemyError,EvaluationDomainError,TypeError,ValueError,AttributeError):
        await session.rollback()
        return await persist_failed_evaluation_run(session,actor_id=actor_id,agent_type=agent_type,
            store_id=store_id,error_code='EVALUATION_RUNNER_FAILED',suite_version=suite_version,
            runner_version=runner_version,dataset_version=dataset_version)


def load_fixed_cases() -> list[dict[str, object]]:
    return json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))


async def seed_fixed_cases(session: AsyncSession) -> None:
    from backend.models import EvaluationCase
    for item in load_fixed_cases():
        existing = await session.get(EvaluationCase,item['id'])
        if existing is not None:
            if any(getattr(existing,k) != item[k] for k in ('agent_type','case_key','case_version','fixture','expected')):
                raise _invalid()
            continue
        session.add(EvaluationCase(**item))
    await session.commit()
