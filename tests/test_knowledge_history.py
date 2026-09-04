import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from backend.common import AuditEventType
from backend.models import AuditEvent, KnowledgeDocumentVersion
from test_knowledge_api import knowledge_api, _headers, _markdown_upload, _seed_active_document


async def test_history_safe_paging_and_mutations_are_audited_once(knowledge_api):
    c = knowledge_api
    headers = _headers(c.tokens['admin'], **{'Idempotency-Key':'history-create'})
    response = await c.client.post('/knowledge/documents', headers=headers,
        data={'name':'Rules','category':'demo'}, files=_markdown_upload())
    assert response.status_code == 202
    doc = response.json()['data']['document_id']
    await c.client.post('/knowledge/documents', headers=headers,
        data={'name':'Rules','category':'demo'}, files=_markdown_upload())
    response = await c.client.post(f'/knowledge/documents/{doc}/versions', headers=headers,
                                  files=_markdown_upload(b'# Second\n\nSafe.'))
    assert response.status_code == 202
    response = await c.client.get(f'/knowledge/documents/{doc}/versions?page_size=1',headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body['total'] == 2 and body['items'][0]['version_number'] == 2
    assert set(body['items'][0]) == {'id','version_number','status','parser_version','chunker_version','embedding_version','error_code','created_at'}
    assert not any(x in response.text for x in ['storage_path','original_filename','sha256','lease_owner','canonical_text'])
    assert (await c.client.post(f'/knowledge/documents/{doc}/disable',headers=headers)).status_code == 200
    events = list(await c.session.scalars(select(AuditEvent).order_by(AuditEvent.created_at)))
    assert [e.event_type for e in events] == [AuditEventType.KNOWLEDGE_DOCUMENT_CREATED,
        AuditEventType.KNOWLEDGE_VERSION_CREATED, AuditEventType.KNOWLEDGE_DOCUMENT_DISABLED]
    assert all(e.actor_id == c.users['admin'].id for e in events)


@pytest.mark.parametrize('role,status', [('operator',403),('supervisor',403),('admin',404)])
async def test_history_role_and_unknown(knowledge_api,role,status):
    response = await knowledge_api.client.get('/knowledge/documents/missing/versions',
        headers=_headers(knowledge_api.tokens[role]))
    assert response.status_code == status


async def test_version_write_failure_rolls_back_audit_and_version(knowledge_api,monkeypatch):
    c = knowledge_api
    doc, version, _ = await _seed_active_document(c)
    async def failed_commit():
        await c.session.flush()
        raise SQLAlchemyError('private failure')
    monkeypatch.setattr(c.session,'commit',failed_commit)
    response = await c.client.post(f'/knowledge/documents/{doc.id}/versions',
        headers=_headers(c.tokens['admin']),files=_markdown_upload())
    assert response.status_code == 503
    assert 'private failure' not in response.text
    assert await c.session.scalar(select(func.count(AuditEvent.id))) == 0
    assert await c.session.scalar(select(func.count(KnowledgeDocumentVersion.id))) == 1
