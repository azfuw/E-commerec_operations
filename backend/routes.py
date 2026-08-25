from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import (
    create_access_token,
    get_current_user,
    require_store_access,
    verify_password,
)
from backend.common import UserRole, UserStatus
from backend.config import Settings, get_settings
from backend.database import get_session
from backend.models import Product, Store, User, UserStoreScope
from backend.schemas import AccessToken, LoginRequest, ProductSummary, StoreSummary

router = APIRouter()


@router.post("/auth/login", response_model=AccessToken)
async def login(
    request: LoginRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AccessToken:
    user = await session.scalar(select(User).where(User.username == request.username))
    if (
        user is None
        or not verify_password(request.password, user.password_hash)
        or user.status is not UserStatus.ACTIVE
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return AccessToken(access_token=create_access_token(user, settings))


@router.get("/stores", response_model=list[StoreSummary])
async def list_stores(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[StoreSummary]:
    statement = select(Store).where(Store.enabled.is_(True))
    if user.role is not UserRole.ADMIN:
        statement = statement.join(UserStoreScope).where(UserStoreScope.user_id == user.id)
    stores = (await session.scalars(statement.order_by(Store.code))).all()
    return [StoreSummary(id=store.id, code=store.code, name=store.name) for store in stores]


@router.get("/stores/{store_id}/products", response_model=list[ProductSummary])
async def list_products(
    store_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ProductSummary]:
    store = await require_store_access(store_id, user, session)
    products = (
        await session.scalars(
            select(Product)
            .where(Product.store_id == store.id, Product.enabled.is_(True))
            .order_by(Product.code)
        )
    ).all()
    return [
        ProductSummary(
            id=product.id,
            code=product.code,
            title=product.title,
            category=product.category,
            current_version=product.current_version,
        )
        for product in products
    ]
