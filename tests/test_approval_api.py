from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.approvals import ApprovalDomainError
from backend.common import (
    ApprovalActionType,
    AuditEventType,
    AuditOutcome,
    OrderStatus,
    ProposalRevisionOrigin,
    RefundStatus,
    UserRole,
    UserStatus,
    WorkflowQuality,
    WorkflowStatus,
    WorkflowType,
)
from backend.models import (
    AnalysisCandidate,
    ApprovalAction,
    AuditEvent,
    ComplianceReview,
    InventorySnapshot,
    KnowledgeDocument,
    Order,
    OrderItem,
    Product,
    ProductProposal,
    ProductSku,
    ProposalRevision,
    PublishRecord,
    Store,
    TrafficDaily,
    User,
    UserStoreScope,
    WorkflowRun,
)
from backend.schemas import (
    ApprovalActionView,
    PublishRecordView,
    ProposalActionRequest,
    ProposalCommentActionRequest,
)
from tests.test_manual_review_api import (
    _manual_body,
    _manual_headers,
    _model_count,
    manual_client,
    manual_route_data,
)


def _headers(user: User, key: str | None = "approval-key-1") -> dict[str, str]:
    return _manual_headers(user, key)


def _body(revision_id: str = "revision-1", **updates: object) -> dict[str, object]:
    values: dict[str, object] = {"revision_id": revision_id}
    values.update(updates)
    return values


async def _submit(client, actor: User, *, key: str = "submit-key"):
    response = await client.post(
        "/proposals/proposal-1/submit",
        json=_body(),
        headers=_headers(actor, key),
    )
    assert response.status_code == 201
    return response


async def _action_counts(session) -> tuple[int, int]:
    return (
        int(await session.scalar(select(func.count()).select_from(ApprovalAction)) or 0),
        int(await session.scalar(select(func.count()).select_from(AuditEvent)) or 0),
    )


async def _publish_count(session) -> int:
    return int(await session.scalar(select(func.count()).select_from(PublishRecord)) or 0)


def _published_output(parent: ProposalRevision) -> dict[str, object]:
    output = dict(parent.proposal_output)
    output.update(
        {
            "title": "优选精梳棉家居商品",
            "selling_points": ["精梳棉触感", "适合日常家居"],
            "description": [
                {
                    "heading": "商品详情",
                    "body": "精梳棉材质，适合日常使用。",
                    "evidence": [{"kind": "citation", "value": "rule-chunk-1"}],
                },
                {
                    "heading": "使用说明",
                    "body": "请按洗护标签清洁。",
                    "evidence": [{"kind": "citation", "value": "rule-chunk-1"}],
                },
            ],
            "keywords": ["精梳棉", "家居"],
            "attribute_completions": [
                {
                    "target_attribute": "材质",
                    "current_value": "棉",
                    "suggested_value": "精梳棉",
                    "reason": "补全可信材质",
                    "evidence": [
                        {"kind": "fact", "value": "product.attributes.材质"}
                    ],
                }
            ],
        }
    )
    return output


async def _prepare_publish_output(session, parent: ProposalRevision) -> None:
    await session.execute(
        update(ProposalRevision)
        .where(ProposalRevision.id == parent.id)
        .values(proposal_output=_published_output(parent))
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _seed_immutable_business_facts(session) -> None:
    order = Order(
        id="order-1",
        store_id="store-1",
        ordered_at=datetime(2026, 8, 1, tzinfo=UTC),
        status=OrderStatus.PAID,
        total_amount=Decimal("100.00"),
    )
    session.add(order)
    await session.flush()
    session.add_all(
        [
            OrderItem(
                id="order-item-1",
                order_id=order.id,
                product_id="product-1",
                sku_id="sku-1",
                quantity=1,
                unit_price=Decimal("100.00"),
                refund_status=RefundStatus.NONE,
            ),
            TrafficDaily(
                id="traffic-1",
                store_id="store-1",
                product_id="product-1",
                metric_date=date(2026, 8, 1),
                impressions=100,
                clicks=10,
                visitors=8,
                add_to_carts=2,
            ),
            InventorySnapshot(
                id="inventory-1",
                store_id="store-1",
                sku_id="sku-1",
                snapshot_date=date(2026, 8, 1),
                on_hand=10,
                inbound=3,
            ),
        ]
    )
    await session.commit()


async def _approve(client, actor: User, *, key: str = "approve-key", revision_id: str = "revision-1"):
    return await client.post(
        "/approvals/proposal-1/approve",
        json=_body(revision_id),
        headers=_headers(actor, key),
    )


_UNSAFE_READ_KEYS = {
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


def _nested_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            nested
            for child in value.values()
            for nested in _nested_keys(child)
        }
    if isinstance(value, list):
        return {nested for child in value for nested in _nested_keys(child)}
    return set()


def _assert_safe_read(response, caplog) -> None:
    assert not _UNSAFE_READ_KEYS & {key.lower() for key in _nested_keys(response.json())}
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


async def _pending_list_item(
    session,
    data: dict[str, object],
    *,
    suffix: str,
    store: Store,
    product: Product,
    actor: User,
    created_at: datetime,
) -> tuple[ProductProposal, ProposalRevision, ApprovalAction]:
    source = data["candidate"]
    analysis = WorkflowRun(
        id=f"analysis-list-{suffix}",
        workflow_type=WorkflowType.ANALYSIS,
        store_id=store.id,
        created_by=actor.id,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2),
        status=WorkflowStatus.COMPLETED,
        quality_status=WorkflowQuality.NORMAL,
        current_step="product_selected",
        created_at=created_at,
    )
    optimization = WorkflowRun(
        id=f"optimization-list-{suffix}",
        workflow_type=WorkflowType.OPTIMIZATION,
        store_id=store.id,
        created_by=actor.id,
        status=WorkflowStatus.PENDING_APPROVAL,
        quality_status=WorkflowQuality.NORMAL,
        current_step="pending_approval",
        input={
            "proposal_id": f"proposal-list-{suffix}",
            "source_analysis_run_id": analysis.id,
            "analysis_candidate_id": f"candidate-list-{suffix}",
            "product_id": product.id,
            "store_id": store.id,
        },
        created_at=created_at,
    )
    session.add_all([analysis, optimization])
    await session.flush()
    candidate = AnalysisCandidate(
        id=f"candidate-list-{suffix}",
        workflow_run_id=analysis.id,
        product_id=product.id,
        rank=1,
        product_code=product.code,
        anomaly_types=list(source.anomaly_types),
        metrics=dict(source.metrics),
        business_impact=source.business_impact,
        evidence=list(source.evidence),
        impact_explanation=source.impact_explanation,
        reason=source.reason,
        recommended_action=source.recommended_action,
        confidence=source.confidence,
        created_at=created_at,
    )
    session.add(candidate)
    await session.flush()
    proposal = ProductProposal(
        id=f"proposal-list-{suffix}",
        analysis_run_id=analysis.id,
        analysis_candidate_id=candidate.id,
        optimization_run_id=optimization.id,
        store_id=store.id,
        product_id=product.id,
        base_product_version=product.current_version,
        selection_idempotency_hash=suffix[0] * 64,
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(proposal)
    await session.flush()
    revision = ProposalRevision(
        id=f"revision-list-{suffix}",
        proposal_id=proposal.id,
        iteration=0,
        revision_number=1,
        origin=ProposalRevisionOrigin.AGENT,
        created_by=actor.id,
        parent_revision_id=None,
        base_product_version=product.current_version,
        trusted_fact_hash=suffix[-1] * 64,
        proposal_output={"title": f"列表方案 {suffix}"},
        citations=[],
        created_at=created_at,
    )
    session.add(revision)
    await session.flush()
    review = ComplianceReview(
        id=f"review-list-{suffix}",
        proposal_id=proposal.id,
        proposal_revision_id=revision.id,
        iteration=0,
        deterministic_checks={"passed": True},
        semantic_review={"passed": True},
        passed=True,
        risk_level=data["review"].risk_level,
        required_changes=[],
        citations=[],
        quality_status=WorkflowQuality.NORMAL,
        created_at=created_at,
    )
    action = ApprovalAction(
        id=f"action-list-{suffix}",
        proposal_id=proposal.id,
        proposal_revision_id=revision.id,
        store_id=store.id,
        actor_id=actor.id,
        actor_role=actor.role,
        action=ApprovalActionType.SUBMIT,
        comment=None,
        idempotency_key_hash="e" * 64,
        request_hash="f" * 64,
        created_at=created_at,
    )
    session.add_all([review, action])
    proposal.current_revision_id = revision.id
    proposal.submitted_revision_id = revision.id
    await session.flush()
    return proposal, revision, action


def test_action_requests_enforce_resource_comment_and_extra_boundaries() -> None:
    request = ProposalCommentActionRequest(
        revision_id="r" * 36,
        comment="  请修订标题  ",
    )

    assert request.revision_id == "r" * 36
    assert request.comment == "请修订标题"
    assert ProposalActionRequest(revision_id="r").revision_id == "r"
    for values in (
        {"revision_id": ""},
        {"revision_id": "r" * 37},
        {"revision_id": "r", "unknown": "value"},
        {"revision_id": "r", "comment": "   "},
        {"revision_id": "r", "comment": "中" * 501},
        {"revision_id": "r", "comment": "需要\u0000修改"},
    ):
        model = ProposalCommentActionRequest if "comment" in values else ProposalActionRequest
        with pytest.raises(ValidationError):
            model.model_validate(values)


def test_action_view_is_safe_and_domain_error_supports_tracebacks() -> None:
    error = ApprovalDomainError("APPROVAL_STATE_CONFLICT", 409)
    with pytest.raises(ApprovalDomainError) as raised:
        raise error
    raised.value.__traceback__ = None

    assert (raised.value.code, raised.value.status_code) == (
        "APPROVAL_STATE_CONFLICT",
        409,
    )
    assert "idempotency" not in ApprovalActionView.model_json_schema()["properties"]
    assert "request_hash" not in ApprovalActionView.model_json_schema()["properties"]


@pytest.mark.parametrize("actor_name", ["operator", "supervisor", "admin"])
async def test_submit_commits_one_action_audit_and_pending_approval_for_all_roles(
    manual_client, manual_route_data, session, actor_name: str
) -> None:
    actor = manual_route_data[actor_name]
    product = manual_route_data["product"]
    product_before = (
        product.title,
        list(product.selling_points),
        product.description,
        list(product.search_keywords),
        dict(product.attributes),
        product.current_version,
    )

    response = await _submit(manual_client, actor)

    action = await session.scalar(select(ApprovalAction))
    audit = await session.scalar(select(AuditEvent))
    proposal = await session.get(ProductProposal, "proposal-1", populate_existing=True)
    run = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    assert action is not None and audit is not None and proposal is not None and run is not None
    assert response.json() == ApprovalActionView.model_validate(action).model_dump(
        mode="json"
    )
    assert (
        action.proposal_id,
        action.proposal_revision_id,
        action.store_id,
        action.actor_id,
        action.actor_role,
        action.action,
        action.comment,
    ) == (
        "proposal-1",
        "revision-1",
        "store-1",
        actor.id,
        actor.role,
        ApprovalActionType.SUBMIT,
        None,
    )
    assert len(action.idempotency_key_hash) == len(action.request_hash) == 64
    assert (
        proposal.submitted_revision_id,
        run.status,
        run.quality_status,
        run.current_step,
        run.error_code,
    ) == (
        "revision-1",
        WorkflowStatus.PENDING_APPROVAL,
        WorkflowQuality.NORMAL,
        "pending_approval",
        None,
    )
    assert (
        audit.event_type,
        audit.outcome,
        audit.actor_id,
        audit.actor_role,
        audit.proposal_id,
        audit.proposal_revision_id,
        audit.workflow_run_id,
        audit.approval_action_id,
        audit.error_code,
        audit.details,
    ) == (
        AuditEventType.PROPOSAL_SUBMITTED,
        AuditOutcome.SUCCESS,
        actor.id,
        actor.role,
        "proposal-1",
        "revision-1",
        "optimization-1",
        action.id,
        None,
        {
            "from_status": "draft_ready",
            "to_status": "pending_approval",
            "quality_status": "normal",
            "current_step": "pending_approval",
        },
    )
    assert product_before == (
        product.title,
        list(product.selling_points),
        product.description,
        list(product.search_keywords),
        dict(product.attributes),
        product.current_version,
    )
    serialized = response.text
    assert all(
        unsafe not in serialized
        for unsafe in (
            "submit-key",
            action.idempotency_key_hash,
            action.request_hash,
            "trusted_fact_hash",
        )
    )


@pytest.mark.parametrize(
    ("path", "body", "key", "expected_status"),
    [
        ("proposal-1", _body(), None, 400),
        ("proposal-1", _body(), "   ", 400),
        ("proposal-1", _body(), "k" * 129, 400),
        ("p" * 37, _body(), "key", 422),
        ("proposal-1", _body(""), "key", 422),
        ("proposal-1", _body("r" * 37), "key", 422),
    ],
)
async def test_submit_rejects_invalid_key_and_resource_bounds_before_writes(
    manual_client,
    manual_route_data,
    session,
    path: str,
    body: dict[str, object],
    key: str | None,
    expected_status: int,
) -> None:
    response = await manual_client.post(
        f"/proposals/{path}/submit",
        json=body,
        headers=_headers(manual_route_data["operator"], key),
    )

    assert response.status_code == expected_status
    assert await _action_counts(session) == (0, 0)


@pytest.mark.parametrize("path", ["reject", "request-changes"])
@pytest.mark.parametrize("comment", ["   ", "中" * 501, "需要\u0000修改"])
async def test_comment_routes_return_the_fixed_safe_code_for_invalid_comments(
    manual_client, manual_route_data, session, path: str, comment: str
) -> None:
    actor = manual_route_data["supervisor"]
    await _submit(manual_client, actor)
    before = await _action_counts(session)

    response = await manual_client.post(
        f"/approvals/proposal-1/{path}",
        json=_body(comment=comment),
        headers=_headers(actor, "invalid-comment-key"),
    )

    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "APPROVAL_COMMENT_INVALID"}}
    assert await _action_counts(session) == before


@pytest.mark.parametrize("path", ["reject", "request-changes"])
@pytest.mark.parametrize("key", [None, "   ", "k" * 129])
async def test_comment_actions_reject_invalid_idempotency_keys_without_writes(
    manual_client, manual_route_data, session, path: str, key: str | None
) -> None:
    actor = manual_route_data["supervisor"]
    await _submit(manual_client, actor)
    before = await _action_counts(session)

    response = await manual_client.post(
        f"/approvals/proposal-1/{path}",
        json=_body(comment="请修改"),
        headers=_headers(actor, key),
    )

    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "IDEMPOTENCY_KEY_INVALID"}}
    assert await _action_counts(session) == before


@pytest.mark.parametrize("path", ["reject", "request-changes"])
async def test_comment_action_routes_bound_proposal_and_revision_ids(
    manual_client, manual_route_data, session, path: str
) -> None:
    actor = manual_route_data["supervisor"]
    await _submit(manual_client, actor)
    before = await _action_counts(session)

    long_path = await manual_client.post(
        f"/approvals/{'p' * 37}/{path}",
        json=_body(comment="请修改"),
        headers=_headers(actor, "long-path-key"),
    )
    long_revision = await manual_client.post(
        f"/approvals/proposal-1/{path}",
        json=_body("r" * 37, comment="请修改"),
        headers=_headers(actor, "long-revision-key"),
    )

    assert (long_path.status_code, long_revision.status_code) == (422, 422)
    assert await _action_counts(session) == before


@pytest.mark.parametrize(
    "case",
    [
        "disabled-actor",
        "disabled-store",
        "missing-scope",
        "cross-store",
        "wrong-run-store",
        "wrong-run-type",
        "wrong-run-input",
        "wrong-product-store",
        "unknown-revision",
    ],
)
async def test_submit_hides_invisible_or_inconsistently_owned_resources(
    manual_client, manual_route_data, session, case: str
) -> None:
    actor = manual_route_data["operator"]
    revision_id = "revision-1"
    if case == "disabled-actor":
        statement = update(User).where(User.id == actor.id).values(status=UserStatus.DISABLED)
    elif case == "disabled-store":
        statement = update(Store).where(Store.id == "store-1").values(enabled=False)
    elif case == "missing-scope":
        statement = delete(UserStoreScope).where(
            UserStoreScope.user_id == actor.id,
            UserStoreScope.store_id == "store-1",
        )
    elif case == "cross-store":
        actor = manual_route_data["other"]
        statement = None
    elif case == "wrong-run-store":
        statement = update(WorkflowRun).where(WorkflowRun.id == "optimization-1").values(
            store_id="store-2"
        )
    elif case == "wrong-run-type":
        statement = update(WorkflowRun).where(WorkflowRun.id == "optimization-1").values(
            workflow_type=WorkflowType.MANUAL_REVIEW,
            status=WorkflowStatus.COMPLETED,
        )
    elif case == "wrong-run-input":
        statement = update(WorkflowRun).where(WorkflowRun.id == "optimization-1").values(
            input={"proposal_id": "other"}
        )
    elif case == "wrong-product-store":
        statement = update(Product).where(Product.id == "product-1").values(store_id="store-2")
    else:
        revision_id = "unknown-revision"
        statement = None
    if statement is not None:
        await session.execute(statement.execution_options(synchronize_session=False))
    await session.commit()

    response = await manual_client.post(
        "/proposals/proposal-1/submit",
        json=_body(revision_id),
        headers=_headers(actor),
    )

    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "PROPOSAL_NOT_FOUND"}}
    assert await _action_counts(session) == (0, 0)


@pytest.mark.parametrize(
    ("case", "expected_status", "expected_code"),
    [
        ("run-state", 409, "PROPOSAL_NOT_SUBMITTABLE"),
        ("stale-revision", 409, "PROPOSAL_NOT_SUBMITTABLE"),
        ("review-failed", 409, "PROPOSAL_NOT_SUBMITTABLE"),
        ("review-quality", 409, "PROPOSAL_NOT_SUBMITTABLE"),
        ("review-error", 409, "PROPOSAL_NOT_SUBMITTABLE"),
        ("review-chain", 503, "PROPOSAL_DATA_INCONSISTENT"),
        ("product-version", 409, "PRODUCT_VERSION_CONFLICT"),
        ("citation-current", 422, "TRUSTED_EVIDENCE_INVALID"),
    ],
)
async def test_submit_rechecks_state_review_version_and_current_citations(
    manual_client,
    manual_route_data,
    session,
    case: str,
    expected_status: int,
    expected_code: str,
) -> None:
    if case == "run-state":
        statement = update(WorkflowRun).where(WorkflowRun.id == "optimization-1").values(
            status=WorkflowStatus.PENDING_MANUAL,
            current_step="manual_review_pending",
        )
    elif case == "stale-revision":
        statement = update(ProductProposal).where(ProductProposal.id == "proposal-1").values(
            current_revision_id=None
        )
    elif case == "review-failed":
        statement = update(ComplianceReview).where(ComplianceReview.id == "review-1").values(
            passed=False
        )
    elif case == "review-quality":
        statement = update(ComplianceReview).where(ComplianceReview.id == "review-1").values(
            quality_status=WorkflowQuality.DEGRADED
        )
    elif case == "review-error":
        statement = update(ComplianceReview).where(ComplianceReview.id == "review-1").values(
            error_code="COMPLIANCE_AGENT_DEGRADED"
        )
    elif case == "review-chain":
        statement = update(ComplianceReview).where(ComplianceReview.id == "review-1").values(
            citations=[]
        )
    elif case == "product-version":
        statement = update(Product).where(Product.id == "product-1").values(current_version=8)
    else:
        statement = update(KnowledgeDocument).where(KnowledgeDocument.id == "document-1").values(
            current_version_id=None
        )
    await session.execute(statement.execution_options(synchronize_session=False))
    await session.commit()

    response = await manual_client.post(
        "/proposals/proposal-1/submit",
        json=_body(),
        headers=_headers(manual_route_data["operator"]),
    )

    assert response.status_code == expected_status
    assert response.json() == {"detail": {"code": expected_code}}
    assert await _action_counts(session) == (0, 0)


async def test_submit_rejects_an_active_manual_review_even_if_status_is_corruptly_draft_ready(
    manual_client, manual_route_data, session
) -> None:
    actor = manual_route_data["operator"]
    manual = await manual_client.post(
        "/proposals/proposal-1/manual-revision",
        json=_manual_body(),
        headers=_headers(actor, "manual-before-submit"),
    )
    assert manual.status_code == 202
    await session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == "optimization-1")
        .values(status=WorkflowStatus.DRAFT_READY, current_step="draft_ready")
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    before = await _action_counts(session)

    response = await manual_client.post(
        "/proposals/proposal-1/submit",
        json=_body(manual.json()["revision_id"]),
        headers=_headers(actor, "submit-with-active-manual"),
    )

    assert response.status_code == 409
    assert response.json() == {"detail": {"code": "PROPOSAL_NOT_SUBMITTABLE"}}
    assert await _action_counts(session) == before


@pytest.mark.parametrize(
    ("actor_name", "action_name", "path", "expected_status"),
    [
        ("operator", "reject", "reject", 403),
        ("operator", "request_changes", "request-changes", 403),
        ("supervisor", "reject", "reject", 201),
        ("admin", "request_changes", "request-changes", 201),
    ],
)
async def test_terminal_actions_enforce_role_and_allow_supervisor_admin_self_review(
    manual_client,
    manual_route_data,
    session,
    actor_name: str,
    action_name: str,
    path: str,
    expected_status: int,
) -> None:
    actor = manual_route_data[actor_name]
    await _submit(manual_client, actor)
    product = manual_route_data["product"]
    product_before = (
        product.title,
        list(product.selling_points),
        product.description,
        list(product.search_keywords),
        dict(product.attributes),
        product.current_version,
    )

    response = await manual_client.post(
        f"/approvals/proposal-1/{path}",
        json=_body(comment="  请补充可信说明  "),
        headers=_headers(actor, f"{action_name}-key"),
    )

    assert response.status_code == expected_status
    proposal = await session.get(ProductProposal, "proposal-1", populate_existing=True)
    run = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    assert proposal is not None and run is not None
    if expected_status == 403:
        assert response.json() == {"detail": {"code": "PROPOSAL_ACTION_FORBIDDEN"}}
        assert await _model_count(session, ApprovalAction) == 1
        audits = list(await session.scalars(select(AuditEvent).order_by(AuditEvent.created_at)))
        assert len(audits) == 2
        assert (
            audits[-1].event_type,
            audits[-1].outcome,
            audits[-1].error_code,
            audits[-1].details,
        ) == (
            AuditEventType.AUTHORIZATION_DENIED,
            AuditOutcome.DENIED,
            "PROPOSAL_ACTION_FORBIDDEN",
            {},
        )
        assert run.status is WorkflowStatus.PENDING_APPROVAL
        assert proposal.submitted_revision_id == "revision-1"
        return
    action = await session.scalar(
        select(ApprovalAction).where(ApprovalAction.action == ApprovalActionType(action_name))
    )
    audit = await session.scalar(
        select(AuditEvent).where(
            AuditEvent.event_type
            == (
                AuditEventType.PROPOSAL_REJECTED
                if action_name == "reject"
                else AuditEventType.PROPOSAL_CHANGES_REQUESTED
            )
        )
    )
    assert action is not None and action.comment == "请补充可信说明"
    assert audit is not None and audit.details == {
        "from_status": "pending_approval",
        "to_status": "rejected" if action_name == "reject" else "pending_manual",
        "quality_status": "normal",
        "current_step": "rejected" if action_name == "reject" else "approval_changes_requested",
    }
    assert "请补充可信说明" not in str(audit.details)
    assert run.status is (
        WorkflowStatus.REJECTED
        if action_name == "reject"
        else WorkflowStatus.PENDING_MANUAL
    )
    assert proposal.submitted_revision_id == (
        "revision-1" if action_name == "reject" else None
    )
    assert product_before == (
        product.title,
        list(product.selling_points),
        product.description,
        list(product.search_keywords),
        dict(product.attributes),
        product.current_version,
    )


async def test_admin_requires_exact_scope_for_submit(
    manual_client, manual_route_data, session
) -> None:
    admin = manual_route_data["admin"]
    await session.execute(
        delete(UserStoreScope).where(
            UserStoreScope.user_id == admin.id,
            UserStoreScope.store_id == "store-1",
        )
    )
    await session.commit()

    response = await manual_client.post(
        "/proposals/proposal-1/submit",
        json=_body(),
        headers=_headers(admin, "admin-without-scope"),
    )

    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "PROPOSAL_NOT_FOUND"}}
    assert await _action_counts(session) == (0, 0)


async def test_terminal_action_refreshes_current_role_before_authorization(
    manual_client, manual_route_data, session
) -> None:
    actor = manual_route_data["supervisor"]
    headers = _headers(actor, "fresh-role-key")
    await _submit(manual_client, actor)
    await session.execute(
        update(User)
        .where(User.id == actor.id)
        .values(role=UserRole.OPERATOR)
        .execution_options(synchronize_session=False)
    )
    await session.commit()

    response = await manual_client.post(
        "/approvals/proposal-1/reject",
        json=_body(comment="拒绝原因"),
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json() == {"detail": {"code": "PROPOSAL_ACTION_FORBIDDEN"}}
    assert await _action_counts(session) == (1, 2)


@pytest.mark.parametrize(
    ("path", "terminal_status", "submitted_revision"),
    [
        ("submit", WorkflowStatus.PENDING_APPROVAL, "revision-1"),
        ("reject", WorkflowStatus.REJECTED, "revision-1"),
        ("request-changes", WorkflowStatus.PENDING_MANUAL, None),
    ],
)
async def test_actions_exactly_replay_from_their_successful_terminal_chain(
    manual_client,
    manual_route_data,
    session,
    path: str,
    terminal_status: WorkflowStatus,
    submitted_revision: str | None,
) -> None:
    actor = manual_route_data["supervisor"]
    if path == "submit":
        route = "/proposals/proposal-1/submit"
        body = _body()
        headers = _headers(actor, "submit-replay")
        first = await manual_client.post(route, json=body, headers=headers)
    else:
        await _submit(manual_client, actor)
        route = f"/approvals/proposal-1/{path}"
        body = _body(comment="请重新核对")
        headers = _headers(actor, f"{path}-replay")
        first = await manual_client.post(route, json=body, headers=headers)
    assert first.status_code == 201
    before = await _action_counts(session)

    replay = await manual_client.post(route, json=body, headers=headers)

    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert await _action_counts(session) == before
    proposal = await session.get(ProductProposal, "proposal-1", populate_existing=True)
    run = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    assert proposal is not None and proposal.submitted_revision_id == submitted_revision
    assert run is not None and run.status is terminal_status


@pytest.mark.parametrize(
    ("terminal_status", "submitted_revision"),
    [
        (WorkflowStatus.REJECTED, "revision-1"),
        (WorkflowStatus.PENDING_MANUAL, None),
        (WorkflowStatus.COMPLETED, "revision-1"),
    ],
)
async def test_submit_replay_rejects_a_forged_later_post_state_without_its_action_chain(
    manual_client,
    manual_route_data,
    session,
    terminal_status: WorkflowStatus,
    submitted_revision: str | None,
) -> None:
    actor = manual_route_data["supervisor"]
    headers = _headers(actor, "submit-forged-later-state")
    first = await manual_client.post(
        "/proposals/proposal-1/submit",
        json=_body(),
        headers=headers,
    )
    assert first.status_code == 201
    await session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == "optimization-1")
        .values(
            status=terminal_status,
            current_step=(
                "rejected"
                if terminal_status is WorkflowStatus.REJECTED
                else "approval_changes_requested"
                if terminal_status is WorkflowStatus.PENDING_MANUAL
                else "simulated_published"
            ),
        )
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(ProductProposal)
        .where(ProductProposal.id == "proposal-1")
        .values(submitted_revision_id=submitted_revision)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    before = await _action_counts(session)

    replay = await manual_client.post(
        "/proposals/proposal-1/submit",
        json=_body(),
        headers=headers,
    )

    assert replay.status_code == 503
    assert replay.json() == {"detail": {"code": "PROPOSAL_DATA_INCONSISTENT"}}
    assert await _action_counts(session) == before
    run = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    proposal = await session.get(ProductProposal, "proposal-1", populate_existing=True)
    assert run is not None and run.status is terminal_status
    assert proposal is not None and proposal.submitted_revision_id == submitted_revision


@pytest.mark.parametrize("path", ["reject", "request-changes"])
async def test_terminal_replay_rejects_a_missing_prior_submit_audit_chain(
    manual_client, manual_route_data, session, path: str
) -> None:
    actor = manual_route_data["supervisor"]
    await _submit(manual_client, actor)
    headers = _headers(actor, f"{path}-broken-prior-submit")
    first = await manual_client.post(
        f"/approvals/proposal-1/{path}",
        json=_body(comment="原始意见"),
        headers=headers,
    )
    assert first.status_code == 201
    submit_action = await session.scalar(
        select(ApprovalAction).where(ApprovalAction.action == ApprovalActionType.SUBMIT)
    )
    assert submit_action is not None
    await session.execute(
        delete(AuditEvent).where(AuditEvent.approval_action_id == submit_action.id)
    )
    await session.commit()
    before = await _action_counts(session)

    replay = await manual_client.post(
        f"/approvals/proposal-1/{path}",
        json=_body(comment="原始意见"),
        headers=headers,
    )

    assert replay.status_code == 503
    assert replay.json() == {"detail": {"code": "PROPOSAL_DATA_INCONSISTENT"}}
    assert await _action_counts(session) == before


@pytest.mark.parametrize("path", ["reject", "request-changes"])
async def test_comment_action_same_key_different_request_conflicts_without_writes(
    manual_client, manual_route_data, session, path: str
) -> None:
    actor = manual_route_data["supervisor"]
    await _submit(manual_client, actor)
    headers = _headers(actor, "same-key")
    first = await manual_client.post(
        f"/approvals/proposal-1/{path}",
        json=_body(comment="原意见"),
        headers=headers,
    )
    assert first.status_code == 201
    before = await _action_counts(session)

    conflict = await manual_client.post(
        f"/approvals/proposal-1/{path}",
        json=_body(comment="不同意见"),
        headers=headers,
    )

    assert conflict.status_code == 409
    assert conflict.json() == {"detail": {"code": "IDEMPOTENCY_REPLAY_CONFLICT"}}
    assert await _action_counts(session) == before


async def test_submit_same_key_different_owned_revision_conflicts_before_state_gate(
    manual_client, manual_route_data, session
) -> None:
    actor = manual_route_data["operator"]
    headers = _headers(actor, "same-submit-key")
    first = await manual_client.post(
        "/proposals/proposal-1/submit",
        json=_body(),
        headers=headers,
    )
    assert first.status_code == 201
    parent = manual_route_data["parent"]
    session.add(
        ProposalRevision(
            id="revision-2",
            proposal_id="proposal-1",
            iteration=None,
            revision_number=2,
            origin=ProposalRevisionOrigin.MANUAL,
            created_by=actor.id,
            parent_revision_id="revision-1",
            base_product_version=7,
            trusted_fact_hash="e" * 64,
            proposal_output=parent.proposal_output,
            citations=parent.citations,
        )
    )
    await session.commit()
    before = await _action_counts(session)

    conflict = await manual_client.post(
        "/proposals/proposal-1/submit",
        json=_body("revision-2"),
        headers=headers,
    )

    assert conflict.status_code == 409
    assert conflict.json() == {"detail": {"code": "IDEMPOTENCY_REPLAY_CONFLICT"}}
    assert await _action_counts(session) == before


@pytest.mark.parametrize(
    ("winner", "loser"),
    [("reject", "request-changes"), ("request-changes", "reject")],
)
async def test_conflicting_terminal_action_winner_is_append_only(
    manual_client, manual_route_data, session, winner: str, loser: str
) -> None:
    actor = manual_route_data["supervisor"]
    await _submit(manual_client, actor)
    first = await manual_client.post(
        f"/approvals/proposal-1/{winner}",
        json=_body(comment="首个动作"),
        headers=_headers(actor, "winner-key"),
    )
    assert first.status_code == 201
    before = await _action_counts(session)

    conflict = await manual_client.post(
        f"/approvals/proposal-1/{loser}",
        json=_body(comment="冲突动作"),
        headers=_headers(actor, "loser-key"),
    )

    assert conflict.status_code == 409
    assert conflict.json() == {"detail": {"code": "APPROVAL_ACTION_CONFLICT"}}
    assert await _action_counts(session) == before


@pytest.mark.parametrize("path", ["submit", "reject", "request-changes"])
async def test_action_commit_failure_rolls_back_action_audit_and_state(
    manual_client, manual_route_data, session, monkeypatch, path: str
) -> None:
    actor = manual_route_data["supervisor"]
    if path != "submit":
        await _submit(manual_client, actor)
    before = await _action_counts(session)
    proposal = await session.get(ProductProposal, "proposal-1")
    run = await session.get(WorkflowRun, "optimization-1")
    assert proposal is not None and run is not None
    before_state = (proposal.submitted_revision_id, run.status, run.current_step)

    async def fail_commit() -> None:
        raise SQLAlchemyError("simulated commit failure")

    monkeypatch.setattr(session, "commit", fail_commit)
    route = (
        "/proposals/proposal-1/submit"
        if path == "submit"
        else f"/approvals/proposal-1/{path}"
    )
    response = await manual_client.post(
        route,
        json=_body(**({} if path == "submit" else {"comment": "失败意见"})),
        headers=_headers(actor, "failure-key"),
    )

    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "PROPOSAL_DATA_INCONSISTENT"}}
    monkeypatch.undo()
    await session.rollback()
    proposal = await session.get(ProductProposal, "proposal-1", populate_existing=True)
    run = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    assert proposal is not None and run is not None
    assert (proposal.submitted_revision_id, run.status, run.current_step) == before_state
    assert await _action_counts(session) == before


@pytest.mark.parametrize(
    ("case", "expected_status", "expected_code"),
    [
        ("state", 409, "APPROVAL_STATE_CONFLICT"),
        ("stale-revision", 409, "APPROVAL_STATE_CONFLICT"),
        ("version", 409, "PRODUCT_VERSION_CONFLICT"),
        ("citation", 422, "TRUSTED_EVIDENCE_INVALID"),
        ("review-chain", 503, "PROPOSAL_DATA_INCONSISTENT"),
    ],
)
async def test_terminal_action_rechecks_pending_pointer_version_review_and_citations(
    manual_client,
    manual_route_data,
    session,
    case: str,
    expected_status: int,
    expected_code: str,
) -> None:
    actor = manual_route_data["supervisor"]
    await _submit(manual_client, actor)
    revision_id = "revision-1"
    if case == "state":
        statement = update(WorkflowRun).where(WorkflowRun.id == "optimization-1").values(
            status=WorkflowStatus.DRAFT_READY,
            current_step="draft_ready",
        )
    elif case == "stale-revision":
        revision_id = "stale-revision"
        session.add(
            ProposalRevision(
                id=revision_id,
                proposal_id="proposal-1",
                iteration=None,
                revision_number=2,
                origin=ProposalRevisionOrigin.MANUAL,
                created_by=actor.id,
                parent_revision_id="revision-1",
                base_product_version=7,
                trusted_fact_hash="e" * 64,
                proposal_output=manual_route_data["parent"].proposal_output,
                citations=manual_route_data["parent"].citations,
            )
        )
        statement = None
    elif case == "version":
        statement = update(Product).where(Product.id == "product-1").values(current_version=8)
    elif case == "citation":
        statement = update(KnowledgeDocument).where(KnowledgeDocument.id == "document-1").values(
            current_version_id=None
        )
    else:
        statement = update(ComplianceReview).where(ComplianceReview.id == "review-1").values(
            citations=[]
        )
    if statement is not None:
        await session.execute(statement.execution_options(synchronize_session=False))
    await session.commit()

    response = await manual_client.post(
        "/approvals/proposal-1/reject",
        json=_body(revision_id, comment="拒绝原因"),
        headers=_headers(actor, "reject-check-key"),
    )

    assert response.status_code == expected_status
    assert response.json() == {"detail": {"code": expected_code}}
    assert await _action_counts(session) == (1, 1)


@pytest.mark.parametrize("ownership_break", ["actor", "scope", "store", "product", "run"])
async def test_exact_replay_rechecks_fresh_authorization_and_ownership(
    manual_client, manual_route_data, session, ownership_break: str
) -> None:
    actor = manual_route_data["supervisor"]
    headers = _headers(actor, "submit-replay-guard")
    first = await manual_client.post(
        "/proposals/proposal-1/submit",
        json=_body(),
        headers=headers,
    )
    assert first.status_code == 201
    before = await _action_counts(session)
    if ownership_break == "actor":
        statement = update(User).where(User.id == actor.id).values(status=UserStatus.DISABLED)
    elif ownership_break == "scope":
        statement = delete(UserStoreScope).where(
            UserStoreScope.user_id == actor.id,
            UserStoreScope.store_id == "store-1",
        )
    elif ownership_break == "store":
        statement = update(Store).where(Store.id == "store-1").values(enabled=False)
    elif ownership_break == "product":
        statement = update(Product).where(Product.id == "product-1").values(store_id="store-2")
    else:
        statement = update(WorkflowRun).where(WorkflowRun.id == "optimization-1").values(
            input={"proposal_id": "other"}
        )
    await session.execute(statement.execution_options(synchronize_session=False))
    await session.commit()

    replay = await manual_client.post(
        "/proposals/proposal-1/submit",
        json=_body(),
        headers=headers,
    )

    assert replay.status_code == 404
    assert replay.json() == {"detail": {"code": "PROPOSAL_NOT_FOUND"}}
    assert await _action_counts(session) == before


@pytest.mark.parametrize("path", ["reject", "request-changes"])
async def test_terminal_exact_replay_rechecks_fresh_scope(
    manual_client, manual_route_data, session, path: str
) -> None:
    actor = manual_route_data["supervisor"]
    await _submit(manual_client, actor)
    headers = _headers(actor, f"{path}-fresh-scope")
    first = await manual_client.post(
        f"/approvals/proposal-1/{path}",
        json=_body(comment="原始意见"),
        headers=headers,
    )
    assert first.status_code == 201
    before = await _action_counts(session)
    await session.execute(
        delete(UserStoreScope).where(
            UserStoreScope.user_id == actor.id,
            UserStoreScope.store_id == "store-1",
        )
    )
    await session.commit()

    replay = await manual_client.post(
        f"/approvals/proposal-1/{path}",
        json=_body(comment="原始意见"),
        headers=headers,
    )

    assert replay.status_code == 404
    assert replay.json() == {"detail": {"code": "PROPOSAL_NOT_FOUND"}}
    assert await _action_counts(session) == before


@pytest.mark.parametrize("different_key", [False, True], ids=["same-key", "different-key"])
@pytest.mark.parametrize("action_name", ["submit", "reject", "request_changes"])
async def test_integrity_race_reloads_and_replays_the_exact_action_winner(
    manual_route_data, session, monkeypatch, action_name: str, different_key: bool
) -> None:
    from backend.approvals import (
        reject_proposal,
        request_proposal_changes,
        submit_proposal,
    )
    from backend.audit_events import add_audit_event

    actor = manual_route_data["supervisor"]
    actor_id = actor.id
    actor_role = actor.role
    if action_name == "submit":
        service = submit_proposal
        request = ProposalActionRequest(revision_id="revision-1")
        action_type = ApprovalActionType.SUBMIT
        event_type = AuditEventType.PROPOSAL_SUBMITTED
        from_status = WorkflowStatus.DRAFT_READY
        to_status = WorkflowStatus.PENDING_APPROVAL
        current_step = "pending_approval"
    else:
        submit = await submit_proposal(
            session,
            actor_id=actor.id,
            proposal_id="proposal-1",
            request=ProposalActionRequest(revision_id="revision-1"),
            idempotency_key="race-prior-submit",
            request_id="race-prior-submit-request",
        )
        assert submit.created is True
        request = ProposalCommentActionRequest(
            revision_id="revision-1", comment="并发动作意见"
        )
        from_status = WorkflowStatus.PENDING_APPROVAL
        if action_name == "reject":
            service = reject_proposal
            action_type = ApprovalActionType.REJECT
            event_type = AuditEventType.PROPOSAL_REJECTED
            to_status = WorkflowStatus.REJECTED
            current_step = "rejected"
        else:
            service = request_proposal_changes
            action_type = ApprovalActionType.REQUEST_CHANGES
            event_type = AuditEventType.PROPOSAL_CHANGES_REQUESTED
            to_status = WorkflowStatus.PENDING_MANUAL
            current_step = "approval_changes_requested"

    original_flush = session.flush
    raced = False

    async def race_flush(*args, **kwargs) -> None:
        nonlocal raced
        if raced:
            await original_flush(*args, **kwargs)
            return
        winner = next(
            row
            for row in session.new
            if isinstance(row, ApprovalAction) and row.action is action_type
        )
        raced = True
        await session.rollback()
        if different_key:
            winner.idempotency_key_hash = hashlib.sha256(
                f"{action_name}-winner-key".encode("utf-8")
            ).hexdigest()
        session.add(winner)
        await original_flush()
        add_audit_event(
            session,
            event_type=event_type,
            outcome=AuditOutcome.SUCCESS,
            store_id="store-1",
            actor_id=actor_id,
            actor_role=actor_role,
            proposal_id="proposal-1",
            proposal_revision_id="revision-1",
            workflow_run_id="optimization-1",
            approval_action_id=winner.id,
            request_id="race-winner-request",
            details={
                "from_status": from_status.value,
                "to_status": to_status.value,
                "quality_status": WorkflowQuality.NORMAL.value,
                "current_step": current_step,
            },
        )
        await original_flush()
        await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == "optimization-1")
            .values(
                status=to_status,
                quality_status=WorkflowQuality.NORMAL,
                current_step=current_step,
                error_code=None,
            )
        )
        await session.execute(
            update(ProductProposal)
            .where(ProductProposal.id == "proposal-1")
            .values(
                submitted_revision_id=(
                    None
                    if action_type is ApprovalActionType.REQUEST_CHANGES
                    else "revision-1"
                )
            )
        )
        await session.commit()
        raise IntegrityError("insert", {}, RuntimeError("simulated action race"))

    monkeypatch.setattr(session, "flush", race_flush)
    if different_key:
        with pytest.raises(ApprovalDomainError) as error:
            await service(
                session,
                actor_id=actor.id,
                proposal_id="proposal-1",
                request=request,
                idempotency_key=f"{action_name}-race-key",
                request_id=f"{action_name}-race-request",
            )
        assert (error.value.code, error.value.status_code) == (
            "APPROVAL_ACTION_CONFLICT",
            409,
        )
    else:
        result = await service(
            session,
            actor_id=actor.id,
            proposal_id="proposal-1",
            request=request,
            idempotency_key=f"{action_name}-race-key",
            request_id=f"{action_name}-race-request",
        )
        assert result.created is False
    expected = 1 if action_name == "submit" else 2
    assert await _action_counts(session) == (expected, expected)


@pytest.mark.parametrize("actor_name", ["supervisor", "admin"])
async def test_approve_atomically_publishes_only_the_allowed_listing_fields_once(
    manual_client, manual_route_data, session, actor_name: str
) -> None:
    actor = manual_route_data[actor_name]
    parent = manual_route_data["parent"]
    await _prepare_publish_output(session, parent)
    await _seed_immutable_business_facts(session)
    await _submit(manual_client, actor)
    product = await session.get(Product, "product-1", populate_existing=True)
    sku = await session.get(ProductSku, "sku-1", populate_existing=True)
    inventory = await session.get(InventorySnapshot, "inventory-1")
    order = await session.get(Order, "order-1")
    order_item = await session.get(OrderItem, "order-item-1")
    traffic = await session.get(TrafficDaily, "traffic-1")
    assert all(value is not None for value in (product, sku, inventory, order, order_item, traffic))
    product_immutable_before = (
        product.code,
        product.category,
        product.brand,
        product.enabled,
    )
    sku_before = (sku.code, dict(sku.spec), sku.price, sku.current_stock)
    inventory_before = (inventory.on_hand, inventory.inbound)
    order_before = (order.status, order.total_amount)
    order_item_before = (
        order_item.quantity,
        order_item.unit_price,
        order_item.refund_status,
    )
    traffic_before = (
        traffic.impressions,
        traffic.clicks,
        traffic.visitors,
        traffic.add_to_carts,
    )

    response = await _approve(manual_client, actor)

    assert response.status_code == 201
    record = await session.scalar(select(PublishRecord))
    action = await session.scalar(
        select(ApprovalAction).where(ApprovalAction.action == ApprovalActionType.APPROVE)
    )
    assert record is not None and action is not None
    assert response.json() == PublishRecordView.model_validate(record).model_dump(mode="json")
    product = await session.get(Product, "product-1", populate_existing=True)
    sku = await session.get(ProductSku, "sku-1", populate_existing=True)
    inventory = await session.get(InventorySnapshot, "inventory-1", populate_existing=True)
    order = await session.get(Order, "order-1", populate_existing=True)
    order_item = await session.get(OrderItem, "order-item-1", populate_existing=True)
    traffic = await session.get(TrafficDaily, "traffic-1", populate_existing=True)
    run = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    assert all(value is not None for value in (product, sku, inventory, order, order_item, traffic, run))
    assert (
        product.title,
        product.selling_points,
        product.description,
        product.search_keywords,
        product.attributes,
        product.current_version,
    ) == (
        "优选精梳棉家居商品",
        ["精梳棉触感", "适合日常家居"],
        "商品详情\n精梳棉材质，适合日常使用。\n\n使用说明\n请按洗护标签清洁。",
        ["精梳棉", "家居"],
        {"材质": "精梳棉"},
        8,
    )
    assert product_immutable_before == (
        product.code,
        product.category,
        product.brand,
        product.enabled,
    )
    assert sku_before == (sku.code, dict(sku.spec), sku.price, sku.current_stock)
    assert inventory_before == (inventory.on_hand, inventory.inbound)
    assert order_before == (order.status, order.total_amount)
    assert order_item_before == (
        order_item.quantity,
        order_item.unit_price,
        order_item.refund_status,
    )
    assert traffic_before == (
        traffic.impressions,
        traffic.clicks,
        traffic.visitors,
        traffic.add_to_carts,
    )
    assert (run.status, run.quality_status, run.current_step, run.error_code) == (
        WorkflowStatus.COMPLETED,
        WorkflowQuality.NORMAL,
        "simulated_published",
        None,
    )
    assert (
        record.proposal_id,
        record.proposal_revision_id,
        record.product_id,
        record.store_id,
        record.approved_by,
        record.approval_action_id,
        record.base_product_version,
        record.published_product_version,
    ) == (
        "proposal-1",
        "revision-1",
        "product-1",
        "store-1",
        actor.id,
        action.id,
        7,
        8,
    )
    assert set(record.before_snapshot) == set(record.after_snapshot) == {
        "title",
        "selling_points",
        "description",
        "search_keywords",
        "attributes",
        "current_version",
    }
    assert record.before_snapshot["current_version"] == 7
    assert record.after_snapshot == {
        "title": product.title,
        "selling_points": product.selling_points,
        "description": product.description,
        "search_keywords": product.search_keywords,
        "attributes": product.attributes,
        "current_version": 8,
    }
    audits = list(
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type.in_(
                    {
                        AuditEventType.PROPOSAL_APPROVED,
                        AuditEventType.SIMULATED_PUBLISH_COMPLETED,
                    }
                )
            )
        )
    )
    assert len(audits) == 2
    assert {audit.approval_action_id for audit in audits} == {action.id}
    assert {audit.publish_record_id for audit in audits} == {record.id}
    assert next(
        audit for audit in audits if audit.event_type is AuditEventType.PROPOSAL_APPROVED
    ).details == {
        "from_status": "pending_approval",
        "to_status": "completed",
        "quality_status": "normal",
        "current_step": "simulated_published",
    }
    assert next(
        audit
        for audit in audits
        if audit.event_type is AuditEventType.SIMULATED_PUBLISH_COMPLETED
    ).details == {
        "changed_fields": [
            "title",
            "selling_points",
            "description",
            "keywords",
            "attribute_completions",
        ],
        "published_from_version": 7,
        "published_to_version": 8,
    }
    assert await _model_count(session, ApprovalAction) == 2
    assert await _publish_count(session) == 1
    assert await _model_count(session, AuditEvent) == 3
    serialized = response.text
    assert "approve-key" not in serialized
    assert record.publish_idempotency_hash not in serialized
    assert action.idempotency_key_hash not in serialized
    assert action.request_hash not in serialized


async def test_operator_approve_is_denied_with_one_safe_audit_and_no_publish(
    manual_client, manual_route_data, session
) -> None:
    actor = manual_route_data["operator"]
    await _submit(manual_client, actor)
    product = await session.get(Product, "product-1")
    assert product is not None
    before = (product.title, product.current_version, await _action_counts(session))

    response = await _approve(manual_client, actor)

    assert response.status_code == 403
    assert response.json() == {"detail": {"code": "PROPOSAL_ACTION_FORBIDDEN"}}
    product = await session.get(Product, "product-1", populate_existing=True)
    assert product is not None and (product.title, product.current_version) == before[:2]
    assert await _model_count(session, ApprovalAction) == 1
    assert await _publish_count(session) == 0
    audits = list(await session.scalars(select(AuditEvent).order_by(AuditEvent.created_at)))
    assert len(audits) == before[2][1] + 1
    assert (
        audits[-1].event_type,
        audits[-1].outcome,
        audits[-1].error_code,
        audits[-1].details,
    ) == (
        AuditEventType.AUTHORIZATION_DENIED,
        AuditOutcome.DENIED,
        "PROPOSAL_ACTION_FORBIDDEN",
        {},
    )


@pytest.mark.parametrize("actor_name", ["operator", "supervisor", "admin"])
async def test_approve_requires_fresh_exact_scope_for_every_role(
    manual_client, manual_route_data, session, actor_name: str
) -> None:
    actor = manual_route_data[actor_name]
    await _submit(manual_client, actor)
    await session.execute(
        delete(UserStoreScope).where(
            UserStoreScope.user_id == actor.id,
            UserStoreScope.store_id == "store-1",
        )
    )
    await session.commit()
    before = await _action_counts(session)

    response = await _approve(manual_client, actor)

    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "PROPOSAL_NOT_FOUND"}}
    assert await _action_counts(session) == before
    assert await _publish_count(session) == 0


async def test_approve_exact_and_completed_new_key_replay_return_one_publish(
    manual_client, manual_route_data, session
) -> None:
    actor = manual_route_data["supervisor"]
    await _prepare_publish_output(session, manual_route_data["parent"])
    await _submit(manual_client, actor)
    first = await _approve(manual_client, actor, key="approve-replay-key")
    assert first.status_code == 201
    before = (await _action_counts(session), await _publish_count(session))

    same_key = await _approve(manual_client, actor, key="approve-replay-key")
    new_key = await _approve(manual_client, actor, key="approve-new-key")

    assert same_key.status_code == new_key.status_code == 200
    assert same_key.json() == new_key.json() == first.json()
    assert (await _action_counts(session), await _publish_count(session)) == before
    product = await session.get(Product, "product-1", populate_existing=True)
    assert product is not None and product.current_version == 8


async def test_approval_list_enforces_scope_stable_pagination_total_and_safe_output(
    manual_client, manual_route_data, session, caplog
) -> None:
    created_at = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    first, _, _ = await _pending_list_item(
        session,
        manual_route_data,
        suffix="a",
        store=manual_route_data["store"],
        product=manual_route_data["product"],
        actor=manual_route_data["operator"],
        created_at=created_at,
    )
    second, _, _ = await _pending_list_item(
        session,
        manual_route_data,
        suffix="b",
        store=manual_route_data["store"],
        product=manual_route_data["product"],
        actor=manual_route_data["supervisor"],
        created_at=created_at,
    )
    rejected, _, _ = await _pending_list_item(
        session,
        manual_route_data,
        suffix="rejected",
        store=manual_route_data["store"],
        product=manual_route_data["product"],
        actor=manual_route_data["operator"],
        created_at=created_at,
    )
    rejected_run = await session.get(WorkflowRun, rejected.optimization_run_id)
    assert rejected_run is not None
    rejected_run.status = WorkflowStatus.REJECTED
    rejected_run.current_step = "rejected"
    await _pending_list_item(
        session,
        manual_route_data,
        suffix="foreign",
        store=manual_route_data["other_store"],
        product=manual_route_data["other_product"],
        actor=manual_route_data["other"],
        created_at=created_at,
    )
    await session.commit()

    for actor_name in ("supervisor", "admin"):
        first_page = await manual_client.get(
            "/approvals?page=1&page_size=1",
            headers=_headers(manual_route_data[actor_name], None),
        )
        second_page = await manual_client.get(
            "/approvals?page=2&page_size=1",
            headers=_headers(manual_route_data[actor_name], None),
        )
        assert first_page.status_code == second_page.status_code == 200
        assert first_page.json()["total"] == second_page.json()["total"] == 2
        assert first_page.json()["page"] == 1
        assert second_page.json()["page"] == 2
        assert first_page.json()["page_size"] == second_page.json()["page_size"] == 1
        assert first_page.json()["items"] == [
            {
                "proposal_id": first.id,
                "proposal_revision_id": "revision-list-a",
                "revision_number": 1,
                "store_id": "store-1",
                "product_id": "product-1",
                "submitted_by": "operator-1",
                "status": "pending_approval",
                "submitted_at": "2026-08-31T12:00:00Z",
            }
        ]
        assert second_page.json()["items"] == [
            {
                "proposal_id": second.id,
                "proposal_revision_id": "revision-list-b",
                "revision_number": 1,
                "store_id": "store-1",
                "product_id": "product-1",
                "submitted_by": "supervisor-1",
                "status": "pending_approval",
                "submitted_at": "2026-08-31T12:00:00Z",
            }
        ]
        assert rejected.id not in first_page.text + second_page.text
        _assert_safe_read(first_page, caplog)
        _assert_safe_read(second_page, caplog)

    stale_scope = await session.get(
        UserStoreScope,
        {"user_id": "admin-1", "store_id": "store-1"},
    )
    assert stale_scope is not None
    await session.execute(
        delete(UserStoreScope).where(
            UserStoreScope.user_id == "admin-1",
            UserStoreScope.store_id == "store-1",
        )
    )
    await session.commit()
    refreshed = await manual_client.get(
        "/approvals", headers=_headers(manual_route_data["admin"], None)
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["items"] == []
    assert refreshed.json()["total"] == 0


@pytest.mark.parametrize("path", ["/approvals", "/audit-events"])
async def test_approval_and_audit_lists_forbid_operator(
    manual_client, manual_route_data, path: str
) -> None:
    response = await manual_client.get(
        path, headers=_headers(manual_route_data["operator"], None)
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": {"code": "PROPOSAL_ACTION_FORBIDDEN"}
    }


@pytest.mark.parametrize(
    "path",
    [
        "/approvals?page=0",
        "/approvals?page_size=0",
        "/approvals?page_size=101",
        "/audit-events?page=0",
        "/audit-events?page_size=0",
        "/audit-events?page_size=101",
        "/audit-events?store_id=",
        f"/audit-events?store_id={'s' * 37}",
        "/audit-events?proposal_id=",
        f"/audit-events?proposal_id={'p' * 37}",
        "/audit-events?action=not-an-action",
    ],
)
async def test_approval_and_audit_list_query_bounds_return_422(
    manual_client, manual_route_data, path: str
) -> None:
    response = await manual_client.get(
        path, headers=_headers(manual_route_data["supervisor"], None)
    )

    assert response.status_code == 422


async def test_audit_list_filters_through_action_and_excludes_foreign_scope_safely(
    manual_client, manual_route_data, session, caplog
) -> None:
    created_at = datetime(2026, 8, 31, 13, 0, tzinfo=UTC)
    actions = [
        ApprovalAction(
            id=f"audit-action-{action.value}",
            proposal_id="proposal-1",
            proposal_revision_id="revision-1",
            store_id="store-1",
            actor_id="supervisor-1",
            actor_role=UserRole.SUPERVISOR,
            action=action,
            comment=None,
            idempotency_key_hash=character * 64,
            request_hash=character.upper() * 64,
            created_at=created_at,
        )
        for action, character in (
            (ApprovalActionType.SUBMIT, "1"),
            (ApprovalActionType.APPROVE, "2"),
        )
    ]
    session.add_all(actions)
    await session.flush()
    session.add_all(
        [
            AuditEvent(
                id="audit-submit",
                event_type=AuditEventType.PROPOSAL_APPROVED,
                outcome=AuditOutcome.SUCCESS,
                actor_id="supervisor-1",
                actor_role=UserRole.SUPERVISOR,
                store_id="store-1",
                proposal_id="proposal-1",
                proposal_revision_id="revision-1",
                workflow_run_id="optimization-1",
                approval_action_id=actions[0].id,
                request_id="audit-submit-request",
                details={"from_status": "pending_approval", "to_status": "completed"},
                created_at=created_at,
            ),
            AuditEvent(
                id="audit-approve",
                event_type=AuditEventType.PROPOSAL_SUBMITTED,
                outcome=AuditOutcome.SUCCESS,
                actor_id="supervisor-1",
                actor_role=UserRole.SUPERVISOR,
                store_id="store-1",
                proposal_id="proposal-1",
                proposal_revision_id="revision-1",
                workflow_run_id="optimization-1",
                approval_action_id=actions[1].id,
                request_id="audit-approve-request",
                details={"to_status": "pending_approval"},
                created_at=created_at,
            ),
            AuditEvent(
                id="audit-foreign",
                event_type=AuditEventType.AUTHORIZATION_DENIED,
                outcome=AuditOutcome.DENIED,
                actor_id="other-1",
                actor_role=UserRole.OPERATOR,
                store_id="store-2",
                request_id="audit-foreign-request",
                details={},
                created_at=created_at,
            ),
        ]
    )
    await session.commit()

    response = await manual_client.get(
        "/audit-events?page=1&page_size=1&store_id=store-1&proposal_id=proposal-1&action=approve",
        headers=_headers(manual_route_data["admin"], None),
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["page"] == response.json()["page_size"] == 1
    item = response.json()["items"][0]
    assert set(item) == {
        "id",
        "event_type",
        "outcome",
        "actor_id",
        "actor_role",
        "store_id",
        "proposal_id",
        "proposal_revision_id",
        "workflow_run_id",
        "approval_action_id",
        "publish_record_id",
        "request_id",
        "error_code",
        "details",
        "created_at",
    }
    assert item == {
        "id": "audit-approve",
        "event_type": "proposal_submitted",
        "outcome": "success",
        "actor_id": "supervisor-1",
        "actor_role": "supervisor",
        "store_id": "store-1",
        "proposal_id": "proposal-1",
        "proposal_revision_id": "revision-1",
        "workflow_run_id": "optimization-1",
        "approval_action_id": "audit-action-approve",
        "publish_record_id": None,
        "request_id": "audit-approve-request",
        "error_code": None,
        "details": {"to_status": "pending_approval"},
        "created_at": "2026-08-31T13:00:00Z",
    }
    assert "audit-foreign" not in response.text
    _assert_safe_read(response, caplog)


async def test_approve_same_key_different_owned_revision_conflicts_before_completed_gate(
    manual_client, manual_route_data, session
) -> None:
    actor = manual_route_data["supervisor"]
    await _submit(manual_client, actor)
    first = await _approve(manual_client, actor, key="approve-same-key")
    assert first.status_code == 201
    parent = manual_route_data["parent"]
    session.add(
        ProposalRevision(
            id="revision-2",
            proposal_id="proposal-1",
            iteration=None,
            revision_number=2,
            origin=ProposalRevisionOrigin.MANUAL,
            created_by=actor.id,
            parent_revision_id="revision-1",
            base_product_version=7,
            trusted_fact_hash="e" * 64,
            proposal_output=parent.proposal_output,
            citations=parent.citations,
        )
    )
    await session.commit()
    before = (await _action_counts(session), await _publish_count(session))

    conflict = await _approve(
        manual_client, actor, key="approve-same-key", revision_id="revision-2"
    )

    assert conflict.status_code == 409
    assert conflict.json() == {"detail": {"code": "IDEMPOTENCY_REPLAY_CONFLICT"}}
    assert (await _action_counts(session), await _publish_count(session)) == before


@pytest.mark.parametrize(
    ("winner", "loser"),
    [
        ("approve", "reject"),
        ("approve", "request-changes"),
        ("reject", "approve"),
        ("request-changes", "approve"),
    ],
)
async def test_approve_and_terminal_conflicts_keep_only_the_winner(
    manual_client, manual_route_data, session, winner: str, loser: str
) -> None:
    actor = manual_route_data["supervisor"]
    await _submit(manual_client, actor)
    winner_response = (
        await _approve(manual_client, actor, key="winner-key")
        if winner == "approve"
        else await manual_client.post(
            f"/approvals/proposal-1/{winner}",
            json=_body(comment="赢家意见"),
            headers=_headers(actor, "winner-key"),
        )
    )
    assert winner_response.status_code == 201
    before = (await _action_counts(session), await _publish_count(session))

    loser_response = (
        await _approve(manual_client, actor, key="loser-key")
        if loser == "approve"
        else await manual_client.post(
            f"/approvals/proposal-1/{loser}",
            json=_body(comment="输家意见"),
            headers=_headers(actor, "loser-key"),
        )
    )

    assert loser_response.status_code == 409
    assert loser_response.json() == {"detail": {"code": "APPROVAL_ACTION_CONFLICT"}}
    assert (await _action_counts(session), await _publish_count(session)) == before


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("product-version", "PRODUCT_VERSION_CONFLICT"),
        ("submitted-pointer", "APPROVAL_STATE_CONFLICT"),
        ("current-pointer", "APPROVAL_STATE_CONFLICT"),
        ("review-failed", "PROPOSAL_DATA_INCONSISTENT"),
        ("review-quality", "PROPOSAL_DATA_INCONSISTENT"),
        ("citation", "TRUSTED_EVIDENCE_INVALID"),
        ("sku-price", "TRUSTED_EVIDENCE_INVALID"),
        ("sku-identity", "TRUSTED_EVIDENCE_INVALID"),
    ],
)
async def test_approve_rechecks_version_pointers_review_citations_and_sku_facts(
    manual_client, manual_route_data, session, case: str, expected_code: str
) -> None:
    actor = manual_route_data["supervisor"]
    await _submit(manual_client, actor)
    if case == "product-version":
        statement = update(Product).where(Product.id == "product-1").values(current_version=8)
    elif case == "submitted-pointer":
        statement = update(ProductProposal).where(ProductProposal.id == "proposal-1").values(
            submitted_revision_id=None
        )
    elif case == "current-pointer":
        statement = update(ProductProposal).where(ProductProposal.id == "proposal-1").values(
            current_revision_id=None
        )
    elif case == "review-failed":
        statement = update(ComplianceReview).where(ComplianceReview.id == "review-1").values(
            passed=False
        )
    elif case == "review-quality":
        statement = update(ComplianceReview).where(ComplianceReview.id == "review-1").values(
            quality_status=WorkflowQuality.DEGRADED
        )
    elif case == "citation":
        statement = update(KnowledgeDocument).where(KnowledgeDocument.id == "document-1").values(
            current_version_id=None
        )
    elif case == "sku-price":
        statement = update(ProductSku).where(ProductSku.id == "sku-1").values(
            price=Decimal("101.00")
        )
    else:
        statement = update(ProductSku).where(ProductSku.id == "sku-1").values(
            code="SKU-CHANGED"
        )
    await session.execute(statement.execution_options(synchronize_session=False))
    await session.commit()
    before = (await _action_counts(session), await _publish_count(session))

    response = await _approve(manual_client, actor)

    assert response.status_code == (422 if expected_code == "TRUSTED_EVIDENCE_INVALID" else 409 if expected_code != "PROPOSAL_DATA_INCONSISTENT" else 503)
    assert response.json() == {"detail": {"code": expected_code}}
    assert (await _action_counts(session), await _publish_count(session)) == before


@pytest.mark.parametrize("corruption", ["snapshot", "submit-audit-publish"])
async def test_submit_replay_rejects_a_corrupted_completed_publish_chain(
    manual_client, manual_route_data, session, corruption: str
) -> None:
    actor = manual_route_data["supervisor"]
    submit_key = "completed-submit-replay"
    await _submit(manual_client, actor, key=submit_key)
    approved = await _approve(manual_client, actor, key="completed-approve")
    assert approved.status_code == 201
    record = await session.scalar(select(PublishRecord))
    assert record is not None
    if corruption == "snapshot":
        record.after_snapshot = {**record.after_snapshot, "title": "伪造标题"}
    else:
        submit_audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.PROPOSAL_SUBMITTED
            )
        )
        assert submit_audit is not None
        submit_audit.publish_record_id = record.id
    await session.commit()
    before = (await _action_counts(session), await _publish_count(session))

    replay = await manual_client.post(
        "/proposals/proposal-1/submit",
        json=_body(),
        headers=_headers(actor, submit_key),
    )

    assert replay.status_code == 503
    assert replay.json() == {"detail": {"code": "PROPOSAL_DATA_INCONSISTENT"}}
    assert (await _action_counts(session), await _publish_count(session)) == before


async def test_approve_replay_rejects_an_extra_misbound_publish_audit(
    manual_client, manual_route_data, session
) -> None:
    actor = manual_route_data["supervisor"]
    await _submit(manual_client, actor)
    approve_key = "extra-publish-audit"
    approved = await _approve(manual_client, actor, key=approve_key)
    assert approved.status_code == 201
    publish_audit = await session.scalar(
        select(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.SIMULATED_PUBLISH_COMPLETED
        )
    )
    assert publish_audit is not None
    session.add(
        AuditEvent(
            id="extra-publish-audit",
            event_type=publish_audit.event_type,
            outcome=publish_audit.outcome,
            actor_id=publish_audit.actor_id,
            actor_role=publish_audit.actor_role,
            store_id=publish_audit.store_id,
            proposal_id=publish_audit.proposal_id,
            proposal_revision_id=publish_audit.proposal_revision_id,
            workflow_run_id=publish_audit.workflow_run_id,
            approval_action_id=publish_audit.approval_action_id,
            publish_record_id=None,
            request_id=publish_audit.request_id,
            error_code=None,
            details=dict(publish_audit.details),
        )
    )
    await session.commit()
    before = (await _action_counts(session), await _publish_count(session))

    replay = await _approve(manual_client, actor, key=approve_key)

    assert replay.status_code == 409
    assert replay.json() == {"detail": {"code": "PUBLISH_REPLAY_CONFLICT"}}
    assert (await _action_counts(session), await _publish_count(session)) == before


async def test_approve_replay_rejects_an_extra_misbound_primary_audit(
    manual_client, manual_route_data, session
) -> None:
    actor = manual_route_data["supervisor"]
    await _submit(manual_client, actor)
    approve_key = "extra-primary-audit"
    approved = await _approve(manual_client, actor, key=approve_key)
    assert approved.status_code == 201
    approved_audit = await session.scalar(
        select(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.PROPOSAL_APPROVED
        )
    )
    assert approved_audit is not None
    session.add(
        AuditEvent(
            id="extra-approved-audit",
            event_type=approved_audit.event_type,
            outcome=approved_audit.outcome,
            actor_id=approved_audit.actor_id,
            actor_role=approved_audit.actor_role,
            store_id="store-2",
            proposal_id=approved_audit.proposal_id,
            proposal_revision_id=approved_audit.proposal_revision_id,
            workflow_run_id=approved_audit.workflow_run_id,
            approval_action_id=approved_audit.approval_action_id,
            publish_record_id=approved_audit.publish_record_id,
            request_id=approved_audit.request_id,
            error_code=approved_audit.error_code,
            details=dict(approved_audit.details),
        )
    )
    await session.commit()
    before = (await _action_counts(session), await _publish_count(session))

    replay = await _approve(manual_client, actor, key=approve_key)

    assert replay.status_code == 409
    assert replay.json() == {"detail": {"code": "PUBLISH_REPLAY_CONFLICT"}}
    assert (await _action_counts(session), await _publish_count(session)) == before


@pytest.mark.parametrize(
    "corruption", ["snapshot", "hash", "action", "approved-audit-publish"]
)
async def test_approve_replay_rejects_non_exact_immutable_publish_chains(
    manual_client, manual_route_data, session, corruption: str
) -> None:
    actor = manual_route_data["supervisor"]
    await _submit(manual_client, actor)
    first = await _approve(manual_client, actor, key="corrupt-replay")
    assert first.status_code == 201
    record = await session.scalar(select(PublishRecord))
    action = await session.scalar(
        select(ApprovalAction).where(ApprovalAction.action == ApprovalActionType.APPROVE)
    )
    assert record is not None and action is not None
    if corruption == "snapshot":
        record.after_snapshot = {**record.after_snapshot, "title": "伪造标题"}
    elif corruption == "hash":
        record.publish_idempotency_hash = "f" * 64
    elif corruption == "action":
        action.store_id = "store-2"
    else:
        approved_audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.PROPOSAL_APPROVED
            )
        )
        assert approved_audit is not None
        approved_audit.publish_record_id = None
    await session.commit()
    before = (await _action_counts(session), await _publish_count(session))

    replay = await _approve(manual_client, actor, key="corrupt-replay")

    assert replay.status_code == 409
    assert replay.json() == {"detail": {"code": "PUBLISH_REPLAY_CONFLICT"}}
    assert (await _action_counts(session), await _publish_count(session)) == before


@pytest.mark.parametrize("fail_at", range(1, 7))
async def test_approve_rolls_back_every_publish_boundary(
    manual_client, manual_route_data, session, monkeypatch, fail_at: int
) -> None:
    actor = manual_route_data["supervisor"]
    await _prepare_publish_output(session, manual_route_data["parent"])
    await _submit(manual_client, actor)
    product = await session.get(Product, "product-1", populate_existing=True)
    run = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    assert product is not None and run is not None
    product_before = (
        product.title,
        list(product.selling_points),
        product.description,
        list(product.search_keywords),
        dict(product.attributes),
        product.current_version,
    )
    run_before = (run.status, run.quality_status, run.current_step, run.error_code)
    counts_before = (await _action_counts(session), await _publish_count(session))
    original_flush = session.flush
    calls = 0

    async def fail_after_flush(*args, **kwargs) -> None:
        nonlocal calls
        calls += 1
        await original_flush(*args, **kwargs)
        if calls == fail_at:
            raise SQLAlchemyError(f"simulated publish boundary {fail_at}")

    monkeypatch.setattr(session, "flush", fail_after_flush)
    response = await _approve(manual_client, actor, key=f"rollback-{fail_at}")

    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "PROPOSAL_DATA_INCONSISTENT"}}
    monkeypatch.undo()
    await session.rollback()
    product = await session.get(Product, "product-1", populate_existing=True)
    run = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    assert product is not None and run is not None
    assert (
        product.title,
        list(product.selling_points),
        product.description,
        list(product.search_keywords),
        dict(product.attributes),
        product.current_version,
    ) == product_before
    assert (run.status, run.quality_status, run.current_step, run.error_code) == run_before
    assert (await _action_counts(session), await _publish_count(session)) == counts_before


async def test_approve_integrity_race_recovers_the_single_completed_winner(
    manual_route_data, session, monkeypatch
) -> None:
    from backend.approvals import approve_proposal, submit_proposal

    actor = manual_route_data["supervisor"]
    actor_id = actor.id
    submitted = await submit_proposal(
        session,
        actor_id=actor_id,
        proposal_id="proposal-1",
        request=ProposalActionRequest(revision_id="revision-1"),
        idempotency_key="publish-race-submit",
        request_id="publish-race-submit-request",
    )
    assert submitted.created is True
    original_flush = session.flush
    raced = False

    async def race_flush(*args, **kwargs) -> None:
        nonlocal raced
        if raced:
            await original_flush(*args, **kwargs)
            return
        raced = True
        await session.rollback()
        monkeypatch.setattr(session, "flush", original_flush)
        winner = await approve_proposal(
            session,
            actor_id=actor_id,
            proposal_id="proposal-1",
            request=ProposalActionRequest(revision_id="revision-1"),
            idempotency_key="publish-winner-key",
            request_id="publish-winner-request",
        )
        assert winner.created is True
        monkeypatch.setattr(session, "flush", race_flush)
        raise IntegrityError("insert", {}, RuntimeError("simulated publish race"))

    monkeypatch.setattr(session, "flush", race_flush)
    result = await approve_proposal(
        session,
        actor_id=actor_id,
        proposal_id="proposal-1",
        request=ProposalActionRequest(revision_id="revision-1"),
        idempotency_key="publish-loser-key",
        request_id="publish-loser-request",
    )

    assert result.created is False
    assert await _publish_count(session) == 1
    assert await _model_count(session, ApprovalAction) == 2
    assert await _model_count(session, AuditEvent) == 3
    product = await session.get(Product, "product-1", populate_existing=True)
    assert product is not None and product.current_version == 8
