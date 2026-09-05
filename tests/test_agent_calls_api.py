import pytest
from backend.models import WorkflowRun,AgentCall,Store
from backend.common import WorkflowType,WorkflowStatus,AgentCallType
from test_knowledge_api import knowledge_api,_headers


@pytest.mark.parametrize('role,count',[('operator',None),('supervisor',1),('admin',2)])
async def test_safe_calls_follow_workflow_scope(knowledge_api,role,count):
    c=knowledge_api
    c.session.add(Store(id='other',name='Other',code='other'))
    await c.session.flush()
    for id,store in [('visible','knowledge-store'),('hidden','other')]:
        c.session.add(WorkflowRun(id=id,store_id=store,created_by=c.users['admin'].id,workflow_type=WorkflowType.OPTIMIZATION,status=WorkflowStatus.COMPLETED,quality_status='normal'))
    await c.session.flush()
    for id in ['visible','hidden']:
        c.session.add(AgentCall(id=id,workflow_run_id=id,node_name='call_product_optimization_agent',call_type=AgentCallType.PRIMARY,attempt=1,
            model='offline-model',prompt_version='v1',status='succeeded',input_hash='hidden-input-hash',prompt_tokens=1,completion_tokens=2,total_tokens=3,duration_ms=4))
    await c.session.commit()
    response = await c.client.get('/agent-calls',headers=_headers(c.tokens[role]))
    if count is None: assert response.status_code == 403; return
    assert response.status_code == 200 and response.json()['total'] == count
    assert response.json()['items'][0]['total_tokens'] == 3
    assert not any(x in response.text for x in ['input_hash','hidden-input-hash','workflow_input','lease','checkpoint'])
