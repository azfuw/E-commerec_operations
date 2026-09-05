from datetime import datetime,UTC
import pytest
from backend.models import EvaluationRun,Store
from backend.common import EvaluationAgentType,EvaluationRunStatus
from test_knowledge_api import knowledge_api, _headers


async def seed_runs(c):
    c.session.add(Store(id='other-store',name='Other',code='other-store',enabled=False))
    await c.session.flush()
    now = datetime.now(UTC)
    for id,store in [('visible','knowledge-store'),('other','other-store'),('global',None)]:
        c.session.add(EvaluationRun(id=id,agent_type=EvaluationAgentType.KNOWLEDGE_RETRIEVAL,
            store_id=store,suite_version='v1',runner_version='v1',dataset_version='v1',execution_mode='offline_fixture',
            status=EvaluationRunStatus.COMPLETED,started_at=now,completed_at=now,created_at=now,
            summary={'total_cases':0,'passed_cases':0,'failed_cases':0,'average_latency_ms':0}))
    await c.session.commit()


@pytest.mark.parametrize('role,count',[('operator',None),('supervisor',1),('admin',3)])
async def test_run_list_scope_and_safe_shape(knowledge_api,role,count):
    c = knowledge_api; await seed_runs(c)
    response = await c.client.get('/agent-evaluations/runs',headers=_headers(c.tokens[role]))
    if count is None: assert response.status_code == 403; return
    assert response.status_code == 200 and response.json()['total'] == count
    assert not any(x in response.text for x in ['"fixture"','"expected"','created_by','raw'])


@pytest.mark.parametrize('id',['global','other','missing'])
async def test_run_detail_hides_global_and_cross_scope(knowledge_api,id):
    c=knowledge_api; await seed_runs(c)
    response = await c.client.get(f'/agent-evaluations/runs/{id}',headers=_headers(c.tokens['supervisor']))
    assert response.status_code == 404


@pytest.mark.parametrize('query',['page=0','page_size=101','status=running','agent_type=other','store_id=','raw=1'])
async def test_run_filter_validation(knowledge_api,query):
    c=knowledge_api
    response = await c.client.get('/agent-evaluations/runs?'+query,headers=_headers(c.tokens['admin']))
    assert response.status_code == 422


async def test_read_only_routes_and_unauthorized(knowledge_api):
    c=knowledge_api
    assert (await c.client.get('/agent-evaluations/runs')).status_code == 401
    assert (await c.client.post('/agent-evaluations/runs',headers=_headers(c.tokens['admin']),json={})).status_code == 405


async def test_stable_pagination_and_store_filter(knowledge_api):
    c = knowledge_api; await seed_runs(c)
    headers = _headers(c.tokens['admin'])
    ids = []
    for page in (1, 2, 3):
        response = await c.client.get(f'/agent-evaluations/runs?page={page}&page_size=1', headers=headers)
        assert response.status_code == 200 and response.json()['total'] == 3
        ids.append(response.json()['items'][0]['id'])
    assert ids == ['visible', 'other', 'global']
    response = await c.client.get('/agent-evaluations/runs?store_id=other-store', headers=headers)
    assert [item['id'] for item in response.json()['items']] == ['other']


async def test_detail_projects_results_and_rejects_unsafe_persisted_metrics(knowledge_api):
    from sqlalchemy import select
    from backend.models import EvaluationResult
    from backend.agent_evaluations import seed_fixed_cases, load_fixed_cases, evaluate_fixed_case, persist_evaluation_run
    c = knowledge_api
    await seed_fixed_cases(c.session)
    run = await persist_evaluation_run(c.session, actor_id='knowledge-api-admin',
        agent_type=EvaluationAgentType.ANALYSIS, store_id='knowledge-store',
        suite_version='v1', runner_version='v1', dataset_version='v1',
        results=[evaluate_fixed_case(case) for case in load_fixed_cases() if case['agent_type'] == 'analysis' and case['enabled']])
    path = f'/agent-evaluations/runs/{run.id}'
    headers = _headers(c.tokens['admin'])
    response = await c.client.get(path, headers=headers)
    assert response.status_code == 200 and len(response.json()['results']) == 2
    assert set(response.json()['results'][0]) == {'case_key', 'case_version', 'agent_type', 'outcome', 'metrics', 'result_code', 'latency_ms'}
    assert not any(key in response.text for key in ('"fixture"', '"expected"', '"created_by"'))
    result = await c.session.scalar(select(EvaluationResult).where(EvaluationResult.evaluation_run_id == run.id))
    result.metrics = {**result.metrics, 'raw_output': 'private-metric-sentinel'}
    await c.session.commit()
    response = await c.client.get(path, headers=headers)
    assert response.status_code == 503 and 'private-metric-sentinel' not in response.text
