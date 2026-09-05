import pytest
from test_knowledge_api import knowledge_api,_headers


@pytest.mark.parametrize('role,status',[('operator',403),('supervisor',403),('admin',200)])
async def test_admin_only_safe_list(knowledge_api,role,status):
    c=knowledge_api
    for path in ('/admin/users','/admin/stores'):
        response=await c.client.get(path,headers=_headers(c.tokens[role]))
        assert response.status_code==status
        assert not any(s in response.text for s in ('password_hash','Authorization','token'))


@pytest.mark.parametrize('path,body',[('/admin/users/knowledge-api-operator',{}),('/admin/users/knowledge-api-operator',{'role':None}),
    ('/admin/users/knowledge-api-operator',{'password':'never-return-this'}),('/admin/stores/knowledge-store',{'enabled':'false'}),('/admin/stores/knowledge-store',{'name':'rename'})])
async def test_admin_write_schema_closed(knowledge_api,path,body):
    c=knowledge_api;response=await c.client.patch(path,json=body,headers=_headers(c.tokens['admin']))
    assert response.status_code==422 and 'never-return-this' not in response.text


async def test_disabling_user_invalidates_old_token(knowledge_api):
    c=knowledge_api
    response=await c.client.patch('/admin/users/knowledge-api-operator',json={'status':'disabled'},headers=_headers(c.tokens['admin']))
    assert response.status_code==200 and response.json()['status']=='disabled'
    assert (await c.client.get('/auth/me',headers=_headers(c.tokens['operator']))).status_code==401


@pytest.mark.parametrize('role', [None, 'operator', 'supervisor'])
async def test_admin_mutations_require_active_admin(knowledge_api, role):
    c = knowledge_api
    headers = _headers(c.tokens[role]) if role else {}
    for method, path, body in [
        ('PATCH', '/admin/users/knowledge-api-operator', {'role': 'supervisor'}),
        ('PUT', '/admin/users/knowledge-api-operator/store-scopes', {'store_ids': []}),
        ('PATCH', '/admin/stores/knowledge-store', {'enabled': False}),
    ]:
        response = await c.client.request(method, path, json=body, headers=headers)
        assert response.status_code == (403 if role else 401)


async def test_scope_endpoint_replaces_and_rejects_without_changes(knowledge_api):
    c = knowledge_api
    path = '/admin/users/knowledge-api-operator/store-scopes'
    headers = _headers(c.tokens['admin'])
    for store_ids in ([], ['knowledge-store']):
        response = await c.client.put(path, json={'store_ids': store_ids}, headers=headers)
        assert response.status_code == 200 and response.json()['store_ids'] == store_ids
    response = await c.client.put(path, json={'store_ids': ['missing']}, headers=headers)
    assert response.status_code == 404
    response = await c.client.get('/admin/users?role=operator', headers=headers)
    assert response.json()['items'][0]['store_ids'] == ['knowledge-store']
