"""Locked mutations of existing users, scopes and stores."""
from dataclasses import dataclass
from sqlalchemy import and_,or_,select,func,delete
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from backend.common import UserRole,UserStatus,AuditEventType,AuditOutcome
from backend.models import User,Store,UserStoreScope
from backend.audit_events import add_audit_event
from backend.schemas import AdminUserPatch,AdminStorePatch


@dataclass
class AdminDomainError(Exception):
    code: str
    status_code: int


async def lock_admin_user_set(session:AsyncSession,actor_id:str,target_id:str)->tuple[User,User,list[User]]:
    rows=list(await session.scalars(select(User).where(or_(User.id.in_((actor_id,target_id)),
        and_(User.role==UserRole.ADMIN,User.status==UserStatus.ACTIVE))).order_by(User.id)
        .execution_options(populate_existing=True).with_for_update()))
    actor=next((row for row in rows if row.id==actor_id),None)
    target=next((row for row in rows if row.id==target_id),None)
    admins=[row for row in rows if row.role is UserRole.ADMIN and row.status is UserStatus.ACTIVE]
    if actor is None or actor.role is not UserRole.ADMIN or actor.status is not UserStatus.ACTIVE:
        raise AdminDomainError('ADMIN_GUARD_VIOLATION',409)
    if target is None:raise AdminDomainError('ADMIN_USER_NOT_FOUND',404)
    return actor,target,admins


async def _reader(session,actor_id):
    actor=await session.scalar(select(User).where(User.id==actor_id).execution_options(populate_existing=True))
    if actor is None or actor.status is not UserStatus.ACTIVE:raise AdminDomainError('ADMIN_AUTHENTICATION_REQUIRED',401)
    if actor.role is not UserRole.ADMIN:raise AdminDomainError('ADMIN_FORBIDDEN',403)
    return actor


def _audit(session,actor,event_type,resource_type,resource_id,details,store_id=None):
    add_audit_event(session,event_type=event_type,outcome=AuditOutcome.SUCCESS,store_id=store_id,
        actor_id=actor.id,actor_role=actor.role,resource_type=resource_type,resource_id=resource_id,details=details)


async def list_admin_users(session:AsyncSession,*,actor_id:str,page:int,page_size:int,role:UserRole|None=None,
    status:UserStatus|None=None,store_id:str|None=None)->tuple[list[User],int]:
    await _reader(session,actor_id)
    statement=select(User)
    if role is not None:statement=statement.where(User.role==role)
    if status is not None:statement=statement.where(User.status==status)
    if store_id is not None:statement=statement.where(User.id.in_(select(UserStoreScope.user_id).where(UserStoreScope.store_id==store_id)))
    total=await session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    return list(await session.scalars(statement.order_by(User.id).offset((page-1)*page_size).limit(page_size))),total


async def list_admin_stores(session:AsyncSession,*,actor_id:str,page:int,page_size:int,enabled:bool|None=None):
    await _reader(session,actor_id)
    statement=select(Store)
    if enabled is not None:statement=statement.where(Store.enabled.is_(enabled))
    total=await session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    return list(await session.scalars(statement.order_by(Store.id).offset((page-1)*page_size).limit(page_size))),total


async def admin_user_views(session,users):
    from backend.schemas import AdminUserView
    scopes={user.id:[] for user in users}
    for user_id,store_id in (await session.execute(select(UserStoreScope.user_id,UserStoreScope.store_id)
        .where(UserStoreScope.user_id.in_(scopes)).order_by(UserStoreScope.store_id))).all():
        scopes[user_id].append(store_id)
    return [AdminUserView(id=u.id,username=u.username,role=u.role,status=u.status,created_at=u.created_at,store_ids=scopes[u.id]) for u in users]


async def update_admin_user(session:AsyncSession,*,actor_id:str,user_id:str,patch:AdminUserPatch)->User:
    try:
        actor,target,admins=await lock_admin_user_set(session,actor_id,user_id)
        if actor.id==target.id and (patch.role not in (None,UserRole.ADMIN) or patch.status==UserStatus.DISABLED):
            raise AdminDomainError('ADMIN_GUARD_VIOLATION',409)
        if not admins:raise AdminDomainError('ADMIN_GUARD_VIOLATION',409)
        before_role,before_status=target.role,target.status
        for key,value in patch.model_dump(exclude_unset=True).items():setattr(target,key,value)
        if not any(row.role is UserRole.ADMIN and row.status is UserStatus.ACTIVE for row in admins):
            raise AdminDomainError('ADMIN_GUARD_VIOLATION',409)
        _audit(session,actor,AuditEventType.ADMIN_USER_UPDATED,'user',target.id,
            {'from_role':before_role.value,'to_role':target.role.value,'from_user_status':before_status.value,
             'to_user_status':target.status.value,'changed_fields':sorted(patch.model_fields_set)})
        await session.flush();await session.commit();return target
    except AdminDomainError:
        await session.rollback();raise
    except SQLAlchemyError:
        await session.rollback();raise AdminDomainError('ADMIN_CONFLICT',409) from None


async def replace_user_store_scopes(session:AsyncSession,*,actor_id:str,user_id:str,store_ids:list[str])->User:
    try:
        if not isinstance(store_ids,list) or len(store_ids)>100 or any(not isinstance(s,str) or not 1<=len(s)<=36 for s in store_ids) or len(store_ids)!=len(set(store_ids)):
            raise AdminDomainError('ADMIN_SCOPE_INVALID',422)
        actor,target,_=await lock_admin_user_set(session,actor_id,user_id)
        await session.scalars(select(UserStoreScope).where(UserStoreScope.user_id==user_id).order_by(UserStoreScope.store_id).with_for_update())
        stores=list(await session.scalars(select(Store).where(Store.id.in_(store_ids)).order_by(Store.id).execution_options(populate_existing=True).with_for_update()))
        if len(stores)!=len(store_ids):raise AdminDomainError('ADMIN_STORE_NOT_FOUND',404)
        if any(not store.enabled for store in stores):raise AdminDomainError('ADMIN_STORE_DISABLED',409)
        await session.execute(delete(UserStoreScope).where(UserStoreScope.user_id==user_id))
        session.add_all([UserStoreScope(user_id=user_id,store_id=id) for id in sorted(store_ids)])
        _audit(session,actor,AuditEventType.ADMIN_USER_SCOPES_REPLACED,'user',user_id,{'scope_count':len(store_ids),'changed_fields':['store_scopes']})
        await session.flush();await session.commit();return target
    except AdminDomainError:
        await session.rollback();raise
    except SQLAlchemyError:
        await session.rollback();raise AdminDomainError('ADMIN_CONFLICT',409) from None


async def update_admin_store(session:AsyncSession,*,actor_id:str,store_id:str,patch:AdminStorePatch)->Store:
    try:
        actor,_,_=await lock_admin_user_set(session,actor_id,actor_id)
        store=await session.scalar(select(Store).where(Store.id==store_id).execution_options(populate_existing=True).with_for_update())
        if store is None:raise AdminDomainError('ADMIN_STORE_NOT_FOUND',404)
        store.enabled=patch.enabled
        _audit(session,actor,AuditEventType.ADMIN_STORE_UPDATED,'store',store.id,{'store_enabled':store.enabled,'changed_fields':['enabled']},store_id=store.id)
        await session.flush();await session.commit();return store
    except AdminDomainError:
        await session.rollback();raise
    except SQLAlchemyError:
        await session.rollback();raise AdminDomainError('ADMIN_CONFLICT',409) from None
