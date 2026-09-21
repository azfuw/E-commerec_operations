"""Fixed offline evaluation facts and safe read projections."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, UTC
import json
import math
from pathlib import Path
from typing import Literal, TYPE_CHECKING

from sqlalchemy import select, func
from sqlalchemy.exc import SQLAlchemyError
from backend.common import EvaluationAgentType, EvaluationRunStatus, UserDepartment, UserRole, UserStatus

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
RUNNER_VERSION = 'phase9-v2'


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
    suite_version: str = SUITE_VERSION, runner_version: str = RUNNER_VERSION,
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


def evaluate_fixed_case(case: dict[str, object]) -> EvaluationResultInput:
    from time import perf_counter
    from pydantic import ValidationError
    started = perf_counter()
    agent_type = EvaluationAgentType(case['agent_type'])
    fixture = case['fixture']
    metrics = {key: (0.0 if agent_type == EvaluationAgentType.KNOWLEDGE_RETRIEVAL else False)
               for key in _METRIC_KEYS[agent_type] - {'latency_ms'}}
    code = 'EVALUATION_PASSED'
    try:
        if agent_type == EvaluationAgentType.ANALYSIS:
            # Fixed set/rank evidence; no analyst invocation or model explanation is needed.
            expected_ids, actual_ids, ranks = fixture['candidate_ids'], fixture['response_ids'], fixture['ranks']
            metrics['candidate_set_valid'] = len(actual_ids) == len(set(actual_ids)) == len(expected_ids) and set(actual_ids) == set(expected_ids)
            metrics['rank_order_valid'] = (
                metrics['candidate_set_valid']
                and len(ranks) == len(actual_ids)
                and all(type(rank) is int for rank in ranks)
                and sorted(ranks) == list(range(1, len(expected_ids) + 1))
                and [candidate_id for _, candidate_id in sorted(zip(ranks, actual_ids))] == expected_ids
            )
        elif agent_type in (EvaluationAgentType.OPTIMIZATION,EvaluationAgentType.COMPLIANCE):
            from backend.schemas import TrustedOptimizationInput, OptimizationProposalOutput
            from backend.optimization_validation import validate_optimization_output
            trusted = TrustedOptimizationInput.model_validate(fixture['trusted'])
            output = OptimizationProposalOutput.model_validate(fixture['candidate'])
            deterministic = validate_optimization_output(trusted,output)
            citation_codes = {'EVIDENCE_CITATION_INVALID','CITATION_UNKNOWN','CITATION_DUPLICATE','CITATION_EVIDENCE_UNLISTED','ATTRIBUTE_SOURCE_MISSING'}
            metrics['citation_valid'] = not any(v.code in citation_codes for v in deterministic.violations)
            if agent_type == EvaluationAgentType.OPTIMIZATION:
                metrics['output_schema_valid'] = True
                metrics['trusted_fact_valid'] = not any(v.code not in citation_codes for v in deterministic.violations)
            else:
                from backend.compliance_agent import ComplianceAgentResponse, validate_compliance_response, ComplianceAgentSchemaError
                metrics['deterministic_valid'] = deterministic.passed
                candidate_citations_valid = metrics['citation_valid']
                metrics['citation_valid'] = False
                semantic = ComplianceAgentResponse.model_validate(fixture['semantic'])
                allowed_ids = {citation.chunk_id for citation in trusted.canonical_rule_citations
                               if citation.active and citation.applicable}
                citation_groups = [[citation.chunk_id for citation in semantic.citations],
                                   *(violation.citation_chunk_ids for violation in semantic.violations),
                                   *(change.citation_chunk_ids for change in semantic.required_changes)]
                metrics['citation_valid'] = candidate_citations_valid and all(
                    len(ids) == len(set(ids)) and set(ids) <= allowed_ids for ids in citation_groups
                )
                try:
                    validate_compliance_response(trusted, semantic)
                    metrics['semantic_schema_valid'] = True
                except ComplianceAgentSchemaError:
                    metrics['semantic_schema_valid'] = False
        else:
            hits = fixture['hits'][:10]
            matches = [hit['document_name'] == fixture['document_name'] and hit['version_number'] == fixture['version_number'] and hit['section'] == fixture['section'] for hit in hits]
            metrics['recall_at_10'] = float(any(matches))
            metrics['mrr'] = 1.0/(matches.index(True)+1) if any(matches) else 0.0
            metrics['citation_document_version_accuracy'] = float(bool(hits) and hits[0]['document_name'] == fixture['document_name'] and hits[0]['version_number'] == fixture['version_number'])
        if metrics != case['expected']: code = 'EVALUATION_EXPECTATION_MISMATCH'
    except (ValidationError,KeyError,TypeError,ValueError):
        code = 'EVALUATION_VALIDATION_FAILED'
    elapsed = (perf_counter()-started)*1000
    metrics['latency_ms'] = elapsed
    validate_evaluation_metrics(agent_type,metrics)
    return EvaluationResultInput(case_id=case['id'],outcome='passed' if code == 'EVALUATION_PASSED' else 'failed',
                                 metrics=metrics,result_code=code,latency_ms=elapsed)


async def _reader(session,actor_id):
    from backend.auth import has_department_access
    from backend.models import User
    actor = await session.scalar(select(User).where(User.id == actor_id).execution_options(populate_existing=True))
    if actor is None or actor.status != UserStatus.ACTIVE:
        raise EvaluationDomainError('EVALUATION_AUTHENTICATION_REQUIRED',401)
    if not has_department_access(actor, UserDepartment.OPERATIONS) or actor.role not in (UserRole.ADMIN,UserRole.SUPERVISOR):
        raise EvaluationDomainError('EVALUATION_FORBIDDEN',403)
    return actor


def _run_view(run):
    from backend.schemas import EvaluationRunView
    validate_evaluation_summary(run.agent_type,run.summary)
    return EvaluationRunView.model_validate(run)


async def list_evaluation_runs(session, *, actor_id, query):
    from backend.auth import store_visibility_predicate
    from backend.models import EvaluationRun,UserStoreScope
    actor = await _reader(session,actor_id)
    statement = select(EvaluationRun)
    statement = statement.where(store_visibility_predicate(actor,EvaluationRun.store_id))
    for name in ('agent_type','status','store_id'):
        value = getattr(query,name)
        if value is not None: statement = statement.where(getattr(EvaluationRun,name) == value)
    total = await session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    runs = list(await session.scalars(statement.order_by(EvaluationRun.created_at.desc(),EvaluationRun.id.desc()).offset((query.page-1)*query.page_size).limit(query.page_size)))
    return [_run_view(run) for run in runs],total


async def get_evaluation_run_for_actor(session, *, actor_id, run_id):
    from backend.auth import store_visibility_predicate
    from backend.models import EvaluationRun,EvaluationResult,EvaluationCase,UserStoreScope
    from backend.schemas import EvaluationRunDetailView,EvaluationResultView
    actor = await _reader(session,actor_id)
    statement = select(EvaluationRun).where(EvaluationRun.id == run_id)
    statement = statement.where(store_visibility_predicate(actor,EvaluationRun.store_id))
    run = await session.scalar(statement)
    if run is None: raise EvaluationDomainError('EVALUATION_RUN_NOT_FOUND',404)
    view = _run_view(run)
    rows = (await session.execute(select(EvaluationResult,EvaluationCase.case_key,EvaluationCase.case_version,EvaluationCase.agent_type)
        .join(EvaluationCase,EvaluationCase.id == EvaluationResult.evaluation_case_id)
        .where(EvaluationResult.evaluation_run_id == run_id).order_by(EvaluationCase.case_key,EvaluationCase.case_version,EvaluationResult.id))).all()
    results = []
    for result,key,version,case_type in rows:
        if result.agent_type != run.agent_type or case_type != run.agent_type: raise _invalid()
        validate_evaluation_metrics(run.agent_type,result.metrics)
        results.append(EvaluationResultView(case_key=key,case_version=version,agent_type=result.agent_type,
            outcome=result.outcome,metrics=result.metrics,result_code=result.result_code,latency_ms=result.latency_ms))
    return EvaluationRunDetailView(**view.model_dump(),results=results)


async def list_safe_agent_calls(session, *, actor_id, query):
    from backend.auth import store_visibility_predicate
    from backend.models import AgentCall,WorkflowRun,Store,UserStoreScope
    from backend.schemas import AgentCallView
    actor = await _reader(session,actor_id)
    names = set(AgentCallView.model_fields)-{'store_id','workflow_type'}
    statement = select(*(getattr(AgentCall,name) for name in sorted(names)),WorkflowRun.store_id,WorkflowRun.workflow_type).join(WorkflowRun,WorkflowRun.id == AgentCall.workflow_run_id).join(Store,Store.id == WorkflowRun.store_id)
    statement = statement.where(store_visibility_predicate(actor,WorkflowRun.store_id))
    for name in ('store_id','workflow_type','node_name','status','error_code'):
        value = getattr(query,name)
        model = WorkflowRun if name in ('store_id','workflow_type') else AgentCall
        if value is not None: statement = statement.where(getattr(model,name) == value)
    total = await session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = (await session.execute(statement.order_by(AgentCall.created_at.desc(),AgentCall.id.desc()).offset((query.page-1)*query.page_size).limit(query.page_size))).mappings().all()
    return [AgentCallView.model_validate(row) for row in rows],total
