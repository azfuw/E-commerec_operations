import hashlib
import json
from dataclasses import dataclass
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.audit_events import add_audit_event
from backend.common import (
    ApprovalActionType,
    AuditEventType,
    AuditOutcome,
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
    Product,
    ProductProposal,
    ProposalRevision,
    Store,
    User,
    UserStoreScope,
    WorkflowRun,
)
from backend.optimization_runs import _recheck_canonical_citations
from backend.schemas import (
    CanonicalRuleCitation,
    ProposalActionRequest,
    ProposalCommentActionRequest,
)


_APPROVAL_ROLES = frozenset({UserRole.SUPERVISOR, UserRole.ADMIN})
_TERMINAL_ACTIONS = frozenset(
    {
        ApprovalActionType.APPROVE,
        ApprovalActionType.REJECT,
        ApprovalActionType.REQUEST_CHANGES,
    }
)


@dataclass
class ApprovalDomainError(Exception):
    code: str
    status_code: int


@dataclass(frozen=True)
class ApprovalActionResult:
    action: ApprovalAction
    created: bool


@dataclass(frozen=True)
class _ActionContext:
    actor: User
    store: Store
    proposal: ProductProposal
    run: WorkflowRun
    product: Product
    revision: ProposalRevision


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _key_hash(idempotency_key: str | None) -> str:
    if not isinstance(idempotency_key, str):
        raise ApprovalDomainError("IDEMPOTENCY_KEY_INVALID", 400)
    if not 1 <= len(idempotency_key.strip()) <= 128:
        raise ApprovalDomainError("IDEMPOTENCY_KEY_INVALID", 400)
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()


def _request_hash(
    *,
    action: ApprovalActionType,
    actor_id: str,
    proposal_id: str,
    request: ProposalActionRequest,
    base_product_version: int,
) -> str:
    encoded = _canonical_json(
        {
            "action": action.value,
            "actor_id": actor_id,
            "proposal_id": proposal_id,
            "revision_id": request.revision_id,
            "request": request.model_dump(mode="json"),
            "base_product_version": base_product_version,
        }
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _authorized_context(
    session: AsyncSession,
    *,
    actor_id: str,
    proposal_id: str,
    revision_id: str,
) -> _ActionContext:
    actor = await session.scalar(
        select(User)
        .where(User.id == actor_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if actor is None or actor.status is not UserStatus.ACTIVE:
        raise ApprovalDomainError("PROPOSAL_NOT_FOUND", 404)
    proposal_hint = await session.scalar(
        select(ProductProposal)
        .where(ProductProposal.id == proposal_id)
        .execution_options(populate_existing=True)
    )
    if proposal_hint is None:
        raise ApprovalDomainError("PROPOSAL_NOT_FOUND", 404)
    store = await session.scalar(
        select(Store)
        .where(Store.id == proposal_hint.store_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    scope = await session.scalar(
        select(UserStoreScope)
        .where(
            UserStoreScope.user_id == actor.id,
            UserStoreScope.store_id == proposal_hint.store_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if store is None or not store.enabled or scope is None:
        raise ApprovalDomainError("PROPOSAL_NOT_FOUND", 404)
    proposal = await session.scalar(
        select(ProductProposal)
        .where(
            ProductProposal.id == proposal_id,
            ProductProposal.store_id == store.id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if proposal is None:
        raise ApprovalDomainError("PROPOSAL_NOT_FOUND", 404)
    run = await session.scalar(
        select(WorkflowRun)
        .where(WorkflowRun.id == proposal.optimization_run_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    product = await session.scalar(
        select(Product)
        .where(Product.id == proposal.product_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    revision = await session.scalar(
        select(ProposalRevision)
        .where(
            ProposalRevision.id == revision_id,
            ProposalRevision.proposal_id == proposal.id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if (
        run is None
        or run.workflow_type is not WorkflowType.OPTIMIZATION
        or run.store_id != store.id
        or not isinstance(run.input, dict)
        or run.input.get("proposal_id") != proposal.id
        or run.input.get("product_id") != proposal.product_id
        or run.input.get("store_id") != store.id
        or product is None
        or product.store_id != store.id
        or revision is None
    ):
        raise ApprovalDomainError("PROPOSAL_NOT_FOUND", 404)
    return _ActionContext(actor, store, proposal, run, product, revision)


def _action_spec(
    action: ApprovalActionType,
) -> tuple[AuditEventType, WorkflowStatus, str]:
    if action is ApprovalActionType.SUBMIT:
        return (
            AuditEventType.PROPOSAL_SUBMITTED,
            WorkflowStatus.PENDING_APPROVAL,
            "pending_approval",
        )
    if action is ApprovalActionType.REJECT:
        return AuditEventType.PROPOSAL_REJECTED, WorkflowStatus.REJECTED, "rejected"
    if action is ApprovalActionType.APPROVE:
        return (
            AuditEventType.PROPOSAL_APPROVED,
            WorkflowStatus.COMPLETED,
            "simulated_published",
        )
    return (
        AuditEventType.PROPOSAL_CHANGES_REQUESTED,
        WorkflowStatus.PENDING_MANUAL,
        "approval_changes_requested",
    )


async def _deny_role(
    session: AsyncSession,
    *,
    context: _ActionContext,
    request_id: str,
) -> None:
    add_audit_event(
        session,
        event_type=AuditEventType.AUTHORIZATION_DENIED,
        outcome=AuditOutcome.DENIED,
        store_id=context.store.id,
        actor_id=context.actor.id,
        actor_role=context.actor.role,
        proposal_id=context.proposal.id,
        proposal_revision_id=context.revision.id,
        workflow_run_id=context.run.id,
        request_id=request_id,
        error_code="PROPOSAL_ACTION_FORBIDDEN",
        details={},
    )
    await session.commit()
    raise ApprovalDomainError("PROPOSAL_ACTION_FORBIDDEN", 403)


async def _matching_audit(
    session: AsyncSession,
    *,
    context: _ActionContext,
    action: ApprovalAction,
) -> AuditEvent | None:
    event_type, to_status, current_step = _action_spec(action.action)
    audits = list(
        await session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.event_type == event_type,
                AuditEvent.outcome == AuditOutcome.SUCCESS,
                AuditEvent.store_id == context.store.id,
                AuditEvent.proposal_id == context.proposal.id,
                AuditEvent.proposal_revision_id == action.proposal_revision_id,
                AuditEvent.workflow_run_id == context.run.id,
                AuditEvent.approval_action_id == action.id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    )
    if len(audits) != 1:
        return None
    audit = audits[0]
    expected_from = (
        WorkflowStatus.DRAFT_READY
        if action.action is ApprovalActionType.SUBMIT
        else WorkflowStatus.PENDING_APPROVAL
    )
    if (
        audit.actor_id != action.actor_id
        or audit.actor_role is not action.actor_role
        or audit.error_code is not None
        or audit.details
        != {
            "from_status": expected_from.value,
            "to_status": to_status.value,
            "quality_status": WorkflowQuality.NORMAL.value,
            "current_step": current_step,
        }
    ):
        return None
    return audit


async def _valid_stored_action(
    session: AsyncSession,
    *,
    context: _ActionContext,
    action: ApprovalAction,
) -> bool:
    comment_valid = (
        isinstance(action.comment, str) and 1 <= len(action.comment.strip()) <= 500
        if action.action
        in {ApprovalActionType.REJECT, ApprovalActionType.REQUEST_CHANGES}
        else action.comment is None
    )
    return (
        action.proposal_id == context.proposal.id
        and action.proposal_revision_id == context.revision.id
        and action.store_id == context.store.id
        and action.actor_role
        in (
            _APPROVAL_ROLES
            if action.action in _TERMINAL_ACTIONS
            else frozenset(UserRole)
        )
        and comment_valid
        and await _matching_audit(session, context=context, action=action) is not None
    )


async def _legal_replay_state(
    session: AsyncSession,
    *,
    context: _ActionContext,
    action: ApprovalAction,
) -> bool:
    if action.action is ApprovalActionType.REJECT:
        state_valid = (
            context.run.status is WorkflowStatus.REJECTED
            and context.proposal.submitted_revision_id == context.revision.id
        )
    elif action.action is ApprovalActionType.REQUEST_CHANGES:
        state_valid = (
            context.run.status is WorkflowStatus.PENDING_MANUAL
            and context.proposal.submitted_revision_id is None
        )
    elif action.action is ApprovalActionType.APPROVE:
        state_valid = (
            context.run.status is WorkflowStatus.COMPLETED
            and context.proposal.submitted_revision_id == context.revision.id
        )
    elif context.run.status is WorkflowStatus.PENDING_MANUAL:
        state_valid = context.proposal.submitted_revision_id is None
    else:
        state_valid = (
            context.run.status
            in {
                WorkflowStatus.PENDING_APPROVAL,
                WorkflowStatus.REJECTED,
                WorkflowStatus.COMPLETED,
            }
            and context.proposal.submitted_revision_id == context.revision.id
        )
    if not state_valid:
        return False

    submit_actions = list(
        await session.scalars(
            select(ApprovalAction)
            .where(
                ApprovalAction.proposal_id == context.proposal.id,
                ApprovalAction.proposal_revision_id == context.revision.id,
                ApprovalAction.action == ApprovalActionType.SUBMIT,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    )
    terminal_actions = list(
        await session.scalars(
            select(ApprovalAction)
            .where(
                ApprovalAction.proposal_id == context.proposal.id,
                ApprovalAction.proposal_revision_id == context.revision.id,
                ApprovalAction.action.in_(_TERMINAL_ACTIONS),
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    )
    if action.action is ApprovalActionType.SUBMIT:
        if len(submit_actions) != 1 or submit_actions[0].id != action.id:
            return False
        expected_terminal = {
            WorkflowStatus.REJECTED: ApprovalActionType.REJECT,
            WorkflowStatus.PENDING_MANUAL: ApprovalActionType.REQUEST_CHANGES,
            WorkflowStatus.COMPLETED: ApprovalActionType.APPROVE,
        }.get(context.run.status)
        if expected_terminal is None:
            return not terminal_actions
        return (
            len(terminal_actions) == 1
            and terminal_actions[0].action is expected_terminal
            and await _valid_stored_action(
                session, context=context, action=terminal_actions[0]
            )
        )
    return (
        len(submit_actions) == 1
        and await _valid_stored_action(
            session, context=context, action=submit_actions[0]
        )
        and len(terminal_actions) == 1
        and terminal_actions[0].id == action.id
    )


async def _replay(
    session: AsyncSession,
    *,
    context: _ActionContext,
    action_type: ApprovalActionType,
    request: ProposalActionRequest,
    key_hash: str,
    request_hash: str,
) -> ApprovalActionResult | None:
    action = await session.scalar(
        select(ApprovalAction)
        .where(
            ApprovalAction.proposal_id == context.proposal.id,
            ApprovalAction.actor_id == context.actor.id,
            ApprovalAction.action == action_type,
            ApprovalAction.idempotency_key_hash == key_hash,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if action is None:
        return None
    if action.request_hash != request_hash:
        raise ApprovalDomainError("IDEMPOTENCY_REPLAY_CONFLICT", 409)
    comment = request.comment if isinstance(request, ProposalCommentActionRequest) else None
    if (
        action.proposal_revision_id != context.revision.id
        or action.store_id != context.store.id
        or action.actor_id != context.actor.id
        or action.actor_role
        not in (
            _APPROVAL_ROLES
            if action_type in _TERMINAL_ACTIONS
            else frozenset(UserRole)
        )
        or action.comment != comment
        or not await _legal_replay_state(
            session, context=context, action=action
        )
        or await _matching_audit(session, context=context, action=action) is None
    ):
        raise ApprovalDomainError("PROPOSAL_DATA_INCONSISTENT", 503)
    return ApprovalActionResult(action, False)


async def _current_review(
    session: AsyncSession,
    *,
    context: _ActionContext,
    submit: bool,
) -> None:
    review = await session.scalar(
        select(ComplianceReview)
        .where(ComplianceReview.proposal_revision_id == context.revision.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if review is None:
        code = "PROPOSAL_NOT_SUBMITTABLE" if submit else "PROPOSAL_DATA_INCONSISTENT"
        raise ApprovalDomainError(code, 409 if submit else 503)
    if (
        review.proposal_id != context.proposal.id
        or review.iteration != context.revision.iteration
        or review.citations != context.revision.citations
    ):
        raise ApprovalDomainError("PROPOSAL_DATA_INCONSISTENT", 503)
    if (
        not review.passed
        or review.quality_status is not WorkflowQuality.NORMAL
        or review.error_code is not None
    ):
        code = "PROPOSAL_NOT_SUBMITTABLE" if submit else "PROPOSAL_DATA_INCONSISTENT"
        raise ApprovalDomainError(code, 409 if submit else 503)
    try:
        citations = [
            CanonicalRuleCitation.model_validate(value)
            for value in context.revision.citations
        ]
    except (TypeError, ValueError, ValidationError):
        raise ApprovalDomainError("PROPOSAL_DATA_INCONSISTENT", 503) from None
    if not await _recheck_canonical_citations(session, context.product, citations):
        raise ApprovalDomainError("TRUSTED_EVIDENCE_INVALID", 422)


async def _conflicting_terminal_action(
    session: AsyncSession, context: _ActionContext
) -> bool:
    return (
        await session.scalar(
            select(ApprovalAction.id)
            .where(
                ApprovalAction.proposal_id == context.proposal.id,
                ApprovalAction.proposal_revision_id == context.revision.id,
                ApprovalAction.action.in_(_TERMINAL_ACTIONS),
            )
            .with_for_update()
        )
        is not None
    )


async def _conflicting_submit_action(
    session: AsyncSession, context: _ActionContext
) -> bool:
    return (
        await session.scalar(
            select(ApprovalAction.id)
            .where(
                ApprovalAction.proposal_id == context.proposal.id,
                ApprovalAction.proposal_revision_id == context.revision.id,
                ApprovalAction.action == ApprovalActionType.SUBMIT,
            )
            .with_for_update()
        )
        is not None
    )


async def _first_write(
    session: AsyncSession,
    *,
    context: _ActionContext,
    action_type: ApprovalActionType,
    request: ProposalActionRequest,
    key_hash: str,
    request_hash: str,
    request_id: str,
) -> ApprovalActionResult:
    submit = action_type is ApprovalActionType.SUBMIT
    if not submit and await _conflicting_terminal_action(session, context):
        raise ApprovalDomainError("APPROVAL_ACTION_CONFLICT", 409)
    if submit:
        if (
            context.run.status is not WorkflowStatus.DRAFT_READY
            or context.proposal.current_revision_id != context.revision.id
            or context.proposal.active_manual_review_run_id is not None
        ):
            raise ApprovalDomainError("PROPOSAL_NOT_SUBMITTABLE", 409)
    elif (
        context.run.status is not WorkflowStatus.PENDING_APPROVAL
        or context.proposal.submitted_revision_id != context.revision.id
        or context.proposal.current_revision_id != context.revision.id
        or context.proposal.active_manual_review_run_id is not None
    ):
        raise ApprovalDomainError("APPROVAL_STATE_CONFLICT", 409)
    if (
        context.product.current_version != context.proposal.base_product_version
        or context.revision.base_product_version != context.proposal.base_product_version
    ):
        raise ApprovalDomainError("PRODUCT_VERSION_CONFLICT", 409)
    await _current_review(session, context=context, submit=submit)
    if not submit:
        submit_action = await session.scalar(
            select(ApprovalAction)
            .where(
                ApprovalAction.proposal_id == context.proposal.id,
                ApprovalAction.proposal_revision_id == context.revision.id,
                ApprovalAction.action == ApprovalActionType.SUBMIT,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if submit_action is None or await _matching_audit(
            session, context=context, action=submit_action
        ) is None:
            raise ApprovalDomainError("PROPOSAL_DATA_INCONSISTENT", 503)
    comment = request.comment if isinstance(request, ProposalCommentActionRequest) else None
    action = ApprovalAction(
        id=str(uuid4()),
        proposal_id=context.proposal.id,
        proposal_revision_id=context.revision.id,
        store_id=context.store.id,
        actor_id=context.actor.id,
        actor_role=context.actor.role,
        action=action_type,
        comment=comment,
        idempotency_key_hash=key_hash,
        request_hash=request_hash,
    )
    session.add(action)
    await session.flush()
    event_type, to_status, current_step = _action_spec(action_type)
    add_audit_event(
        session,
        event_type=event_type,
        outcome=AuditOutcome.SUCCESS,
        store_id=context.store.id,
        actor_id=context.actor.id,
        actor_role=context.actor.role,
        proposal_id=context.proposal.id,
        proposal_revision_id=context.revision.id,
        workflow_run_id=context.run.id,
        approval_action_id=action.id,
        request_id=request_id,
        details={
            "from_status": (
                WorkflowStatus.DRAFT_READY.value
                if submit
                else WorkflowStatus.PENDING_APPROVAL.value
            ),
            "to_status": to_status.value,
            "quality_status": WorkflowQuality.NORMAL.value,
            "current_step": current_step,
        },
    )
    await session.flush()
    context.run.status = to_status
    context.run.quality_status = WorkflowQuality.NORMAL
    context.run.current_step = current_step
    context.run.error_code = None
    if submit:
        context.proposal.submitted_revision_id = context.revision.id
    elif action_type is ApprovalActionType.REQUEST_CHANGES:
        context.proposal.submitted_revision_id = None
    await session.commit()
    return ApprovalActionResult(action, True)


async def _perform_action(
    session: AsyncSession,
    *,
    actor_id: str,
    proposal_id: str,
    request: ProposalActionRequest,
    idempotency_key: str | None,
    request_id: str,
    action_type: ApprovalActionType,
) -> ApprovalActionResult:
    key_hash = _key_hash(idempotency_key)

    async def load_and_replay() -> tuple[_ActionContext, ApprovalActionResult | None, str]:
        context = await _authorized_context(
            session,
            actor_id=actor_id,
            proposal_id=proposal_id,
            revision_id=request.revision_id,
        )
        if action_type in _TERMINAL_ACTIONS and context.actor.role not in _APPROVAL_ROLES:
            await _deny_role(session, context=context, request_id=request_id)
        request_hash = _request_hash(
            action=action_type,
            actor_id=actor_id,
            proposal_id=proposal_id,
            request=request,
            base_product_version=context.proposal.base_product_version,
        )
        replay = await _replay(
            session,
            context=context,
            action_type=action_type,
            request=request,
            key_hash=key_hash,
            request_hash=request_hash,
        )
        return context, replay, request_hash

    try:
        context, replay, request_hash = await load_and_replay()
        if replay is not None:
            await session.commit()
            return replay
        return await _first_write(
            session,
            context=context,
            action_type=action_type,
            request=request,
            key_hash=key_hash,
            request_hash=request_hash,
            request_id=request_id,
        )
    except IntegrityError:
        await session.rollback()
        try:
            context, replay, _ = await load_and_replay()
            if replay is not None:
                await session.commit()
                return replay
            if action_type in _TERMINAL_ACTIONS and await _conflicting_terminal_action(
                session, context
            ):
                raise ApprovalDomainError("APPROVAL_ACTION_CONFLICT", 409)
            if action_type is ApprovalActionType.SUBMIT and await _conflicting_submit_action(
                session, context
            ):
                raise ApprovalDomainError("APPROVAL_ACTION_CONFLICT", 409)
            raise ApprovalDomainError("PROPOSAL_DATA_INCONSISTENT", 503)
        except ApprovalDomainError:
            await session.rollback()
            raise
        except SQLAlchemyError:
            await session.rollback()
            raise ApprovalDomainError("PROPOSAL_DATA_INCONSISTENT", 503) from None
    except ApprovalDomainError:
        await session.rollback()
        raise
    except (SQLAlchemyError, ValueError):
        await session.rollback()
        raise ApprovalDomainError("PROPOSAL_DATA_INCONSISTENT", 503) from None


async def submit_proposal(
    session: AsyncSession,
    *,
    actor_id: str,
    proposal_id: str,
    request: ProposalActionRequest,
    idempotency_key: str | None,
    request_id: str,
) -> ApprovalActionResult:
    return await _perform_action(
        session,
        actor_id=actor_id,
        proposal_id=proposal_id,
        request=request,
        idempotency_key=idempotency_key,
        request_id=request_id,
        action_type=ApprovalActionType.SUBMIT,
    )


async def reject_proposal(
    session: AsyncSession,
    *,
    actor_id: str,
    proposal_id: str,
    request: ProposalCommentActionRequest,
    idempotency_key: str | None,
    request_id: str,
) -> ApprovalActionResult:
    return await _perform_action(
        session,
        actor_id=actor_id,
        proposal_id=proposal_id,
        request=request,
        idempotency_key=idempotency_key,
        request_id=request_id,
        action_type=ApprovalActionType.REJECT,
    )


async def request_proposal_changes(
    session: AsyncSession,
    *,
    actor_id: str,
    proposal_id: str,
    request: ProposalCommentActionRequest,
    idempotency_key: str | None,
    request_id: str,
) -> ApprovalActionResult:
    return await _perform_action(
        session,
        actor_id=actor_id,
        proposal_id=proposal_id,
        request=request,
        idempotency_key=idempotency_key,
        request_id=request_id,
        action_type=ApprovalActionType.REQUEST_CHANGES,
    )
