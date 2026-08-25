from datetime import UTC
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from backend.common import UserRole, UserStatus, utc_now
from backend.models import Product, ProductSku, Store, User


def test_utc_now_is_timezone_aware() -> None:
    assert utc_now().tzinfo is UTC


async def test_model_defaults_are_persisted(session) -> None:
    store = Store(id="store-1", name="旗舰店", code="flagship")
    user = User(
        id="user-1",
        username="operator",
        password_hash="hashed-password",
        role=UserRole.OPERATOR,
    )
    session.add_all([store, user])
    await session.flush()

    product = Product(
        id="p-1", store_id="store-1", code="SKU-001", title="商品一", category="数码"
    )
    session.add(product)
    await session.flush()

    assert user.status is UserStatus.ACTIVE
    assert user.created_at.tzinfo is UTC
    assert store.enabled is True
    assert store.created_at.tzinfo is UTC
    assert product.brand == ""
    assert product.description == ""
    assert product.selling_points == []
    assert product.search_keywords == []
    assert product.attributes == {}
    assert product.current_version == 1
    assert product.enabled is True


async def test_product_code_is_unique_within_store(session) -> None:
    store = Store(id="store-1", name="旗舰店", code="flagship")
    session.add_all(
        [
            store,
            Product(
                id="p-1",
                store_id="store-1",
                code="SKU-001",
                title="商品一",
                category="数码",
            ),
            Product(
                id="p-2",
                store_id="store-1",
                code="SKU-001",
                title="商品二",
                category="数码",
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        await session.commit()


async def test_sku_rejects_negative_price(session) -> None:
    store = Store(id="store-1", name="旗舰店", code="flagship")
    product = Product(
        id="p-1", store_id="store-1", code="SKU-001", title="商品一", category="数码"
    )
    session.add(store)
    await session.flush()
    session.add(product)
    await session.flush()
    session.add(
        ProductSku(
            id="sku-1",
            product_id="p-1",
            code="P1-BLACK",
            spec={"颜色": "黑色"},
            price=Decimal("-1.00"),
            current_stock=10,
        )
    )

    with pytest.raises(IntegrityError):
        await session.commit()
