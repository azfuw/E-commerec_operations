import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal, TypedDict

import httpx
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.common import AgentCallType, WorkflowQuality
from backend.compliance_agent import (
    COMPLIANCE_PROMPT_VERSION,
    ComplianceAgentCallRecord,
    ComplianceAgentResponse,
    ProductComplianceAgentClient,
)
from backend.config import Settings
from backend.manual_review_runs import (
    OwnedManualReviewContext,
    claim_next_manual_review_run,
    fail_manual_review_run,
    finalize_manual_review,
    load_owned_manual_review_context,
    persist_manual_compliance_review,
    renew_manual_review_lease,
)
from backend.models import AgentCall, ComplianceReview
from backend.optimization_validation import DeterministicComplianceResult, validate_optimization_output
from backend.schemas import TrustedOptimizationInput


KnowledgeLoadErrorCode = Literal[
    "KNOWLEDGE_MODEL_UNAVAILABLE",
    "KNOWLEDGE_DEPENDENCY_TIMEOUT",
    "KNOWLEDGE_DEPENDENCY_ERROR",
    "KNOWLEDGE_ZERO_HIT",
    "KNOWLEDGE_LOW_CONFIDENCE",
]
BeforeTrustedInputExternalAttempt = Callable[[], Awaitable[None]]
TrustedInputLoader = Callable[
    [OwnedManualReviewContext, BeforeTrustedInputExternalAttempt], Awaitable[TrustedOptimizationInput]
]

_KNOWLEDGE_CODES = {
    "KNOWLEDGE_MODEL_UNAVAILABLE",
    "KNOWLEDGE_DEPENDENCY_TIMEOUT",
    "KNOWLEDGE_DEPENDENCY_ERROR",
    "KNOWLEDGE_ZERO_HIT",
    "KNOWLEDGE_LOW_CONFIDENCE",
}


class ManualReviewLeaseLost(RuntimeError):
    pass


class ManualReviewCheckpointFailure(RuntimeError):
    pass


class TrustedInputLoadFailure(RuntimeError):
    def __init__(self, error_code: KnowledgeLoadErrorCode) -> None:
        if error_code not in _KNOWLEDGE_CODES:
            raise ValueError("unsupported knowledge load error")
        self.error_code = error_code
        super().__init__(error_code)


class ManualReviewWorkflowState(TypedDict, total=False):
    workflow_run_id: str
    manual_review_run_id: str
    proposal_id: str
    revision_id: str
    review_id: str | None
    review_passed: bool | None
    review_quality_status: Literal["normal", "degraded"] | None
    error_code: str | None
    next_node: Literal[
        "load_or_resume",
        "load_trusted_input",
        "run_deterministic_checks",
        "call_compliance_agent",
        "persist_manual_review",
        "finalize_manual_review",
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
        except ManualReviewCheckpointFailure:
            raise
        except Exception:
            self.failed = True
            raise ManualReviewCheckpointFailure() from None

    async def aput(self, *args, **kwargs):
        try:
            return await self._saver.aput(*args, **kwargs)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        except ManualReviewCheckpointFailure:
            raise
        except Exception:
            self.failed = True
            raise ManualReviewCheckpointFailure() from None

    async def aput_writes(self, *args, **kwargs):
        try:
            return await self._saver.aput_writes(*args, **kwargs)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        except ManualReviewCheckpointFailure:
            raise
        except Exception:
            self.failed = True
            raise ManualReviewCheckpointFailure() from None


def _safe_state(
    *,
    workflow_run_id: str,
    manual_review_run_id: str | None = None,
    proposal_id: str | None = None,
    revision_id: str | None = None,
    review_id: str | None = None,
    review_passed: bool | None = None,
    review_quality_status: WorkflowQuality | None = None,
    error_code: str | None = None,
    next_node: ManualReviewWorkflowState["next_node"],
) -> ManualReviewWorkflowState:
    state: ManualReviewWorkflowState = {
        "workflow_run_id": workflow_run_id,
        "review_id": review_id,
        "review_passed": review_passed,
        "review_quality_status": (
            review_quality_status.value if review_quality_status is not None else None
        ),
        "error_code": error_code,
        "next_node": next_node,
    }
    if manual_review_run_id is not None:
        state["manual_review_run_id"] = manual_review_run_id
    if proposal_id is not None:
        state["proposal_id"] = proposal_id
    if revision_id is not None:
        state["revision_id"] = revision_id
    return state


def _context_state(
    context: OwnedManualReviewContext, *, next_node: ManualReviewWorkflowState["next_node"]
) -> ManualReviewWorkflowState:
    return _safe_state(
        workflow_run_id=context.workflow_run_id,
        manual_review_run_id=context.manual_review_run_id,
        proposal_id=context.proposal_id,
        revision_id=context.proposal_revision_id,
        review_id=context.current_review_id,
        next_node=next_node,
    )


async def _context_or_stop(
    session: AsyncSession, workflow_run_id: str, lease_owner: str
) -> OwnedManualReviewContext | None:
    loaded = await load_owned_manual_review_context(
        session, workflow_run_id=workflow_run_id, lease_owner=lease_owner
    )
    if loaded.disposition == "lease_lost":
        raise ManualReviewLeaseLost()
    if loaded.error_code == "MANUAL_REVIEW_DATABASE_ERROR":
        raise SQLAlchemyError()
    return loaded.context


def _dependency_trusted(
    context: OwnedManualReviewContext, error_code: KnowledgeLoadErrorCode
) -> TrustedOptimizationInput:
    return TrustedOptimizationInput(
        store_id=context.store_id,
        product_id=context.product_id,
        base_product_version=context.base_product_version,
        title=context.title,
        category=context.category,
        brand=context.brand,
        selling_points=list(context.selling_points),
        description=context.description,
        search_keywords=list(context.search_keywords),
        attributes=dict(context.attributes),
        skus=list(context.skus),
        candidate_metrics=context.candidate_metrics,
        candidate_evidence=list(context.candidate_evidence),
        rag_quality=(
            "zero_hit" if error_code == "KNOWLEDGE_ZERO_HIT"
            else "low_confidence" if error_code == "KNOWLEDGE_LOW_CONFIDENCE"
            else "normal"
        ),
        canonical_rule_citations=list(context.canonical_citations),
    )


def _same_context(left: OwnedManualReviewContext, right: OwnedManualReviewContext) -> bool:
    return (
        left.manual_review_run_id,
        left.proposal_id,
        left.proposal_revision_id,
        left.base_product_version,
        left.proposal_output,
        left.canonical_citations,
        left.current_review_id,
    ) == (
        right.manual_review_run_id,
        right.proposal_id,
        right.proposal_revision_id,
        right.base_product_version,
        right.proposal_output,
        right.canonical_citations,
        right.current_review_id,
    )


async def _stored_calls_valid(
    session: AsyncSession, context: OwnedManualReviewContext, model: str
) -> bool:
    review = await session.get(ComplianceReview, context.current_review_id)
    calls = list(
        await session.scalars(
            select(AgentCall).where(AgentCall.workflow_run_id == context.workflow_run_id)
        )
    )
    no_call_errors = {
        "DEEPSEEK_KEY_MISSING",
        "KNOWLEDGE_MODEL_UNAVAILABLE",
        "KNOWLEDGE_DEPENDENCY_TIMEOUT",
        "KNOWLEDGE_DEPENDENCY_ERROR",
        "KNOWLEDGE_ZERO_HIT",
        "KNOWLEDGE_LOW_CONFIDENCE",
    }
    if review is None:
        return False
    if not calls:
        return review.error_code in no_call_errors
    if len(calls) not in {1, 2}:
        return False
    calls.sort(key=lambda call: 0 if call.call_type is AgentCallType.PRIMARY else 1)
    expected = (
        ("call_product_compliance_agent", AgentCallType.PRIMARY),
        ("repair_product_compliance_schema", AgentCallType.SCHEMA_REPAIR),
    )
    if any(
        call.node_name != node_name
        or call.call_type is not call_type
        or call.iteration != 0
        or call.attempt != 1
        or call.model != model
        or call.prompt_version != COMPLIANCE_PROMPT_VERSION
        or call.status != ("succeeded" if call.error_code is None else "failed")
        for call, (node_name, call_type) in zip(calls, expected)
    ):
        return False
    if len(calls) == 1 and calls[0].error_code == "DEEPSEEK_SCHEMA_INVALID":
        return False
    if len(calls) == 2 and calls[0].error_code != "DEEPSEEK_SCHEMA_INVALID":
        return False
    final_error = calls[-1].error_code
    return (
        final_error == review.error_code
        if final_error is not None
        else review.error_code in {None, "COMPLIANCE_AGENT_DEGRADED"}
    )


def build_manual_review_graph(
    *,
    session: AsyncSession,
    settings: Settings,
    lease_owner: str,
    checkpointer: BaseCheckpointSaver,
    trusted_input_loader: TrustedInputLoader,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CompiledStateGraph:
    boundary = _CheckpointBoundary(checkpointer)
    trusted_inputs: dict[str, TrustedOptimizationInput] = {}
    deterministic_results: dict[str, DeterministicComplianceResult] = {}
    dependency_errors: dict[str, KnowledgeLoadErrorCode] = {}
    invocations: dict[
        str, tuple[ComplianceAgentResponse | None, tuple[ComplianceAgentCallRecord, ...], str | None]
    ] = {}

    def check_checkpoint() -> None:
        if boundary.cancelled:
            raise asyncio.CancelledError()
        if boundary.failed:
            raise ManualReviewCheckpointFailure()

    async def renew_before_rag(workflow_run_id: str) -> None:
        if not await renew_manual_review_lease(
            session,
            workflow_run_id=workflow_run_id,
            lease_owner=lease_owner,
            lease_seconds=settings.optimization_lease_seconds,
        ):
            raise ManualReviewLeaseLost()

    def renew_before_deepseek(workflow_run_id: str):
        async def renew(_attempt: int) -> bool:
            return await renew_manual_review_lease(
                session,
                workflow_run_id=workflow_run_id,
                lease_owner=lease_owner,
                lease_seconds=settings.optimization_lease_seconds,
            )

        return renew

    async def load_or_resume(state: ManualReviewWorkflowState) -> ManualReviewWorkflowState:
        check_checkpoint()
        workflow_run_id = state["workflow_run_id"]
        context = await _context_or_stop(session, workflow_run_id, lease_owner)
        if context is None:
            return _safe_state(workflow_run_id=workflow_run_id, next_node="stop")
        return _context_state(
            context,
            next_node="finalize_manual_review" if context.current_review_id else "load_trusted_input",
        )

    async def load_trusted_input(state: ManualReviewWorkflowState) -> ManualReviewWorkflowState:
        check_checkpoint()
        workflow_run_id = state["workflow_run_id"]
        context = await _context_or_stop(session, workflow_run_id, lease_owner)
        if context is None:
            return _safe_state(workflow_run_id=workflow_run_id, next_node="stop")
        if context.current_review_id:
            return _context_state(context, next_node="finalize_manual_review")
        try:
            trusted = await trusted_input_loader(
                context, lambda: renew_before_rag(workflow_run_id)
            )
        except TrustedInputLoadFailure as error:
            trusted_inputs[workflow_run_id] = _dependency_trusted(context, error.error_code)
            dependency_errors[workflow_run_id] = error.error_code
            return _context_state(context, next_node="run_deterministic_checks")
        refreshed = await _context_or_stop(session, workflow_run_id, lease_owner)
        if refreshed is None:
            return _safe_state(workflow_run_id=workflow_run_id, next_node="stop")
        if not _same_context(context, refreshed):
            return _context_state(refreshed, next_node="load_or_resume")
        trusted_inputs[workflow_run_id] = trusted
        return _context_state(refreshed, next_node="run_deterministic_checks")

    async def run_deterministic_checks(state: ManualReviewWorkflowState) -> ManualReviewWorkflowState:
        check_checkpoint()
        workflow_run_id = state["workflow_run_id"]
        context = await _context_or_stop(session, workflow_run_id, lease_owner)
        trusted = trusted_inputs.get(workflow_run_id)
        if context is None:
            return _safe_state(workflow_run_id=workflow_run_id, next_node="stop")
        if context.current_review_id:
            return _context_state(context, next_node="finalize_manual_review")
        if trusted is None:
            return _context_state(context, next_node="load_trusted_input")
        deterministic_results[workflow_run_id] = validate_optimization_output(
            trusted, context.proposal_output
        )
        if workflow_run_id in dependency_errors:
            invocations[workflow_run_id] = (None, (), dependency_errors[workflow_run_id])
            return _context_state(context, next_node="persist_manual_review")
        return _context_state(context, next_node="call_compliance_agent")

    async def call_compliance_agent(state: ManualReviewWorkflowState) -> ManualReviewWorkflowState:
        check_checkpoint()
        workflow_run_id = state["workflow_run_id"]
        context = await _context_or_stop(session, workflow_run_id, lease_owner)
        trusted = trusted_inputs.get(workflow_run_id)
        deterministic = deterministic_results.get(workflow_run_id)
        if context is None:
            return _safe_state(workflow_run_id=workflow_run_id, next_node="stop")
        if context.current_review_id:
            return _context_state(context, next_node="finalize_manual_review")
        if trusted is None or deterministic is None:
            return _context_state(context, next_node="load_trusted_input")
        if trusted.rag_quality != "normal":
            invocations[workflow_run_id] = (
                None,
                (),
                "KNOWLEDGE_ZERO_HIT" if trusted.rag_quality == "zero_hit" else "KNOWLEDGE_LOW_CONFIDENCE",
            )
            return _context_state(context, next_node="persist_manual_review")
        refreshed_deterministic = validate_optimization_output(trusted, context.proposal_output)
        if refreshed_deterministic != deterministic:
            return _context_state(context, next_node="run_deterministic_checks")
        client = ProductComplianceAgentClient(settings, transport=transport)
        primary = await client.request(
            context.proposal_output,
            deterministic,
            trusted,
            call_type=AgentCallType.PRIMARY,
            iteration=0,
            before_http_attempt=renew_before_deepseek(workflow_run_id),
            max_attempts=1,
        )
        records = list(primary.records)
        if primary.error_code == "LEASE_LOST":
            raise ManualReviewLeaseLost()
        invocation = primary
        if invocation.response is None and invocation.error_code == "DEEPSEEK_SCHEMA_INVALID":
            repair = await client.request(
                context.proposal_output,
                deterministic,
                trusted,
                call_type=AgentCallType.SCHEMA_REPAIR,
                iteration=0,
                before_http_attempt=renew_before_deepseek(workflow_run_id),
                max_attempts=1,
            )
            records.extend(repair.records)
            if repair.error_code == "LEASE_LOST":
                raise ManualReviewLeaseLost()
            invocation = repair
        invocations[workflow_run_id] = (
            invocation.response,
            tuple(records),
            invocation.error_code,
        )
        persisted = await persist_manual_compliance_review(
            session,
            workflow_run_id=workflow_run_id,
            lease_owner=lease_owner,
            trusted=trusted,
            deterministic=deterministic,
            response=invocation.response,
            calls=records,
            error_code=invocation.error_code,
        )
        if persisted.disposition == "lease_lost":
            raise ManualReviewLeaseLost()
        if persisted.disposition == "failed":
            return _safe_state(
                workflow_run_id=workflow_run_id,
                manual_review_run_id=context.manual_review_run_id,
                proposal_id=context.proposal_id,
                revision_id=context.proposal_revision_id,
                error_code=persisted.error_code,
                next_node="stop",
            )
        return _safe_state(
            workflow_run_id=workflow_run_id,
            manual_review_run_id=context.manual_review_run_id,
            proposal_id=context.proposal_id,
            revision_id=context.proposal_revision_id,
            review_id=persisted.review_id,
            review_passed=persisted.passed,
            review_quality_status=persisted.quality_status,
            error_code=persisted.error_code,
            next_node="persist_manual_review",
        )

    async def persist_review(state: ManualReviewWorkflowState) -> ManualReviewWorkflowState:
        check_checkpoint()
        workflow_run_id = state["workflow_run_id"]
        context = await _context_or_stop(session, workflow_run_id, lease_owner)
        trusted = trusted_inputs.get(workflow_run_id)
        deterministic = deterministic_results.get(workflow_run_id)
        invocation = invocations.get(workflow_run_id)
        if context is None:
            return _safe_state(workflow_run_id=workflow_run_id, next_node="stop")
        if context.current_review_id:
            return _context_state(context, next_node="finalize_manual_review")
        if trusted is None or deterministic is None or invocation is None:
            return _context_state(context, next_node="load_trusted_input")
        response, calls, error_code = invocation
        persisted = await persist_manual_compliance_review(
            session,
            workflow_run_id=workflow_run_id,
            lease_owner=lease_owner,
            trusted=trusted,
            deterministic=deterministic,
            response=response,
            calls=calls,
            error_code=error_code,
        )
        if persisted.disposition == "lease_lost":
            raise ManualReviewLeaseLost()
        if persisted.disposition == "failed":
            return _safe_state(
                workflow_run_id=workflow_run_id,
                manual_review_run_id=context.manual_review_run_id,
                proposal_id=context.proposal_id,
                revision_id=context.proposal_revision_id,
                error_code=persisted.error_code,
                next_node="stop",
            )
        return _safe_state(
            workflow_run_id=workflow_run_id,
            manual_review_run_id=context.manual_review_run_id,
            proposal_id=context.proposal_id,
            revision_id=context.proposal_revision_id,
            review_id=persisted.review_id,
            review_passed=persisted.passed,
            review_quality_status=persisted.quality_status,
            error_code=persisted.error_code,
            next_node="finalize_manual_review",
        )

    async def finalize(state: ManualReviewWorkflowState) -> ManualReviewWorkflowState:
        check_checkpoint()
        workflow_run_id = state["workflow_run_id"]
        context = await _context_or_stop(session, workflow_run_id, lease_owner)
        if context is None:
            return _safe_state(workflow_run_id=workflow_run_id, next_node="stop")
        if context.current_review_id and not await _stored_calls_valid(
            session, context, settings.deepseek_model
        ):
            result = await fail_manual_review_run(
                session,
                workflow_run_id=workflow_run_id,
                lease_owner=lease_owner,
                error_code="MANUAL_REVIEW_REPLAY_CONFLICT",
            )
            if result.disposition == "lease_lost":
                raise ManualReviewLeaseLost()
            return _safe_state(
                workflow_run_id=workflow_run_id,
                manual_review_run_id=context.manual_review_run_id,
                proposal_id=context.proposal_id,
                revision_id=context.proposal_revision_id,
                review_id=context.current_review_id,
                error_code=result.error_code,
                next_node="stop",
            )
        result = await finalize_manual_review(
            session, workflow_run_id=workflow_run_id, lease_owner=lease_owner
        )
        if result.disposition == "lease_lost":
            raise ManualReviewLeaseLost()
        return _safe_state(
            workflow_run_id=workflow_run_id,
            manual_review_run_id=context.manual_review_run_id,
            proposal_id=context.proposal_id,
            revision_id=context.proposal_revision_id,
            review_id=state.get("review_id", context.current_review_id),
            review_passed=state.get("review_passed"),
            review_quality_status=(
                WorkflowQuality(state["review_quality_status"])
                if state.get("review_quality_status") is not None
                else None
            ),
            error_code=result.error_code,
            next_node="stop",
        )

    def route(state: ManualReviewWorkflowState) -> str:
        return state.get("next_node", "stop")

    graph = StateGraph(ManualReviewWorkflowState)
    graph.add_node("load_or_resume", load_or_resume)
    graph.add_node("load_trusted_input", load_trusted_input)
    graph.add_node("run_deterministic_checks", run_deterministic_checks)
    graph.add_node("call_compliance_agent", call_compliance_agent)
    graph.add_node("persist_manual_review", persist_review)
    graph.add_node("finalize_manual_review", finalize)
    graph.add_edge(START, "load_or_resume")
    graph.add_conditional_edges(
        "load_or_resume", route,
        {"load_trusted_input": "load_trusted_input", "finalize_manual_review": "finalize_manual_review", "stop": END},
    )
    graph.add_conditional_edges(
        "load_trusted_input", route,
        {"load_or_resume": "load_or_resume", "run_deterministic_checks": "run_deterministic_checks", "finalize_manual_review": "finalize_manual_review", "stop": END},
    )
    graph.add_conditional_edges(
        "run_deterministic_checks", route,
        {"load_trusted_input": "load_trusted_input", "call_compliance_agent": "call_compliance_agent", "persist_manual_review": "persist_manual_review", "finalize_manual_review": "finalize_manual_review", "stop": END},
    )
    graph.add_conditional_edges(
        "call_compliance_agent", route,
        {"load_trusted_input": "load_trusted_input", "run_deterministic_checks": "run_deterministic_checks", "persist_manual_review": "persist_manual_review", "finalize_manual_review": "finalize_manual_review", "stop": END},
    )
    graph.add_conditional_edges(
        "persist_manual_review", route,
        {"load_trusted_input": "load_trusted_input", "finalize_manual_review": "finalize_manual_review", "stop": END},
    )
    graph.add_conditional_edges("finalize_manual_review", route, {"stop": END})
    return graph.compile(checkpointer=boundary)


async def _fail_current_run(
    session_factory: async_sessionmaker[AsyncSession],
    workflow_run_id: str,
    lease_owner: str,
    error_code: Literal["MANUAL_REVIEW_DATABASE_ERROR", "MANUAL_REVIEW_CHECKPOINT_ERROR"],
) -> None:
    async with session_factory() as session:
        await fail_manual_review_run(
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
) -> str | None:
    workflow_run_id: str | None = None
    failure_code: Literal["MANUAL_REVIEW_DATABASE_ERROR", "MANUAL_REVIEW_CHECKPOINT_ERROR"] | None = None
    async with session_factory() as session:
        claim = await claim_next_manual_review_run(
            session,
            lease_owner=lease_owner,
            lease_seconds=settings.optimization_lease_seconds,
        )
        if claim is None:
            return None
        workflow_run_id = claim.workflow_run_id
        try:
            graph = build_manual_review_graph(
                session=session,
                settings=settings,
                lease_owner=lease_owner,
                checkpointer=checkpointer,
                trusted_input_loader=trusted_input_loader,
                transport=transport,
            )
            await graph.ainvoke(
                {"workflow_run_id": workflow_run_id},
                config={"configurable": {"thread_id": workflow_run_id}},
            )
        except ManualReviewLeaseLost:
            return workflow_run_id
        except ManualReviewCheckpointFailure:
            await session.rollback()
            failure_code = "MANUAL_REVIEW_CHECKPOINT_ERROR"
        except SQLAlchemyError:
            await session.rollback()
            failure_code = "MANUAL_REVIEW_DATABASE_ERROR"
    if failure_code is not None:
        assert workflow_run_id is not None
        await _fail_current_run(session_factory, workflow_run_id, lease_owner, failure_code)
    return workflow_run_id
