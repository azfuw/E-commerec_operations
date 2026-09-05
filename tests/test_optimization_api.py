import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, update

from backend.auth import create_access_token, hash_password
from backend.common import (
    ApprovalActionType,
    ComplianceRiskLevel,
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
    ApprovalAction,
    ComplianceReview,
    ManualReviewRun,
    Product,
    ProductProposal,
    ProposalRevision,
    PublishRecord,
    Store,
    User,
    UserStoreScope,
    WorkflowRun,
)
from backend.proposals import select_product_for_optimization
from tests.test_manual_review_api import (
    _manual_body,
    _manual_headers,
    manual_client,
    manual_route_data,
)


def _headers(token: str, *, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _analysis_run(
    run_id: str,
    *,
    store_id: str = "store-1",
    status: WorkflowStatus = WorkflowStatus.AWAITING_SELECTION,
    workflow_type: WorkflowType = WorkflowType.ANALYSIS,
) -> WorkflowRun:
    return WorkflowRun(
        id=run_id,
        workflow_type=workflow_type,
        store_id=store_id,
        created_by="operator-1",
        start_date=date(2026, 8, 1) if workflow_type is WorkflowType.ANALYSIS else None,
        end_date=date(2026, 8, 2) if workflow_type is WorkflowType.ANALYSIS else None,
        status=status,
        quality_status=WorkflowQuality.NORMAL,
        input={"source": "test"},
    )


def _candidate(
    candidate_id: str,
    run_id: str,
    product_id: str,
    rank: int,
) -> AnalysisCandidate:
    return AnalysisCandidate(
        id=candidate_id,
        workflow_run_id=run_id,
        product_id=product_id,
        rank=rank,
        product_code=f"PRODUCT-{rank}",
        anomaly_types=["low_conversion"],
        metrics={
            "product_id": product_id,
            "product_code": f"PRODUCT-{rank}",
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


async def _seed_awaiting_selection(
    session,
    *,
    run_id: str = "analysis-1",
    first_candidate_id: str = "candidate-1",
    first_product_id: str = "product-1",
    include_second_candidate: bool = True,
) -> WorkflowRun:
    run = _analysis_run(run_id)
    candidates = [_candidate(first_candidate_id, run.id, first_product_id, 1)]
    if include_second_candidate:
        candidates.append(_candidate("candidate-2", run.id, "product-2", 2))
    session.add(run)
    await session.flush()
    session.add_all(candidates)
    await session.flush()
    return run


async def _count(session, model) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


@pytest_asyncio.fixture
async def selection_data(session) -> dict[str, User | Store | Product]:
    password_hash = hash_password("DemoPass!2026")
    operator = User(
        id="operator-1",
        username="operator",
        password_hash=password_hash,
        role=UserRole.OPERATOR,
    )
    supervisor = User(
        id="supervisor-1",
        username="supervisor",
        password_hash=password_hash,
        role=UserRole.SUPERVISOR,
    )
    admin = User(
        id="admin-1",
        username="admin",
        password_hash=password_hash,
        role=UserRole.ADMIN,
    )
    other_operator = User(
        id="other-operator-1",
        username="other-operator",
        password_hash=password_hash,
        role=UserRole.OPERATOR,
    )
    store = Store(id="store-1", name="目标店铺", code="target")
    other_store = Store(id="store-2", name="其他店铺", code="other")
    products = [
        Product(id="product-1", store_id=store.id, code="PRODUCT-1", title="商品一", category="数码", current_version=7),
        Product(id="product-2", store_id=store.id, code="PRODUCT-2", title="商品二", category="数码", current_version=7),
        Product(id="other-product", store_id=other_store.id, code="OTHER-1", title="其他商品", category="数码", current_version=3),
    ]
    session.add_all([operator, supervisor, admin, other_operator, store, other_store])
    await session.flush()
    session.add_all(products)
    await session.flush()
    session.add_all(
        [
            UserStoreScope(user_id=operator.id, store_id=store.id),
            UserStoreScope(user_id=supervisor.id, store_id=store.id),
            UserStoreScope(user_id=admin.id, store_id=store.id),
            UserStoreScope(user_id=other_operator.id, store_id=other_store.id),
        ]
    )
    await session.flush()
    return {
        "operator": operator,
        "supervisor": supervisor,
        "admin": admin,
        "other_operator": other_operator,
        "store": store,
        "other_store": other_store,
        "product_1": products[0],
        "product_2": products[1],
        "other_product": products[2],
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


def _token(user: User) -> str:
    return create_access_token(user, get_settings())


_UNSAFE_PROPOSAL_READ_KEYS = {
    "idempotency_key_hash",
    "request_hash",
    "trusted_fact_hash",
    "publish_idempotency_hash",
    "lease_owner",
    "lease_expires_at",
    "checkpoint",
    "input",
    "output",
    "prompt",
    "provider",
    "path",
    "vector",
    "authorization",
    "key",
    "raw_response",
}


def _proposal_read_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            nested
            for child in value.values()
            for nested in _proposal_read_keys(child)
        }
    if isinstance(value, list):
        return {nested for child in value for nested in _proposal_read_keys(child)}
    return set()


def _assert_safe_proposal_read(response, caplog) -> None:
    assert not _UNSAFE_PROPOSAL_READ_KEYS & {
        key.lower() for key in _proposal_read_keys(response.json())
    }
    captured = caplog.text.lower()
    assert not any(
        value in captured
        for value in (
            "idempotency_key_hash",
            "request_hash",
            "trusted_fact_hash",
            "publish_idempotency_hash",
            "lease_owner",
            "lease_expires_at",
            "checkpoint",
            "authorization",
            "deepseek_api_key",
            "raw_response",
        )
    )


async def test_select_product_commits_all_four_selection_changes_once(
    client, selection_data, session
) -> None:
    await _seed_awaiting_selection(session)

    response = await client.post(
        "/analysis-runs/analysis-1/select-product",
        json={"candidate_id": "candidate-1"},
        headers=_headers(_token(selection_data["operator"]), idempotency_key="client-key-1"),
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "accepted"
    proposal = await session.get(ProductProposal, body["proposal_id"])
    optimization = await session.get(WorkflowRun, body["optimization_workflow_run_id"])
    analysis = await session.get(WorkflowRun, "analysis-1")
    assert proposal is not None and optimization is not None and analysis is not None
    assert proposal.selection_idempotency_hash == hashlib.sha256(b"client-key-1").hexdigest()
    assert proposal.selection_idempotency_hash != "client-key-1"
    assert (optimization.workflow_type, optimization.status, optimization.start_date, optimization.end_date) == (
        WorkflowType.OPTIMIZATION,
        WorkflowStatus.ACCEPTED,
        None,
        None,
    )
    assert optimization.input == {
        "proposal_id": proposal.id,
        "source_analysis_run_id": analysis.id,
        "analysis_candidate_id": "candidate-1",
        "product_id": "product-1",
        "store_id": "store-1",
    }
    assert analysis.status is WorkflowStatus.COMPLETED
    assert analysis.current_step == "product_selected"


async def test_select_product_replays_same_candidate_and_rejects_reselection(
    client, selection_data, session
) -> None:
    await _seed_awaiting_selection(session)
    token = _token(selection_data["operator"])
    first = await client.post(
        "/analysis-runs/analysis-1/select-product",
        json={"candidate_id": "candidate-1"},
        headers=_headers(token, idempotency_key="first-key"),
    )
    assert first.status_code == 202
    first_body = first.json()
    proposal = await session.get(ProductProposal, first_body["proposal_id"])
    assert proposal is not None
    original_hash = proposal.selection_idempotency_hash

    replay = await client.post(
        "/analysis-runs/analysis-1/select-product",
        json={"candidate_id": "candidate-1"},
        headers=_headers(token, idempotency_key="different-valid-key"),
    )
    conflict = await client.post(
        "/analysis-runs/analysis-1/select-product",
        json={"candidate_id": "candidate-2"},
        headers=_headers(token, idempotency_key="candidate-two-key"),
    )

    assert replay.status_code == 200
    assert replay.json() == first_body
    await session.refresh(proposal)
    assert proposal.selection_idempotency_hash == original_hash
    assert await _count(session, ProductProposal) == 1
    assert await _count(session, WorkflowRun) == 2
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": {"code": "ANALYSIS_SELECTION_CONFLICT"}}


@pytest.mark.parametrize("idempotency_key", [None, "   ", "a" * 129])
async def test_select_product_rejects_invalid_idempotency_key_without_writes(
    client, selection_data, session, idempotency_key: str | None
) -> None:
    await _seed_awaiting_selection(session)
    response = await client.post(
        "/analysis-runs/analysis-1/select-product",
        json={"candidate_id": "candidate-1"},
        headers=_headers(_token(selection_data["operator"]), idempotency_key=idempotency_key),
    )

    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "ANALYSIS_IDEMPOTENCY_KEY_INVALID"}}
    assert await _count(session, ProductProposal) == 0
    assert await _count(session, WorkflowRun) == 1


async def test_select_product_uses_character_limit_and_original_utf8_key_hash(
    client, selection_data, session
) -> None:
    await _seed_awaiting_selection(session)
    key = "中" * 128

    result = await select_product_for_optimization(
        session,
        selection_data["operator"].id,
        "analysis-1",
        "candidate-1",
        key,
    )

    assert result.created is True
    assert result.proposal.selection_idempotency_hash == hashlib.sha256(key.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("actor", ["supervisor", "admin"])
async def test_only_operator_can_select_even_when_scoped(
    client, selection_data, session, actor: str
) -> None:
    await _seed_awaiting_selection(session)
    response = await client.post(
        "/analysis-runs/analysis-1/select-product",
        json={"candidate_id": "candidate-1"},
        headers=_headers(_token(selection_data[actor]), idempotency_key="role-key"),
    )

    assert response.status_code == 403
    assert response.json() == {"detail": {"code": "ANALYSIS_SELECTION_FORBIDDEN"}}
    assert await _count(session, ProductProposal) == 0


async def test_selection_reloads_current_role_scope_and_store_state(
    client, selection_data, session
) -> None:
    token = _token(selection_data["operator"])
    user = await session.get(User, "operator-1")
    store = await session.get(Store, "store-1")
    scope = await session.get(UserStoreScope, {"user_id": "operator-1", "store_id": "store-1"})
    assert user is selection_data["operator"] and store is selection_data["store"] and scope is not None
    user_id, store_id = user.id, store.id

    await _seed_awaiting_selection(
        session,
        run_id="role-run",
        first_candidate_id="role-candidate",
        include_second_candidate=False,
    )
    await session.commit()
    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(role=UserRole.SUPERVISOR)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    role_response = await client.post(
        "/analysis-runs/role-run/select-product",
        json={"candidate_id": "role-candidate"},
        headers=_headers(token, idempotency_key="role-change-key"),
    )
    assert role_response.status_code == 403
    assert role_response.json() == {"detail": {"code": "ANALYSIS_SELECTION_FORBIDDEN"}}

    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(role=UserRole.OPERATOR)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    await _seed_awaiting_selection(
        session,
        run_id="store-run",
        first_candidate_id="store-candidate",
        include_second_candidate=False,
    )
    await session.commit()
    await session.execute(
        update(Store)
        .where(Store.id == store_id)
        .values(enabled=False)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    store_response = await client.post(
        "/analysis-runs/store-run/select-product",
        json={"candidate_id": "store-candidate"},
        headers=_headers(token, idempotency_key="store-change-key"),
    )
    assert store_response.status_code == 404
    assert store_response.json() == {"detail": {"code": "ANALYSIS_SELECTION_NOT_FOUND"}}

    await session.execute(
        update(Store)
        .where(Store.id == store_id)
        .values(enabled=True)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    await _seed_awaiting_selection(
        session,
        run_id="scope-run",
        first_candidate_id="scope-candidate",
        include_second_candidate=False,
    )
    await session.commit()
    await session.execute(
        delete(UserStoreScope).where(
            UserStoreScope.user_id == user_id,
            UserStoreScope.store_id == store_id,
        )
    )
    await session.commit()
    scope_response = await client.post(
        "/analysis-runs/scope-run/select-product",
        json={"candidate_id": "scope-candidate"},
        headers=_headers(token, idempotency_key="scope-change-key"),
    )
    assert scope_response.status_code == 404
    assert scope_response.json() == {"detail": {"code": "ANALYSIS_SELECTION_NOT_FOUND"}}
    assert await _count(session, ProductProposal) == 0


async def test_disabled_current_user_token_is_rejected_before_selection(
    client, selection_data, session
) -> None:
    token = _token(selection_data["operator"])
    await _seed_awaiting_selection(session)
    await session.execute(
        update(User)
        .where(User.id == "operator-1")
        .values(status=UserStatus.DISABLED)
        .execution_options(synchronize_session=False)
    )
    session.expire(selection_data["operator"])

    response = await client.post(
        "/analysis-runs/analysis-1/select-product",
        json={"candidate_id": "candidate-1"},
        headers=_headers(token, idempotency_key="disabled-key"),
    )

    assert response.status_code == 401
    assert await _count(session, ProductProposal) == 0


async def test_selection_hides_foreign_or_invalid_inputs_and_keeps_transaction_atomic(
    client, selection_data, session
) -> None:
    token = _token(selection_data["operator"])
    await _seed_awaiting_selection(session)
    other_run = _analysis_run("other-run", store_id="store-2")
    other_candidate = _candidate("other-candidate", other_run.id, "other-product", 1)
    invalid_type = _analysis_run(
        "optimization-type-run",
        status=WorkflowStatus.ACCEPTED,
        workflow_type=WorkflowType.OPTIMIZATION,
    )
    not_ready = _analysis_run("not-ready-run", status=WorkflowStatus.ACCEPTED)
    mismatch_run = _analysis_run("mismatch-run")
    mismatch_candidate = _candidate("mismatch-candidate", mismatch_run.id, "product-1", 1)
    mismatch_candidate.product_code = "WRONG-PRODUCT-CODE"
    session.add_all([other_run, invalid_type, not_ready, mismatch_run])
    await session.flush()
    session.add_all([other_candidate, mismatch_candidate])
    await session.flush()
    await session.commit()

    requests = [
        ("missing-run", "candidate-1", 404, "ANALYSIS_SELECTION_NOT_FOUND"),
        ("analysis-1", "other-candidate", 404, "ANALYSIS_SELECTION_NOT_FOUND"),
        ("optimization-type-run", "candidate-1", 409, "ANALYSIS_SELECTION_INVALID_TYPE"),
        ("not-ready-run", "candidate-1", 409, "ANALYSIS_SELECTION_NOT_READY"),
        ("mismatch-run", "mismatch-candidate", 409, "ANALYSIS_PRODUCT_MISMATCH"),
    ]
    for index, (run_id, candidate_id, status_code, code) in enumerate(requests):
        response = await client.post(
            f"/analysis-runs/{run_id}/select-product",
            json={"candidate_id": candidate_id},
            headers=_headers(token, idempotency_key=f"error-key-{index}"),
        )
        assert response.status_code == status_code
        assert response.json() == {"detail": {"code": code}}

    analysis = await session.get(WorkflowRun, "analysis-1")
    assert analysis is not None
    assert analysis.status is WorkflowStatus.AWAITING_SELECTION
    assert await _count(session, ProductProposal) == 0
    assert await _count(session, WorkflowRun) == 5


async def _selected_proposal(client, selection_data, session) -> tuple[ProductProposal, WorkflowRun]:
    await _seed_awaiting_selection(session)
    response = await client.post(
        "/analysis-runs/analysis-1/select-product",
        json={"candidate_id": "candidate-1"},
        headers=_headers(_token(selection_data["operator"]), idempotency_key="selection-key"),
    )
    assert response.status_code == 202
    proposal = await session.get(ProductProposal, response.json()["proposal_id"])
    optimization = await session.get(WorkflowRun, response.json()["optimization_workflow_run_id"])
    assert proposal is not None and optimization is not None
    return proposal, optimization


async def test_proposal_read_requires_exact_scope_for_all_allowed_roles_and_returns_safe_empty_detail(
    client, selection_data, session
) -> None:
    proposal, optimization = await _selected_proposal(client, selection_data, session)
    proposal_id = proposal.id
    tokens = {actor: _token(selection_data[actor]) for actor in ("operator", "supervisor", "admin")}
    other_token = _token(selection_data["other_operator"])
    for actor in ("operator", "supervisor", "admin"):
        response = await client.get(
            f"/proposals/{proposal.id}", headers=_headers(tokens[actor])
        )
        assert response.status_code == 200
        body = response.json()
        assert body["proposal"]["id"] == proposal.id
        assert body["proposal"]["base_product_version"] == 7
        assert body["optimization_run"] == {
            "id": optimization.id,
            "workflow_type": "optimization",
            "status": "accepted",
            "quality_status": "normal",
            "error_code": None,
        }
        assert body["current_revision"] is None
        assert body["current_review"] is None
        assert not {
            "selection_idempotency_hash",
            "lease_owner",
            "lease_expires_at",
            "checkpoint",
            "input",
            "output",
            "prompt",
            "raw_response",
            "path",
            "vector",
            "key",
        } & (set(body["proposal"]) | set(body["optimization_run"]))

    await session.execute(
        delete(UserStoreScope).where(
            UserStoreScope.user_id == "admin-1", UserStoreScope.store_id == "store-1"
        )
    )
    await session.commit()
    no_scope = await client.get(f"/proposals/{proposal_id}", headers=_headers(tokens["admin"]))
    unknown = await client.get("/proposals/missing-proposal", headers=_headers(tokens["operator"]))
    cross_store = await client.get(
        f"/proposals/{proposal_id}", headers=_headers(other_token)
    )
    assert no_scope.status_code == 200
    assert unknown.status_code == cross_store.status_code == 404
    assert unknown.json() == cross_store.json() == {
        "detail": {"code": "PROPOSAL_NOT_FOUND"}
    }


async def test_proposal_read_returns_linked_revision_and_review_only(client, selection_data, session) -> None:
    proposal, optimization = await _selected_proposal(client, selection_data, session)
    revision = ProposalRevision(
        id="revision-1",
        proposal_id=proposal.id,
        iteration=0,
        revision_number=1,
        origin=ProposalRevisionOrigin.AGENT,
        created_by=optimization.created_by,
        parent_revision_id=None,
        base_product_version=proposal.base_product_version,
        trusted_fact_hash="a" * 64,
        proposal_output={"title": "可信商品标题"},
        citations=[{"chunk_id": "chunk-1"}],
    )
    review = ComplianceReview(
        id="review-1",
        proposal_id=proposal.id,
        proposal_revision_id=revision.id,
        iteration=0,
        deterministic_checks={"passed": True},
        semantic_review={"passed": True},
        passed=True,
        risk_level=ComplianceRiskLevel.LOW,
        required_changes=[],
        citations=[{"chunk_id": "chunk-1"}],
        quality_status=WorkflowQuality.NORMAL,
    )
    session.add(revision)
    await session.flush()
    proposal.current_revision_id = revision.id
    await session.flush()
    session.add(review)
    await session.flush()

    response = await client.get(
        f"/proposals/{proposal.id}", headers=_headers(_token(selection_data["operator"]))
    )

    assert response.status_code == 200
    body = response.json()
    assert body["current_revision"] == {
        "id": revision.id,
        "iteration": 0,
        "revision_number": 1,
        "origin": "agent",
        "created_by": optimization.created_by,
        "parent_revision_id": None,
        "base_product_version": 7,
        "proposal_output": {"title": "可信商品标题"},
        "citations": [{"chunk_id": "chunk-1"}],
    }
    assert body["current_review"] == {
        "id": review.id,
        "iteration": 0,
        "deterministic_checks": {"passed": True},
        "semantic_review": {"passed": True},
        "passed": True,
        "risk_level": "low",
        "quality_status": "normal",
        "required_changes": [],
        "citations": [{"chunk_id": "chunk-1"}],
        "error_code": None,
    }


async def _other_proposal(session) -> ProductProposal:
    run = _analysis_run("analysis-other")
    candidate = _candidate("candidate-other", run.id, "product-2", 1)
    optimization = _analysis_run(
        "optimization-other", status=WorkflowStatus.ACCEPTED, workflow_type=WorkflowType.OPTIMIZATION
    )
    proposal = ProductProposal(
        id="proposal-other",
        analysis_run_id=run.id,
        analysis_candidate_id=candidate.id,
        optimization_run_id=optimization.id,
        store_id="store-1",
        product_id="product-2",
        base_product_version=7,
        selection_idempotency_hash="b" * 64,
    )
    session.add_all([run, optimization])
    await session.flush()
    session.add(candidate)
    await session.flush()
    session.add(proposal)
    await session.flush()
    return proposal


@pytest.mark.parametrize("corruption", ["foreign_revision", "foreign_review", "wrong_review_iteration"])
async def test_proposal_read_rejects_inconsistent_revision_review_chains(
    client, selection_data, session, corruption: str
) -> None:
    proposal, _ = await _selected_proposal(client, selection_data, session)
    other_proposal = await _other_proposal(session)
    revision = ProposalRevision(
        id="revision-current",
        proposal_id=proposal.id,
        iteration=0,
        revision_number=1,
        origin=ProposalRevisionOrigin.AGENT,
        created_by="operator-1",
        parent_revision_id=None,
        base_product_version=7,
        trusted_fact_hash="c" * 64,
        proposal_output={"title": "当前方案"},
        citations=[],
    )
    other_revision = ProposalRevision(
        id="revision-other",
        proposal_id=other_proposal.id,
        iteration=0,
        revision_number=1,
        origin=ProposalRevisionOrigin.AGENT,
        created_by="operator-1",
        parent_revision_id=None,
        base_product_version=7,
        trusted_fact_hash="d" * 64,
        proposal_output={"title": "其他方案"},
        citations=[],
    )
    session.add_all([revision, other_revision])
    await session.flush()
    if corruption == "foreign_revision":
        proposal.current_revision_id = other_revision.id
    else:
        proposal.current_revision_id = revision.id
        review = ComplianceReview(
            id=f"review-{corruption}",
            proposal_id=other_proposal.id if corruption == "foreign_review" else proposal.id,
            proposal_revision_id=revision.id,
            iteration=0 if corruption == "foreign_review" else 1,
            deterministic_checks={"passed": False},
            semantic_review={"passed": False},
            passed=False,
            risk_level=ComplianceRiskLevel.MEDIUM,
            required_changes=[],
            citations=[],
            quality_status=WorkflowQuality.NORMAL,
        )
        session.add(review)
    await session.flush()

    response = await client.get(
        f"/proposals/{proposal.id}", headers=_headers(_token(selection_data["operator"]))
    )

    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "PROPOSAL_DATA_INCONSISTENT"}}
    assert "其他方案" not in response.text


async def test_proposal_read_reloads_current_scope_and_store_state(client, selection_data, session) -> None:
    proposal, _ = await _selected_proposal(client, selection_data, session)
    token = _token(selection_data["operator"])
    user = await session.get(User, "operator-1")
    store = await session.get(Store, "store-1")
    scope = await session.get(UserStoreScope, {"user_id": "operator-1", "store_id": "store-1"})
    assert user is selection_data["operator"] and store is selection_data["store"] and scope is not None
    proposal_id, user_id, store_id = proposal.id, user.id, store.id

    await session.execute(
        update(Store)
        .where(Store.id == store_id)
        .values(enabled=False)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    disabled_store = await client.get(f"/proposals/{proposal_id}", headers=_headers(token))
    assert disabled_store.status_code == 404
    assert disabled_store.json() == {"detail": {"code": "PROPOSAL_NOT_FOUND"}}

    await session.execute(
        update(Store)
        .where(Store.id == store_id)
        .values(enabled=True)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    await session.execute(
        delete(UserStoreScope).where(
            UserStoreScope.user_id == user_id, UserStoreScope.store_id == store_id
        )
    )
    await session.commit()
    removed_scope = await client.get(f"/proposals/{proposal_id}", headers=_headers(token))
    assert removed_scope.status_code == 404
    assert removed_scope.json() == {"detail": {"code": "PROPOSAL_NOT_FOUND"}}


async def test_proposal_read_rejects_a_non_optimization_workflow_link(
    client, selection_data, session
) -> None:
    proposal, _ = await _selected_proposal(client, selection_data, session)
    proposal.optimization_run_id = proposal.analysis_run_id
    await session.flush()

    response = await client.get(
        f"/proposals/{proposal.id}", headers=_headers(_token(selection_data["operator"]))
    )

    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "PROPOSAL_DATA_INCONSISTENT"}}


async def test_optimization_workflow_view_keeps_typed_null_dates(client, selection_data, session) -> None:
    run = _analysis_run(
        "optimization-view", status=WorkflowStatus.ACCEPTED, workflow_type=WorkflowType.OPTIMIZATION
    )
    session.add(run)
    await session.flush()

    response = await client.get(
        f"/workflow-runs/{run.id}", headers=_headers(_token(selection_data["operator"]))
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": run.id,
        "workflow_type": "optimization",
        "store_id": "store-1",
        "start_date": None,
        "end_date": None,
        "status": "accepted",
        "quality_status": "normal",
        "current_step": None,
        "attempt_count": 0,
        "candidates_ready": False,
        "error_code": None,
    }


async def test_proposal_read_returns_safe_manual_revision_and_active_review_summary(
    manual_client, manual_route_data, session, caplog
) -> None:
    accepted = await manual_client.post(
        "/proposals/proposal-1/manual-revision",
        json=_manual_body(),
        headers=_manual_headers(manual_route_data["operator"], "proposal-read-manual-key"),
    )
    assert accepted.status_code == 202
    manual = await session.scalar(select(ManualReviewRun))
    assert manual is not None

    response = await manual_client.get(
        "/proposals/proposal-1",
        headers=_manual_headers(manual_route_data["operator"], None),
    )

    assert response.status_code == 200
    body = response.json()
    assert {
        field: body["current_revision"][field]
        for field in (
            "id",
            "iteration",
            "revision_number",
            "origin",
            "created_by",
            "parent_revision_id",
        )
    } == {
        "id": manual.proposal_revision_id,
        "iteration": None,
        "revision_number": 2,
        "origin": "manual",
        "created_by": "operator-1",
        "parent_revision_id": "revision-1",
    }
    assert body["current_review"] is None
    assert body["active_manual_review"] == {
        "manual_review_run_id": manual.id,
        "workflow_run_id": manual.workflow_run_id,
        "proposal_revision_id": manual.proposal_revision_id,
        "status": "accepted",
        "quality_status": "normal",
        "current_step": "accepted",
        "error_code": None,
    }
    assert body["submitted_revision"] is None
    assert body["latest_action"] is None
    assert body["publish_record"] is None
    _assert_safe_proposal_read(response, caplog)


async def test_proposal_read_returns_safe_submitted_action_and_publish_summaries(
    manual_client, manual_route_data, session, caplog
) -> None:
    proposal = manual_route_data["proposal"]
    parent = manual_route_data["parent"]
    optimization = manual_route_data["optimization"]
    proposal.submitted_revision_id = parent.id
    optimization.status = WorkflowStatus.COMPLETED
    optimization.current_step = "simulated_published"
    action = ApprovalAction(
        id="proposal-read-approve-action",
        proposal_id=proposal.id,
        proposal_revision_id=parent.id,
        store_id=proposal.store_id,
        actor_id="supervisor-1",
        actor_role=UserRole.SUPERVISOR,
        action=ApprovalActionType.APPROVE,
        comment=None,
        idempotency_key_hash="8" * 64,
        request_hash="9" * 64,
        created_at=datetime(2026, 8, 31, 15, 0, tzinfo=UTC),
    )
    session.add(action)
    await session.flush()
    publish = PublishRecord(
        id="proposal-read-publish",
        proposal_id=proposal.id,
        proposal_revision_id=parent.id,
        product_id=proposal.product_id,
        store_id=proposal.store_id,
        approved_by="supervisor-1",
        approval_action_id=action.id,
        publish_idempotency_hash="7" * 64,
        before_snapshot={"current_version": 7},
        after_snapshot={"current_version": 8},
        base_product_version=7,
        published_product_version=8,
        published_at=datetime(2026, 8, 31, 15, 1, tzinfo=UTC),
    )
    session.add(publish)
    await session.commit()

    response = await manual_client.get(
        "/proposals/proposal-1",
        headers=_manual_headers(manual_route_data["admin"], None),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["current_revision"]["iteration"] == 0
    assert body["current_revision"]["revision_number"] == 1
    assert body["current_revision"]["origin"] == "agent"
    assert body["current_revision"]["created_by"] == "operator-1"
    assert body["current_revision"]["parent_revision_id"] is None
    assert body["current_review"]["iteration"] == 0
    assert body["active_manual_review"] is None
    assert body["submitted_revision"]["id"] == parent.id
    assert body["latest_action"] == {
        "id": action.id,
        "proposal_id": proposal.id,
        "proposal_revision_id": parent.id,
        "actor_id": "supervisor-1",
        "actor_role": "supervisor",
        "action": "approve",
        "comment": None,
        "created_at": "2026-08-31T15:00:00Z",
    }
    assert body["publish_record"] == {
        "id": publish.id,
        "proposal_id": proposal.id,
        "proposal_revision_id": parent.id,
        "product_id": proposal.product_id,
        "store_id": proposal.store_id,
        "approved_by": "supervisor-1",
        "approval_action_id": action.id,
        "before_snapshot": {"current_version": 7},
        "after_snapshot": {"current_version": 8},
        "base_product_version": 7,
        "published_product_version": 8,
        "published_at": "2026-08-31T15:01:00Z",
    }
    _assert_safe_proposal_read(response, caplog)


@pytest.mark.parametrize(
    "corruption",
    ["active_manual_workflow_store", "latest_action_store", "publish_product"],
)
async def test_proposal_read_rejects_inconsistent_manual_approval_publish_links_from_stale_rows(
    manual_client, manual_route_data, session, corruption: str
) -> None:
    proposal = manual_route_data["proposal"]
    parent = manual_route_data["parent"]
    action = None
    if corruption == "active_manual_workflow_store":
        accepted = await manual_client.post(
            "/proposals/proposal-1/manual-revision",
            json=_manual_body(),
            headers=_manual_headers(manual_route_data["operator"], "stale-manual-key"),
        )
        assert accepted.status_code == 202
        await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == accepted.json()["manual_review_workflow_run_id"])
            .values(store_id="store-2")
            .execution_options(synchronize_session=False)
        )
    else:
        action = ApprovalAction(
            id=f"inconsistent-{corruption}-action",
            proposal_id=proposal.id,
            proposal_revision_id=parent.id,
            store_id="store-2" if corruption == "latest_action_store" else proposal.store_id,
            actor_id="supervisor-1",
            actor_role=UserRole.SUPERVISOR,
            action=ApprovalActionType.APPROVE,
            comment=None,
            idempotency_key_hash="4" * 64,
            request_hash="5" * 64,
        )
        session.add(action)
        await session.flush()
        if corruption == "publish_product":
            session.add(
                PublishRecord(
                    id="inconsistent-publish",
                    proposal_id=proposal.id,
                    proposal_revision_id=parent.id,
                    product_id="product-2",
                    store_id=proposal.store_id,
                    approved_by="supervisor-1",
                    approval_action_id=action.id,
                    publish_idempotency_hash="6" * 64,
                    before_snapshot={"current_version": 7},
                    after_snapshot={"current_version": 8},
                    base_product_version=7,
                    published_product_version=8,
                )
            )
    await session.commit()

    response = await manual_client.get(
        "/proposals/proposal-1",
        headers=_manual_headers(manual_route_data["operator"], None),
    )

    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "PROPOSAL_DATA_INCONSISTENT"}}
