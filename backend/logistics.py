"""Scoped logistics operations and a deterministic, evidence-backed patrol agent."""

import hashlib
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from backend.auth import get_current_user, require_logistics_user, require_department, require_store_access, store_visibility_predicate
from backend.common import OrderStatus, UserDepartment, UserRole, UserStatus, utc_now
from backend.database import get_session
from backend.logistics_models import AgentRun, ExceptionTask, ReturnCase, Shipment, ShipmentEvent
from backend.models import Order, Store, User, UserStoreScope

router = APIRouter(prefix="/logistics", tags=["logistics"], dependencies=[Depends(require_logistics_user)])
ShipmentStatus = Literal["pending_dispatch", "in_transit", "delivered"]
ReturnStatus = Literal["requested", "approved", "in_transit", "received", "closed", "rejected"]
ExceptionStatus = Literal["open", "in_progress", "resolved"]
ExceptionKind = Literal["dispatch_overdue", "no_movement", "delivery_overdue", "return_overdue"]
ACTIVE_RETURNS = {"requested", "approved", "in_transit", "received"}
STATE_LABELS = {"pending_dispatch": "待发货", "in_transit": "运输中", "delivered": "已签收"}
RETURN_LABELS = {"requested": "待审核", "approved": "待寄回", "in_transit": "退回运输中", "received": "待质检", "closed": "已结案", "rejected": "已驳回"}
RULES = {
    "dispatch_overdue": ("发货超时", "核对仓库备货和快递交接情况，补录实际发货时间。"),
    "no_movement": ("轨迹停滞", "核实承运商最新扫描，确认包裹位置并记录跟进结果。"),
    "delivery_overdue": ("到货超时", "联系承运商核查派送进度，向客服同步预计到货时间。"),
    "return_overdue": ("退货处理超时", "核对退回包裹及仓库验收进度，补录轨迹或验收结果。"),
}


def utc(value: datetime) -> datetime:
    # SQLite drops timezone information; all persisted logistics dates are UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def iso(value: datetime | None) -> str | None:
    return utc(value).isoformat().replace("+00:00", "Z") if value is not None else None


def local_time(value: datetime) -> str:
    return utc(value).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M 北京时间")


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ShipmentCreate(Request):
    order_id: str = Field(min_length=1, max_length=36)
    carrier: str = Field(min_length=1, max_length=64)
    tracking_no: str = Field(min_length=1, max_length=100)
    destination: str = Field(min_length=1, max_length=128)
    dispatch_due_at: AwareDatetime
    expected_delivery_at: AwareDatetime

    @model_validator(mode="after")
    def chronological(self):
        if self.expected_delivery_at < self.dispatch_due_at:
            raise ValueError("预计到货时间不能早于发货截止时间")
        return self


class EventCreate(Request):
    action: Literal["dispatch", "transit", "deliver"]
    occurred_at: AwareDatetime
    location: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1000)


class ReturnCreate(Request):
    shipment_id: str = Field(min_length=1, max_length=36)
    reason: str = Field(min_length=1, max_length=500)
    expected_return_at: AwareDatetime


class ReturnTransition(Request):
    status: Literal["approved", "in_transit", "received", "closed", "rejected"]
    occurred_at: AwareDatetime
    carrier: str | None = Field(default=None, min_length=1, max_length=64)
    tracking_no: str | None = Field(default=None, min_length=1, max_length=100)
    note: str = Field(min_length=1, max_length=1000)


class ExceptionAction(Request):
    action: Literal["assign", "resolve"]
    assignee_id: str | None = Field(default=None, min_length=1, max_length=36)
    resolution: str | None = Field(default=None, min_length=1, max_length=1000)


class PatrolRequest(Request):
    store_id: str | None = Field(default=None, min_length=1, max_length=36)


class AgentQuestion(PatrolRequest):
    question: str = Field(min_length=1, max_length=1000)


async def _logistics_actor(session: AsyncSession, actor: User, *, lock: bool = False) -> User:
    statement = select(User).where(User.id == actor.id).execution_options(populate_existing=True)
    if lock:
        statement = statement.with_for_update()
    current = await session.scalar(statement)
    require_department(current, UserDepartment.LOGISTICS)
    return current


async def scope_ids(session: AsyncSession, actor: User, store_id: str | None) -> list[str]:
    actor = await _logistics_actor(session, actor)
    if store_id is not None:
        await require_store_access(store_id, actor, session)
        return [store_id]
    return list((await session.scalars(select(Store.id).where(Store.enabled.is_(True), store_visibility_predicate(actor, Store.id)).order_by(Store.id))).all())


async def shipment_rows(session: AsyncSession, actor: User, store_id: str | None = None):
    ids = await scope_ids(session, actor, store_id)
    # ponytail: aggregate the visible portfolio in memory; push metrics into SQL for large portfolios.
    return (await session.execute(select(Shipment, Store.name, Order.ordered_at).join(Store, Store.id == Shipment.store_id).join(Order, Order.id == Shipment.order_id).where(Shipment.store_id.in_(ids)).order_by(Shipment.created_at.desc(), Shipment.id))).all()


async def scoped_shipment(session: AsyncSession, actor: User, shipment_id: str, *, lock: bool = False):
    actor = await _logistics_actor(session, actor, lock=lock)
    statement = select(Shipment, Store.name, Order.ordered_at).join(Store, Store.id == Shipment.store_id).join(Order, Order.id == Shipment.order_id).where(Shipment.id == shipment_id, Store.enabled.is_(True), store_visibility_predicate(actor, Shipment.store_id))
    if lock:
        statement = statement.with_for_update(of=Shipment).execution_options(populate_existing=True)
    row = (await session.execute(statement)).first()
    if row is None:
        raise HTTPException(404, "物流记录不存在或不在可访问店铺内")
    return row


async def related(session: AsyncSession, shipments: list[Shipment]):
    ids = [shipment.id for shipment in shipments]
    returns = {case.shipment_id: case for case in (await session.scalars(select(ReturnCase).where(ReturnCase.shipment_id.in_(ids)))).all()}
    counts = Counter((await session.scalars(select(ExceptionTask.shipment_id).where(ExceptionTask.shipment_id.in_(ids), ExceptionTask.status != "resolved"))).all())
    return returns, counts


def issues(shipment: Shipment, case: ReturnCase | None, now: datetime) -> list[dict]:
    found = []

    def add(kind: str, anchor: datetime, description: str, severity: str = "high", return_id: str | None = None, threshold_hours: int | None = None):
        evidence = {"shipment_id": shipment.id, "order_id": shipment.order_id, "tracking_no": shipment.tracking_no, "status": shipment.status, "observed_at": iso(now), "trigger_at": iso(anchor), "threshold_hours": threshold_hours, "return_id": return_id}
        if case is not None and return_id:
            evidence["return_status"] = case.status
        found.append({"kind": kind, "severity": severity, "title": RULES[kind][0], "description": description, "recommended_action": RULES[kind][1], "evidence": evidence, "return_id": return_id,
                      "dedupe_key": hashlib.sha256(f"{shipment.id}:{kind}:{return_id or ''}:{iso(anchor)}".encode()).hexdigest()})

    if shipment.status == "pending_dispatch" and utc(shipment.dispatch_due_at) < now:
        hours = (now - utc(shipment.dispatch_due_at)).total_seconds() / 3600
        add("dispatch_overdue", shipment.dispatch_due_at, f"超过承诺发货时间 {hours:.1f} 小时，尚无发货记录。")
    if shipment.status == "in_transit":
        if utc(shipment.expected_delivery_at) < now:
            hours = (now - utc(shipment.expected_delivery_at)).total_seconds() / 3600
            add("delivery_overdue", shipment.expected_delivery_at, f"超过预计到货时间 {hours:.1f} 小时，尚无签收记录。")
        anchor = shipment.last_event_at or shipment.dispatched_at
        if anchor is not None and utc(anchor) < now - timedelta(hours=48):
            hours = (now - utc(anchor)).total_seconds() / 3600
            add("no_movement", anchor, f"最近 {hours:.1f} 小时没有新的物流扫描，阈值为 48 小时。", "medium", threshold_hours=48)
    if case is not None and case.status in ACTIVE_RETURNS:
        if case.status == "received":
            anchor = utc(case.received_at) + timedelta(hours=24)
            text = "退货已到仓超过 24 小时，尚未登记质检和结案。"
        else:
            anchor = utc(case.expected_return_at)
            text = f"超过预计退回时间 {(now - anchor).total_seconds() / 3600:.1f} 小时，尚未确认退货入仓。"
        if anchor < now:
            add("return_overdue", anchor, text, return_id=case.id)
    return found


def shipment_json(row, case: ReturnCase | None, open_count: int, now: datetime) -> dict:
    shipment, store_name, _ = row
    risks = issues(shipment, case, now)
    return {"id": shipment.id, "order_id": shipment.order_id, "order_number": shipment.order_id, "store_id": shipment.store_id, "store_name": store_name, "carrier": shipment.carrier, "tracking_no": shipment.tracking_no, "status": shipment.status, "destination": shipment.destination,
            "dispatch_due_at": iso(shipment.dispatch_due_at), "expected_delivery_at": iso(shipment.expected_delivery_at), "dispatched_at": iso(shipment.dispatched_at), "delivered_at": iso(shipment.delivered_at), "last_event_at": iso(shipment.last_event_at), "last_location": shipment.last_location,
            "risk_level": "high" if any(risk["severity"] == "high" for risk in risks) else "medium" if risks else "normal", "issue_codes": [risk["kind"] for risk in risks], "open_exception_count": open_count, "created_at": iso(shipment.created_at), "is_demo": shipment.is_demo}


def return_json(case: ReturnCase, shipment: Shipment, store_name: str) -> dict:
    return {"id": case.id, "shipment_id": case.shipment_id, "order_id": shipment.order_id, "order_number": shipment.order_id, "store_id": case.store_id, "store_name": store_name, "status": case.status, "reason": case.reason, "carrier": case.carrier, "tracking_no": case.tracking_no,
            "requested_at": iso(case.requested_at), "expected_return_at": iso(case.expected_return_at), "received_at": iso(case.received_at), "closed_at": iso(case.closed_at), "updated_at": iso(case.updated_at), "is_demo": case.is_demo, "shipment_tracking_no": shipment.tracking_no, "destination": shipment.destination}


def event_json(event: ShipmentEvent) -> dict:
    return {key: iso(getattr(event, key)) if key in {"occurred_at", "created_at"} else getattr(event, key) for key in ("id", "shipment_id", "return_id", "event_type", "occurred_at", "location", "description", "actor_name", "source", "created_at")}


def exception_json(row) -> dict:
    task, order_id, store_name, assignee_name = row
    result = {key: getattr(task, key) for key in ("id", "shipment_id", "return_id", "store_id", "kind", "severity", "status", "title", "description", "recommended_action", "evidence", "assignee_id", "resolution")}
    return {**result, "order_number": order_id, "store_name": store_name, "assignee_name": assignee_name, "created_at": iso(task.created_at), "resolved_at": iso(task.resolved_at)}


def task_select():
    return select(ExceptionTask, Shipment.order_id, Store.name, User.username).join(Shipment, Shipment.id == ExceptionTask.shipment_id).join(Store, Store.id == ExceptionTask.store_id).outerjoin(User, User.id == ExceptionTask.assignee_id)


async def detail(session: AsyncSession, actor: User, shipment_id: str) -> dict:
    row = await scoped_shipment(session, actor, shipment_id)
    cases, counts = await related(session, [row[0]])
    events = (await session.scalars(select(ShipmentEvent).where(ShipmentEvent.shipment_id == shipment_id).order_by(ShipmentEvent.created_at, ShipmentEvent.id))).all()
    tasks = (await session.execute(task_select().where(ExceptionTask.shipment_id == shipment_id).order_by(ExceptionTask.created_at.desc(), ExceptionTask.id))).all()
    return {**shipment_json(row, cases.get(shipment_id), counts[shipment_id], utc_now()), "events": [event_json(item) for item in events], "exceptions": [exception_json(item) for item in tasks], "return_case": return_json(cases[shipment_id], row[0], row[1]) if shipment_id in cases else None}


def add_event(session: AsyncSession, shipment: Shipment, actor: User | None, event_type: str, occurred_at: datetime, description: str, *, location: str = "", return_id: str | None = None, source: str = "manual"):
    session.add(ShipmentEvent(shipment_id=shipment.id, return_id=return_id, event_type=event_type, occurred_at=utc(occurred_at), location=location, description=description, actor_id=actor.id if actor else None, actor_name=actor.username if actor else "规则巡检 Agent", source=source))


async def commit(session: AsyncSession):
    try:
        await session.commit()
    except StaleDataError:
        await session.rollback()
        raise HTTPException(409, "记录已被另一位同事更新，请刷新后重试") from None
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "记录已存在或当前状态不允许此操作，请刷新后重试") from None


def check_time(value: datetime, earliest: datetime):
    if utc(value) > utc_now() + timedelta(seconds=5):
        raise HTTPException(422, "实际发生时间不能在未来")
    if utc(value) < utc(earliest):
        raise HTTPException(422, "发生时间不能早于上一物流节点或订单时间")


def page_of(items: list[dict], page: int, page_size: int) -> dict:
    return {"items": items[(page - 1) * page_size: page * page_size], "total": len(items), "page": page, "page_size": page_size}


@router.get("/orders")
async def eligible_orders(store_id: str | None = None, q: str = Query(default="", max_length=100), limit: int = Query(default=100, ge=1, le=500), actor: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    ids = await scope_ids(session, actor, store_id)
    query = select(Order, Store.name).join(Store).where(Order.store_id.in_(ids), Order.status != OrderStatus.CANCELLED, ~Order.id.in_(select(Shipment.order_id)))
    if q.strip():
        query = query.where(Order.id.contains(q.strip(), autoescape=True))
    rows = (await session.execute(query.order_by(Order.ordered_at.desc(), Order.id).limit(limit))).all()
    return [{"id": order.id, "store_id": order.store_id, "store_name": name, "ordered_at": iso(order.ordered_at), "status": order.status.value} for order, name in rows]


@router.get("/shipments")
async def list_shipments(store_id: str | None = None, status: ShipmentStatus | None = None, q: str = Query(default="", max_length=100), carrier: str | None = Query(default=None, max_length=64), risk_only: bool = False, page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100), actor: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    rows = await shipment_rows(session, actor, store_id)
    cases, counts = await related(session, [row[0] for row in rows])
    now = utc_now()
    result = []
    for row in rows:
        shipment = row[0]
        if status and shipment.status != status or carrier and shipment.carrier != carrier:
            continue
        if q.strip() and q.strip().casefold() not in " ".join([shipment.order_id, shipment.tracking_no, shipment.destination, shipment.carrier, row[1]]).casefold():
            continue
        value = shipment_json(row, cases.get(shipment.id), counts[shipment.id], now)
        if not risk_only or value["issue_codes"]:
            result.append(value)
    return page_of(result, page, page_size)


@router.post("/shipments", status_code=201)
async def create_shipment(payload: ShipmentCreate, actor: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    actor = await _logistics_actor(session, actor, lock=True)
    order = await session.scalar(select(Order).join(Store).where(Order.id == payload.order_id, Store.enabled.is_(True), store_visibility_predicate(actor, Order.store_id)))
    if order is None:
        raise HTTPException(404, "订单不存在或不在可访问店铺内")
    if order.status == OrderStatus.CANCELLED:
        raise HTTPException(409, "已取消订单不能创建物流单")
    if utc(payload.dispatch_due_at) < utc(order.ordered_at):
        raise HTTPException(422, "发货截止时间不能早于下单时间")
    shipment = Shipment(id=str(uuid4()), order_id=order.id, store_id=order.store_id, carrier=payload.carrier, tracking_no=payload.tracking_no, destination=payload.destination, dispatch_due_at=utc(payload.dispatch_due_at), expected_delivery_at=utc(payload.expected_delivery_at))
    session.add(shipment)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "订单已建立物流单，或此承运商的运单号已存在") from None
    shipment_id = shipment.id
    add_event(session, shipment, actor, "created", utc_now(), "建立物流跟踪记录；等待仓库发货。")
    await commit(session)
    return await detail(session, actor, shipment_id)


@router.get("/shipments/{shipment_id}")
async def get_shipment(shipment_id: str, actor: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await detail(session, actor, shipment_id)


@router.post("/shipments/{shipment_id}/events")
async def record_event(shipment_id: str, payload: EventCreate, actor: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    shipment, _, ordered_at = await scoped_shipment(session, actor, shipment_id, lock=True)
    allowed = {"pending_dispatch": {"dispatch"}, "in_transit": {"transit", "deliver"}, "delivered": set()}
    if payload.action not in allowed[shipment.status]:
        raise HTTPException(409, "当前物流状态不允许此操作；已签收记录不可回退")
    check_time(payload.occurred_at, shipment.last_event_at or ordered_at)
    if payload.action == "dispatch":
        shipment.status, shipment.dispatched_at = "in_transit", utc(payload.occurred_at)
    elif payload.action == "deliver":
        shipment.status, shipment.delivered_at = "delivered", utc(payload.occurred_at)
    shipment.last_event_at, shipment.last_location = utc(payload.occurred_at), payload.location
    add_event(session, shipment, actor, payload.action, payload.occurred_at, payload.description, location=payload.location)
    await commit(session)
    return await detail(session, actor, shipment_id)


@router.get("/returns")
async def list_returns(store_id: str | None = None, status: ReturnStatus | None = None, q: str = Query(default="", max_length=100), page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100), actor: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    ids = await scope_ids(session, actor, store_id)
    query = select(ReturnCase, Shipment, Store.name).join(Shipment, Shipment.id == ReturnCase.shipment_id).join(Store, Store.id == ReturnCase.store_id).where(ReturnCase.store_id.in_(ids))
    if status:
        query = query.where(ReturnCase.status == status)
    if q.strip():
        query = query.where(or_(Shipment.order_id.contains(q.strip(), autoescape=True), Shipment.tracking_no.contains(q.strip(), autoescape=True), ReturnCase.tracking_no.contains(q.strip(), autoescape=True), ReturnCase.reason.contains(q.strip(), autoescape=True)))
    rows = (await session.execute(query.order_by(ReturnCase.requested_at.desc(), ReturnCase.id))).all()
    return page_of([return_json(*row) for row in rows], page, page_size)


@router.post("/returns", status_code=201)
async def create_return(payload: ReturnCreate, actor: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    shipment, name, _ = await scoped_shipment(session, actor, payload.shipment_id, lock=True)
    if shipment.status == "pending_dispatch":
        raise HTTPException(409, "尚未发货的物流单无需退回包裹")
    now = utc_now()
    if utc(payload.expected_return_at) <= now:
        raise HTTPException(422, "预计退回时间必须晚于申请时间")
    case = ReturnCase(id=str(uuid4()), shipment_id=shipment.id, store_id=shipment.store_id, reason=payload.reason, expected_return_at=utc(payload.expected_return_at), requested_at=now, updated_at=now, is_demo=shipment.is_demo)
    session.add(case)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "此物流单已有退货申请") from None
    add_event(session, shipment, actor, "return_requested", now, payload.reason, return_id=case.id)
    await commit(session)
    return return_json(case, shipment, name)


async def scoped_return(session: AsyncSession, actor: User, return_id: str):
    actor = await _logistics_actor(session, actor, lock=True)
    statement = select(ReturnCase, Shipment, Store.name).join(Shipment, Shipment.id == ReturnCase.shipment_id).join(Store, Store.id == ReturnCase.store_id).where(ReturnCase.id == return_id, Store.enabled.is_(True), store_visibility_predicate(actor, ReturnCase.store_id))
    result = (await session.execute(statement)).first()
    if result is None:
        raise HTTPException(404, "退货记录不存在或不在可访问店铺内")
    # All writes lock shipment before its return/task, matching patrol's lock order.
    await scoped_shipment(session, actor, result[1].id, lock=True)
    return (await session.execute(statement.with_for_update(of=ReturnCase).execution_options(populate_existing=True))).one()


@router.post("/returns/{return_id}/transition")
async def transition_return(return_id: str, payload: ReturnTransition, actor: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    case, shipment, name = await scoped_return(session, actor, return_id)
    allowed = {"requested": {"approved", "rejected"}, "approved": {"in_transit"}, "in_transit": {"received"}, "received": {"closed"}, "closed": set(), "rejected": set()}
    if payload.status not in allowed[case.status]:
        raise HTTPException(409, "退货必须按申请、审核、寄回、入仓、质检结案的顺序推进")
    check_time(payload.occurred_at, case.updated_at)
    if payload.status == "in_transit":
        if not payload.carrier or not payload.tracking_no:
            raise HTTPException(422, "寄回时必须填写承运商和退货运单号")
        case.carrier, case.tracking_no = payload.carrier, payload.tracking_no
    elif payload.carrier is not None or payload.tracking_no is not None:
        raise HTTPException(422, "承运商和运单号只在退货寄回时登记")
    case.status, case.updated_at = payload.status, utc(payload.occurred_at)
    if payload.status == "received":
        case.received_at = utc(payload.occurred_at)
    elif payload.status == "closed":
        case.closed_at = utc(payload.occurred_at)
    note = f"质检结案（仅登记物流结果，未执行退款）：{payload.note}" if payload.status == "closed" else payload.note
    add_event(session, shipment, actor, f"return_{payload.status}", payload.occurred_at, note, return_id=case.id)
    await commit(session)
    return return_json(case, shipment, name)


@router.get("/assignees")
async def assignees(store_id: str, actor: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    await scope_ids(session, actor, store_id)
    users = (await session.scalars(select(User).where(User.status == UserStatus.ACTIVE, or_(User.role == UserRole.ADMIN, User.department == UserDepartment.LOGISTICS), or_(User.role == UserRole.ADMIN, User.id.in_(select(UserStoreScope.user_id).where(UserStoreScope.store_id == store_id)))).order_by(User.username))).all()
    return [{"id": user.id, "username": user.username} for user in users]


@router.get("/exceptions")
async def list_exceptions(store_id: str | None = None, status: ExceptionStatus | None = None, kind: ExceptionKind | None = None, page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100), actor: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    ids = await scope_ids(session, actor, store_id)
    query = task_select().where(ExceptionTask.store_id.in_(ids))
    if status:
        query = query.where(ExceptionTask.status == status)
    if kind:
        query = query.where(ExceptionTask.kind == kind)
    rows = (await session.execute(query.order_by(ExceptionTask.created_at.desc(), ExceptionTask.id))).all()
    return page_of([exception_json(row) for row in rows], page, page_size)


@router.patch("/exceptions/{task_id}")
async def update_exception(task_id: str, payload: ExceptionAction, actor: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    actor = await _logistics_actor(session, actor, lock=True)
    statement = task_select().where(ExceptionTask.id == task_id, Store.enabled.is_(True), store_visibility_predicate(actor, ExceptionTask.store_id))
    row = (await session.execute(statement)).first()
    if row is None:
        raise HTTPException(404, "异常任务不存在或不在可访问店铺内")
    shipment = (await scoped_shipment(session, actor, row[0].shipment_id, lock=True))[0]
    row = (await session.execute(statement.with_for_update(of=ExceptionTask).execution_options(populate_existing=True))).one()
    task = row[0]
    if task.status == "resolved":
        raise HTTPException(409, "已处理异常不可再次修改")
    now = utc_now()
    if payload.action == "assign":
        if not payload.assignee_id or payload.resolution is not None:
            raise HTTPException(422, "指派操作需要负责人，不能填写结案记录")
        assignee = await session.scalar(select(User).where(User.id == payload.assignee_id, User.status == UserStatus.ACTIVE, or_(User.role == UserRole.ADMIN, User.department == UserDepartment.LOGISTICS), or_(User.role == UserRole.ADMIN, User.id.in_(select(UserStoreScope.user_id).where(UserStoreScope.store_id == task.store_id)))).execution_options(populate_existing=True).with_for_update())
        if assignee is None:
            raise HTTPException(422, "负责人必须是可访问该店铺的有效用户")
        task.assignee_id, task.status = assignee.id, "in_progress"
        note = f"异常任务 {task.id}（{task.title}）指派给 {assignee.username}。"
    else:
        if not payload.resolution or payload.assignee_id is not None:
            raise HTTPException(422, "处理异常必须填写结果；负责人请通过指派操作修改")
        task.status, task.resolved_at, task.resolution = "resolved", now, payload.resolution
        note = f"异常任务 {task.id}（{task.title}）已处理：{payload.resolution}"
    add_event(session, shipment, actor, f"exception_{payload.action}", now, note, return_id=task.return_id)
    await commit(session)
    return exception_json((await session.execute(task_select().where(ExceptionTask.id == task_id))).one())


def run_json(run: AgentRun) -> dict:
    return {key: iso(getattr(run, key)) if key in {"started_at", "completed_at"} else getattr(run, key) for key in ("id", "status", "mode", "started_at", "completed_at", "scanned_shipments", "created_exceptions", "existing_exceptions", "summary")}


async def visible_runs(session: AsyncSession, actor: User, ids: list[str]) -> list[AgentRun]:
    allowed = set(ids) & set(await scope_ids(session, actor, None))
    query = select(AgentRun).order_by(AgentRun.started_at.desc(), AgentRun.id)
    if actor.role != UserRole.ADMIN:
        query = query.where(or_(AgentRun.created_by == actor.id, AgentRun.created_by.is_(None)))
    return [run for run in (await session.scalars(query)).all() if set(run.scope_store_ids).issubset(allowed)]


async def patrol(session: AsyncSession, actor: User | None, rows, ids: list[str], *, now: datetime | None = None) -> AgentRun:
    if actor is not None:
        actor = await _logistics_actor(session, actor, lock=True)
        if not set(ids).issubset(await scope_ids(session, actor, None)):
            raise HTTPException(403, "物流巡检店铺权限已变更")
    shipment_ids = [row[0].id for row in rows]
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite" and shipment_ids:
        # SQLite has one writer: reserve that lock before refreshing the worker snapshot.
        await session.execute(update(Shipment).where(Shipment.id.in_(shipment_ids)).values(version=Shipment.version).execution_options(synchronize_session=False))
    statement = select(Shipment, Store.name, Order.ordered_at).join(Store, Store.id == Shipment.store_id).join(Order, Order.id == Shipment.order_id).where(Shipment.id.in_(shipment_ids), Shipment.store_id.in_(ids), Store.enabled.is_(True)).order_by(Shipment.id)
    if actor is not None:
        statement = statement.where(store_visibility_predicate(actor, Shipment.store_id))
    rows = (await session.execute(statement.with_for_update(of=Shipment).execution_options(populate_existing=True))).all()
    shipment_ids = [row[0].id for row in rows]
    cases = {case.shipment_id: case for case in (await session.scalars(select(ReturnCase).where(ReturnCase.shipment_id.in_(shipment_ids)).order_by(ReturnCase.shipment_id, ReturnCase.id).with_for_update(of=ReturnCase).execution_options(populate_existing=True))).all()}
    now = utc(now or utc_now())
    created = existing = 0
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    for row in rows:
        shipment = row[0]
        for issue in issues(shipment, cases.get(shipment.id), now):
            task_id = str(uuid4())
            values = {**issue, "id": task_id, "shipment_id": shipment.id, "store_id": shipment.store_id, "status": "open", "created_at": now, "resolution": "", "version": 1}
            inserted = (await session.execute(insert(ExceptionTask).values(**values).on_conflict_do_nothing(index_elements=["dedupe_key"]).returning(ExceptionTask.id))).scalar_one_or_none()
            if inserted:
                created += 1
                add_event(session, shipment, actor, "exception_detected", now, f"规则巡检发现{issue['title']}：{issue['description']} 任务 {task_id}。", return_id=issue["return_id"], source="agent")
            else:
                existing += 1
    summary = f"已巡检 {len(rows)} 单，新建 {created} 条异常任务，已有 {existing} 条相同证据记录。仅分析已登记的物流事实，未调用承运商接口。"
    run = AgentRun(id=str(uuid4()), created_by=actor.id if actor else None, scope_store_ids=ids, status="completed", mode="deterministic_rules", started_at=now, completed_at=utc_now(), scanned_shipments=len(rows), created_exceptions=created, existing_exceptions=existing, summary=summary)
    session.add(run)
    await commit(session)
    return run


@router.post("/agent/patrol")
async def run_patrol(payload: PatrolRequest, actor: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    ids = await scope_ids(session, actor, payload.store_id)
    rows = await shipment_rows(session, actor, payload.store_id)
    return run_json(await patrol(session, actor, rows, ids))


@router.get("/agent/runs")
async def agent_runs(store_id: str | None = None, limit: int = Query(default=20, ge=1, le=100), actor: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return [run_json(run) for run in (await visible_runs(session, actor, await scope_ids(session, actor, store_id)))[:limit]]


@router.get("/dashboard")
async def dashboard(store_id: str | None = None, actor: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    ids = await scope_ids(session, actor, store_id)
    rows = await shipment_rows(session, actor, store_id)
    shipments = [row[0] for row in rows]
    cases, _ = await related(session, shipments)
    now = utc_now()
    local_today = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    counts = Counter(shipment.status for shipment in shipments)
    delivered = [shipment for shipment in shipments if shipment.status == "delivered"]
    on_time = lambda items: round(100 * sum(utc(item.delivered_at) <= utc(item.expected_delivery_at) for item in items) / len(items), 1) if items else None
    tasks = (await session.execute(task_select().where(ExceptionTask.store_id.in_(ids), ExceptionTask.status != "resolved").order_by(ExceptionTask.severity, ExceptionTask.created_at, ExceptionTask.id))).all()
    events = (await session.scalars(select(ShipmentEvent).join(Shipment, Shipment.id == ShipmentEvent.shipment_id).where(Shipment.store_id.in_(ids)).order_by(ShipmentEvent.created_at.desc(), ShipmentEvent.id).limit(8))).all()
    carriers = defaultdict(list)
    for shipment in shipments:
        carriers[shipment.carrier].append(shipment)
    runs = await visible_runs(session, actor, ids)
    modes = {shipment.is_demo for shipment in shipments}
    return {"as_of": iso(now), "metrics": {"total_shipments": len(shipments), "pending_dispatch": counts["pending_dispatch"], "in_transit": counts["in_transit"], "delivered_today": sum(utc(shipment.delivered_at).astimezone(ZoneInfo("Asia/Shanghai")).date() == local_today for shipment in delivered), "open_exceptions": len(tasks), "active_returns": sum(case.status in ACTIVE_RETURNS for case in cases.values()), "on_time_rate": on_time(delivered)},
            "status_distribution": [{"status": status, "count": counts[status]} for status in ("pending_dispatch", "in_transit", "delivered")],
            "carriers": [{"carrier": name, "total": len(items), "delivered": sum(item.status == "delivered" for item in items), "on_time_rate": on_time([item for item in items if item.status == "delivered"])} for name, items in sorted(carriers.items(), key=lambda pair: (-len(pair[1]), pair[0]))],
            "recent_exceptions": [exception_json(row) for row in tasks[:6]], "recent_events": [event_json(event) for event in events],
            "agent": {"mode": "deterministic_rules", "label": "规则巡检 Agent", "carrier_connected": False, "last_run_at": iso(runs[0].completed_at) if runs else None, "last_run_status": runs[0].status if runs else None}, "data_mode": "empty" if not modes else "demo" if modes == {True} else "operational" if modes == {False} else "mixed"}


async def answer_question(session: AsyncSession, actor: User, question: str, store_id: str | None) -> dict:
    rows = await shipment_rows(session, actor, store_id)
    cases, counts = await related(session, [row[0] for row in rows])
    now = utc_now()
    normalized = question.casefold()
    focus = "summary" if any(word in normalized for word in ("日报", "简报", "概况", "汇总", "summary", "brief")) else "return" if any(word in normalized for word in ("退货", "退回", "退单", "return")) else "dispatch" if any(word in normalized for word in ("发货", "dispatch")) else "delivery" if any(word in normalized for word in ("到货", "到达", "签收", "delivery")) else "stalled" if any(word in normalized for word in ("停滞", "轨迹", "tracking")) else "summary"
    direct = [row for row in rows if row[0].order_id.casefold() in normalized or row[0].tracking_no.casefold() in normalized]
    supported = direct or any(word in normalized for word in ("退货", "退回", "退单", "发货", "到货", "到达", "签收", "停滞", "轨迹", "物流", "异常", "超时", "日报", "简报", "概况", "情况", "汇总", "今天", "风险", "任务", "return", "dispatch", "delivery", "tracking", "summary", "brief"))
    matched = []
    for row in direct or rows:
        shipment = row[0]
        case = cases.get(shipment.id)
        risks = issues(shipment, case, now)
        kinds = {risk["kind"] for risk in risks}
        overdue_question = any(word in normalized for word in ("超时", "逾期", "异常", "延误", "overdue", "late"))
        include = bool(direct)
        if not direct and supported:
            if focus == "return":
                include = case is not None and ("return_overdue" in kinds if overdue_question else True)
            elif focus == "dispatch":
                include = "dispatch_overdue" in kinds if overdue_question else shipment.status == "pending_dispatch"
            elif focus == "delivery":
                include = "delivery_overdue" in kinds if overdue_question else shipment.status in {"in_transit", "delivered"}
            elif focus == "stalled":
                include = "no_movement" in kinds if "停滞" in normalized else shipment.status == "in_transit"
            else:
                include = bool(risks) or counts[shipment.id] > 0
        if include:
            facts = [f"{STATE_LABELS[shipment.status]}；承运商 {shipment.carrier}；目的地 {shipment.destination}", f"发货截止 {local_time(shipment.dispatch_due_at)}；预计到货 {local_time(shipment.expected_delivery_at)}"]
            if shipment.dispatched_at:
                facts.append(f"实际发货 {local_time(shipment.dispatched_at)}")
            if shipment.delivered_at:
                facts.append(f"实际签收 {local_time(shipment.delivered_at)}")
            if shipment.last_event_at:
                facts.append(f"最后物流节点 {local_time(shipment.last_event_at)}，{shipment.last_location}")
            if case:
                facts.append(f"退货状态 {RETURN_LABELS[case.status]}；预计退回 {local_time(case.expected_return_at)}")
            facts.extend(risk["description"] for risk in risks)
            matched.append({"shipment_id": shipment.id, "order_number": shipment.order_id, "tracking_no": shipment.tracking_no, "store_name": row[1], "status": shipment.status, "detail": "；".join(facts)})
    count = len(matched)
    if not supported:
        answer = "目前支持物流概况、发货超时、到货延误、轨迹停滞、退货进度，以及按完整订单号或运单号查询。请在这些范围内提问。"
    elif direct:
        answer = f"找到 {count} 单匹配的物流记录，详细时间、进度和风险见下方订单证据。"
    elif focus == "summary":
        risky = sum(bool(issues(row[0], cases.get(row[0].id), now)) for row in rows)
        answer = f"当前可访问店铺共 {len(rows)} 单：待发货 {sum(row[0].status == 'pending_dispatch' for row in rows)} 单，在途 {sum(row[0].status == 'in_transit' for row in rows)} 单，已签收 {sum(row[0].status == 'delivered' for row in rows)} 单；{risky} 单触发风险规则，{sum(counts.values())} 条异常任务尚待处理，{sum(case.status in ACTIVE_RETURNS for case in cases.values())} 单退货处理中。"
    else:
        topic = {"return": "退货", "dispatch": "发货", "delivery": "到货", "stalled": "运输轨迹"}[focus]
        answer = f"根据已登记事实，找到 {count} 单符合此次{topic}查询。" if count else f"当前可访问店铺中没有符合此次{topic}查询的记录。"
    return {"answer": answer + " 数据来自本系统登记记录；规则巡检未连接实时承运商，也未调用大模型。", "mode": "deterministic_rules", "as_of": iso(now), "citations": matched, "suggestions": ["哪些订单发货超时？", "哪些包裹轨迹停滞？", "退货处理进度如何？", "生成今日物流简报"]}


@router.post("/agent/query")
async def agent_query(payload: AgentQuestion, actor: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer_question(session, actor, payload.question, payload.store_id)


@router.get("/agent/brief")
async def agent_brief(store_id: str | None = None, actor: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer_question(session, actor, "生成今日物流简报", store_id)
