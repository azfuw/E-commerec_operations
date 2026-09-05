import pytest
from sqlalchemy import func,select
from sqlalchemy.exc import SQLAlchemyError
from backend.admin_management import AdminDomainError,update_admin_user,replace_user_store_scopes,update_admin_store
from backend.schemas import AdminUserPatch,AdminStorePatch
from backend.models import User,Store,UserStoreScope,AuditEvent
from backend.common import UserRole,UserStatus


async def seed(session):
    session.add_all([User(id='a',username='a',password_hash='test',role=UserRole.ADMIN),
        User(id='b',username='b',password_hash='test',role=UserRole.ADMIN),
        User(id='operator',username='operator',password_hash='test',role=UserRole.OPERATOR),
        Store(id='s1',name='Store 1',code='s1'),Store(id='s2',name='Store 2',code='s2'),Store(id='disabled',name='Disabled',code='disabled',enabled=False)])
    await session.flush();session.add(UserStoreScope(user_id='operator',store_id='s1'));await session.commit()


@pytest.mark.parametrize('patch',[{'status':'disabled'},{'role':'supervisor'}])
async def test_cannot_disable_or_demote_self(session,patch):
    await seed(session)
    with pytest.raises(AdminDomainError) as error:await update_admin_user(session,actor_id='a',user_id='a',patch=AdminUserPatch(**patch))
    assert error.value.code=='ADMIN_GUARD_VIOLATION'
    assert await session.scalar(select(func.count(AuditEvent.id)))==0


async def test_updates_and_exact_scope_replacement_each_audit_once(session):
    await seed(session)
    await update_admin_user(session,actor_id='a',user_id='operator',patch=AdminUserPatch(role=UserRole.SUPERVISOR))
    await replace_user_store_scopes(session,actor_id='a',user_id='operator',store_ids=['s2'])
    assert list(await session.scalars(select(UserStoreScope.store_id).where(UserStoreScope.user_id=='operator')))==['s2']
    await replace_user_store_scopes(session,actor_id='a',user_id='operator',store_ids=[])
    await update_admin_store(session,actor_id='a',store_id='s2',patch=AdminStorePatch(enabled=False))
    assert await session.scalar(select(func.count(AuditEvent.id)))==4
    assert not (await session.get(Store,'s2')).enabled


@pytest.mark.parametrize('stores',[['s1','s1'],['unknown'],['disabled']])
async def test_invalid_replacement_keeps_old_scope_and_no_audit(session,stores):
    await seed(session)
    with pytest.raises(AdminDomainError):await replace_user_store_scopes(session,actor_id='a',user_id='operator',store_ids=stores)
    assert list(await session.scalars(select(UserStoreScope.store_id).where(UserStoreScope.user_id=='operator')))==['s1']
    assert await session.scalar(select(func.count(AuditEvent.id)))==0


async def test_failed_scope_commit_rolls_back_entire_replacement(session,monkeypatch):
    await seed(session)
    async def fail():
        await session.flush();raise SQLAlchemyError('private database failure')
    monkeypatch.setattr(session,'commit',fail)
    with pytest.raises(AdminDomainError) as error:await replace_user_store_scopes(session,actor_id='a',user_id='operator',store_ids=['s2'])
    assert error.value.code=='ADMIN_CONFLICT'
    assert list(await session.scalars(select(UserStoreScope.store_id).where(UserStoreScope.user_id=='operator')))==['s1']
    assert await session.scalar(select(func.count(AuditEvent.id)))==0
