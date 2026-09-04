from datetime import UTC, datetime
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from backend.common import EvaluationAgentType, EvaluationRunStatus, UserRole
from backend.models import User, Store, EvaluationCase, EvaluationRun, EvaluationResult, AuditEvent
from backend.agent_evaluations import EvaluationDomainError, EvaluationResultInput, validate_evaluation_metrics, persist_evaluation_run


@pytest.mark.parametrize('metrics', [
    {'candidate_set_valid':True,'rank_order_valid':True,'latency_ms':float('nan')},
    {'candidate_set_valid':1,'rank_order_valid':True,'latency_ms':1},
    {'candidate_set_valid':True,'rank_order_valid':True,'latency_ms':-1},
    {'candidate_set_valid':True,'rank_order_valid':True,'latency_ms':1,'raw':'secret'},
    {'latency_ms':1},
])
def test_closed_metrics(metrics):
    with pytest.raises(EvaluationDomainError,match='EVALUATION_INPUT_INVALID'):
        validate_evaluation_metrics(EvaluationAgentType.ANALYSIS,metrics)


async def seed(session):
    session.add_all([User(id='admin',username='admin',password_hash='test',role=UserRole.ADMIN),
                     Store(id='store',code='store',name='Store')])
    await session.flush()
    session.add_all([EvaluationCase(id='case',agent_type=EvaluationAgentType.ANALYSIS,case_key='case',case_version=1,fixture={'ids':['p']},expected={'valid':True}),
                    EvaluationCase(id='disabled',agent_type=EvaluationAgentType.ANALYSIS,case_key='disabled',case_version=1,fixture={'ids':['p']},expected={'valid':True},enabled=False)])
    await session.commit()


def arguments():
    return dict(actor_id='admin',agent_type=EvaluationAgentType.ANALYSIS,store_id='store',
                suite_version='v1',runner_version='v1',dataset_version='v1',results=[EvaluationResultInput(
                    case_id='case',outcome='passed',metrics={'candidate_set_valid':True,'rank_order_valid':True,'latency_ms':1.0},result_code='EVALUATION_PASSED',latency_ms=1.0)])


async def test_complete_runs_are_new_immutable_and_audited(session):
    await seed(session)
    first = await persist_evaluation_run(session,**arguments())
    second = await persist_evaluation_run(session,**arguments())
    assert first.id != second.id
    assert first.status is EvaluationRunStatus.COMPLETED
    assert first.summary['total_cases'] == first.summary['passed_cases'] == 1
    assert await session.scalar(select(func.count(EvaluationResult.id))) == 2
    assert await session.scalar(select(func.count(AuditEvent.id))) == 2


async def test_flush_failure_leaves_failed_header_without_partial_results(session,monkeypatch):
    await seed(session)
    original = session.flush
    failed_once = False
    async def fail_first(*args,**kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise IntegrityError('INSERT',{},Exception('private error'))
        await original(*args,**kwargs)
    monkeypatch.setattr(session,'flush',fail_first)
    run = await persist_evaluation_run(session,**arguments())
    assert run.status is EvaluationRunStatus.FAILED
    assert run.error_code == 'EVALUATION_RUNNER_FAILED'
    assert await session.scalar(select(func.count(EvaluationResult.id))) == 0
    assert await session.scalar(select(func.count(EvaluationRun.id))) == 1
    assert await session.scalar(select(func.count(AuditEvent.id))) == 1


@pytest.mark.parametrize('change',['missing','duplicate','disabled','mismatch','bad_metric'])
async def test_invalid_group_cannot_persist_partial_success(session,change):
    await seed(session)
    args = arguments()
    if change == 'missing': args['results'] = []
    if change == 'duplicate': args['results'] *= 2
    if change == 'disabled': args['results'][0] = EvaluationResultInput('disabled','passed',args['results'][0].metrics,'EVALUATION_PASSED',1)
    if change == 'mismatch': args['agent_type'] = EvaluationAgentType.OPTIMIZATION
    if change == 'bad_metric': args['results'][0].metrics['latency_ms'] = float('inf')
    run = await persist_evaluation_run(session,**args)
    assert run.status is EvaluationRunStatus.FAILED
    assert await session.scalar(select(func.count(EvaluationResult.id))) == 0
