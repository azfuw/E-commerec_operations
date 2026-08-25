from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common import OrderStatus, RefundStatus
from backend.models import InventorySnapshot, Order, OrderItem, Product, ProductSku, TrafficDaily
from backend.schemas import AnomalyCandidate, InventoryRisk, ProductMetrics, StoreMetrics


class ProductNotInStore(Exception):
    pass


ACTIVE_ORDER_STATUSES = (OrderStatus.PAID, OrderStatus.COMPLETED)
REFUND_STATUSES = (RefundStatus.REFUNDED, RefundStatus.RETURNED)


def order_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(start_date, time.min, tzinfo=UTC),
        datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=UTC),
    )


async def require_product(session: AsyncSession, store_id: str, product_id: str) -> Product:
    product = await session.scalar(
        select(Product).where(Product.id == product_id, Product.store_id == store_id)
    )
    if product is None:
        raise ProductNotInStore(product_id)
    return product


async def traffic_totals(
    session: AsyncSession,
    store_id: str,
    start_date: date,
    end_date: date,
    product_id: str | None = None,
) -> tuple[int, int]:
    statement = select(func.sum(TrafficDaily.impressions), func.sum(TrafficDaily.clicks)).where(
        TrafficDaily.store_id == store_id,
        TrafficDaily.metric_date >= start_date,
        TrafficDaily.metric_date <= end_date,
    )
    if product_id is not None:
        statement = statement.where(TrafficDaily.product_id == product_id)
    impressions, clicks = (await session.execute(statement)).one()
    return int(impressions or 0), int(clicks or 0)


async def order_totals(
    session: AsyncSession,
    store_id: str,
    start_date: date,
    end_date: date,
    product_id: str | None = None,
) -> tuple[int, int, Decimal, int]:
    start_at, end_at = order_bounds(start_date, end_date)
    order_filters = (
        Order.store_id == store_id,
        Order.status.in_(ACTIVE_ORDER_STATUSES),
        Order.ordered_at >= start_at,
        Order.ordered_at < end_at,
    )
    if product_id is None:
        orders = await session.scalar(select(func.count(Order.id)).where(*order_filters))
        revenue = await session.scalar(select(func.sum(Order.total_amount)).where(*order_filters))
    else:
        product_order_filters = (*order_filters, OrderItem.product_id == product_id)
        orders = await session.scalar(
            select(func.count(func.distinct(Order.id))).join(OrderItem).where(*product_order_filters)
        )
        revenue = await session.scalar(
            select(func.sum(OrderItem.quantity * OrderItem.unit_price))
            .join(Order)
            .where(*product_order_filters)
        )

    item_filters = (*order_filters,)
    if product_id is not None:
        item_filters = (*item_filters, OrderItem.product_id == product_id)
    units = await session.scalar(
        select(func.sum(OrderItem.quantity)).join(Order).where(*item_filters)
    )
    refunds = await session.scalar(
        select(func.count(OrderItem.id))
        .join(Order)
        .where(*item_filters, OrderItem.refund_status.in_(REFUND_STATUSES))
    )
    return int(orders or 0), int(units or 0), Decimal(revenue or 0), int(refunds or 0)


async def product_metrics(
    session: AsyncSession,
    store_id: str,
    product: Product,
    start_date: date,
    end_date: date,
) -> ProductMetrics:
    impressions, clicks = await traffic_totals(session, store_id, start_date, end_date, product.id)
    orders, units, revenue, refunds = await order_totals(
        session, store_id, start_date, end_date, product.id
    )
    return ProductMetrics.from_totals(
        impressions=impressions,
        clicks=clicks,
        orders=orders,
        units=units,
        revenue=revenue,
        refunds=refunds,
    ).model_copy(update={"product_id": product.id, "product_code": product.code})


async def get_store_summary(
    session: AsyncSession, store_id: str, start_date: date, end_date: date
) -> StoreMetrics:
    impressions, clicks = await traffic_totals(session, store_id, start_date, end_date)
    orders, units, revenue, refunds = await order_totals(session, store_id, start_date, end_date)
    metrics = ProductMetrics.from_totals(
        impressions=impressions,
        clicks=clicks,
        orders=orders,
        units=units,
        revenue=revenue,
        refunds=refunds,
    )
    return StoreMetrics(store_id=store_id, **metrics.model_dump(exclude={"product_id", "product_code"}))


async def get_product_metrics(
    session: AsyncSession,
    store_id: str,
    product_id: str,
    start_date: date,
    end_date: date,
) -> ProductMetrics:
    product = await require_product(session, store_id, product_id)
    return await product_metrics(session, store_id, product, start_date, end_date)


async def compare_store_products(
    session: AsyncSession,
    store_id: str,
    product_ids: list[str],
    start_date: date,
    end_date: date,
) -> list[ProductMetrics]:
    products = list(
        await session.scalars(
            select(Product).where(Product.store_id == store_id, Product.id.in_(product_ids))
        )
    )
    products_by_id = {product.id: product for product in products}
    if len(products_by_id) != len(set(product_ids)):
        raise ProductNotInStore("product is unknown or belongs to another store")
    return [
        await product_metrics(session, store_id, products_by_id[product_id], start_date, end_date)
        for product_id in product_ids
    ]


async def get_inventory_risk(
    session: AsyncSession, store_id: str, product_id: str
) -> InventoryRisk:
    product = await require_product(session, store_id, product_id)
    latest_date = await session.scalar(
        select(func.max(InventorySnapshot.snapshot_date))
        .join(ProductSku, ProductSku.id == InventorySnapshot.sku_id)
        .where(InventorySnapshot.store_id == store_id, ProductSku.product_id == product.id)
    )
    if latest_date is None:
        return InventoryRisk(
            product_id=product.id,
            snapshot_date=None,
            on_hand=0,
            seven_day_units_sold=0,
            threshold=3,
            is_at_risk=True,
        )
    on_hand = await session.scalar(
        select(func.sum(InventorySnapshot.on_hand))
        .join(ProductSku, ProductSku.id == InventorySnapshot.sku_id)
        .where(
            InventorySnapshot.store_id == store_id,
            ProductSku.product_id == product.id,
            InventorySnapshot.snapshot_date == latest_date,
        )
    )
    seven_day_start = latest_date - timedelta(days=6)
    _, seven_day_units_sold, _, _ = await order_totals(
        session, store_id, seven_day_start, latest_date, product.id
    )
    threshold = max(3, seven_day_units_sold)
    return InventoryRisk(
        product_id=product.id,
        snapshot_date=latest_date,
        on_hand=int(on_hand or 0),
        seven_day_units_sold=seven_day_units_sold,
        threshold=threshold,
        is_at_risk=int(on_hand or 0) <= threshold,
    )


async def find_anomalous_products(
    session: AsyncSession,
    store_id: str,
    start_date: date,
    end_date: date,
    limit: int = 5,
) -> list[AnomalyCandidate]:
    products = list(
        await session.scalars(
            select(Product).where(Product.store_id == store_id).order_by(Product.code)
        )
    )
    current_metrics = [
        await product_metrics(session, store_id, product, start_date, end_date) for product in products
    ]
    period_days = (end_date - start_date).days + 1
    previous_start = start_date - timedelta(days=period_days)
    previous_end = start_date - timedelta(days=1)
    previous_metrics = {
        product.id: await product_metrics(session, store_id, product, previous_start, previous_end)
        for product in products
    }
    median_conversion_rate = median(metric.conversion_rate for metric in current_metrics)
    candidates: list[AnomalyCandidate] = []
    for product, metrics in zip(products, current_metrics, strict=True):
        previous = previous_metrics[product.id]
        inventory_risk = await get_inventory_risk(session, store_id, product.id)
        anomaly_types: list[str] = []
        evidence: list[str] = []
        business_impact = Decimal("0.00")
        if metrics.clicks >= 50 and metrics.conversion_rate < median_conversion_rate * Decimal("0.60"):
            anomaly_types.append("low_conversion")
            evidence.extend(
                [
                    f"clicks={metrics.clicks}",
                    f"conversion_rate={metrics.conversion_rate}",
                    f"store_median_conversion_rate={median_conversion_rate}",
                ]
            )
        if metrics.revenue <= previous.revenue * Decimal("0.70"):
            anomaly_types.append("sales_drop")
            evidence.extend(
                [
                    f"current_revenue={metrics.revenue}",
                    f"previous_revenue={previous.revenue}",
                ]
            )
            business_impact += max(previous.revenue - metrics.revenue, Decimal("0.00"))
        if metrics.units >= 10 and metrics.refund_rate >= Decimal("0.1000"):
            anomaly_types.append("high_refund")
            evidence.extend(
                [
                    f"completed_units={metrics.units}",
                    f"refund_rate={metrics.refund_rate}",
                ]
            )
            business_impact += (metrics.revenue * metrics.refund_rate).quantize(Decimal("0.01"))
        if inventory_risk.is_at_risk:
            anomaly_types.append("stock_risk")
            evidence.extend(
                [
                    f"on_hand={inventory_risk.on_hand}",
                    f"seven_day_units_sold={inventory_risk.seven_day_units_sold}",
                    f"stock_threshold={inventory_risk.threshold}",
                ]
            )
            business_impact += metrics.revenue
        if anomaly_types:
            candidates.append(
                AnomalyCandidate(
                    product_id=product.id,
                    product_code=product.code,
                    anomaly_types=tuple(anomaly_types),
                    score=Decimal(len(anomaly_types)),
                    business_impact=business_impact,
                    metrics=metrics,
                    evidence=evidence,
                )
            )
    return sorted(candidates, key=lambda candidate: (-candidate.score, candidate.product_code))[:limit]
