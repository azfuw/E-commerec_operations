from datetime import UTC,datetime,timedelta
import pytest
from sqlalchemy import select,func
from backend.audit_events import AuditEventFilters,AuditEventDomainError,list_audit_events
from backend.models import AuditEvent,Store,User,UserStoreScope
from backend.common import AuditEventType,AuditOutcome,UserRole


@pytest.mark.parametrize('values',[{'page':0},{'page_size':101},{'store_id':''},{'event_type':'invalid'},
    {'created_from':datetime(2026,1,1,tzinfo=UTC),'created_to':datetime(2026,2,2,tzinfo=UTC)},
    {'created_from':datetime(2026,1,1),'created_to':datetime(2026,1,2)},
    {'created_from':datetime(2026,1,2,tzinfo=UTC),'created_to':datetime(2026,1,1,tzinfo=UTC)}])
def test_invalid_audit_filters(values):
    with pytest.raises(AuditEventDomainError,match='AUDIT_FILTER_INVALID'):AuditEventFilters(**values)


async def seed(session):
    session.add_all([User(id='admin',username='admin',password_hash='test',role=UserRole.ADMIN),
        User(id='supervisor',username='supervisor',password_hash='test',role=UserRole.SUPERVISOR),
        Store(id='disabled',name='Disabled',code='disabled',enabled=False),Store(id='other',name='Other',code='other')])
    await session.flush();session.add(UserStoreScope(user_id='supervisor',store_id='disabled'))
    now=datetime(2026,9,1,tzinfo=UTC)
    for id,store in [('a',None),('b','disabled'),('c','other')]:
        session.add(AuditEvent(id=id,event_type=AuditEventType.ADMIN_STORE_UPDATED,outcome=AuditOutcome.SUCCESS,
            store_id=store,actor_id='admin',actor_role=UserRole.ADMIN,resource_type='store',resource_id='disabled',details={'store_enabled':False},created_at=now))
    await session.commit()


async def test_audit_global_and_disabled_history_stable_paging(session):
    await seed(session)
    events,total=await list_audit_events(session,actor_id='admin',filters=AuditEventFilters(page_size=2))
    assert total==3 and [e.id for e in events]==['c','b']
    events,total=await list_audit_events(session,actor_id='supervisor',filters=AuditEventFilters())
    assert total==1 and [e.id for e in events]==['b']
    events,total=await list_audit_events(session,actor_id='admin',filters=AuditEventFilters(event_type=AuditEventType.ADMIN_STORE_UPDATED,actor_id='admin',outcome=AuditOutcome.SUCCESS,store_id='disabled'))
    assert total==1 and events[0].id=='b'
    assert await session.scalar(select(func.count(AuditEvent.id)))==3


async def test_unsafe_persisted_details_fail_closed(session):
    await seed(session)
    row=await session.get(AuditEvent,'c');row.details={'password':'do-not-expose'};await session.commit()
    with pytest.raises(AuditEventDomainError) as error:
        await list_audit_events(session,actor_id='admin',filters=AuditEventFilters())
    assert error.value.status_code==503
