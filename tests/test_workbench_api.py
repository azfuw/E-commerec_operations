from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import SQLAlchemyError

from backend.auth import create_access_token, hash_password
from backend.common import (
    ProposalRevisionOrigin,
    UserRole,
    UserStatus,
    WorkflowQuality,
    WorkflowStatus,
    WorkflowType,
)
from backend.config import get_settings
from backend.database import get_session
from backend.main import create_app
from backend.models import (
    AnalysisCandidate,
    ManualReviewRun,
    Product,
    ProductProposal,
    ProposalRevision,
    Store,
    User,
    UserStoreScope,
    WorkflowRun,
)


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user, get_settings())}"}


def _run(
    run_id: str,
    *,
    workflow_type: WorkflowType,
    store_id: str = "store-1",
    status: WorkflowStatus,
    created_by: str = "operator-1",
    updated_at: datetime,
) -> WorkflowRun:
    return WorkflowRun(
        id=run_id,
        workflow_type=workflow_type,
        store_id=store_id,
        created_by=created_by,
        start_date=date(2026, 8, 1) if workflow_type is WorkflowType.ANALYSIS else None,
        end_date=date(2026, 8, 31) if workflow_type is WorkflowType.ANALYSIS else None,
        status=status,
        quality_status=WorkflowQuality.NORMAL,
        current_step="review" if workflow_type is not WorkflowType.ANALYSIS else "rank",
        input={},
        updated_at=updated_at,
    )


def _candidate(candidate_id: str, run_id: str, product_id: str) -> AnalysisCandidate:
    return AnalysisCandidate(
        id=candidate_id,
        workflow_run_id=run_id,
        product_id=product_id,
        rank=1,
        product_code=product_id.upper(),
        anomaly_types=["low_conversion"],
        metrics={},
        business_impact=Decimal("1.00"),
        evidence=["trusted"],
        impact_explanation="影响",
        reason="原因",
        recommended_action="处理",
        confidence=Decimal("0.8000"),
    )


@pytest_asyncio.fixture
async def workbench_data(session) -> dict[str, object]:
    password_hash = hash_password("DemoPass!2026")
    operator = User(
        id="operator-1",
        username="workbench-operator",
        password_hash=password_hash,
        role=UserRole.OPERATOR,
    )
    supervisor = User(
        id="supervisor-1",
        username="workbench-supervisor",
        password_hash=password_hash,
        role=UserRole.SUPERVISOR,
    )
    admin = User(
        id="admin-1",
        username="workbench-admin",
        password_hash=password_hash,
        role=UserRole.ADMIN,
    )
    other_operator = User(
        id="other-operator",
        username="workbench-other",
        password_hash=password_hash,
        role=UserRole.OPERATOR,
    )
    unscoped_user = User(
        id="unscoped-user",
        username="workbench-unscoped",
        password_hash=password_hash,
        role=UserRole.OPERATOR,
    )
    disabled_user = User(
        id="disabled-user",
        username="workbench-disabled",
        password_hash=password_hash,
        role=UserRole.OPERATOR,
        status=UserStatus.DISABLED,
    )
    store = Store(id="store-1", name="目标店铺", code="target")
    other_store = Store(id="store-2", name="其他店铺", code="other")
    disabled_store = Store(
        id="disabled-store", name="停用店铺", code="disabled", enabled=False
    )
    session.add_all(
        [
            operator,
            supervisor,
            admin,
            other_operator,
            unscoped_user,
            disabled_user,
            store,
            other_store,
            disabled_store,
        ]
    )
    await session.flush()
    products = [
        Product(
            id="product-1",
            store_id=store.id,
            code="PRODUCT-1",
            title="商品一",
            category="数码",
        ),
        Product(
            id="product-2",
            store_id=store.id,
            code="PRODUCT-2",
            title="商品二",
            category="数码",
        ),
        Product(
            id="other-product",
            store_id=other_store.id,
            code="OTHER-1",
            title="其他商品",
            category="数码",
        ),
    ]
    session.add_all(products)
    await session.flush()
    session.add_all(
        [
            UserStoreScope(user_id=operator.id, store_id=store.id),
            UserStoreScope(user_id=supervisor.id, store_id=store.id),
            UserStoreScope(user_id=admin.id, store_id=store.id),
            UserStoreScope(user_id=disabled_user.id, store_id=store.id),
            UserStoreScope(user_id=other_operator.id, store_id=other_store.id),
        ]
    )
    await session.flush()

    analysis_awaiting = _run(
        "analysis-awaiting-selection",
        workflow_type=WorkflowType.ANALYSIS,
        status=WorkflowStatus.AWAITING_SELECTION,
        updated_at=datetime(2026, 9, 1, 11, 0),
    )
    analysis_with_proposal = _run(
        "analysis-with-proposal",
        workflow_type=WorkflowType.ANALYSIS,
        status=WorkflowStatus.COMPLETED,
        updated_at=datetime(2026, 9, 1, 9, 0),
    )
    analysis_running_source = _run(
        "analysis-running-source",
        workflow_type=WorkflowType.ANALYSIS,
        status=WorkflowStatus.COMPLETED,
        updated_at=datetime(2026, 9, 1, 8, 0),
    )
    analysis_other = _run(
        "analysis-other",
        workflow_type=WorkflowType.ANALYSIS,
        store_id=other_store.id,
        status=WorkflowStatus.COMPLETED,
        created_by=other_operator.id,
        updated_at=datetime(2026, 9, 1, 13, 0),
    )
    optimization_needs_action = _run(
        "optimization-needs-action",
        workflow_type=WorkflowType.OPTIMIZATION,
        status=WorkflowStatus.PENDING_MANUAL,
        updated_at=datetime(2026, 9, 1, 12, 0),
    )
    optimization_running = _run(
        "optimization-running",
        workflow_type=WorkflowType.OPTIMIZATION,
        status=WorkflowStatus.PENDING_MANUAL,
        updated_at=datetime(2026, 9, 1, 7, 0),
    )
    optimization_other = _run(
        "optimization-other",
        workflow_type=WorkflowType.OPTIMIZATION,
        store_id=other_store.id,
        status=WorkflowStatus.DRAFT_READY,
        created_by=other_operator.id,
        updated_at=datetime(2026, 9, 1, 14, 0),
    )
    manual_workflow = _run(
        "manual-workflow",
        workflow_type=WorkflowType.MANUAL_REVIEW,
        status=WorkflowStatus.ACCEPTED,
        updated_at=datetime(2026, 9, 1, 10, 0),
    )
    session.add_all(
        [
            analysis_awaiting,
            analysis_with_proposal,
            analysis_running_source,
            analysis_other,
            optimization_needs_action,
            optimization_running,
            optimization_other,
            manual_workflow,
        ]
    )
    await session.flush()
    candidates = [
        _candidate("candidate-action", analysis_with_proposal.id, products[0].id),
        _candidate("candidate-running", analysis_running_source.id, products[1].id),
        _candidate("candidate-other", analysis_other.id, products[2].id),
    ]
    session.add_all(candidates)
    await session.flush()
    proposals = [
        ProductProposal(
            id="proposal-needs-action",
            analysis_run_id=analysis_with_proposal.id,
            analysis_candidate_id=candidates[0].id,
            optimization_run_id=optimization_needs_action.id,
            store_id=store.id,
            product_id=products[0].id,
            base_product_version=1,
            selection_idempotency_hash="a" * 64,
        ),
        ProductProposal(
            id="proposal-running",
            analysis_run_id=analysis_running_source.id,
            analysis_candidate_id=candidates[1].id,
            optimization_run_id=optimization_running.id,
            store_id=store.id,
            product_id=products[1].id,
            base_product_version=1,
            selection_idempotency_hash="b" * 64,
        ),
        ProductProposal(
            id="proposal-other",
            analysis_run_id=analysis_other.id,
            analysis_candidate_id=candidates[2].id,
            optimization_run_id=optimization_other.id,
            store_id=other_store.id,
            product_id=products[2].id,
            base_product_version=1,
            selection_idempotency_hash="c" * 64,
        ),
    ]
    session.add_all(proposals)
    await session.flush()
    revision = ProposalRevision(
        id="revision-running",
        proposal_id=proposals[1].id,
        iteration=0,
        revision_number=1,
        origin=ProposalRevisionOrigin.AGENT,
        created_by=operator.id,
        base_product_version=1,
        trusted_fact_hash="d" * 64,
        proposal_output={},
        citations=[],
    )
    session.add(revision)
    await session.flush()
    manual_run = ManualReviewRun(
        id="manual-run",
        workflow_run_id=manual_workflow.id,
        proposal_id=proposals[1].id,
        proposal_revision_id=revision.id,
        submitted_by=operator.id,
        idempotency_key_hash="e" * 64,
        request_hash="f" * 64,
    )
    session.add(manual_run)
    await session.flush()
    proposals[1].current_revision_id = revision.id
    proposals[1].active_manual_review_run_id = manual_run.id
    await session.flush()
    return {
        "operator": operator,
        "supervisor": supervisor,
        "admin": admin,
        "other_operator": other_operator,
        "unscoped_user": unscoped_user,
        "disabled_user": disabled_user,
        "optimization_needs_action": optimization_needs_action,
    }


@pytest_asyncio.fixture
async def client(session) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def override_get_session():
        yield session

    app.dependency_overrides[get_session] = override_get_session
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as test_client:
        yield test_client
    app.dependency_overrides.clear()


async def test_workbench_merges_authorized_tasks_and_prefers_active_manual(
    client, workbench_data
) -> None:
    response = await client.get(
        "/workbench/tasks", headers=_headers(workbench_data["operator"])
    )

    assert response.status_code == 200
    body = response.json()
    assert body["page"] == 1
    assert body["page_size"] == 20
    assert body["total"] == 3
    assert [item["id"] for item in body["items"]] == [
        "proposal-needs-action",
        "analysis-awaiting-selection",
        "proposal-running",
    ]
    assert body["items"][0]["action_required"] == "edit_proposal"
    assert body["items"][0]["requires_current_user_action"] is True
    assert body["items"][1]["action_required"] == "select_product"
    assert body["items"][2]["workflow_run_id"] == "manual-workflow"
    assert body["items"][2]["workflow_type"] == "manual_review"
    assert body["items"][2]["action_required"] == "wait"
    assert body["items"][2]["requires_current_user_action"] is False
    assert body["items"][0]["updated_at"] == "2026-09-01T12:00:00Z"
    assert "analysis-with-proposal" not in {item["id"] for item in body["items"]}
    assert "proposal-other" not in {item["id"] for item in body["items"]}
    assert not {
        "lease_owner",
        "lease_expires_at",
        "input",
        "output",
        "error_code",
        "idempotency_key_hash",
        "request_hash",
    } & set(body["items"][0])


async def test_workbench_filters_and_pages_after_stable_sort(
    client, workbench_data
) -> None:
    headers = _headers(workbench_data["operator"])
    proposal = await client.get("/workbench/tasks?kind=proposal", headers=headers)
    pending_manual = await client.get(
        "/workbench/tasks?status=pending_manual", headers=headers
    )
    store = await client.get("/workbench/tasks?store_id=store-1", headers=headers)
    page = await client.get("/workbench/tasks?page=2&page_size=1", headers=headers)

    assert proposal.status_code == pending_manual.status_code == store.status_code == 200
    assert proposal.json()["total"] == 2
    assert {item["kind"] for item in proposal.json()["items"]} == {"proposal"}
    assert [item["id"] for item in pending_manual.json()["items"]] == [
        "proposal-needs-action"
    ]
    assert store.json()["total"] == 3
    assert page.status_code == 200
    assert page.json()["total"] == 3
    assert [item["id"] for item in page.json()["items"]] == [
        "analysis-awaiting-selection"
    ]


@pytest.mark.parametrize("store_id", ["missing-store", "store-2", "disabled-store"])
async def test_workbench_store_filter_hides_unknown_disabled_and_unscoped_stores(
    client, workbench_data, store_id: str
) -> None:
    response = await client.get(
        f"/workbench/tasks?store_id={store_id}",
        headers=_headers(workbench_data["operator"]),
    )

    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "WORKBENCH_STORE_NOT_FOUND"}}


async def test_workbench_only_returns_exact_user_scope(client, workbench_data) -> None:
    response = await client.get(
        "/workbench/tasks", headers=_headers(workbench_data["other_operator"])
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == ["proposal-other"]


async def test_workbench_active_user_without_scope_sees_empty_and_store_is_hidden(
    client, workbench_data
) -> None:
    headers = _headers(workbench_data["unscoped_user"])

    unfiltered = await client.get("/workbench/tasks", headers=headers)
    filtered = await client.get(
        "/workbench/tasks?store_id=store-1", headers=headers
    )

    assert unfiltered.status_code == 200
    assert unfiltered.json()["items"] == []
    assert unfiltered.json()["total"] == 0
    assert filtered.status_code == 404
    assert filtered.json() == {
        "detail": {"code": "WORKBENCH_STORE_NOT_FOUND"}
    }


async def test_workbench_rejects_disabled_user(client, workbench_data) -> None:
    response = await client.get(
        "/workbench/tasks", headers=_headers(workbench_data["disabled_user"])
    )

    assert response.status_code == 401


async def test_pending_approval_action_depends_on_fresh_database_role(
    client, session, workbench_data
) -> None:
    workbench_data["optimization_needs_action"].status = WorkflowStatus.PENDING_APPROVAL
    await session.flush()

    for actor_name, expected_action, required in [
        ("operator", "wait", False),
        ("supervisor", "review_approval", True),
        ("admin", "review_approval", True),
    ]:
        response = await client.get(
            "/workbench/tasks?kind=proposal&status=pending_approval",
            headers=_headers(workbench_data[actor_name]),
        )
        assert response.status_code == 200
        assert response.json()["total"] == 1
        item = response.json()["items"][0]
        assert item["action_required"] == expected_action
        assert item["requires_current_user_action"] is required


@pytest.mark.parametrize("actor_name", ["supervisor", "admin"])
async def test_non_operator_awaiting_selection_is_readonly(
    client, workbench_data, actor_name: str
) -> None:
    response = await client.get(
        "/workbench/tasks?kind=analysis&status=awaiting_selection",
        headers=_headers(workbench_data[actor_name]),
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    item = response.json()["items"][0]
    assert item["id"] == "analysis-awaiting-selection"
    assert item["action_required"] == "view_result"
    assert item["requires_current_user_action"] is False


async def test_workbench_read_failure_is_stable_and_rolls_back(
    client, session, workbench_data, monkeypatch
) -> None:
    async def fail_read(*_args, **_kwargs):
        raise SQLAlchemyError("unsafe database detail")

    rollback = AsyncMock()
    monkeypatch.setattr(session, "scalar", fail_read)
    monkeypatch.setattr(session, "rollback", rollback)

    response = await client.get(
        "/workbench/tasks", headers=_headers(workbench_data["operator"])
    )

    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "WORKBENCH_READ_FAILED"}}
    rollback.assert_awaited_once()
    assert "unsafe database detail" not in response.text
