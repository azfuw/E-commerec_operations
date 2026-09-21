from datetime import timedelta

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from sqlalchemy import select, true, ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common import UserDepartment, UserRole, UserStatus, utc_now
from backend.config import Settings, get_settings
from backend.database import get_session
from backend.models import Store, User, UserStoreScope

password_hasher = PasswordHash.recommended()
bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_hasher.verify(password, password_hash)


def create_access_token(user: User, settings: Settings) -> str:
    now = utc_now()
    payload = {
        "sub": user.id,
        "role": user.role.value,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_minutes),
    }
    return jwt.encode(
        payload,
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def credentials_exception() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> User:
    if credentials is None:
        raise credentials_exception()

    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "role", "iat", "exp"]},
        )
        user_id = payload["sub"]
    except (jwt.PyJWTError, KeyError):
        raise credentials_exception() from None

    if not isinstance(user_id, str):
        raise credentials_exception()

    user = await session.get(User, user_id, populate_existing=True)
    if user is None or user.status is not UserStatus.ACTIVE:
        raise credentials_exception()
    return user


def store_visibility_predicate(actor: User, store_id_column: ColumnElement[str]) -> ColumnElement[bool]:
    if actor.role is UserRole.ADMIN:
        return true()
    return store_id_column.in_(select(UserStoreScope.store_id).where(UserStoreScope.user_id == actor.id))


def has_department_access(user: User | None, department: UserDepartment) -> bool:
    return user is not None and user.status is UserStatus.ACTIVE and (
        user.role is UserRole.ADMIN or user.department is department
    )


def require_department(user: User | None, department: UserDepartment) -> None:
    if not has_department_access(user, department):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Department access forbidden")


async def require_operations_user(user: User = Depends(get_current_user)) -> User:
    require_department(user, UserDepartment.OPERATIONS)
    return user


async def require_logistics_user(user: User = Depends(get_current_user)) -> User:
    require_department(user, UserDepartment.LOGISTICS)
    return user


def require_roles(*roles: UserRole):
    async def role_dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return user

    return role_dependency


async def require_store_access(
    store_id: str, user: User, session: AsyncSession
) -> Store:
    store = await session.scalar(
        select(Store).where(Store.id == store_id, Store.enabled.is_(True))
    )
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Store not found")
    if user.role is UserRole.ADMIN:
        return store

    scope = await session.scalar(
        select(UserStoreScope).where(UserStoreScope.user_id == user.id, UserStoreScope.store_id == store.id)
        .execution_options(populate_existing=True)
    )
    if scope is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return store
