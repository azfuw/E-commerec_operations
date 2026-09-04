import pytest
from backend.models import Store, UserStoreScope
from test_knowledge_api import knowledge_api, _headers, _seed_active_document


@pytest.mark.parametrize('store_id,expected', [(None,422),('',422),('x'*37,422),('unknown',404),('disabled',404),('other',403)])
async def test_authorization_precedes_loader(knowledge_api,store_id,expected):
    c = knowledge_api
    await _seed_active_document(c)
    c.session.add_all([Store(id='other',code='other',name='Other'), Store(id='disabled',code='disabled',name='Disabled',enabled=False)])
    await c.session.commit()
    body = {'query':'rules'}
    if store_id is not None: body['store_id'] = store_id
    response = await c.client.post('/knowledge/search',json=body,headers=_headers(c.tokens['operator']))
    assert response.status_code == expected
    assert c.loader.calls == 0


@pytest.mark.parametrize('role',['operator','supervisor','admin'])
async def test_authorized_search_preserves_retrieval(knowledge_api,role):
    c = knowledge_api
    await _seed_active_document(c)
    c.session.add(Store(id='search-store',code='search-store',name='Search'))
    await c.session.flush()
    if role != 'admin':
        c.session.add(UserStoreScope(user_id=c.users[role].id,store_id='search-store'))
    await c.session.commit()
    response = await c.client.post('/knowledge/search',json={'store_id':'search-store','query':'rules'},headers=_headers(c.tokens[role]))
    assert response.status_code == 200
    assert response.json()['data']['citations']
    assert c.loader.calls == 1
