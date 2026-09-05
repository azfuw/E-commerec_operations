from datetime import UTC, datetime
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from backend.common import EvaluationAgentType, EvaluationRunStatus, UserRole
from backend.models import User, Store, EvaluationCase, EvaluationRun, EvaluationResult, AuditEvent
from backend.agent_evaluations import EvaluationDomainError, EvaluationResultInput, validate_evaluation_metrics, persist_evaluation_run
from backend.agent_evaluations import evaluate_fixed_case, load_fixed_cases


@pytest.mark.parametrize('response_ids,ranks,valid', [
    (['p2', 'p1'], [2, 1], True),
    (['p2', 'p1'], [1, 2], False),
    (['p1', 'p2'], [1, 2], True),
    (['p1', 'p2'], [2, 1], False),
    (['p1', 'p2'], [True, 2], False),
    (['p1', 'p2'], [1.0, 2.0], False),
    (['p1'], [1, 2], False),
])
def test_analysis_rank_metric_checks_candidate_identity(response_ids, ranks, valid):
    case = next(case for case in load_fixed_cases() if case['id'] == 'p9-analysis-exact-v1')
    case['fixture'].update(response_ids=response_ids, ranks=ranks)
    result = evaluate_fixed_case(case)
    assert result.metrics['rank_order_valid'] is valid
    assert result.outcome == ('passed' if valid else 'failed')


@pytest.mark.parametrize('semantic_patch', [
    {'risk_level': 'high'},
    {'degraded': True},
    {'passed': False},
    {'required_changes': [{'source_track': 'semantic', 'source_violation_code': 'EXAGGERATION',
        'field': 'title', 'instruction': '删除夸大描述', 'citation_chunk_ids': ['rule-chunk-1']}]},
])
def test_semantic_contract_failure_does_not_falsely_fail_valid_citations(semantic_patch):
    case = next(case for case in load_fixed_cases() if case['id'] == 'p9-compliance-valid-v1')
    case['fixture']['semantic'].update(semantic_patch)
    result = evaluate_fixed_case(case)
    assert result.metrics['deterministic_valid'] is True
    assert result.metrics['semantic_schema_valid'] is False
    assert result.metrics['citation_valid'] is True
    assert result.outcome == 'failed'


@pytest.mark.parametrize('location', ['citations', 'violations', 'required_changes'])
def test_compliance_unknown_reference_fails_citation_and_semantic_contract(location):
    case = next(case for case in load_fixed_cases() if case['id'] == 'p9-compliance-valid-v1')
    semantic = case['fixture']['semantic']
    semantic.update(passed=False, risk_level='high',
        violations=[{'code':'EXAGGERATION', 'field':'title', 'message_zh':'夸大描述', 'citation_chunk_ids':['rule-chunk-1']}],
        required_changes=[{'source_track':'semantic', 'source_violation_code':'EXAGGERATION', 'field':'title',
            'instruction':'删除夸大描述', 'citation_chunk_ids':['rule-chunk-1']}])
    if location == 'citations':
        semantic[location] = [{'chunk_id':'unknown'}]
    else:
        semantic[location][0]['citation_chunk_ids'] = ['unknown']
    result = evaluate_fixed_case(case)
    assert result.metrics['semantic_schema_valid'] is False
    assert result.metrics['citation_valid'] is False
    assert result.outcome == 'failed'


@pytest.mark.parametrize('agent_type,case_id,metric', [
    (EvaluationAgentType.ANALYSIS, 'p9-analysis-exact-v1', 'rank_order_valid'),
    (EvaluationAgentType.COMPLIANCE, 'p9-compliance-valid-v1', 'semantic_schema_valid'),
])
async def test_semantic_regressions_are_recorded_as_failed_cases(session, agent_type, case_id, metric):
    from backend.agent_evaluations import seed_fixed_cases
    session.add_all([User(id='admin', username='admin', password_hash='test', role=UserRole.ADMIN),
                     Store(id='store', code='store', name='Store')])
    await session.commit()
    await seed_fixed_cases(session)
    cases = [case for case in load_fixed_cases() if case['agent_type'] == agent_type.value and case['enabled']]
    changed = next(case for case in cases if case['id'] == case_id)
    if agent_type == EvaluationAgentType.ANALYSIS:
        changed['fixture']['ranks'] = [1, 2]
    else:
        changed['fixture']['semantic']['risk_level'] = 'high'
    run = await persist_evaluation_run(session, actor_id='admin', agent_type=agent_type, store_id='store',
        suite_version='test', runner_version='test', dataset_version='test',
        results=[evaluate_fixed_case(case) for case in cases])
    assert run.summary['failed_cases'] == run.summary['passed_cases'] == 1
    assert run.summary[metric] == (0.0 if agent_type == EvaluationAgentType.ANALYSIS else 0.5)
    result = await session.scalar(select(EvaluationResult).where(
        EvaluationResult.evaluation_run_id == run.id, EvaluationResult.evaluation_case_id == case_id))
    assert result.outcome == 'failed' and result.metrics[metric] is False
    if agent_type == EvaluationAgentType.COMPLIANCE:
        assert result.metrics['citation_valid'] is True and run.summary['citation_valid'] == 1.0


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
