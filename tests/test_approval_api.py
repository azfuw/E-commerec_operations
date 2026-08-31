from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.approvals import ApprovalDomainError
from backend.common import (
    ApprovalActionType,
    AuditEventType,
    AuditOutcome,
    ProposalRevisionOrigin,
    UserRole,
    UserStatus,
    WorkflowQuality,
    WorkflowStatus,
    WorkflowType,
)
from backend.models import (
    ApprovalAction,
    AuditEvent,
    ComplianceReview,
    KnowledgeDocument,
    Product,
    ProductProposal,
    ProposalRevision,
    Store,
    User,
    UserStoreScope,
    WorkflowRun,
)
from backend.schemas import (
    ApprovalActionView,
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
