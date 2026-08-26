from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.auth import create_access_token, hash_password
from backend.common import UserRole, WorkflowQuality, WorkflowStatus
from backend.config import get_settings
from backend.database import get_session
from backend.main import create_app
from backend.models import AnalysisCandidate, Product, Store, User, UserStoreScope, WorkflowRun


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _workflow_run(run_id: str, **changes: object) -> WorkflowRun:
    values: dict[str, object] = {
        "id": run_id,
        "workflow_type": "analysis",
        "store_id": "flagship",
        "created_by": "operator-user",
        "start_date": date(2026, 7, 26),
        "end_date": date(2026, 8, 24),
        "status": WorkflowStatus.ACCEPTED,
        "quality_status": WorkflowQuality.NORMAL,
        "input": {
            "store_id": "flagship",
            "start_date": "2026-07-26",
            "end_date": "2026-08-24",
        },
    }
    values.update(changes)
    return WorkflowRun(**values)


def _candidate(candidate_id: str, run_id: str, product_id: str, rank: int) -> AnalysisCandidate:
    return AnalysisCandidate(
        id=candidate_id,
        workflow_run_id=run_id,
        product_id=product_id,
        rank=rank,
        product_code=f"FLAGSHIP-00{rank}",
        anomaly_types=["low_conversion"],
        metrics={
            "product_id": product_id,
            "product_code": f"FLAGSHIP-00{rank}",
            "impressions": 100,
            "clicks": 10,
            "orders": 1,
            "units": 1,
            "revenue": "10.00",
            "refunds": 0,
            "ctr": "0.1000",
            "conversion_rate": "0.1000",
            "refund_rate": "0.0000",
            "average_order_value": "10.0000",
        },
        business_impact=Decimal("10.00"),
        evidence=["clicks=10"],
        impact_explanation="影响说明",
        reason="原因",
        recommended_action="建议",
        confidence=Decimal("0.8000"),
    )


@pytest_asyncio.fixture
async def analysis_data(session) -> dict[str, User | Store]:
    password_hash = hash_password("DemoPass!2026")
    operator = User(
        id="operator-user",
        username="operator",
        password_hash=password_hash,
        role=UserRole.OPERATOR,
    )
    unassigned = User(
        id="unassigned-user",
        username="unassigned",
        password_hash=password_hash,
        role=UserRole.OPERATOR,
    )
    admin = User(
        id="admin-user",
        username="admin",
        password_hash=password_hash,
        role=UserRole.ADMIN,
    )
    flagship = Store(id="flagship", name="旗舰店", code="flagship")
    other = Store(id="other-store", name="其他店", code="other")
    disabled = Store(id="disabled-store", name="已停用店", code="disabled", enabled=False)
    session.add_all([operator, unassigned, admin, flagship, other, disabled])
    await session.flush()
    session.add_all(
        [
            UserStoreScope(user_id=operator.id, store_id=flagship.id),
            Product(
                id="product-1",
                store_id=flagship.id,
                code="FLAGSHIP-001",
                title="商品一",
                category="数码",
            ),
            Product(
                id="product-2",
                store_id=flagship.id,
                code="FLAGSHIP-002",
                title="商品二",
                category="数码",
            ),
        ]
    )
    await session.flush()
    return {
        "operator": operator,
        "unassigned": unassigned,
        "admin": admin,
        "flagship": flagship,
        "disabled": disabled,
    }


@pytest_asyncio.fixture
async def client(session) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def override_get_session():
        yield session

    app.dependency_overrides[get_session] = override_get_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def operator_token(analysis_data) -> str:
    return create_access_token(analysis_data["operator"], get_settings())


@pytest_asyncio.fixture
async def unassigned_token(analysis_data) -> str:
    return create_access_token(analysis_data["unassigned"], get_settings())


@pytest_asyncio.fixture
async def admin_token(analysis_data) -> str:
    return create_access_token(analysis_data["admin"], get_settings())


async def test_authorized_user_creates_accepted_runs_at_inclusive_boundaries(
    client, operator_token, session
) -> None:
    for start_date, end_date in [("2026-08-24", "2026-08-24"), ("2026-05-27", "2026-08-24")]:
        response = await client.post(
            "/analysis-runs",
            json={"store_id": "flagship", "start_date": start_date, "end_date": end_date},
            headers=_headers(operator_token),
        )

        assert response.status_code == 202
        assert response.json()["status"] == "accepted"
        run = await session.get(WorkflowRun, response.json()["workflow_run_id"])
        assert run is not None
        assert (
            run.workflow_type,
            run.status,
            run.quality_status,
            run.attempt_count,
            run.lease_owner,
            run.lease_expires_at,
        ) == (
            "analysis",
            WorkflowStatus.ACCEPTED,
            WorkflowQuality.NORMAL,
            0,
            None,
            None,
        )
        assert run.input == {
            "store_id": "flagship",
            "start_date": start_date,
            "end_date": end_date,
        }


@pytest.mark.parametrize(
    ("start_date", "end_date"),
    [("2026-08-24", "2026-08-23"), ("2026-05-26", "2026-08-24")],
)
async def test_analysis_run_rejects_reversed_or_ninety_one_day_ranges(
    client, operator_token, start_date, end_date
) -> None:
    response = await client.post(
        "/analysis-runs",
        json={"store_id": "flagship", "start_date": start_date, "end_date": end_date},
        headers=_headers(operator_token),
    )

    assert response.status_code == 422


async def test_analysis_run_creation_enforces_token_and_current_store_access(
    client, operator_token, unassigned_token, admin_token
) -> None:
    payload = {"store_id": "flagship", "start_date": "2026-07-26", "end_date": "2026-08-24"}
    missing_token = await client.post("/analysis-runs", json=payload)
    unassigned = await client.post(
        "/analysis-runs", json=payload, headers=_headers(unassigned_token)
    )
    disabled = await client.post(
        "/analysis-runs",
        json={**payload, "store_id": "disabled-store"},
        headers=_headers(admin_token),
    )

    assert missing_token.status_code == 401
    assert unassigned.status_code == 403
    assert disabled.status_code == 404


async def test_workflow_reads_require_current_database_scope(
    client, operator_token, analysis_data, session
) -> None:
    run = _workflow_run("scope-run")
    session.add(run)
    await session.flush()
    scope = await session.get(
        UserStoreScope,
        {"user_id": analysis_data["operator"].id, "store_id": analysis_data["flagship"].id},
    )
    assert scope is not None
    await session.delete(scope)
    await session.flush()

    run_response = await client.get(f"/workflow-runs/{run.id}", headers=_headers(operator_token))
    candidates_response = await client.get(
        f"/analysis-runs/{run.id}/candidates", headers=_headers(operator_token)
    )

    assert run_response.status_code == 403
    assert candidates_response.status_code == 403


async def test_authorized_workflow_read_returns_only_safe_fields(client, operator_token, session) -> None:
    run = _workflow_run(
        "safe-run",
        current_step="collect_facts",
        error_code="SAFE_ERROR",
    )
    session.add(run)
    await session.flush()

    response = await client.get(f"/workflow-runs/{run.id}", headers=_headers(operator_token))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == run.id
    assert body["candidates_ready"] is False
    assert body["error_code"] == "SAFE_ERROR"
    assert "lease_owner" not in body
    assert "lease_expires_at" not in body
    assert "input" not in body
    assert "output" not in body
    assert "quality" not in body


async def test_candidates_are_not_ready_for_any_nonterminal_or_failed_run(
    client, operator_token, session
) -> None:
    runs = [
        _workflow_run("accepted-run"),
        _workflow_run(
            "processing-run",
            status=WorkflowStatus.PROCESSING,
            lease_owner="worker-a",
            lease_expires_at=datetime(2026, 8, 25, tzinfo=UTC),
        ),
        _workflow_run("failed-run", status=WorkflowStatus.FAILED, error_code="FACT_ERROR"),
    ]
    session.add_all(runs)
    await session.flush()

    for run in runs:
        response = await client.get(
            f"/analysis-runs/{run.id}/candidates", headers=_headers(operator_token)
        )
        assert response.status_code == 409
        assert response.json() == {"detail": {"code": "ANALYSIS_NOT_READY"}}


async def test_ready_candidates_are_rank_ordered_and_unknown_runs_need_authentication(
    client, operator_token, session
) -> None:
    run = _workflow_run("ready-run", status=WorkflowStatus.AWAITING_SELECTION)
    session.add(run)
    await session.flush()
    session.add_all(
        [
            _candidate("candidate-2", run.id, "product-1", rank=2),
            _candidate("candidate-1", run.id, "product-2", rank=1),
        ]
    )
    await session.flush()

    candidates = await client.get(
        f"/analysis-runs/{run.id}/candidates", headers=_headers(operator_token)
    )
    unknown_workflow = await client.get(
        "/workflow-runs/missing-run", headers=_headers(operator_token)
    )
    unknown_candidates = await client.get(
        "/analysis-runs/missing-run/candidates", headers=_headers(operator_token)
    )
    unauthenticated_workflow = await client.get("/workflow-runs/missing-run")
    unauthenticated_candidates = await client.get("/analysis-runs/missing-run/candidates")

    assert candidates.status_code == 200
    assert [candidate["rank"] for candidate in candidates.json()] == [1, 2]
    assert unknown_workflow.status_code == 404
    assert unknown_candidates.status_code == 404
    assert unauthenticated_workflow.status_code == 401
    assert unauthenticated_candidates.status_code == 401
