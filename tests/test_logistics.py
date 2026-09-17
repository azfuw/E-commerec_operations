import asyncio
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm.exc import StaleDataError

from backend.auth import get_current_user
from backend.common import OrderStatus, UserRole
from backend.database import get_session
from backend.models import Base, Order, Store, User, UserStoreScope


@pytest_asyncio.fixture
async def logistics_data(session):
    now = datetime.now(UTC).replace(microsecond=0)
    actor = User(id="log-operator", username="log-operator", password_hash="unused", role=UserRole.OPERATOR)
    outsider = User(id="log-outsider", username="log-outsider", password_hash="unused", role=UserRole.OPERATOR)
    session.add_all([actor, outsider, Store(id="log-a", name="华东店", code="log-a"), Store(id="log-b", name="华南店", code="log-b")])
    await session.flush()
    session.add_all([UserStoreScope(user_id=actor.id, store_id="log-a"), UserStoreScope(user_id=outsider.id, store_id="log-b")])
    for store, order_id in [("log-a", "log-order-a"), ("log-b", "log-order-b"), ("log-a", "log-order-c")]:
        session.add(Order(id=order_id, store_id=store, ordered_at=now - timedelta(days=4), status=OrderStatus.PAID, total_amount=Decimal("129.00")))
    await session.commit()
    return actor, now


@pytest_asyncio.fixture
async def logistics_client(session, logistics_data):
    from backend.logistics import router

    app = FastAPI()
    app.include_router(router)
    async def current_actor():
        return await session.get(User, "log-operator", populate_existing=True)

    app.dependency_overrides[get_current_user] = current_actor

    async def connection():
        yield session

    app.dependency_overrides[get_session] = connection
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def create_shipment(client, now, order_id="log-order-a"):
    response = await client.post("/logistics/shipments", json={
        "order_id": order_id, "carrier": "顺丰速运", "tracking_no": "SF-" + order_id,
        "destination": "上海市 浦东新区", "dispatch_due_at": (now - timedelta(days=2)).isoformat(),
        "expected_delivery_at": (now + timedelta(days=2)).isoformat(),
    })
    assert response.status_code == 201, response.text
    return response.json()


async def test_logistics_scope_and_unknown_rows_do_not_leak(logistics_client, logistics_data):
    _, now = logistics_data
    shipment = await create_shipment(logistics_client, now)
    listed = await logistics_client.get("/logistics/shipments")
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == shipment["id"]
    denied = await logistics_client.get("/logistics/shipments", params={"store_id": "log-b"})
    assert denied.status_code == 403
    forbidden_create = await logistics_client.post("/logistics/shipments", json={
        "order_id": "log-order-b", "carrier": "顺丰速运", "tracking_no": "hidden",
        "destination": "广州", "dispatch_due_at": now.isoformat(),
        "expected_delivery_at": (now + timedelta(days=2)).isoformat(),
    })
    assert forbidden_create.status_code == 404
    assert (await logistics_client.get("/logistics/shipments/unknown")).status_code == 404


async def test_shipment_transition_enforces_utc_order_and_terminal_state(logistics_client, logistics_data, session):
    _, now = logistics_data
    shipment = await create_shipment(logistics_client, now)
    route = f"/logistics/shipments/{shipment['id']}/events"
    early_delivery = await logistics_client.post(route, json={"action": "deliver", "occurred_at": now.isoformat(), "location": "上海", "description": "签收"})
    assert early_delivery.status_code == 409
    naive = await logistics_client.post(route, json={"action": "dispatch", "occurred_at": now.replace(tzinfo=None).isoformat(), "location": "杭州", "description": "揽收"})
    assert naive.status_code == 422
    dispatched = await logistics_client.post(route, json={"action": "dispatch", "occurred_at": (now - timedelta(hours=2)).isoformat(), "location": "杭州仓", "description": "仓库出库"})
    assert dispatched.status_code == 200, dispatched.text
    assert dispatched.json()["status"] == "in_transit"
    backward = await logistics_client.post(route, json={"action": "transit", "occurred_at": (now - timedelta(hours=3)).isoformat(), "location": "杭州", "description": "倒序事件"})
    assert backward.status_code == 422
    delivered = await logistics_client.post(route, json={"action": "deliver", "occurred_at": now.astimezone(timezone(timedelta(hours=8))).isoformat(), "location": "上海", "description": "本人签收"})
    assert delivered.status_code == 200
    assert delivered.json()["status"] == "delivered"
    assert datetime.fromisoformat(delivered.json()["delivered_at"].replace("Z", "+00:00")) == now
    repeated = await logistics_client.post(route, json={"action": "transit", "occurred_at": now.isoformat(), "location": "上海", "description": "再次运输"})
    assert repeated.status_code == 409
    detail = (await logistics_client.get(f"/logistics/shipments/{shipment['id']}")).json()
    assert [event["event_type"] for event in detail["events"]] == ["created", "dispatch", "deliver"]
    order = await session.get(Order, "log-order-a")
    assert order.status == OrderStatus.PAID


async def test_patrol_is_idempotent_scoped_and_resolution_is_auditable(logistics_client, logistics_data, session):
    _, now = logistics_data
    shipment = await create_shipment(logistics_client, now)
    first = await logistics_client.post("/logistics/agent/patrol", json={})
    assert first.status_code == 200, first.text
    assert first.json()["created_exceptions"] == 1
    second = await logistics_client.post("/logistics/agent/patrol", json={})
    assert second.json()["created_exceptions"] == 0
    assert second.json()["existing_exceptions"] == 1
    tasks = (await logistics_client.get("/logistics/exceptions")).json()
    assert tasks["total"] == 1
    task = tasks["items"][0]
    assert task["kind"] == "dispatch_overdue"
    assert task["evidence"]["shipment_id"] == shipment["id"]
    forbidden = await logistics_client.patch(f"/logistics/exceptions/{task['id']}", json={"action": "assign", "assignee_id": "log-outsider"})
    assert forbidden.status_code == 422
    assigned = await logistics_client.patch(f"/logistics/exceptions/{task['id']}", json={"action": "assign", "assignee_id": "log-operator"})
    assert assigned.json()["status"] == "in_progress"
    no_reason = await logistics_client.patch(f"/logistics/exceptions/{task['id']}", json={"action": "resolve", "resolution": " "})
    assert no_reason.status_code == 422
    resolved = await logistics_client.patch(f"/logistics/exceptions/{task['id']}", json={"action": "resolve", "resolution": "仓库已确认，将于今晚交接快递"})
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["resolved_at"]
    third = await logistics_client.post("/logistics/agent/patrol", json={})
    assert third.json()["created_exceptions"] == 0
    query = await logistics_client.post("/logistics/agent/query", json={"question": "哪些订单发货超时？"})
    assert query.status_code == 200
    assert query.json()["mode"] == "deterministic_rules"
    assert query.json()["citations"][0]["shipment_id"] == shipment["id"]
    assert (await logistics_client.get("/logistics/dashboard")).json()["metrics"]["open_exceptions"] == 0


async def test_returns_follow_validated_progress_and_keep_history(logistics_client, logistics_data):
    _, now = logistics_data
    shipment = await create_shipment(logistics_client, now)
    events_url = f"/logistics/shipments/{shipment['id']}/events"
    await logistics_client.post(events_url, json={"action": "dispatch", "occurred_at": (now - timedelta(hours=1)).isoformat(), "location": "仓库", "description": "发货"})
    response = await logistics_client.post("/logistics/returns", json={"shipment_id": shipment["id"], "reason": "客户申请退回", "expected_return_at": (now + timedelta(days=3)).isoformat()})
    assert response.status_code == 201, response.text
    case = response.json()
    route = f"/logistics/returns/{case['id']}/transition"
    invalid = await logistics_client.post(route, json={"status": "closed", "occurred_at": datetime.now(UTC).isoformat(), "note": "跳过验收"})
    assert invalid.status_code == 409
    for status in ["approved", "in_transit", "received", "closed"]:
        payload = {"status": status, "occurred_at": datetime.now(UTC).isoformat(), "note": "已核实"}
        if status == "in_transit":
            payload.update(carrier="中通快递", tracking_no="ZT-RETURN-001")
        step = await logistics_client.post(route, json=payload)
        assert step.status_code == 200, step.text
        assert step.json()["status"] == status
    terminal = await logistics_client.post(route, json={"status": "approved", "occurred_at": datetime.now(UTC).isoformat(), "note": "重新处理"})
    assert terminal.status_code == 409
    duplicate = await logistics_client.post("/logistics/returns", json={"shipment_id": shipment["id"], "reason": "重复", "expected_return_at": (now + timedelta(days=3)).isoformat()})
    assert duplicate.status_code == 409
    detail = (await logistics_client.get(f"/logistics/shipments/{shipment['id']}")).json()
    assert detail["return_case"]["status"] == "closed"
    assert len([event for event in detail["events"] if event["return_id"] == case["id"]]) == 5


async def test_demo_seed_is_persistent_idempotent_without_order_mutation(session, logistics_data):
    from backend.logistics_models import Shipment, ShipmentEvent
    from backend.logistics_seed import seed_logistics_demo

    _, now = logistics_data
    before = [(order.id, order.status, order.ordered_at, order.total_amount) for order in (await session.scalars(select(Order))).all()]
    first = await seed_logistics_demo(session, now=now)
    second = await seed_logistics_demo(session, now=now + timedelta(days=1))
    assert first["created_shipments"] == 3
    assert second["created_shipments"] == 0
    assert await session.scalar(select(func.count()).select_from(Shipment)) == 3
    assert await session.scalar(select(func.count()).select_from(ShipmentEvent)) >= 3
    after = [(order.id, order.status, order.ordered_at, order.total_amount) for order in (await session.scalars(select(Order))).all()]
    assert before == after


async def test_patrol_rules_and_demo_timelines_cover_return_delays(session, logistics_data, logistics_client):
    from backend.logistics import utc
    from backend.logistics_models import ExceptionTask, ReturnCase, Shipment
    from backend.logistics_seed import seed_logistics_demo

    _, now = logistics_data
    for index in range(18):
        session.add(Order(id=f"scenario-{index:02}", store_id="log-a", ordered_at=now - timedelta(days=14), status=OrderStatus.PAID, total_amount=Decimal("20.00")))
    for order in (await session.scalars(select(Order))).all():
        order.ordered_at = now - timedelta(days=14)
    await session.commit()
    await seed_logistics_demo(session, now)
    assert set((await session.scalars(select(ExceptionTask.kind))).all()) == {"dispatch_overdue", "no_movement", "delivery_overdue", "return_overdue"}
    cases = (await session.scalars(select(ReturnCase))).all()
    assert {case.status for case in cases} == {"requested", "approved", "in_transit", "received", "closed"}
    for case in cases:
        shipment = await session.get(Shipment, case.shipment_id)
        assert utc(shipment.dispatched_at) <= utc(shipment.delivered_at) <= utc(case.requested_at) <= utc(case.updated_at)
        assert utc(case.expected_return_at) > utc(case.requested_at)
    dashboard = (await logistics_client.get("/logistics/dashboard")).json()
    assert dashboard["data_mode"] == "demo"
    assert dashboard["metrics"]["total_shipments"] == 12
    assert dashboard["agent"]["carrier_connected"] is False
    results = (await logistics_client.get("/logistics/shipments", params={"risk_only": True, "page_size": 100})).json()
    assert all(item["issue_codes"] for item in results["items"])
    brief = (await logistics_client.get("/logistics/agent/brief")).json()
    assert "共 12 单" in brief["answer"] and "待发货" in brief["answer"] and "退货处理中" in brief["answer"]
    assert any(item["status"] == "pending_dispatch" for item in brief["citations"])
    assert "北京时间" in brief["citations"][0]["detail"]
    synonym = (await logistics_client.post("/logistics/agent/query", json={"question": "退单处理进度"})).json()
    assert len(synonym["citations"]) == len(cases)


async def test_events_are_database_append_only_and_terminal_dates_are_required(session, logistics_client, logistics_data):
    _, now = logistics_data
    shipment = await create_shipment(logistics_client, now)
    with pytest.raises(IntegrityError, match="append-only"):
        await session.execute(text("UPDATE logistics_events SET description='tampered' WHERE shipment_id=:id"), {"id": shipment["id"]})
    await session.rollback()
    with pytest.raises(IntegrityError, match="append-only"):
        await session.execute(text("DELETE FROM logistics_events WHERE shipment_id=:id"), {"id": shipment["id"]})
    await session.rollback()
    with pytest.raises(IntegrityError):
        await session.execute(text("UPDATE logistics_shipments SET status='delivered', dispatched_at=:now, delivered_at=NULL WHERE id=:id"), {"id": shipment["id"], "now": now})
    await session.rollback()


async def test_future_events_and_duplicate_tracking_are_rejected(logistics_client, logistics_data):
    _, now = logistics_data
    shipment = await create_shipment(logistics_client, now)
    response = await logistics_client.post(f"/logistics/shipments/{shipment['id']}/events", json={"action": "dispatch", "occurred_at": (now + timedelta(days=1)).isoformat(), "location": "上海", "description": "未来发货"})
    assert response.status_code == 422
    duplicate = await logistics_client.post("/logistics/shipments", json={"order_id": "log-order-c", "carrier": shipment["carrier"], "tracking_no": shipment["tracking_no"], "destination": "上海", "dispatch_due_at": now.isoformat(), "expected_delivery_at": (now + timedelta(days=1)).isoformat()})
    assert duplicate.status_code == 409


async def test_concurrent_patrols_create_one_task_and_stale_shipments_cannot_overwrite(tmp_path):
    from backend.logistics import patrol, shipment_rows
    from backend.logistics_models import ExceptionTask, Shipment, ShipmentEvent

    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'concurrent.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    try:
        async with factory() as session:
            session.add_all([Store(id="parallel-store", name="并发测试", code="parallel"), User(id="parallel-user", username="parallel", password_hash="unused", role=UserRole.ADMIN)])
            await session.flush()
            session.add(Order(id="parallel-order", store_id="parallel-store", ordered_at=now - timedelta(days=3), status=OrderStatus.PAID, total_amount=Decimal("10")))
            await session.flush()
            session.add(Shipment(id="parallel-shipment", order_id="parallel-order", store_id="parallel-store", carrier="test", tracking_no="parallel", destination="杭州", dispatch_due_at=now - timedelta(days=1), expected_delivery_at=now + timedelta(days=1)))
            await session.commit()
        ready = 0
        gate = asyncio.Event()

        async def run():
            nonlocal ready
            async with factory() as session:
                actor = await session.get(User, "parallel-user")
                rows = await shipment_rows(session, actor)
                ready += 1
                if ready == 2:
                    gate.set()
                await gate.wait()
                return await patrol(session, actor, rows, ["parallel-store"], now=now)

        runs = await asyncio.gather(run(), run())
        assert sum(run.created_exceptions for run in runs) == 1
        assert sum(run.existing_exceptions for run in runs) == 1
        async with factory() as one, factory() as two:
            assert await one.scalar(select(func.count()).select_from(ExceptionTask)) == 1
            assert await one.scalar(select(func.count()).select_from(ShipmentEvent).where(ShipmentEvent.event_type == "exception_detected")) == 1
            first = await one.get(Shipment, "parallel-shipment")
            stale = await two.get(Shipment, "parallel-shipment")
            first.last_location = "最新位置"
            await one.commit()
            stale.last_location = "过时位置"
            with pytest.raises(StaleDataError):
                await two.commit()
    finally:
        await engine.dispose()


async def test_all_logistics_routes_require_authentication():
    from backend.logistics import router

    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in ["dashboard", "shipments", "returns", "exceptions", "agent/runs", "agent/brief"]:
            assert (await client.get(f"/logistics/{path}")).status_code == 401


async def test_patrol_refreshes_and_locks_facts_changed_since_worker_snapshot(session, logistics_client, logistics_data):
    from backend.logistics import EventCreate, patrol, record_event, shipment_rows
    from backend.logistics_models import ExceptionTask

    _, now = logistics_data
    shipment = await create_shipment(logistics_client, now)
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    async with factory() as reader, factory() as writer:
        actor = await reader.get(User, "log-operator")
        rows = await shipment_rows(reader, actor)
        assert rows[0][0].status == "pending_dispatch"
        writer_actor = await writer.get(User, "log-operator")
        await record_event(shipment["id"], EventCreate(action="dispatch", occurred_at=now, location="杭州仓", description="已交接快递"), writer_actor, writer)
        assert rows[0][0].status == "pending_dispatch"
        run = await patrol(reader, actor, rows, ["log-a"])
        assert run.created_exceptions == 0
        assert run.existing_exceptions == 0
        assert await reader.scalar(select(func.count()).select_from(ExceptionTask)) == 0


async def test_hidden_store_facts_never_enter_details_actions_or_agent_evidence(session, logistics_client, logistics_data):
    from backend.logistics_models import ExceptionTask, Shipment
    from backend.logistics_seed import seed_logistics_demo

    _, now = logistics_data
    await seed_logistics_demo(session, now)
    hidden = await session.scalar(select(Shipment).where(Shipment.store_id == "log-b"))
    hidden_id, hidden_tracking = hidden.id, hidden.tracking_no
    hidden_task = await session.scalar(select(ExceptionTask).where(ExceptionTask.store_id == "log-b"))
    assert hidden_task is not None
    task_id = hidden_task.id
    assert (await logistics_client.get(f"/logistics/shipments/{hidden_id}")).status_code == 404
    mutation = await logistics_client.post(f"/logistics/shipments/{hidden_id}/events", json={"action": "dispatch", "occurred_at": now.isoformat(), "location": "杭州仓", "description": "越权登记"})
    assert mutation.status_code == 404
    assert (await logistics_client.patch(f"/logistics/exceptions/{task_id}", json={"action": "resolve", "resolution": "越权结案"})).status_code == 404
    queried = (await logistics_client.post("/logistics/agent/query", json={"question": hidden_tracking})).json()
    assert queried["citations"] == []
    for endpoint in ("shipments", "exceptions"):
        assert all(item["store_id"] == "log-a" for item in (await logistics_client.get(f"/logistics/{endpoint}")).json()["items"])
    runs = (await logistics_client.get("/logistics/agent/runs")).json()
    assert len(runs) == 1 and runs[0]["scanned_shipments"] == 2
    disabled = await session.get(Store, "log-a")
    disabled.enabled = False
    await session.commit()
    assert (await logistics_client.get("/logistics/dashboard")).json()["metrics"]["total_shipments"] == 0
    assert (await logistics_client.get(f"/logistics/shipments/{hidden_id}")).status_code == 404
