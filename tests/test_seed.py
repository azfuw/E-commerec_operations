from datetime import date

from sqlalchemy import func, select

from backend.common import OrderStatus, RefundStatus, UserRole
from backend.models import (
    InventorySnapshot,
    Order,
    OrderItem,
    Product,
    ProductSku,
    Store,
    TrafficDaily,
    User,
)
from backend.seed import clear_demo_data, seed_demo_data


async def test_seed_is_idempotent(session) -> None:
    first = await seed_demo_data(session)
    products_after_first_seed = await session.scalar(select(func.count(Product.id)))

    second = await seed_demo_data(session)
    products_after_second_seed = await session.scalar(select(func.count(Product.id)))

    assert second == first
    assert products_after_first_seed == products_after_second_seed == 300


async def test_seed_has_required_shape_and_real_anomaly_data(session) -> None:
    summary = await seed_demo_data(session)

    assert summary.stores == 3
    assert summary.products == 300
    assert summary.days == 90
    assert {"low_conversion", "sales_drop", "high_refund", "stock_risk"} <= set(
        summary.anomaly_types
    )
    assert await session.scalar(select(func.count(func.distinct(TrafficDaily.metric_date)))) == 90
    assert await session.scalar(select(func.count(func.distinct(InventorySnapshot.snapshot_date)))) == 90

    sku_counts = (
        await session.execute(
            select(ProductSku.product_id, func.count(ProductSku.id)).group_by(ProductSku.product_id)
        )
    ).all()
    assert len(sku_counts) == 300
    assert {count for _, count in sku_counts} <= {1, 2, 3}
    assert await session.scalar(
        select(func.count(ProductSku.id))
        .join(
            InventorySnapshot,
            (InventorySnapshot.sku_id == ProductSku.id)
            & (InventorySnapshot.snapshot_date == date(2026, 8, 24)),
        )
        .where(ProductSku.current_stock == InventorySnapshot.on_hand)
    ) == sum(count for _, count in sku_counts)

    low_conversion_id = await session.scalar(
        select(Product.id).where(Product.code == "FLAGSHIP-001")
    )
    assert await session.scalar(
        select(func.sum(TrafficDaily.clicks)).where(TrafficDaily.product_id == low_conversion_id)
    ) > 0
    assert await session.scalar(
        select(func.count(OrderItem.id)).where(OrderItem.product_id == low_conversion_id)
    ) == 2

    sales_drop_id = await session.scalar(
        select(Product.id).where(Product.code == "DIGITAL-001")
    )
    previous_orders = await session.scalar(
        select(func.count(OrderItem.id))
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            OrderItem.product_id == sales_drop_id,
            Order.ordered_at < date(2026, 7, 26),
        )
    )
    current_orders = await session.scalar(
        select(func.count(OrderItem.id))
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            OrderItem.product_id == sales_drop_id,
            Order.ordered_at >= date(2026, 7, 26),
        )
    )
    assert previous_orders > current_orders

    high_refund_id = await session.scalar(
        select(Product.id).where(Product.code == "HOME-001")
    )
    completed_items = await session.scalar(
        select(func.count(OrderItem.id))
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            OrderItem.product_id == high_refund_id,
            Order.status == OrderStatus.COMPLETED,
        )
    )
    refunded_items = await session.scalar(
        select(func.count(OrderItem.id)).where(
            OrderItem.product_id == high_refund_id,
            OrderItem.refund_status.in_([RefundStatus.REFUNDED, RefundStatus.RETURNED]),
        )
    )
    assert completed_items >= 10
    assert refunded_items >= 2

    stock_risk_id = await session.scalar(
        select(Product.id).where(Product.code == "FLAGSHIP-006")
    )
    stock_risk_sku_id = await session.scalar(
        select(ProductSku.id).where(ProductSku.product_id == stock_risk_id)
    )
    assert await session.scalar(
        select(InventorySnapshot.on_hand).where(
            InventorySnapshot.sku_id == stock_risk_sku_id,
            InventorySnapshot.snapshot_date == date(2026, 8, 24),
        )
    ) == 1


async def test_clear_demo_data_preserves_external_rows_and_seed_repeats(session) -> None:
    external_store = Store(id="external-store", name="外部店", code="external")
    external_user = User(
        id="external-user",
        username="external",
        password_hash="external-password-hash",
        role=UserRole.ADMIN,
    )
    session.add_all([external_store, external_user])
    await session.flush()

    await seed_demo_data(session)
    first_codes = (
        await session.scalars(
            select(Product.code).where(
                Product.code.in_(["FLAGSHIP-001", "DIGITAL-001", "HOME-001", "FLAGSHIP-006"])
            )
        )
    ).all()

    await clear_demo_data(session)

    assert await session.get(Store, external_store.id) is not None
    assert await session.get(User, external_user.id) is not None
    assert await session.scalar(
        select(func.count(Store.id)).where(Store.code.in_(["flagship", "digital", "home"]))
    ) == 0

    await seed_demo_data(session)
    second_codes = (
        await session.scalars(
            select(Product.code).where(
                Product.code.in_(["FLAGSHIP-001", "DIGITAL-001", "HOME-001", "FLAGSHIP-006"])
            )
        )
    ).all()

    assert sorted(second_codes) == sorted(first_codes)
