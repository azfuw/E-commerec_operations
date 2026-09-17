"""Idempotent logistics-only examples anchored to existing commerce orders."""

from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common import OrderStatus, utc_now
from backend.logistics import patrol, utc
from backend.logistics_models import ReturnCase, Shipment, ShipmentEvent
from backend.models import Order, Store


def demo_id(kind: str, order_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"logistics-demo:{kind}:{order_id}"))


async def seed_logistics_demo(session: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """Create up to 12 untracked orders per store, once; never refresh existing facts."""
    now = utc(now or utc_now())
    stores = (await session.scalars(select(Store).where(Store.enabled.is_(True)).order_by(Store.code))).all()
    created = return_count = 0
    carriers = [("顺丰速运", "SF"), ("中通快递", "ZT"), ("圆通速递", "YT"), ("京东物流", "JD")]
    destinations = ["上海市 · 浦东新区", "杭州市 · 西湖区", "广州市 · 天河区", "成都市 · 武侯区", "北京市 · 朝阳区", "苏州市 · 工业园区"]
    for store in stores:
        # Stable selection includes already tracked orders, so restarts cannot expand the seed.
        orders = (await session.scalars(select(Order).where(Order.store_id == store.id, Order.status != OrderStatus.CANCELLED).order_by(Order.ordered_at.desc(), Order.id).limit(12))).all()
        store_rows = []
        for index, order in enumerate(orders):
            if await session.scalar(select(Shipment.id).where(Shipment.order_id == order.id)):
                continue
            age = (now - utc(order.ordered_at)).total_seconds()
            if age < 60:
                continue
            scale = min(1, (age - 30) / (8 * 86400))

            def at(hours: float) -> datetime:
                return now + timedelta(hours=hours * scale)

            scenario = index % 12
            carrier, prefix = carriers[index % len(carriers)]
            dispatch_due, expected, dispatched, delivered, last = at(-48), at(24), None, None, None
            state = "pending_dispatch"
            if scenario == 0:
                dispatch_due, expected = at(-28), at(48)
            elif scenario == 1:
                dispatch_due, expected = at(8), at(72)
            else:
                state, dispatched, last = "in_transit", at(-72), at(-2)
                dispatch_due = at(-60)
                if scenario == 3:
                    last, expected = at(-60), at(8)
                elif scenario == 4:
                    last, expected = at(-3), at(-18)
                elif scenario >= 5:
                    state, dispatched, dispatch_due = "delivered", at(-168), at(-156)
                    delivered = at(-2) if scenario == 5 else at(-20) if scenario == 6 else at(-120)
                    expected = at(4) if scenario == 5 else at(-26) if scenario == 6 else at(-108)
                    last = delivered
            shipment = Shipment(id=demo_id("shipment", order.id), order_id=order.id, store_id=store.id, carrier=carrier, tracking_no=f"{prefix}{uuid5(NAMESPACE_URL, order.id).int % 10**12:012d}", destination=destinations[index % len(destinations)], status=state,
                                dispatch_due_at=dispatch_due, expected_delivery_at=expected, dispatched_at=dispatched, delivered_at=delivered, last_event_at=last, last_location="客户已签收" if delivered else "华东转运中心" if dispatched else "杭州仓 · 待出库", is_demo=True, created_at=at(-180))
            session.add(shipment)
            await session.flush()
            created += 1
            store_rows.append((shipment, store.name, order.ordered_at))

            def event(kind: str, time: datetime, description: str, location: str, return_id: str | None = None):
                session.add(ShipmentEvent(id=demo_id(kind, order.id), shipment_id=shipment.id, return_id=return_id, event_type=kind, occurred_at=time, location=location, description=description, actor_name="演示数据", source="demo", created_at=time))

            event("created", shipment.created_at, "演示物流单已建立，进入仓库备货。", "杭州仓")
            if dispatched:
                event("dispatch", dispatched, "仓库已完成出库，承运商揽收包裹。", "杭州仓")
                if delivered:
                    event("transit", dispatched + timedelta(hours=8 * scale), "包裹已到达目的城市转运中心。", "目的地转运中心")
                    event("deliver", delivered, "收件人已签收包裹。", shipment.destination)
                else:
                    event("transit", last, "包裹已到达转运中心，等待下一班次。", "华东转运中心")
            if scenario >= 7:
                return_state = {7: "requested", 8: "approved", 9: "in_transit", 10: "received", 11: "closed"}[scenario]
                requested = at(-96)
                expected_return = at(48) if scenario == 7 else at(-12)
                received = at(-28) if scenario in {10, 11} else None
                closed = at(-4) if scenario == 11 else None
                updated = closed or received or (at(-60) if scenario >= 9 else at(-72) if scenario == 8 else requested)
                returned = ReturnCase(id=demo_id("return", order.id), shipment_id=shipment.id, store_id=store.id, status=return_state, reason=["尺码不合适，申请退回", "外包装破损，申请售后", "客户申请七天无理由退货"][index % 3], carrier="中通快递" if scenario >= 9 else "", tracking_no=f"ZT-R{uuid5(NAMESPACE_URL, order.id).int % 10**10:010d}" if scenario >= 9 else "", requested_at=requested, expected_return_at=expected_return, received_at=received, closed_at=closed, updated_at=updated, is_demo=True)
                session.add(returned)
                await session.flush()
                return_count += 1
                event("return_requested", requested, returned.reason, "售后服务台", returned.id)
                if scenario >= 8:
                    event("return_approved", at(-72), "退货申请已审核通过，等待客户寄回。", "售后服务台", returned.id)
                if scenario >= 9:
                    event("return_in_transit", at(-60), "客户已寄回包裹，退货运单已登记。", "退货运输中", returned.id)
                if received:
                    event("return_received", received, "仓库已收到退货包裹，等待质检。", "杭州退货仓", returned.id)
                if closed:
                    event("return_closed", closed, "质检完成，商品外观及配件完好；物流结案，未执行退款。", "杭州退货仓", returned.id)
        if store_rows:
            await session.flush()
            await patrol(session, None, store_rows, [store.id], now=now)
    await session.commit()
    return {"created_shipments": created, "created_returns": return_count}
