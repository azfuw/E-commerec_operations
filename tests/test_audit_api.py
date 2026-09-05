import pytest
from datetime import UTC,datetime
from backend.models import AuditEvent
from backend.common import AuditEventType,AuditOutcome
from test_knowledge_api import knowledge_api,_headers


@pytest.mark.parametrize('query',['event_type=unknown','actor_id=','outcome=other','page=0','page_size=101',
    'created_from=2026-01-01T00:00:00Z&created_to=2026-02-02T00:00:00Z',
    'created_from=2026-01-01T00:00:00&created_to=2026-01-02T00:00:00'])
async def test_audit_filter_errors_are_bounded(knowledge_api,query):
    c=knowledge_api
    response=await c.client.get('/audit-events?'+query,headers=_headers(c.tokens['admin']))
    assert response.status_code==422


async def test_audit_global_events_and_filtered_descending_page(knowledge_api):
    c=knowledge_api;now=datetime.now(UTC)
    for id,kind in [('a',AuditEventType.ADMIN_USER_UPDATED),('b',AuditEventType.ADMIN_USER_UPDATED),('c',AuditEventType.ADMIN_STORE_UPDATED)]:
        c.session.add(AuditEvent(id=id,event_type=kind,outcome=AuditOutcome.SUCCESS,store_id=None,details={},created_at=now))
    await c.session.commit()
    response=await c.client.get('/audit-events?event_type=admin_user_updated&page_size=1',headers=_headers(c.tokens['admin']))
    assert response.status_code==200
    assert response.json()['total']==2 and response.json()['items'][0]['id']=='b'
    assert 'request_hash' not in response.text
    supervisor=await c.client.get('/audit-events',headers=_headers(c.tokens['supervisor']))
    assert supervisor.json()['total']==0


async def test_management_validation_never_echoes_input(knowledge_api):
    c=knowledge_api
    for path in ('/audit-events','/agent-evaluations/runs','/agent-calls'):
        response=await c.client.get(path+'?private=never-echo-this',headers=_headers(c.tokens['admin']))
        assert response.status_code==422 and 'never-echo-this' not in response.text
