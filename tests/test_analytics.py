from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from backend.analytics import (
    ProductNotInStore,
    compare_store_products,
    find_anomalous_products,
    get_inventory_risk,
    get_product_metrics,
    get_store_summary,
)
from backend.common import OrderStatus, RefundStatus
from backend.models import InventorySnapshot, Order, OrderItem, Product, ProductSku, Store, TrafficDaily
from backend.schemas import ProductMetrics
from backend.seed import seed_demo_data


def test_product_metric_formulas_and_zero_denominators() -> None:
    metrics = ProductMetrics.from_totals(
        impressions=1000,
        clicks=100,
        orders=5,
        units=6,
        revenue=Decimal("299.00"),
        refunds=1,
    )

    assert metrics.ctr == Decimal("0.1000")
    assert metrics.conversion_rate == Decimal("0.0500")
    assert metrics.refund_rate == Decimal("0.2000")
    assert metrics.average_order_value == Decimal("59.8000")

    zero_metrics = ProductMetrics.from_totals(
        impressions=0,
        clicks=0,
        orders=0,
        units=0,
        revenue=Decimal("0.00"),
        refunds=0,
    )
    assert zero_metrics.ctr == Decimal("0.0000")
    assert zero_metrics.conversion_rate == Decimal("0.0000")
    assert zero_metrics.refund_rate == Decimal("0.0000")
    assert zero_metrics.average_order_value == Decimal("0.0000")


async def test_metrics_include_end_date_and_only_paid_or_completed_orders(session) -> None:
    store = Store(id="metrics-store", code="metrics", name="指标店")
    product = Product(
        id="metrics-product",
        store_id=store.id,
        code="METRICS-001",
        title="指标商品",
        category="演示",
    )
    sku = ProductSku(
        id="metrics-sku",
        product_id=product.id,
        code="METRICS-001-S1",
        spec={"规格": "标准"},
        price=Decimal("10.00"),
        current_stock=5,
    )
    session.add(store)
    await session.flush()
    session.add_all([product, sku])
    await session.flush()
    session.add(
        TrafficDaily(
            id="metrics-traffic",
            store_id=store.id,
            product_id=product.id,
            metric_date=date(2026, 8, 24),
            impressions=1000,
            clicks=100,
            visitors=90,
            add_to_carts=10,
        )
    )
    session.add_all(
        [
            Order(
                id="metrics-paid",
                store_id=store.id,
                ordered_at=datetime(2026, 8, 24, 12, tzinfo=UTC),
                status=OrderStatus.PAID,
                total_amount=Decimal("20.00"),
            ),
            Order(
                id="metrics-next-day",
                store_id=store.id,
                ordered_at=datetime(2026, 8, 25, tzinfo=UTC),
                status=OrderStatus.COMPLETED,
                total_amount=Decimal("30.00"),
            ),
            Order(
                id="metrics-shipped",
                store_id=store.id,
                ordered_at=datetime(2026, 8, 24, 12, tzinfo=UTC),
                status=OrderStatus.SHIPPED,
                total_amount=Decimal("40.00"),
            ),
        ]
    )
    session.add_all(
        [
            OrderItem(
                id="metrics-paid-item",
                order_id="metrics-paid",
                product_id=product.id,
                sku_id=sku.id,
                quantity=2,
                unit_price=Decimal("10.00"),
                refund_status=RefundStatus.REFUNDED,
            ),
            OrderItem(
                id="metrics-next-day-item",
                order_id="metrics-next-day",
                product_id=product.id,
                sku_id=sku.id,
                quantity=3,
                unit_price=Decimal("10.00"),
                refund_status=RefundStatus.NONE,
            ),
            OrderItem(
                id="metrics-shipped-item",
                order_id="metrics-shipped",
                product_id=product.id,
                sku_id=sku.id,
                quantity=4,
                unit_price=Decimal("10.00"),
                refund_status=RefundStatus.NONE,
            ),
        ]
    )
    await session.flush()

    metrics = await get_product_metrics(
        session, store.id, product.id, date(2026, 8, 24), date(2026, 8, 24)
    )
    summary = await get_store_summary(session, store.id, date(2026, 8, 24), date(2026, 8, 24))

    assert (metrics.orders, metrics.units, metrics.revenue, metrics.refunds) == (
        1,
        2,
        Decimal("20.00"),
        1,
    )
    assert metrics.conversion_rate == Decimal("0.0100")
    assert (summary.orders, summary.units, summary.revenue, summary.refunds) == (
        1,
        2,
        Decimal("20.00"),
        1,
    )


async def test_product_store_boundaries_are_enforced(session) -> None:
    await seed_demo_data(session)
    flagship_id = await session.scalar(select(Store.id).where(Store.code == "flagship"))
    flagship_product_id = await session.scalar(
        select(Product.id).join(Store).where(Store.code == "flagship")
    )
    home_product_id = await session.scalar(select(Product.id).join(Store).where(Store.code == "home"))

    with pytest.raises(ProductNotInStore):
        await get_product_metrics(
            session, flagship_id, home_product_id, date(2026, 7, 26), date(2026, 8, 24)
        )
    with pytest.raises(ProductNotInStore):
        await compare_store_products(
            session,
            flagship_id,
            [flagship_product_id, home_product_id],
            date(2026, 7, 26),
            date(2026, 8, 24),
        )
    with pytest.raises(ProductNotInStore):
        await compare_store_products(
            session,
            flagship_id,
            [flagship_product_id, "missing-product"],
            date(2026, 7, 26),
            date(2026, 8, 24),
        )
    with pytest.raises(ProductNotInStore):
        await get_inventory_risk(session, flagship_id, home_product_id)


async def test_inventory_risk_uses_all_skus_at_latest_snapshot_and_seven_day_sales(session) -> None:
    store = Store(id="risk-store", code="risk", name="风险店")
    product = Product(
        id="risk-product",
        store_id=store.id,
        code="RISK-001",
        title="风险商品",
        category="演示",
    )
    sku_one = ProductSku(
        id="risk-sku-1",
        product_id=product.id,
        code="RISK-001-S1",
        spec={"规格": "一"},
        price=Decimal("10.00"),
        current_stock=1,
    )
    sku_two = ProductSku(
        id="risk-sku-2",
        product_id=product.id,
        code="RISK-001-S2",
        spec={"规格": "二"},
        price=Decimal("10.00"),
        current_stock=2,
    )
    session.add(store)
    await session.flush()
    session.add_all([product, sku_one, sku_two])
    await session.flush()
    session.add_all(
        [
            InventorySnapshot(
                id="risk-snapshot-1",
                store_id=store.id,
                sku_id=sku_one.id,
                snapshot_date=date(2026, 8, 24),
                on_hand=1,
                inbound=0,
            ),
            InventorySnapshot(
                id="risk-snapshot-2",
                store_id=store.id,
                sku_id=sku_two.id,
                snapshot_date=date(2026, 8, 24),
                on_hand=2,
                inbound=0,
            ),
            Order(
                id="risk-current-order",
                store_id=store.id,
                ordered_at=datetime(2026, 8, 24, 12, tzinfo=UTC),
                status=OrderStatus.COMPLETED,
                total_amount=Decimal("40.00"),
            ),
            Order(
                id="risk-old-order",
                store_id=store.id,
                ordered_at=datetime(2026, 8, 17, 12, tzinfo=UTC),
                status=OrderStatus.COMPLETED,
                total_amount=Decimal("50.00"),
            ),
        ]
    )
    session.add_all(
        [
            OrderItem(
                id="risk-current-item",
                order_id="risk-current-order",
                product_id=product.id,
                sku_id=sku_one.id,
                quantity=4,
                unit_price=Decimal("10.00"),
                refund_status=RefundStatus.NONE,
            ),
            OrderItem(
                id="risk-old-item",
                order_id="risk-old-order",
                product_id=product.id,
                sku_id=sku_two.id,
                quantity=5,
                unit_price=Decimal("10.00"),
                refund_status=RefundStatus.NONE,
            ),
        ]
    )
    await session.flush()

    risk = await get_inventory_risk(session, store.id, product.id)

    assert risk.snapshot_date == date(2026, 8, 24)
    assert risk.on_hand == 3
    assert risk.seven_day_units_sold == 4
    assert risk.threshold == 4
    assert risk.is_at_risk is True


async def test_anomaly_ranking_uses_real_seeded_values_and_deterministic_ties(session) -> None:
    await seed_demo_data(session)
    store_ids = {
        code: await session.scalar(select(Store.id).where(Store.code == code))
        for code in ("flagship", "digital", "home")
    }
    start_date = date(2026, 7, 26)
    end_date = date(2026, 8, 24)

    flagship_candidates = await find_anomalous_products(
        session, store_ids["flagship"], start_date, end_date, limit=5
    )
    assert len(flagship_candidates) == 5
    assert flagship_candidates == sorted(
        flagship_candidates, key=lambda candidate: (-candidate.score, candidate.product_code)
    )
    assert all(candidate.evidence and all("=" in value for value in candidate.evidence) for candidate in flagship_candidates)

    detected_types = set()
    for store_id in store_ids.values():
        candidates = await find_anomalous_products(session, store_id, start_date, end_date, limit=10)
        detected_types.update(
            anomaly_type for candidate in candidates for anomaly_type in candidate.anomaly_types
        )
    assert {"low_conversion", "sales_drop", "high_refund", "stock_risk"} <= detected_types
