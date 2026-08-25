from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.common import OrderStatus, RefundStatus, UserRole, UserStatus, utc_now
from backend.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(
            UserRole,
            name="user_role",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    status: Mapped[UserStatus] = mapped_column(
        Enum(
            UserStatus,
            name="user_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        default=lambda: UserStatus.ACTIVE,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=lambda: True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class UserStoreScope(Base):
    __tablename__ = "user_store_scopes"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), primary_key=True)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("store_id", "code", name="uq_products_store_id_code"),
        CheckConstraint("current_version >= 1", name="ck_products_current_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    brand: Mapped[str] = mapped_column(String(128), default=lambda: "", nullable=False)
    selling_points: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    description: Mapped[str] = mapped_column(Text, default=lambda: "", nullable=False)
    search_keywords: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    attributes: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, default=lambda: 1, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=lambda: True, nullable=False)


class ProductSku(Base):
    __tablename__ = "product_skus"
    __table_args__ = (
        UniqueConstraint("product_id", "code", name="uq_product_skus_product_id_code"),
        CheckConstraint("price >= 0", name="ck_product_skus_price"),
        CheckConstraint("current_stock >= 0", name="ck_product_skus_current_stock"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    spec: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    current_stock: Mapped[int] = mapped_column(Integer, nullable=False)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (CheckConstraint("total_amount >= 0", name="ck_orders_total_amount"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False)
    ordered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(
            OrderStatus,
            name="order_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_quantity"),
        CheckConstraint("unit_price >= 0", name="ck_order_items_unit_price"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), nullable=False)
    sku_id: Mapped[str] = mapped_column(ForeignKey("product_skus.id"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    refund_status: Mapped[RefundStatus] = mapped_column(
        Enum(
            RefundStatus,
            name="refund_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )


class TrafficDaily(Base):
    __tablename__ = "traffic_daily"
    __table_args__ = (
        UniqueConstraint("store_id", "product_id", "metric_date", name="uq_traffic_daily_store_product_date"),
        CheckConstraint("impressions >= 0", name="ck_traffic_daily_impressions"),
        CheckConstraint("clicks >= 0", name="ck_traffic_daily_clicks"),
        CheckConstraint("visitors >= 0", name="ck_traffic_daily_visitors"),
        CheckConstraint("add_to_carts >= 0", name="ck_traffic_daily_add_to_carts"),
        CheckConstraint("clicks <= impressions", name="ck_traffic_daily_clicks_impressions"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), nullable=False)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    impressions: Mapped[int] = mapped_column(Integer, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False)
    visitors: Mapped[int] = mapped_column(Integer, nullable=False)
    add_to_carts: Mapped[int] = mapped_column(Integer, nullable=False)


class InventorySnapshot(Base):
    __tablename__ = "inventory_snapshots"
    __table_args__ = (
        UniqueConstraint("store_id", "sku_id", "snapshot_date", name="uq_inventory_store_sku_date"),
        CheckConstraint("on_hand >= 0", name="ck_inventory_snapshots_on_hand"),
        CheckConstraint("inbound >= 0", name="ck_inventory_snapshots_inbound"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False)
    sku_id: Mapped[str] = mapped_column(ForeignKey("product_skus.id"), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False)
    inbound: Mapped[int] = mapped_column(Integer, nullable=False)
