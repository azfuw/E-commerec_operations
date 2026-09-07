from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.manual_review_worker as manual_review_worker
from backend.common import (
    AgentCallType,
    ApprovalActionType,
    AuditEventType,
    AuditOutcome,
    WorkflowQuality,
    WorkflowStatus,
)
from backend.models import (
    AgentCall,
    ApprovalAction,
    AuditEvent,
    ComplianceReview,
    InventorySnapshot,
    ManualReviewRun,
    Product,
    ProductProposal,
    ProductSku,
    PlatformDelivery,
    ProposalRevision,
    PublishRecord,
    WorkflowRun,
)
from backend.schemas import OutputCitation
from tests.test_approval_api import (
    _body,
    _headers,
    _nested_keys,
    _seed_immutable_business_facts,
)
from tests.test_manual_review_api import (
    RULE_CHUNK,
    _description_change,
    _manual_body,
    _manual_headers,
    _section,
    _title_change,
    _trusted,
    manual_client,
    manual_route_data,
)
from tests.test_manual_review_worker import (
    _ManualLoader,
    _ManualRecordingSaver,
    _manual_worker_settings,
    _manual_worker_transport,
    _passing_response,
)


_UNSAFE_KEYS = {
    "authorization",
    "cookie",
    "idempotency_key",
    "idempotency_key_hash",
    "input_hash",
    "key",
    "lease_owner",
    "path",
    "prompt",
    "provider",
    "publish_idempotency_hash",
    "raw_response",
    "request_hash",
    "trusted_fact_hash",
    "vector",
}


def _assert_safe(value: object, *, secrets: tuple[str, ...] = ()) -> None:
    assert not _UNSAFE_KEYS & {key.lower() for key in _nested_keys(value)}
    serialized = json.dumps(value, ensure_ascii=False, default=str).lower()
    for secret in (*secrets, "mock-key", "authorization", "raw_response"):
        assert secret.lower() not in serialized


def _assert_safe_audits(audits, *, delivery_id: str, secrets: tuple[str, ...]) -> None:
    enqueue = next(
        audit
        for audit in audits
        if audit.event_type is AuditEventType.PLATFORM_DELIVERY_ENQUEUED
    )
    assert (enqueue.resource_type, enqueue.resource_id, enqueue.details) == (
        "platform_delivery",
        delivery_id,
        {
            "provider": "contract_simulator",
            "platform_delivery_status": "pending",
            "attempt_count": 0,
        },
    )
    _assert_safe(
        [
            audit.details
            for audit in audits
            if audit.event_type is not AuditEventType.PLATFORM_DELIVERY_ENQUEUED
        ],
        secrets=secrets,
    )


def _manual_payload(
    parent_revision_id: str,
    *,
    title: str,
    body: str,
) -> dict[str, object]:
    section = _section(body=body)
    return _manual_body(
        parent_revision_id=parent_revision_id,
        title=title,
        description=[section.model_dump(mode="json")],
        changes=[
            _title_change(suggested=title).model_dump(mode="json"),
            _description_change(sections=[section]).model_dump(mode="json"),
        ],
    )


async def _post(client, responses: list[object], path: str, *, body, headers):
    response = await client.post(path, json=body, headers=headers)
    responses.append(response.json())
    return response


async def _assert_replay_and_conflict(
    client,
    responses: list[object],
    *,
    path: str,
    first,
    body: dict[str, object],
    changed_body: dict[str, object],
    headers: dict[str, str],
) -> None:
    replay = await _post(client, responses, path, body=body, headers=headers)
    conflict = await _post(client, responses, path, body=changed_body, headers=headers)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": {"code": "IDEMPOTENCY_REPLAY_CONFLICT"}}


def _listing(product: Product) -> dict[str, object]:
    return {
        "title": product.title,
        "selling_points": list(product.selling_points),
        "description": product.description,
        "search_keywords": list(product.search_keywords),
        "attributes": dict(product.attributes),
        "current_version": product.current_version,
    }


async def _audits(session) -> list[AuditEvent]:
    return list(
        await session.scalars(
            select(AuditEvent).order_by(AuditEvent.created_at, AuditEvent.id)
        )
    )


async def _run_manual_review(
    session,
    saver: _ManualRecordingSaver,
    *,
    schema_repair: bool,
) -> tuple[str, list[AgentCall]]:
    assert session.bind is not None
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    loader = _ManualLoader(_trusted())
    requests: list[dict[str, object]] = []
    response = _passing_response().model_copy(
        update={"citations": [OutputCitation(chunk_id=RULE_CHUNK)]}
    )

    processed = await manual_review_worker.run_once(
        factory,
        settings=_manual_worker_settings(),
        lease_owner="vertical-worker",
        checkpointer=saver,
        trusted_input_loader=loader,
        transport=_manual_worker_transport(
            response,
            requests,
            schema_invalid_primary=schema_repair,
        ),
    )

    assert processed is not None
    session.expire_all()
    calls = list(
        await session.scalars(
            select(AgentCall).where(AgentCall.workflow_run_id == processed)
        )
    )
    assert sum(call.call_type is AgentCallType.PRIMARY for call in calls) == 1
    assert sum(call.call_type is AgentCallType.SCHEMA_REPAIR for call in calls) == int(
        schema_repair
    )
    assert len(calls) == len(requests) == 1 + int(schema_repair)
    assert all(
        call.iteration == 0
        and call.attempt == 1
        and call.node_name
        in {"call_product_compliance_agent", "repair_product_compliance_schema"}
        for call in calls
    )
    assert (loader.calls, loader.renewals) == (1, 1)
    assert processed in saver.thread_ids
    _assert_safe(saver.states)
    return processed, calls


@pytest_asyncio.fixture
async def pending_manual_data(manual_route_data, session):
    run = manual_route_data["optimization"]
    run.status = WorkflowStatus.PENDING_MANUAL
    run.current_step = "approval_changes_requested"
    await session.commit()
    return manual_route_data


@pytest.mark.parametrize("approver_name", ["supervisor", "admin"])
async def test_pending_manual_public_flow_self_approves_and_publishes_allowed_listing_once(
    manual_client,
    pending_manual_data,
    session,
    approver_name: str,
) -> None:
    actor = pending_manual_data[approver_name]
    actor_id = actor.id
    actor_role = actor.role
    await _seed_immutable_business_facts(session)
    product = await session.get(Product, "product-1", populate_existing=True)
    sku = await session.get(ProductSku, "sku-1", populate_existing=True)
    inventory = await session.get(InventorySnapshot, "inventory-1")
    assert product is not None and sku is not None and inventory is not None
    immutable_product = (product.code, product.category, product.brand, product.enabled)
    immutable_sku = (sku.code, dict(sku.spec), sku.price, sku.current_stock)
    immutable_inventory = (inventory.on_hand, inventory.inbound)
    responses: list[object] = []

    manual_key = f"flow-one-{approver_name}-manual"
    submit_key = f"flow-one-{approver_name}-submit"
    approve_key = f"flow-one-{approver_name}-approve"
    manual_headers = _manual_headers(actor, manual_key)
    submit_headers = _headers(actor, submit_key)
    approve_headers = _headers(actor, approve_key)
    manual_body = _manual_payload(
        "revision-1",
        title="人工发布家居标题",
        body="人工发布商品详情",
    )
    manual = await _post(
        manual_client,
        responses,
        "/proposals/proposal-1/manual-revision",
        body=manual_body,
        headers=manual_headers,
    )
    assert manual.status_code == 202
    revision_id = manual.json()["revision_id"]
    workflow_id = manual.json()["manual_review_workflow_run_id"]
    saver = _ManualRecordingSaver()
    processed, calls = await _run_manual_review(session, saver, schema_repair=False)
    assert processed == workflow_id
    assert [call.call_type for call in calls] == [AgentCallType.PRIMARY]
    await _assert_replay_and_conflict(
        manual_client,
        responses,
        path="/proposals/proposal-1/manual-revision",
        first=manual,
        body=manual_body,
        changed_body=_manual_payload(
            "revision-1",
            title="另一人工发布标题",
            body="另一人工发布详情",
        ),
        headers=manual_headers,
    )

    submit_body = _body(revision_id)
    submitted = await _post(
        manual_client,
        responses,
        "/proposals/proposal-1/submit",
        body=submit_body,
        headers=submit_headers,
    )
    assert submitted.status_code == 201
    approved = await _post(
        manual_client,
        responses,
        "/approvals/proposal-1/approve",
        body=_body(revision_id),
        headers=approve_headers,
    )
    assert approved.status_code == 201
    await _assert_replay_and_conflict(
        manual_client,
        responses,
        path="/proposals/proposal-1/submit",
        first=submitted,
        body=submit_body,
        changed_body=_body("revision-1"),
        headers=submit_headers,
    )
    await _assert_replay_and_conflict(
        manual_client,
        responses,
        path="/approvals/proposal-1/approve",
        first=approved,
        body=_body(revision_id),
        changed_body=_body("revision-1"),
        headers=approve_headers,
    )

    product = await session.get(Product, "product-1", populate_existing=True)
    sku = await session.get(ProductSku, "sku-1", populate_existing=True)
    inventory = await session.get(InventorySnapshot, "inventory-1", populate_existing=True)
    assert product is not None and sku is not None and inventory is not None
    assert _listing(product) == {
        "title": "人工发布家居标题",
        "selling_points": ["棉质家居设计"],
        "description": "商品详情\n人工发布商品详情",
        "search_keywords": ["家居"],
        "attributes": {"材质": "精梳棉"},
        "current_version": 8,
    }
    assert immutable_product == (product.code, product.category, product.brand, product.enabled)
    assert immutable_sku == (sku.code, dict(sku.spec), sku.price, sku.current_stock)
    assert immutable_inventory == (inventory.on_hand, inventory.inbound)
    assert int(await session.scalar(select(func.count()).select_from(PublishRecord)) or 0) == 1
    deliveries = list(await session.scalars(select(PlatformDelivery)))
    assert len(deliveries) == 1
    actions = list(await session.scalars(select(ApprovalAction)))
    assert [action.action for action in actions] == [
        ApprovalActionType.SUBMIT,
        ApprovalActionType.APPROVE,
    ]
    assert actions[-1].actor_id == actor_id and actions[-1].actor_role == actor_role
    audits = await _audits(session)
    audit_keys = [(audit.created_at, audit.id) for audit in audits]
    assert audit_keys == sorted(audit_keys)
    assert len(audits) == 7
    assert [audit.event_type for audit in audits[:4]] == [
        AuditEventType.MANUAL_REVISION_CREATED,
        AuditEventType.MANUAL_REVIEW_CLAIMED,
        AuditEventType.MANUAL_REVIEW_COMPLETED,
        AuditEventType.PROPOSAL_SUBMITTED,
    ]
    assert {audit.event_type for audit in audits[4:]} == {
        AuditEventType.PLATFORM_DELIVERY_ENQUEUED,
        AuditEventType.PROPOSAL_APPROVED,
        AuditEventType.SIMULATED_PUBLISH_COMPLETED,
    }
    assert all(audit.outcome is AuditOutcome.SUCCESS and audit.error_code is None for audit in audits)
    _assert_safe_audits(
        audits,
        delivery_id=deliveries[0].id,
        secrets=(manual_key, submit_key, approve_key),
    )
    _assert_safe(responses, secrets=(manual_key, submit_key, approve_key))


async def test_request_changes_public_flow_creates_a_fresh_review_and_preserves_first_history(
    manual_client,
    manual_route_data,
    session,
) -> None:
    actor = manual_route_data["supervisor"]
    await _seed_immutable_business_facts(session)
    product = await session.get(Product, "product-1", populate_existing=True)
    sku = await session.get(ProductSku, "sku-1", populate_existing=True)
    inventory = await session.get(InventorySnapshot, "inventory-1")
    assert product is not None and sku is not None and inventory is not None
    original_listing = _listing(product)
    immutable_sku = (sku.code, dict(sku.spec), sku.price, sku.current_stock)
    immutable_inventory = (inventory.on_hand, inventory.inbound)
    responses: list[object] = []
    saver = _ManualRecordingSaver()
    first_manual_key = "flow-two-first-manual"
    first_submit_key = "flow-two-first-submit"
    changes_key = "flow-two-request-changes"
    second_manual_key = "flow-two-second-manual"
    second_submit_key = "flow-two-second-submit"
    approve_key = "flow-two-approve"
    first_manual_headers = _manual_headers(actor, first_manual_key)
    first_submit_headers = _headers(actor, first_submit_key)
    changes_headers = _headers(actor, changes_key)
    second_manual_headers = _manual_headers(actor, second_manual_key)
    second_submit_headers = _headers(actor, second_submit_key)
    approve_headers = _headers(actor, approve_key)

    first_manual_body = _manual_payload(
        "revision-1",
        title="首轮人工修订标题",
        body="首轮人工修订详情",
    )
    first_manual = await _post(
        manual_client,
        responses,
        "/proposals/proposal-1/manual-revision",
        body=first_manual_body,
        headers=first_manual_headers,
    )
    assert first_manual.status_code == 202
    first_revision_id = first_manual.json()["revision_id"]
    first_workflow_id = first_manual.json()["manual_review_workflow_run_id"]
    processed, first_calls = await _run_manual_review(session, saver, schema_repair=False)
    assert processed == first_workflow_id
    first_revision = await session.get(ProposalRevision, first_revision_id, populate_existing=True)
    first_workflow = await session.get(WorkflowRun, first_workflow_id, populate_existing=True)
    first_review = await session.scalar(
        select(ComplianceReview).where(
            ComplianceReview.proposal_revision_id == first_revision_id
        )
    )
    assert first_revision is not None and first_workflow is not None and first_review is not None
    first_history = copy.deepcopy(
        {
            "revision": (
                first_revision.proposal_output,
                first_revision.citations,
                first_revision.parent_revision_id,
                first_revision.revision_number,
            ),
            "workflow": (
                first_workflow.status,
                first_workflow.quality_status,
                first_workflow.current_step,
                first_workflow.input,
                first_workflow.error_code,
            ),
            "review": (
                first_review.deterministic_checks,
                first_review.semantic_review,
                first_review.passed,
                first_review.citations,
                first_review.error_code,
            ),
            "calls": [
                (
                    call.id,
                    call.node_name,
                    call.call_type,
                    call.iteration,
                    call.attempt,
                    call.status,
                    call.error_code,
                )
                for call in first_calls
            ],
        }
    )
    first_checkpoints = copy.deepcopy(saver.states)

    first_submit_body = _body(first_revision_id)
    first_submit = await _post(
        manual_client,
        responses,
        "/proposals/proposal-1/submit",
        body=first_submit_body,
        headers=first_submit_headers,
    )
    assert first_submit.status_code == 201
    await _assert_replay_and_conflict(
        manual_client,
        responses,
        path="/proposals/proposal-1/submit",
        first=first_submit,
        body=first_submit_body,
        changed_body=_body("revision-1"),
        headers=first_submit_headers,
    )
    changes_body = _body(first_revision_id, comment="请补充可信商品说明")
    changes = await _post(
        manual_client,
        responses,
        "/approvals/proposal-1/request-changes",
        body=changes_body,
        headers=changes_headers,
    )
    assert changes.status_code == 201
    await _assert_replay_and_conflict(
        manual_client,
        responses,
        path="/approvals/proposal-1/request-changes",
        first=changes,
        body=changes_body,
        changed_body=_body(first_revision_id, comment="另一条修改意见"),
        headers=changes_headers,
    )
    product = await session.get(Product, "product-1", populate_existing=True)
    assert product is not None and _listing(product) == original_listing

    second_manual_body = _manual_payload(
        first_revision_id,
        title="二次人工修订标题",
        body="二次人工修订详情",
    )
    second_manual = await _post(
        manual_client,
        responses,
        "/proposals/proposal-1/manual-revision",
        body=second_manual_body,
        headers=second_manual_headers,
    )
    assert second_manual.status_code == 202
    second_revision_id = second_manual.json()["revision_id"]
    second_workflow_id = second_manual.json()["manual_review_workflow_run_id"]
    assert (second_revision_id, second_workflow_id) != (
        first_revision_id,
        first_workflow_id,
    )
    processed, second_calls = await _run_manual_review(session, saver, schema_repair=True)
    assert processed == second_workflow_id
    assert {call.call_type for call in second_calls} == {
        AgentCallType.PRIMARY,
        AgentCallType.SCHEMA_REPAIR,
    }
    await _assert_replay_and_conflict(
        manual_client,
        responses,
        path="/proposals/proposal-1/manual-revision",
        first=second_manual,
        body=second_manual_body,
        changed_body=_manual_payload(
            first_revision_id,
            title="冲突的二次修订标题",
            body="冲突的二次修订详情",
        ),
        headers=second_manual_headers,
    )

    second_submit_body = _body(second_revision_id)
    second_submit = await _post(
        manual_client,
        responses,
        "/proposals/proposal-1/submit",
        body=second_submit_body,
        headers=second_submit_headers,
    )
    assert second_submit.status_code == 201
    await _assert_replay_and_conflict(
        manual_client,
        responses,
        path="/proposals/proposal-1/submit",
        first=second_submit,
        body=second_submit_body,
        changed_body=_body(first_revision_id),
        headers=second_submit_headers,
    )
    approve_body = _body(second_revision_id)
    approved = await _post(
        manual_client,
        responses,
        "/approvals/proposal-1/approve",
        body=approve_body,
        headers=approve_headers,
    )
    assert approved.status_code == 201
    await _assert_replay_and_conflict(
        manual_client,
        responses,
        path="/approvals/proposal-1/approve",
        first=approved,
        body=approve_body,
        changed_body=_body(first_revision_id),
        headers=approve_headers,
    )

    first_revision = await session.get(ProposalRevision, first_revision_id, populate_existing=True)
    first_workflow = await session.get(WorkflowRun, first_workflow_id, populate_existing=True)
    first_review = await session.scalar(
        select(ComplianceReview)
        .where(ComplianceReview.proposal_revision_id == first_revision_id)
        .execution_options(populate_existing=True)
    )
    current_first_calls = list(
        await session.scalars(
            select(AgentCall).where(AgentCall.workflow_run_id == first_workflow_id)
        )
    )
    assert first_revision is not None and first_workflow is not None and first_review is not None
    assert {
        "revision": (
            first_revision.proposal_output,
            first_revision.citations,
            first_revision.parent_revision_id,
            first_revision.revision_number,
        ),
        "workflow": (
            first_workflow.status,
            first_workflow.quality_status,
            first_workflow.current_step,
            first_workflow.input,
            first_workflow.error_code,
        ),
        "review": (
            first_review.deterministic_checks,
            first_review.semantic_review,
            first_review.passed,
            first_review.citations,
            first_review.error_code,
        ),
        "calls": [
            (
                call.id,
                call.node_name,
                call.call_type,
                call.iteration,
                call.attempt,
                call.status,
                call.error_code,
            )
            for call in current_first_calls
        ],
    } == first_history
    assert saver.states[: len(first_checkpoints)] == first_checkpoints
    assert saver.thread_ids == {first_workflow_id, second_workflow_id}

    product = await session.get(Product, "product-1", populate_existing=True)
    sku = await session.get(ProductSku, "sku-1", populate_existing=True)
    inventory = await session.get(InventorySnapshot, "inventory-1", populate_existing=True)
    assert product is not None and sku is not None and inventory is not None
    assert _listing(product) == {
        "title": "二次人工修订标题",
        "selling_points": ["棉质家居设计"],
        "description": "商品详情\n二次人工修订详情",
        "search_keywords": ["家居"],
        "attributes": {"材质": "精梳棉"},
        "current_version": 8,
    }
    assert immutable_sku == (sku.code, dict(sku.spec), sku.price, sku.current_stock)
    assert immutable_inventory == (inventory.on_hand, inventory.inbound)
    assert int(await session.scalar(select(func.count()).select_from(PublishRecord)) or 0) == 1
    deliveries = list(await session.scalars(select(PlatformDelivery)))
    assert len(deliveries) == 1
    audits = await _audits(session)
    audit_keys = [(audit.created_at, audit.id) for audit in audits]
    assert audit_keys == sorted(audit_keys)
    assert len(audits) == 12
    assert [audit.event_type for audit in audits[:9]] == [
        AuditEventType.MANUAL_REVISION_CREATED,
        AuditEventType.MANUAL_REVIEW_CLAIMED,
        AuditEventType.MANUAL_REVIEW_COMPLETED,
        AuditEventType.PROPOSAL_SUBMITTED,
        AuditEventType.PROPOSAL_CHANGES_REQUESTED,
        AuditEventType.MANUAL_REVISION_CREATED,
        AuditEventType.MANUAL_REVIEW_CLAIMED,
        AuditEventType.MANUAL_REVIEW_COMPLETED,
        AuditEventType.PROPOSAL_SUBMITTED,
    ]
    assert {audit.event_type for audit in audits[9:]} == {
        AuditEventType.PLATFORM_DELIVERY_ENQUEUED,
        AuditEventType.PROPOSAL_APPROVED,
        AuditEventType.SIMULATED_PUBLISH_COMPLETED,
    }
    assert all(audit.outcome is AuditOutcome.SUCCESS and audit.error_code is None for audit in audits)
    secrets = (
        "flow-two-first-manual",
        first_submit_key,
        changes_key,
        second_manual_key,
        second_submit_key,
        approve_key,
    )
    _assert_safe_audits(
        audits,
        delivery_id=deliveries[0].id,
        secrets=secrets,
    )
    _assert_safe(responses, secrets=secrets)


async def test_reject_public_flow_is_terminal_and_preserves_product_and_suggestions(
    manual_client,
    manual_route_data,
    session,
) -> None:
    actor = manual_route_data["supervisor"]
    product = await session.get(Product, "product-1", populate_existing=True)
    parent = await session.get(ProposalRevision, "revision-1", populate_existing=True)
    assert product is not None and parent is not None
    product_before = _listing(product)
    suggestions_before = copy.deepcopy(parent.proposal_output)
    assert suggestions_before["price_suggestions"]
    assert suggestions_before["sku_suggestions"]
    responses: list[object] = []

    submit_key = "flow-three-submit"
    submit_body = _body("revision-1")
    submit_headers = _headers(actor, submit_key)
    submitted = await _post(
        manual_client,
        responses,
        "/proposals/proposal-1/submit",
        body=submit_body,
        headers=submit_headers,
    )
    assert submitted.status_code == 201
    submit_replay = await _post(
        manual_client,
        responses,
        "/proposals/proposal-1/submit",
        body=submit_body,
        headers=submit_headers,
    )
    assert submit_replay.status_code == 200
    assert submit_replay.json() == submitted.json()
    reject_key = "flow-three-reject"
    reject_body = _body("revision-1", comment="主管拒绝此版本")
    reject_headers = _headers(actor, reject_key)
    rejected = await _post(
        manual_client,
        responses,
        "/approvals/proposal-1/reject",
        body=reject_body,
        headers=reject_headers,
    )
    assert rejected.status_code == 201
    await _assert_replay_and_conflict(
        manual_client,
        responses,
        path="/approvals/proposal-1/reject",
        first=rejected,
        body=reject_body,
        changed_body=_body("revision-1", comment="另一条拒绝意见"),
        headers=reject_headers,
    )

    product = await session.get(Product, "product-1", populate_existing=True)
    parent = await session.get(ProposalRevision, "revision-1", populate_existing=True)
    proposal = await session.get(ProductProposal, "proposal-1", populate_existing=True)
    run = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    assert product is not None and parent is not None and proposal is not None and run is not None
    assert _listing(product) == product_before
    assert parent.proposal_output == suggestions_before
    assert run.status is WorkflowStatus.REJECTED
    assert run.quality_status is WorkflowQuality.NORMAL
    assert run.current_step == "rejected"
    assert proposal.submitted_revision_id == "revision-1"
    assert int(await session.scalar(select(func.count()).select_from(PublishRecord)) or 0) == 0
    assert int(await session.scalar(select(func.count()).select_from(AgentCall)) or 0) == 0
    assert int(await session.scalar(select(func.count()).select_from(ManualReviewRun)) or 0) == 0
    reject_actions = list(
        await session.scalars(
            select(ApprovalAction).where(
                ApprovalAction.action == ApprovalActionType.REJECT
            )
        )
    )
    reject_audits = list(
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.PROPOSAL_REJECTED
            )
        )
    )
    assert len(reject_actions) == len(reject_audits) == 1
    assert reject_actions[0].comment == "主管拒绝此版本"
    assert reject_audits[0].details == {
        "from_status": "pending_approval",
        "to_status": "rejected",
        "quality_status": "normal",
        "current_step": "rejected",
    }
    audits = await _audits(session)
    assert [audit.event_type for audit in audits] == [
        AuditEventType.PROPOSAL_SUBMITTED,
        AuditEventType.PROPOSAL_REJECTED,
    ]
    assert all(audit.outcome is AuditOutcome.SUCCESS and audit.error_code is None for audit in audits)
    _assert_safe([audit.details for audit in audits], secrets=(submit_key, reject_key))
    _assert_safe(responses, secrets=(submit_key, reject_key))
