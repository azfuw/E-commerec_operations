import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid5

from sqlalchemy import delete, func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import hash_password
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
    UserStoreScope,
)

DEMO_NAMESPACE = UUID("11bf6490-c3ba-5ed1-bdcf-46d5c31f96be")
STORE_CODES = ("flagship", "digital", "home")
ANOMALY_TYPES = ("high_refund", "low_conversion", "sales_drop", "stock_risk")
DEMO_USERNAMES = ("operator", "supervisor", "admin")


@dataclass(frozen=True)
class SeedSummary:
    stores: int
    products: int
    days: int
    anomaly_types: tuple[str, ...]


def stable_id(key: str) -> str:
    return str(uuid5(DEMO_NAMESPACE, key))


def product_code(store_code: str, number: int) -> str:
    return f"{store_code.upper()}-{number:03d}"


def demo_summary() -> SeedSummary:
    return SeedSummary(
        stores=3,
        products=300,
        days=90,
        anomaly_types=ANOMALY_TYPES,
    )


async def seed_demo_data(
    session: AsyncSession,
    seed: int = 20260825,
    end_date: date = date(2026, 8, 24),
) -> SeedSummary:
    store_ids = [stable_id(f"store:{code}") for code in STORE_CODES]
    existing_stores = await session.scalar(
        select(func.count(Store.id)).where(Store.id.in_(store_ids))
    )
    if existing_stores == len(STORE_CODES):
        return demo_summary()

    rng = random.Random(seed)
    store_names = {"flagship": "旗舰店", "digital": "数码店", "home": "家居店"}
    store_id_by_code = {code: stable_id(f"store:{code}") for code in STORE_CODES}
    user_id_by_name = {name: stable_id(f"user:{name}") for name in DEMO_USERNAMES}
    password_hash = hash_password("DemoPass!2026")

    await session.execute(
        insert(Store),
        [
            {
                "id": store_id_by_code[code],
                "name": store_names[code],
                "code": code,
                "enabled": True,
                "created_at": datetime(2026, 8, 24, tzinfo=UTC),
            }
            for code in STORE_CODES
        ],
    )
    await session.execute(
        insert(User),
        [
            {
                "id": user_id_by_name["operator"],
                "username": "operator",
                "password_hash": password_hash,
                "role": UserRole.OPERATOR,
                "status": "active",
                "created_at": datetime(2026, 8, 24, tzinfo=UTC),
            },
            {
                "id": user_id_by_name["supervisor"],
                "username": "supervisor",
                "password_hash": password_hash,
                "role": UserRole.SUPERVISOR,
                "status": "active",
                "created_at": datetime(2026, 8, 24, tzinfo=UTC),
            },
            {
                "id": user_id_by_name["admin"],
                "username": "admin",
                "password_hash": password_hash,
                "role": UserRole.ADMIN,
                "status": "active",
                "created_at": datetime(2026, 8, 24, tzinfo=UTC),
            },
        ],
    )
    await session.execute(
        insert(UserStoreScope),
        [{"user_id": user_id_by_name["operator"], "store_id": store_id_by_code["flagship"]}]
        + [
            {"user_id": user_id_by_name["supervisor"], "store_id": store_id_by_code[code]}
            for code in STORE_CODES
        ],
    )

    low_conversion = {product_code("flagship", number) for number in range(1, 6)}
    sales_drop = {product_code("digital", number) for number in range(1, 6)}
    high_refund = {product_code("home", number) for number in range(1, 6)}
    stock_risk = {product_code("flagship", number) for number in range(6, 11)}
    dates = [end_date - timedelta(days=offset) for offset in range(89, -1, -1)]
    product_rows: list[dict[str, object]] = []
    sku_rows: list[dict[str, object]] = []
    traffic_rows: list[dict[str, object]] = []
    inventory_rows: list[dict[str, object]] = []
    product_data: list[dict[str, object]] = []

    for store_code in STORE_CODES:
        for number in range(1, 101):
            code = product_code(store_code, number)
            product_id = stable_id(f"product:{store_code}:{number}")
            product_rows.append(
                {
                    "id": product_id,
                    "store_id": store_id_by_code[store_code],
                    "code": code,
                    "title": f"{store_names[store_code]}商品{number}",
                    "category": ("数码", "家居", "办公")[number % 3],
                    "brand": f"演示品牌{number % 10}",
                    "selling_points": ["稳定供货", "本地演示"],
                    "description": f"{code} 的可重复演示商品数据。",
                    "search_keywords": [store_code, "演示商品"],
                    "attributes": {"款号": code},
                    "current_version": 1,
                    "enabled": True,
                }
            )
            sku_count = 1 + rng.randrange(3)
            primary_sku_id = ""
            for sku_number in range(1, sku_count + 1):
                sku_id = stable_id(f"sku:{store_code}:{number}:{sku_number}")
                if sku_number == 1:
                    primary_sku_id = sku_id
                price = Decimal("99.00") + Decimal(rng.randrange(30))
                sku_rows.append(
                    {
                        "id": sku_id,
                        "product_id": product_id,
                        "code": f"{code}-S{sku_number}",
                        "spec": {"规格": f"标准款{sku_number}"},
                        "price": price,
                        "current_stock": 50,
                    }
                )
                for metric_date in dates:
                    on_hand = 20 + rng.randrange(40)
                    if code in stock_risk and metric_date >= end_date - timedelta(days=6):
                        on_hand = 1
                    inventory_rows.append(
                        {
                            "id": stable_id(f"inventory:{sku_id}:{metric_date.isoformat()}"),
                            "store_id": store_id_by_code[store_code],
                            "sku_id": sku_id,
                            "snapshot_date": metric_date,
                            "on_hand": on_hand,
                            "inbound": rng.randrange(8),
                        }
                    )
            for metric_date in dates:
                clicks = 80 + rng.randrange(40)
                if code in low_conversion:
                    clicks = 300
                traffic_rows.append(
                    {
                        "id": stable_id(f"traffic:{product_id}:{metric_date.isoformat()}"),
                        "store_id": store_id_by_code[store_code],
                        "product_id": product_id,
                        "metric_date": metric_date,
                        "impressions": clicks * (10 + rng.randrange(5)),
                        "clicks": clicks,
                        "visitors": max(clicks - rng.randrange(10), 0),
                        "add_to_carts": max(clicks // 10, 1),
                    }
                )
            product_data.append(
                {
                    "code": code,
                    "id": product_id,
                    "store_id": store_id_by_code[store_code],
                    "sku_id": primary_sku_id,
                    "price": Decimal("99.00"),
                    "number": number,
                }
            )

    await session.execute(insert(Product), product_rows)
    await session.execute(insert(ProductSku), sku_rows)
    await session.execute(insert(TrafficDaily), traffic_rows)
    await session.execute(insert(InventorySnapshot), inventory_rows)

    order_rows: list[dict[str, object]] = []
    item_rows: list[dict[str, object]] = []

    def add_order(
        product: dict[str, object],
        ordered_on: date,
        serial: str,
        order_status: OrderStatus,
        refund_status: RefundStatus = RefundStatus.NONE,
    ) -> None:
        order_id = stable_id(f"order:{product['id']}:{ordered_on.isoformat()}:{serial}")
        order_rows.append(
            {
                "id": order_id,
                "store_id": product["store_id"],
                "ordered_at": datetime(
                    ordered_on.year, ordered_on.month, ordered_on.day, 12, tzinfo=UTC
                ),
                "status": order_status,
                "total_amount": product["price"],
            }
        )
        item_rows.append(
            {
                "id": stable_id(f"order-item:{order_id}"),
                "order_id": order_id,
                "product_id": product["id"],
                "sku_id": product["sku_id"],
                "quantity": 1,
                "unit_price": product["price"],
                "refund_status": refund_status,
            }
        )

    for product in product_data:
        base_date = end_date - timedelta(days=(int(product["number"]) - 1) % 30)
        base_status = (
            OrderStatus.COMPLETED if int(product["number"]) % 2 == 0 else OrderStatus.PAID
        )
        add_order(product, base_date, "base", base_status)
        if product["code"] in low_conversion:
            add_order(product, end_date - timedelta(days=35), "previous", OrderStatus.COMPLETED)
        if product["code"] in sales_drop:
            for index in range(5):
                add_order(
                    product,
                    end_date - timedelta(days=35 + index),
                    f"previous-{index}",
                    OrderStatus.COMPLETED,
                )
        if product["code"] in high_refund:
            for index in range(12):
                refund = (
                    RefundStatus.REFUNDED
                    if index in {0, 2}
                    else RefundStatus.RETURNED
                    if index == 1
                    else RefundStatus.NONE
                )
                add_order(
                    product,
                    end_date - timedelta(days=index % 30),
                    f"refund-{index}",
                    OrderStatus.COMPLETED,
                    refund,
                )
        if product["code"] in stock_risk:
            for index in range(5):
                add_order(
                    product,
                    end_date - timedelta(days=index),
                    f"stock-{index}",
                    OrderStatus.COMPLETED,
                )

    await session.execute(insert(Order), order_rows)
    await session.execute(insert(OrderItem), item_rows)
    await session.flush()
    return demo_summary()


async def clear_demo_data(session: AsyncSession) -> None:
    store_ids = [stable_id(f"store:{code}") for code in STORE_CODES]
    user_ids = [stable_id(f"user:{name}") for name in DEMO_USERNAMES]
    product_ids = select(Product.id).where(Product.store_id.in_(store_ids))
    sku_ids = select(ProductSku.id).where(ProductSku.product_id.in_(product_ids))
    order_ids = select(Order.id).where(Order.store_id.in_(store_ids))

    await session.execute(
        delete(OrderItem).where(
            or_(
                OrderItem.order_id.in_(order_ids),
                OrderItem.product_id.in_(product_ids),
                OrderItem.sku_id.in_(sku_ids),
            )
        )
    )
    await session.execute(delete(InventorySnapshot).where(InventorySnapshot.sku_id.in_(sku_ids)))
    await session.execute(delete(TrafficDaily).where(TrafficDaily.product_id.in_(product_ids)))
    await session.execute(delete(Order).where(Order.id.in_(order_ids)))
    await session.execute(delete(ProductSku).where(ProductSku.id.in_(sku_ids)))
    await session.execute(delete(Product).where(Product.id.in_(product_ids)))
    await session.execute(
        delete(UserStoreScope).where(
            or_(UserStoreScope.store_id.in_(store_ids), UserStoreScope.user_id.in_(user_ids))
        )
    )
    await session.execute(delete(Store).where(Store.id.in_(store_ids)))
    await session.execute(delete(User).where(User.id.in_(user_ids)))
    await session.flush()
