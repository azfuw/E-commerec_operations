"""Exercise department boundaries with real tokens and persisted permissions."""

from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select, update

from backend.auth import hash_password
from backend.common import UserDepartment, UserRole, WorkflowStatus
from backend.models import ApprovalAction, AuditEvent, Product, PublishRecord, User, UserStoreScope, WorkflowRun
from tests.test_auth_and_scope import auth_data, client
from tests.test_manual_review_api import manual_client, manual_route_data
from tests.test_logistics import logistics_data


async def login_headers(client, username):
    response = await client.post("/auth/login", json={"username": username, "password": "DemoPass!2026"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}", "Idempotency-Key": "department-test"}


async def test_current_token_uses_admin_updated_department_and_shared_routes(client, session, auth_data):
    operator = await login_headers(client, "operator")
    admin = await login_headers(client, "admin")
    assert (await client.get("/workbench/tasks", headers=operator)).status_code == 200
    changed = await client.patch("/admin/users/operator-user", headers=admin, json={"department": "logistics"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["department"] == "logistics"
    assert (await client.get("/auth/me", headers=operator)).json()["department"] == "logistics"
    assert (await client.get("/stores", headers=operator)).status_code == 200
    assert (await client.get("/workbench/tasks", headers=operator)).status_code == 403
    assert (await client.get("/logistics/dashboard?store_id=flagship", headers=operator)).status_code == 200
    assert (await client.get("/logistics/dashboard?store_id=other-store", headers=operator)).status_code == 403
    event = await session.scalar(select(AuditEvent).where(AuditEvent.resource_id == "operator-user"))
    assert event.details["from_department"] == "operations"
    assert event.details["to_department"] == "logistics"
    assert event.details["changed_fields"] == ["department"]


@pytest.mark.parametrize("path", [
    "/workbench/tasks", "/stores/flagship/products", "/workflow-runs/missing",
    "/analysis-runs/missing/candidates", "/proposals/missing", "/approvals",
    "/knowledge/documents", "/audit-events", "/agent-evaluations/runs", "/agent-calls",
])
async def test_logistics_supervisor_cannot_read_operations(client, session, auth_data, path):
    auth_data["operator"].role = UserRole.SUPERVISOR
    auth_data["operator"].department = UserDepartment.LOGISTICS
    await session.commit()
    headers = await login_headers(client, "operator")
    assert (await client.get(path, headers=headers)).status_code == 403


async def test_logistics_approval_denied_without_mutation(manual_client, session, manual_route_data):
    operator = await login_headers(manual_client, "operator")
    supervisor = await login_headers(manual_client, "supervisor")
    submitted = await manual_client.post("/proposals/proposal-1/submit", headers=operator, json={"revision_id": "revision-1"})
    assert submitted.status_code == 201, submitted.text
    await session.execute(update(User).where(User.id == "supervisor-1").values(department=UserDepartment.LOGISTICS))
    await session.commit()
    before = [await session.scalar(select(func.count()).select_from(model)) for model in (ApprovalAction, AuditEvent, PublishRecord)]
    denied = await manual_client.post("/approvals/proposal-1/approve", headers=supervisor, json={"revision_id": "revision-1"})
    assert denied.status_code == 403, denied.text
    assert [await session.scalar(select(func.count()).select_from(model)) for model in (ApprovalAction, AuditEvent, PublishRecord)] == before
    assert (await session.get(Product, "product-1", populate_existing=True)).current_version == 7
    assert (await session.get(WorkflowRun, "optimization-1", populate_existing=True)).status == WorkflowStatus.PENDING_APPROVAL


async def test_internal_approval_checks_department_before_side_effects(session, manual_route_data):
    from backend.approvals import ApprovalDomainError, submit_proposal
    from backend.schemas import ProposalActionRequest

    manual_route_data["supervisor"].department = UserDepartment.LOGISTICS
    await session.commit()
    with pytest.raises(ApprovalDomainError) as error:
        await submit_proposal(session, actor_id="supervisor-1", proposal_id="proposal-1",
            request=ProposalActionRequest(revision_id="revision-1"), idempotency_key="internal", request_id="internal")
    assert error.value.status_code == 403
    assert await session.scalar(select(func.count()).select_from(ApprovalAction)) == 0


async def test_internal_workbench_checks_department(session, auth_data):
    from backend.workbench import WorkbenchDomainError, list_workbench_tasks

    auth_data["operator"].department = UserDepartment.LOGISTICS
    await session.commit()
    with pytest.raises(WorkbenchDomainError) as error:
        await list_workbench_tasks(session, actor_id="operator-user", page=1, page_size=20, store_id=None, kind=None, status=None)
    assert error.value.status_code == 403


async def test_admin_bypasses_department_but_keeps_action_scope(manual_client, session, manual_route_data):
    manual_route_data["admin"].department = UserDepartment.LOGISTICS
    await session.execute(delete(UserStoreScope).where(UserStoreScope.user_id == "admin-1"))
    await session.commit()
    headers = await login_headers(manual_client, "admin")
    assert (await manual_client.get("/workbench/tasks", headers=headers)).status_code == 200
    assert (await manual_client.get("/logistics/dashboard", headers=headers)).status_code == 200
    response = await manual_client.post("/proposals/proposal-1/submit", headers=headers, json={"revision_id": "revision-1"})
    assert response.status_code == 404


async def test_internal_logistics_rejects_operations_actor(session, auth_data):
    from backend.logistics import shipment_rows, patrol
    from backend.logistics_models import AgentRun

    with pytest.raises(HTTPException) as error:
        await shipment_rows(session, auth_data["operator"])
    assert error.value.status_code == 403
    with pytest.raises(HTTPException):
        await patrol(session, auth_data["operator"], [], ["flagship"])
    assert await session.scalar(select(func.count()).select_from(AgentRun)) == 0


async def test_logistics_assignments_exclude_operations_and_refresh_stale_permission(manual_client, session, logistics_data):
    from backend.logistics import patrol, shipment_rows
    from backend.logistics_models import AgentRun, ExceptionTask, Shipment, ShipmentEvent

    actor, now = logistics_data
    actor.password_hash = hash_password("DemoPass!2026")
    operations = User(id="operations-peer", username="operations-peer", password_hash="unused", role=UserRole.SUPERVISOR)
    admin = User(id="department-admin", username="department-admin", password_hash="unused", role=UserRole.ADMIN)
    session.add_all([operations, admin])
    await session.flush()
    session.add(UserStoreScope(user_id=operations.id, store_id="log-a"))
    await session.commit()
    headers = await login_headers(manual_client, "log-operator")
    created = await manual_client.post("/logistics/shipments", headers=headers, json={
        "order_id": "log-order-a", "carrier": "Test", "tracking_no": "department-tracking", "destination": "上海",
        "dispatch_due_at": (now - timedelta(days=2)).isoformat(), "expected_delivery_at": (now + timedelta(days=2)).isoformat(),
    })
    assert created.status_code == 201, created.text
    shipment_id = created.json()["id"]
    assert (await manual_client.post("/logistics/agent/patrol", headers=headers, json={})).status_code == 200
    assignees = await manual_client.get("/logistics/assignees?store_id=log-a", headers=headers)
    assert {item["id"] for item in assignees.json()} == {"log-operator", "department-admin"}
    task = await session.scalar(select(ExceptionTask).where(ExceptionTask.shipment_id == shipment_id))
    task_id = task.id
    before_events = await session.scalar(select(func.count()).select_from(ShipmentEvent))
    forbidden_assignment = await manual_client.patch(f"/logistics/exceptions/{task_id}", headers=headers,
        json={"action": "assign", "assignee_id": "operations-peer"})
    assert forbidden_assignment.status_code == 422
    assert (await session.get(ExceptionTask, task_id, populate_existing=True)).assignee_id is None
    assert await session.scalar(select(func.count()).select_from(ShipmentEvent)) == before_events
    snapshot = await shipment_rows(session, actor)
    await session.execute(update(User).where(User.id == actor.id).values(department=UserDepartment.OPERATIONS).execution_options(synchronize_session=False))
    await session.commit()
    before = [await session.scalar(select(func.count()).select_from(model)) for model in (AgentRun, ExceptionTask, ShipmentEvent, Shipment)]
    with pytest.raises(HTTPException) as error:
        await patrol(session, actor, snapshot, ["log-a"])
    assert error.value.status_code == 403
    for path, payload in [
        (f"/logistics/shipments/{shipment_id}/events", {"action": "dispatch", "occurred_at": now.isoformat(), "location": "仓库", "description": "越权"}),
        ("/logistics/agent/patrol", {}),
    ]:
        assert (await manual_client.post(path, headers=headers, json=payload)).status_code == 403
    assert [await session.scalar(select(func.count()).select_from(model)) for model in (AgentRun, ExceptionTask, ShipmentEvent, Shipment)] == before
    assert (await session.get(Shipment, shipment_id, populate_existing=True)).status == "pending_dispatch"
