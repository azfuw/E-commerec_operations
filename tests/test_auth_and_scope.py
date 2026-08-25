from collections.abc import AsyncIterator
from datetime import timedelta

import jwt
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.auth import create_access_token, hash_password
from backend.common import UserRole, UserStatus, utc_now
from backend.config import get_settings
from backend.database import get_session
from backend.main import create_app
from backend.models import Product, Store, User, UserStoreScope


@pytest_asyncio.fixture
async def auth_data(session):
    password_hash = hash_password("DemoPass!2026")
    operator = User(
        id="operator-user",
        username="operator",
        password_hash=password_hash,
        role=UserRole.OPERATOR,
    )
    admin = User(
        id="admin-user",
        username="admin",
        password_hash=password_hash,
        role=UserRole.ADMIN,
    )
    disabled = User(
        id="disabled-user",
        username="disabled",
        password_hash=password_hash,
        role=UserRole.OPERATOR,
        status=UserStatus.DISABLED,
    )
    flagship = Store(id="flagship", name="旗舰店", code="flagship")
    other = Store(id="other-store", name="其他店", code="other")
    disabled_store = Store(
        id="disabled-store", name="已停用店", code="disabled", enabled=False
    )
    session.add_all([operator, admin, disabled, flagship, other, disabled_store])
    await session.flush()
    session.add_all(
        [
            UserStoreScope(user_id=operator.id, store_id=flagship.id),
            Product(
                id="flagship-live",
                store_id=flagship.id,
                code="LIVE-001",
                title="在售商品",
                category="数码",
                enabled=True,
            ),
            Product(
                id="flagship-hidden",
                store_id=flagship.id,
                code="HIDDEN-001",
                title="已停用商品",
                category="数码",
                enabled=False,
            ),
            Product(
                id="other-live",
                store_id=other.id,
                code="OTHER-001",
                title="其他店商品",
                category="家居",
                enabled=True,
            ),
        ]
    )
    await session.flush()
    return {"operator": operator, "admin": admin, "disabled": disabled}


@pytest_asyncio.fixture
async def client(session) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def override_get_session():
        yield session

    app.dependency_overrides[get_session] = override_get_session
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def operator_token(auth_data) -> str:
    return create_access_token(auth_data["operator"], get_settings())


@pytest_asyncio.fixture
async def admin_token(auth_data) -> str:
    return create_access_token(auth_data["admin"], get_settings())


async def test_active_user_can_log_in(client, auth_data) -> None:
    response = await client.post(
        "/auth/login", json={"username": "operator", "password": "DemoPass!2026"}
    )

    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"
    assert response.json()["access_token"]


async def test_operator_only_sees_assigned_stores(client, auth_data, operator_token) -> None:
    response = await client.get(
        "/stores", headers={"Authorization": f"Bearer {operator_token}"}
    )

    assert response.status_code == 200
    assert [store["code"] for store in response.json()] == ["flagship"]


async def test_operator_cannot_read_unassigned_store(client, auth_data, operator_token) -> None:
    response = await client.get(
        "/stores/other-store/products",
        headers={"Authorization": f"Bearer {operator_token}"},
    )

    assert response.status_code == 403


async def test_product_query_filters_disabled_products(client, auth_data, operator_token) -> None:
    response = await client.get(
        "/stores/flagship/products",
        headers={"Authorization": f"Bearer {operator_token}"},
    )

    assert response.status_code == 200
    assert [product["code"] for product in response.json()] == ["LIVE-001"]


async def test_database_role_overrides_token_role_claim(client, auth_data) -> None:
    settings = get_settings()
    now = utc_now()
    forged_admin_claim = jwt.encode(
        {
            "sub": auth_data["operator"].id,
            "role": UserRole.ADMIN.value,
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )

    response = await client.get(
        "/stores", headers={"Authorization": f"Bearer {forged_admin_claim}"}
    )

    assert response.status_code == 200
    assert [store["code"] for store in response.json()] == ["flagship"]


async def test_admin_cannot_read_disabled_store(client, auth_data, admin_token) -> None:
    response = await client.get(
        "/stores/disabled-store/products",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 404


async def test_disabled_user_cannot_login(client, auth_data) -> None:
    response = await client.post(
        "/auth/login", json={"username": "disabled", "password": "DemoPass!2026"}
    )

    assert response.status_code == 401


async def test_bad_password_and_unknown_username_share_error(client, auth_data) -> None:
    bad_password = await client.post(
        "/auth/login", json={"username": "operator", "password": "WrongPass!2026"}
    )
    unknown_user = await client.post(
        "/auth/login", json={"username": "missing", "password": "DemoPass!2026"}
    )

    assert bad_password.status_code == unknown_user.status_code == 401
    assert bad_password.json() == unknown_user.json()


async def test_missing_token_is_rejected(client) -> None:
    response = await client.get("/stores")

    assert response.status_code == 401


async def test_invalid_token_is_rejected(client) -> None:
    response = await client.get("/stores", headers={"Authorization": "Bearer invalid"})

    assert response.status_code == 401


async def test_unknown_token_subject_is_rejected(client, auth_data) -> None:
    settings = get_settings()
    now = utc_now()
    unknown_subject_token = jwt.encode(
        {
            "sub": "missing-user",
            "role": UserRole.OPERATOR.value,
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )

    response = await client.get(
        "/stores", headers={"Authorization": f"Bearer {unknown_subject_token}"}
    )

    assert response.status_code == 401


async def test_disabled_token_subject_is_rejected(client, auth_data) -> None:
    token = create_access_token(auth_data["disabled"], get_settings())

    response = await client.get("/stores", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


async def test_expired_token_is_rejected(client, auth_data) -> None:
    settings = get_settings()
    now = utc_now()
    expired_token = jwt.encode(
        {
            "sub": auth_data["operator"].id,
            "role": UserRole.OPERATOR.value,
            "iat": now - timedelta(minutes=10),
            "exp": now - timedelta(minutes=5),
        },
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )

    response = await client.get("/stores", headers={"Authorization": f"Bearer {expired_token}"})

    assert response.status_code == 401
