import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Literal, TypedDict

import httpx
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.common import AgentCallType, WorkflowQuality
from backend.compliance_agent import (
    ComplianceAgentCallRecord,
    ProductComplianceAgentClient,
)
from backend.config import Settings
from backend.optimization_agent import (
    OptimizationAgentCallRecord,
    ProductOptimizationAgentClient,
)
from backend.optimization_runs import (
    OptimizationFailureCode,
    OptimizationTerminalResult,
    OwnedOptimizationContext,
    PendingManualErrorCode,
    claim_next_optimization_run,
    defer_optimization_manual,
    fail_optimization_run,
    finalize_optimization_draft,
    load_owned_optimization_context,
    persist_compliance_failure,
    persist_compliance_review,
    persist_optimization_revision,
    renew_optimization_lease,
    update_optimization_step,
)
from backend.optimization_validation import DeterministicComplianceResult, validate_optimization_output
from backend.schemas import OptimizationProposalOutput, TrustedOptimizationInput, ValidatedRequiredChange


KnowledgeLoadErrorCode = Literal[
    "KNOWLEDGE_MODEL_UNAVAILABLE",
    "KNOWLEDGE_DEPENDENCY_TIMEOUT",
    "KNOWLEDGE_DEPENDENCY_ERROR",
    "KNOWLEDGE_ZERO_HIT",
    "KNOWLEDGE_LOW_CONFIDENCE",
]
BeforeTrustedInputExternalAttempt = Callable[[], Awaitable[None]]
TrustedInputLoader = Callable[
    [OwnedOptimizationContext, BeforeTrustedInputExternalAttempt], Awaitable[TrustedOptimizationInput]
]

_PENDING_CODES = {
    "DEEPSEEK_KEY_MISSING",
    "DEEPSEEK_TIMEOUT",
    "DEEPSEEK_TRANSPORT",
    "DEEPSEEK_RATE_LIMIT",
    "DEEPSEEK_SERVER_ERROR",
    "DEEPSEEK_UNAUTHORIZED",
    "DEEPSEEK_FORBIDDEN",
    "DEEPSEEK_HTTP_ERROR",
    "DEEPSEEK_SCHEMA_INVALID",
    "KNOWLEDGE_MODEL_UNAVAILABLE",
    "KNOWLEDGE_DEPENDENCY_TIMEOUT",
    "KNOWLEDGE_DEPENDENCY_ERROR",
    "KNOWLEDGE_ZERO_HIT",
    "KNOWLEDGE_LOW_CONFIDENCE",
    "COMPLIANCE_AGENT_DEGRADED",
    "OPTIMIZATION_ITERATION_LIMIT",
}
_KNOWLEDGE_CODES = {
    "KNOWLEDGE_MODEL_UNAVAILABLE",
    "KNOWLEDGE_DEPENDENCY_TIMEOUT",
    "KNOWLEDGE_DEPENDENCY_ERROR",
    "KNOWLEDGE_ZERO_HIT",
    "KNOWLEDGE_LOW_CONFIDENCE",
}


class OptimizationLeaseLost(RuntimeError):
    pass


class OptimizationCheckpointFailure(RuntimeError):
    pass


class TrustedInputLoadFailure(RuntimeError):
    def __init__(self, error_code: KnowledgeLoadErrorCode) -> None:
        if error_code not in _KNOWLEDGE_CODES:
            raise ValueError("unsupported knowledge load error")
        self.error_code = error_code
        super().__init__(error_code)


class OptimizationWorkflowState(TypedDict, total=False):
    workflow_run_id: str
    iteration: int
    revision_id: str | None
    review_id: str | None
    review_passed: bool | None
    review_quality_status: Literal["normal", "degraded"] | None
    error_code: PendingManualErrorCode | OptimizationFailureCode | None
    next_node: Literal[
        "load_or_resume",
        "persist_revision",
        "persist_review",
        "finalize_draft",
        "defer_manual",
        "stop",
    ]


class _CheckpointBoundary(BaseCheckpointSaver):
    def __init__(self, saver: BaseCheckpointSaver) -> None:
        super().__init__(serde=saver.serde)
        self._saver = saver
        self.cancelled = False
        self.failed = False

    @property
    def config_specs(self):
        return self._saver.config_specs

    def get_next_version(self, current, channel):
        return self._saver.get_next_version(current, channel)

    async def aget_tuple(self, *args, **kwargs):
        try:
            return await self._saver.aget_tuple(*args, **kwargs)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        except OptimizationCheckpointFailure:
            raise
        except Exception:
            self.failed = True
            raise OptimizationCheckpointFailure() from None

    async def aput(self, *args, **kwargs):
        try:
            return await self._saver.aput(*args, **kwargs)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        except OptimizationCheckpointFailure:
            raise
        except Exception:
            self.failed = True
            raise OptimizationCheckpointFailure() from None

    async def aput_writes(self, *args, **kwargs):
        try:
            return await self._saver.aput_writes(*args, **kwargs)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        except OptimizationCheckpointFailure:
            raise
        except Exception:
            self.failed = True
            raise OptimizationCheckpointFailure() from None


def _safe_state(
    *,
    workflow_run_id: str,
    iteration: int,
    revision_id: str | None,
    review_id: str | None,
    review_passed: bool | None,
    review_quality_status: WorkflowQuality | None,
    error_code: str | None,
    next_node: OptimizationWorkflowState["next_node"],
) -> OptimizationWorkflowState:
    return {
        "workflow_run_id": workflow_run_id,
        "iteration": iteration,
        "revision_id": revision_id,
        "review_id": review_id,
        "review_passed": review_passed,
        "review_quality_status": (
            review_quality_status.value if review_quality_status is not None else None
        ),
        "error_code": error_code,
        "next_node": next_node,
    }


def _context_state(
    context: OwnedOptimizationContext, *, iteration: int, next_node: OptimizationWorkflowState["next_node"]
) -> OptimizationWorkflowState:
    return _safe_state(
        workflow_run_id=context.workflow_run_id,
        iteration=iteration,
        revision_id=context.current_revision_id,
        review_id=context.current_review_id,
        review_passed=context.current_review_passed,
        review_quality_status=context.current_review_quality_status,
        error_code=context.current_review_error_code,
        next_node=next_node,
    )


def _deterministic_changes(
    deterministic: DeterministicComplianceResult,
) -> tuple[ValidatedRequiredChange, ...]:
    return tuple(
        ValidatedRequiredChange(
            source_track="deterministic",
            source_violation_code=violation.code,
            field=violation.field,
            instruction=violation.message_zh,
            citation_chunk_ids=[],
        )
        for violation in sorted(
            deterministic.violations,
            key=lambda item: (item.code, item.field, item.message_zh),
        )
    )


def _pending_code(value: str | None) -> PendingManualErrorCode:
    if value not in _PENDING_CODES:
        raise ValueError("unsupported pending-manual code")
    return value


async def _context_or_stop(
    session: AsyncSession, workflow_run_id: str, lease_owner: str
) -> OwnedOptimizationContext | None:
    loaded = await load_owned_optimization_context(
        session, workflow_run_id=workflow_run_id, lease_owner=lease_owner
    )
    if loaded.disposition == "lease_lost":
        raise OptimizationLeaseLost()
    return loaded.context


def _target_revision(
    context: OwnedOptimizationContext,
) -> tuple[int, tuple[ValidatedRequiredChange, ...]] | None:
    if context.current_revision_id is None:
        return 0, ()
    if context.current_review_id is None:
        return None
    if (
        context.current_review_passed is False
        and context.current_review_quality_status is WorkflowQuality.NORMAL
        and context.current_review_error_code is None
        and context.current_required_changes
        and context.current_revision_iteration is not None
        and context.current_revision_iteration < 2
    ):
        return context.current_revision_iteration + 1, context.current_required_changes
    return None


def _terminal_state(
    context: OwnedOptimizationContext, result: OptimizationTerminalResult
) -> OptimizationWorkflowState:
    if result.disposition == "lease_lost":
        raise OptimizationLeaseLost()
    return _safe_state(
        workflow_run_id=context.workflow_run_id,
        iteration=context.current_revision_iteration or 0,
        revision_id=context.current_revision_id,
        review_id=context.current_review_id,
        review_passed=context.current_review_passed,
        review_quality_status=context.current_review_quality_status,
        error_code=result.error_code,
        next_node="stop",
    )


def build_optimization_graph(
    *,
    session: AsyncSession,
    settings: Settings,
    lease_owner: str,
    checkpointer: BaseCheckpointSaver,
    trusted_input_loader: TrustedInputLoader,
    transport: httpx.AsyncBaseTransport | None = None,
    max_agent_attempts: int = 3,
):
    if max_agent_attempts not in {1, 2, 3}:
        raise ValueError("max_agent_attempts must be between 1 and 3")

    boundary = _CheckpointBoundary(checkpointer)

    def check_checkpoint() -> None:
        if boundary.cancelled:
            raise asyncio.CancelledError()
        if boundary.failed:
            raise OptimizationCheckpointFailure()

    async def renew_before_rag(workflow_run_id: str) -> None:
        if not await renew_optimization_lease(
            session,
            workflow_run_id=workflow_run_id,
            lease_owner=lease_owner,
            lease_seconds=settings.optimization_lease_seconds,
        ):
            raise OptimizationLeaseLost()

    def renew_before_deepseek(workflow_run_id: str):
        async def renew(_attempt: int) -> bool:
            return await renew_optimization_lease(
                session,
                workflow_run_id=workflow_run_id,
                lease_owner=lease_owner,
                lease_seconds=settings.optimization_lease_seconds,
            )

        return renew

    async def load_or_resume(state: OptimizationWorkflowState) -> OptimizationWorkflowState:
        check_checkpoint()
        workflow_run_id = state["workflow_run_id"]
        context = await _context_or_stop(session, workflow_run_id, lease_owner)
        if context is None:
            return _safe_state(
                workflow_run_id=workflow_run_id,
                iteration=0,
                revision_id=None,
                review_id=None,
                review_passed=None,
                review_quality_status=None,
                error_code=None,
                next_node="stop",
            )
        if context.current_revision_id is None:
            return _context_state(context, iteration=0, next_node="persist_revision")
        if context.current_review_id is None:
            return _context_state(
                context,
                iteration=context.current_revision_iteration or 0,
                next_node="persist_review",
            )
        if context.current_review_passed is True:
            return _context_state(
                context,
                iteration=context.current_revision_iteration or 0,
                next_node="finalize_draft",
            )
        target = _target_revision(context)
        if target is not None:
            return _context_state(context, iteration=target[0], next_node="persist_revision")
        return _context_state(
            context,
            iteration=context.current_revision_iteration or 0,
            next_node="defer_manual",
        )

    async def defer(
        context: OwnedOptimizationContext,
        error_code: PendingManualErrorCode,
        iteration: int,
        calls: Sequence[OptimizationAgentCallRecord] = (),
    ) -> OptimizationWorkflowState:
        result = await defer_optimization_manual(
            session,
            workflow_run_id=context.workflow_run_id,
            lease_owner=lease_owner,
            error_code=error_code,
            iteration=iteration,
            optimization_calls=calls,
        )
        return _terminal_state(context, result)

    async def persist_revision(state: OptimizationWorkflowState) -> OptimizationWorkflowState:
        check_checkpoint()
        workflow_run_id = state["workflow_run_id"]
        context = await _context_or_stop(session, workflow_run_id, lease_owner)
        if context is None:
            return _safe_state(
                workflow_run_id=workflow_run_id, iteration=0, revision_id=None, review_id=None,
                review_passed=None, review_quality_status=None, error_code=None, next_node="stop",
            )
        target = _target_revision(context)
        if target is None:
            return _context_state(
                context,
                iteration=context.current_revision_iteration or 0,
                next_node="load_or_resume",
            )
        iteration, required_changes = target
        if not await update_optimization_step(
            session,
            workflow_run_id=workflow_run_id,
            lease_owner=lease_owner,
            current_step="optimization_input",
        ):
            raise OptimizationLeaseLost()
        try:
            trusted = await trusted_input_loader(
                context, lambda: renew_before_rag(workflow_run_id)
            )
        except TrustedInputLoadFailure as error:
            return await defer(context, _pending_code(error.error_code), iteration)
        refreshed = await _context_or_stop(session, workflow_run_id, lease_owner)
        if refreshed is None:
            return _context_state(context, iteration=iteration, next_node="stop")
        if _target_revision(refreshed) != target:
            return _context_state(
                refreshed,
                iteration=refreshed.current_revision_iteration or 0,
                next_node="load_or_resume",
            )
        context = refreshed
        if trusted.rag_quality in {"zero_hit", "low_confidence"}:
            return await defer(
                context,
                _pending_code(
                    "KNOWLEDGE_ZERO_HIT"
                    if trusted.rag_quality == "zero_hit"
                    else "KNOWLEDGE_LOW_CONFIDENCE"
                ),
                iteration,
            )
        if not await update_optimization_step(
            session,
            workflow_run_id=workflow_run_id,
            lease_owner=lease_owner,
            current_step="call_optimization_agent",
        ):
            raise OptimizationLeaseLost()
        client = ProductOptimizationAgentClient(settings, transport=transport)
        invocation = await client.request(
            trusted,
            required_changes=required_changes,
            call_type=AgentCallType.PRIMARY,
            iteration=iteration,
            before_http_attempt=renew_before_deepseek(workflow_run_id),
            max_attempts=max_agent_attempts,
        )
        records = list(invocation.records)
        if invocation.error_code == "LEASE_LOST":
            raise OptimizationLeaseLost()
        if invocation.response is None and invocation.error_code == "DEEPSEEK_SCHEMA_INVALID":
            repair = await client.request(
                trusted,
                required_changes=required_changes,
                call_type=AgentCallType.SCHEMA_REPAIR,
                iteration=iteration,
                before_http_attempt=renew_before_deepseek(workflow_run_id),
                max_attempts=max_agent_attempts,
            )
            records.extend(repair.records)
            if repair.error_code == "LEASE_LOST":
                raise OptimizationLeaseLost()
            invocation = repair
        if invocation.response is None:
            return await defer(context, _pending_code(invocation.error_code), iteration, records)
        deterministic = validate_optimization_output(trusted, invocation.response)
        persisted = await persist_optimization_revision(
            session,
            workflow_run_id=workflow_run_id,
            lease_owner=lease_owner,
            iteration=iteration,
            trusted=trusted,
            output=invocation.response,
            canonical_citations=deterministic.canonical_citations,
            calls=records,
        )
        if persisted.disposition == "lease_lost":
            raise OptimizationLeaseLost()
        if persisted.disposition == "failed":
            return _context_state(context, iteration=iteration, next_node="stop")
        return _safe_state(
            workflow_run_id=workflow_run_id,
            iteration=iteration,
            revision_id=persisted.revision_id,
            review_id=None,
            review_passed=None,
            review_quality_status=None,
            error_code=None,
            next_node="load_or_resume",
        )

    async def persist_review(state: OptimizationWorkflowState) -> OptimizationWorkflowState:
        check_checkpoint()
        workflow_run_id = state["workflow_run_id"]
        context = await _context_or_stop(session, workflow_run_id, lease_owner)
        if context is None:
            return _safe_state(
                workflow_run_id=workflow_run_id, iteration=0, revision_id=None, review_id=None,
                review_passed=None, review_quality_status=None, error_code=None, next_node="stop",
            )
        if context.current_revision_id is None or context.current_revision_iteration is None:
            result = await fail_optimization_run(
                session,
                workflow_run_id=workflow_run_id,
                lease_owner=lease_owner,
                error_code="OPTIMIZATION_CONTEXT_INCONSISTENT",
            )
            return _terminal_state(context, result)
        iteration = context.current_revision_iteration
        if context.current_review_id is not None:
            return _context_state(context, iteration=iteration, next_node="load_or_resume")
        if context.current_proposal_output is None:
            result = await fail_optimization_run(
                session,
                workflow_run_id=workflow_run_id,
                lease_owner=lease_owner,
                error_code="OPTIMIZATION_CONTEXT_INCONSISTENT",
            )
            return _terminal_state(context, result)
        if not await update_optimization_step(
            session,
            workflow_run_id=workflow_run_id,
            lease_owner=lease_owner,
            current_step="optimization_review",
        ):
            raise OptimizationLeaseLost()
        try:
            trusted = await trusted_input_loader(
                context, lambda: renew_before_rag(workflow_run_id)
            )
        except TrustedInputLoadFailure as error:
            return await defer(context, _pending_code(error.error_code), iteration)
        refreshed = await _context_or_stop(session, workflow_run_id, lease_owner)
        if refreshed is None:
            return _context_state(context, iteration=iteration, next_node="stop")
        if (
            refreshed.current_revision_id != context.current_revision_id
            or refreshed.current_review_id is not None
            or refreshed.current_revision_iteration != iteration
        ):
            return _context_state(
                refreshed,
                iteration=refreshed.current_revision_iteration or 0,
                next_node="load_or_resume",
            )
        context = refreshed
        deterministic = validate_optimization_output(trusted, context.current_proposal_output)
        deterministic_changes = _deterministic_changes(deterministic)
        if trusted.rag_quality in {"zero_hit", "low_confidence"}:
            error_code = _pending_code(
                "KNOWLEDGE_ZERO_HIT"
                if trusted.rag_quality == "zero_hit"
                else "KNOWLEDGE_LOW_CONFIDENCE"
            )
            failure = await persist_compliance_failure(
                session,
                workflow_run_id=workflow_run_id,
                lease_owner=lease_owner,
                revision_id=context.current_revision_id,
                iteration=iteration,
                deterministic=deterministic,
                error_code=error_code,
                required_changes=deterministic_changes,
                canonical_citations=deterministic.canonical_citations,
                calls=(),
            )
            if failure.disposition == "lease_lost":
                raise OptimizationLeaseLost()
            if failure.disposition == "failed":
                return _context_state(context, iteration=iteration, next_node="stop")
            refreshed = await _context_or_stop(session, workflow_run_id, lease_owner)
            if refreshed is None:
                return _context_state(context, iteration=iteration, next_node="stop")
            return await defer(refreshed, error_code, iteration)
        if not await update_optimization_step(
            session,
            workflow_run_id=workflow_run_id,
            lease_owner=lease_owner,
            current_step="call_compliance_agent",
        ):
            raise OptimizationLeaseLost()
        client = ProductComplianceAgentClient(settings, transport=transport)
        invocation = await client.request(
            context.current_proposal_output,
            deterministic,
            trusted,
            call_type=AgentCallType.PRIMARY,
            iteration=iteration,
            before_http_attempt=renew_before_deepseek(workflow_run_id),
            max_attempts=max_agent_attempts,
        )
        records: list[ComplianceAgentCallRecord] = list(invocation.records)
        if invocation.error_code == "LEASE_LOST":
            raise OptimizationLeaseLost()
        if invocation.response is None and invocation.error_code == "DEEPSEEK_SCHEMA_INVALID":
            repair = await client.request(
                context.current_proposal_output,
                deterministic,
                trusted,
                call_type=AgentCallType.SCHEMA_REPAIR,
                iteration=iteration,
                before_http_attempt=renew_before_deepseek(workflow_run_id),
                max_attempts=max_agent_attempts,
            )
            records.extend(repair.records)
            if repair.error_code == "LEASE_LOST":
                raise OptimizationLeaseLost()
            invocation = repair
        if invocation.response is None:
            error_code = _pending_code(invocation.error_code)
            failure = await persist_compliance_failure(
                session,
                workflow_run_id=workflow_run_id,
                lease_owner=lease_owner,
                revision_id=context.current_revision_id,
                iteration=iteration,
                deterministic=deterministic,
                error_code=error_code,
                required_changes=deterministic_changes,
                canonical_citations=deterministic.canonical_citations,
                calls=records,
            )
            if failure.disposition == "lease_lost":
                raise OptimizationLeaseLost()
            if failure.disposition == "failed":
                return _context_state(context, iteration=iteration, next_node="stop")
            refreshed = await _context_or_stop(session, workflow_run_id, lease_owner)
            if refreshed is None:
                return _context_state(context, iteration=iteration, next_node="stop")
            return await defer(refreshed, error_code, iteration)
        changes = (*deterministic_changes, *invocation.response.required_changes)
        persisted = await persist_compliance_review(
            session,
            workflow_run_id=workflow_run_id,
            lease_owner=lease_owner,
            revision_id=context.current_revision_id,
            iteration=iteration,
            deterministic=deterministic,
            semantic=invocation.response,
            required_changes=changes,
            canonical_citations=deterministic.canonical_citations,
            calls=records,
        )
        if persisted.disposition == "lease_lost":
            raise OptimizationLeaseLost()
        if persisted.disposition == "failed":
            return _context_state(context, iteration=iteration, next_node="stop")
        refreshed = await _context_or_stop(session, workflow_run_id, lease_owner)
        if refreshed is None:
            return _context_state(context, iteration=iteration, next_node="stop")
        return _context_state(refreshed, iteration=iteration, next_node="load_or_resume")

    async def finalize_draft(state: OptimizationWorkflowState) -> OptimizationWorkflowState:
        check_checkpoint()
        context = await _context_or_stop(session, state["workflow_run_id"], lease_owner)
        if context is None:
            return _safe_state(
                workflow_run_id=state["workflow_run_id"], iteration=0, revision_id=None, review_id=None,
                review_passed=None, review_quality_status=None, error_code=None, next_node="stop",
            )
        result = await finalize_optimization_draft(
            session, workflow_run_id=context.workflow_run_id, lease_owner=lease_owner
        )
        return _terminal_state(context, result)

    async def defer_manual(state: OptimizationWorkflowState) -> OptimizationWorkflowState:
        check_checkpoint()
        context = await _context_or_stop(session, state["workflow_run_id"], lease_owner)
        if context is None:
            return _safe_state(
                workflow_run_id=state["workflow_run_id"], iteration=0, revision_id=None, review_id=None,
                review_passed=None, review_quality_status=None, error_code=None, next_node="stop",
            )
        iteration = context.current_revision_iteration
        if iteration is None:
            result = await fail_optimization_run(
                session,
                workflow_run_id=context.workflow_run_id,
                lease_owner=lease_owner,
                error_code="OPTIMIZATION_CONTEXT_INCONSISTENT",
            )
            return _terminal_state(context, result)
        if context.current_review_passed:
            return await defer(context, "DEEPSEEK_TIMEOUT", iteration)
        if context.current_review_error_code in _PENDING_CODES:
            return await defer(context, _pending_code(context.current_review_error_code), iteration)
        if (
            iteration == 2
            and context.current_review_passed is False
            and context.current_review_quality_status is WorkflowQuality.NORMAL
            and context.current_review_error_code is None
        ):
            return await defer(context, "OPTIMIZATION_ITERATION_LIMIT", iteration)
        result = await fail_optimization_run(
            session,
            workflow_run_id=context.workflow_run_id,
            lease_owner=lease_owner,
            error_code="OPTIMIZATION_CONTEXT_INCONSISTENT",
        )
        return _terminal_state(context, result)

    async def stop(_state: OptimizationWorkflowState) -> OptimizationWorkflowState:
        check_checkpoint()
        return {}

    def route(state: OptimizationWorkflowState) -> str:
        return state.get("next_node", "stop")

    graph = StateGraph(OptimizationWorkflowState)
    graph.add_node("load_or_resume", load_or_resume)
    graph.add_node("persist_revision", persist_revision)
    graph.add_node("persist_review", persist_review)
    graph.add_node("finalize_draft", finalize_draft)
    graph.add_node("defer_manual", defer_manual)
    graph.add_node("stop", stop)
    graph.add_edge(START, "load_or_resume")
    graph.add_conditional_edges(
        "load_or_resume",
        route,
        {
            "persist_revision": "persist_revision",
            "persist_review": "persist_review",
            "finalize_draft": "finalize_draft",
            "defer_manual": "defer_manual",
            "stop": "stop",
        },
    )
    graph.add_conditional_edges(
        "persist_revision",
        route,
        {"load_or_resume": "load_or_resume", "stop": "stop"},
    )
    graph.add_conditional_edges(
        "persist_review",
        route,
        {"load_or_resume": "load_or_resume", "stop": "stop"},
    )
    graph.add_conditional_edges("finalize_draft", route, {"stop": "stop"})
    graph.add_conditional_edges("defer_manual", route, {"stop": "stop"})
    graph.add_edge("stop", END)
    return graph.compile(checkpointer=boundary)


async def _fail_current_run(
    session_factory: async_sessionmaker[AsyncSession],
    workflow_run_id: str,
    lease_owner: str,
    error_code: OptimizationFailureCode,
) -> None:
    async with session_factory() as session:
        await fail_optimization_run(
            session,
            workflow_run_id=workflow_run_id,
            lease_owner=lease_owner,
            error_code=error_code,
        )


async def run_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    lease_owner: str,
    checkpointer: BaseCheckpointSaver,
    trusted_input_loader: TrustedInputLoader,
    transport: httpx.AsyncBaseTransport | None = None,
    max_agent_attempts: int = 3,
) -> str | None:
    if max_agent_attempts not in {1, 2, 3}:
        raise ValueError("max_agent_attempts must be between 1 and 3")
    workflow_run_id: str | None = None
    failure_code: OptimizationFailureCode | None = None
    async with session_factory() as session:
        claim = await claim_next_optimization_run(
            session,
            lease_owner=lease_owner,
            lease_seconds=settings.optimization_lease_seconds,
        )
        if claim is None:
            return None
        workflow_run_id = claim.workflow_run_id
        try:
            graph = build_optimization_graph(
                session=session,
                settings=settings,
                lease_owner=lease_owner,
                checkpointer=checkpointer,
                trusted_input_loader=trusted_input_loader,
                transport=transport,
                max_agent_attempts=max_agent_attempts,
            )
            await graph.ainvoke(
                {"workflow_run_id": workflow_run_id},
                config={"configurable": {"thread_id": workflow_run_id}},
            )
        except OptimizationLeaseLost:
            return workflow_run_id
        except OptimizationCheckpointFailure:
            await session.rollback()
            failure_code = "OPTIMIZATION_CHECKPOINT_ERROR"
        except SQLAlchemyError:
            await session.rollback()
            failure_code = "OPTIMIZATION_DATABASE_ERROR"
    if failure_code is not None:
        assert workflow_run_id is not None
        await _fail_current_run(session_factory, workflow_run_id, lease_owner, failure_code)
    return workflow_run_id
