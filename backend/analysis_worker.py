from dataclasses import asdict
from datetime import date
from decimal import Decimal
from typing import TypedDict

import httpx
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.analysis_agent import (
    PROMPT_VERSION,
    AgentCallRecord,
    AgentSchemaError,
    DeepSeekAnalysisClient,
    _input_hash,
    build_degraded_drafts,
    collect_analysis_facts,
    validate_agent_response,
)
from backend.analysis_runs import (
    claim_next_analysis_run,
    fail_analysis_run,
    persist_analysis_completion,
    renew_analysis_lease,
    update_analysis_step,
)
from backend.common import AgentCallType, WorkflowQuality, WorkflowStatus
from backend.config import Settings
from backend.models import WorkflowRun
from backend.schemas import (
    AgentAnalysisResponse,
    AgentCandidateDraft,
    AnalysisCandidateView,
    AnalysisFacts,
)


class LeaseLostError(RuntimeError):
    pass


class CheckpointFailure(RuntimeError):
    pass


class _WorkflowFailure(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class AnalysisWorkflowState(TypedDict, total=False):
    workflow_run_id: str
    store_id: str
    start_date: str
    end_date: str
    facts: dict[str, object]
    drafts: list[dict[str, object]]
    candidates: list[dict[str, object]]
    call_records: list[dict[str, object]]
    quality_status: str
    error_code: str | None


def _record_state(record: AgentCallRecord) -> dict[str, object]:
    value = asdict(record)
    value["call_type"] = record.call_type.value
    value["estimated_cost"] = (
        str(record.estimated_cost) if record.estimated_cost is not None else None
    )
    return value


def _record_from_state(value: dict[str, object]) -> AgentCallRecord:
    estimated_cost = value.get("estimated_cost")
    return AgentCallRecord(
        node_name=str(value["node_name"]),
        call_type=AgentCallType(str(value["call_type"])),
        attempt=int(value["attempt"]),
        model=str(value["model"]),
        prompt_version=str(value["prompt_version"]),
        status=str(value["status"]),
        input_hash=str(value["input_hash"]),
        prompt_tokens=int(value["prompt_tokens"]),
        completion_tokens=int(value["completion_tokens"]),
        total_tokens=int(value["total_tokens"]),
        duration_ms=int(value["duration_ms"]),
        estimated_cost=Decimal(str(estimated_cost)) if estimated_cost is not None else None,
        error_code=str(value["error_code"]) if value.get("error_code") is not None else None,
    )


async def _require_step(
    session: AsyncSession, workflow_run_id: str, lease_owner: str, current_step: str
) -> None:
    try:
        updated = await update_analysis_step(
            session,
            workflow_run_id=workflow_run_id,
            lease_owner=lease_owner,
            current_step=current_step,
        )
    except SQLAlchemyError as error:
        raise _WorkflowFailure("DATABASE_ERROR") from error
    if not updated:
        raise LeaseLostError()


async def _renew_or_lose(
    session: AsyncSession,
    workflow_run_id: str,
    lease_owner: str,
    lease_seconds: int,
) -> bool:
    try:
        return await renew_analysis_lease(
            session,
            workflow_run_id=workflow_run_id,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
        )
    except SQLAlchemyError as error:
        raise _WorkflowFailure("DATABASE_ERROR") from error


def _degradation_record(facts: AnalysisFacts, settings: Settings, error_code: str | None) -> AgentCallRecord:
    return AgentCallRecord(
        node_name="validate_and_reconcile",
        call_type=AgentCallType.PRIMARY,
        attempt=0,
        model=settings.deepseek_model,
        prompt_version=PROMPT_VERSION,
        status="degraded",
        input_hash=_input_hash(facts),
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        duration_ms=0,
        estimated_cost=None,
        error_code=error_code,
    )


def _merge_candidates(
    facts: AnalysisFacts, drafts: list[AgentCandidateDraft]
) -> list[AnalysisCandidateView]:
    trusted = {candidate.product_id: candidate for candidate in facts.candidates}
    return [
        AnalysisCandidateView(
            product_id=draft.product_id,
            rank=draft.rank,
            product_code=trusted[draft.product_id].product_code,
            anomaly_types=trusted[draft.product_id].anomaly_types,
            metrics=trusted[draft.product_id].metrics,
            business_impact=trusted[draft.product_id].business_impact,
            evidence=trusted[draft.product_id].evidence,
            impact_explanation=draft.impact_explanation,
            reason=draft.reason,
            recommended_action=draft.recommended_action,
            confidence=draft.confidence,
        )
        for draft in drafts
    ]


def build_analysis_graph(
    *,
    session: AsyncSession,
    settings: Settings,
    lease_owner: str,
    checkpointer: BaseCheckpointSaver,
    transport: httpx.AsyncBaseTransport | None = None,
):
    client = DeepSeekAnalysisClient(settings, transport=transport)

    async def load_run(state: AnalysisWorkflowState) -> AnalysisWorkflowState:
        workflow_run_id = state["workflow_run_id"]
        try:
            run = await session.scalar(
                select(WorkflowRun).where(
                    WorkflowRun.id == workflow_run_id,
                    WorkflowRun.status == WorkflowStatus.PROCESSING,
                    WorkflowRun.lease_owner == lease_owner,
                )
            )
        except SQLAlchemyError as error:
            raise _WorkflowFailure("DATABASE_ERROR") from error
        if run is None:
            raise LeaseLostError()
        try:
            values = run.input
            store_id = str(values["store_id"])
            start_date = date.fromisoformat(str(values["start_date"]))
            end_date = date.fromisoformat(str(values["end_date"]))
            if (
                run.workflow_type != "analysis"
                or store_id != run.store_id
                or start_date != run.start_date
                or end_date != run.end_date
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise _WorkflowFailure("INPUT_ERROR") from None
        await _require_step(session, workflow_run_id, lease_owner, "load_run")
        return {
            "store_id": store_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }

    async def gather_facts(state: AnalysisWorkflowState) -> AnalysisWorkflowState:
        await _require_step(session, state["workflow_run_id"], lease_owner, "collect_facts")
        try:
            facts = await collect_analysis_facts(
                session,
                state["store_id"],
                date.fromisoformat(state["start_date"]),
                date.fromisoformat(state["end_date"]),
            )
            if not facts.candidates:
                raise ValueError
        except (SQLAlchemyError, ValueError, TypeError):
            raise _WorkflowFailure("FACT_COLLECTION_ERROR") from None
        return {"facts": facts.model_dump(mode="json")}

    def before_http_attempt(workflow_run_id: str):
        async def renew(_call_type: AgentCallType, _attempt: int) -> bool:
            return await _renew_or_lose(
                session,
                workflow_run_id=workflow_run_id,
                lease_owner=lease_owner,
                lease_seconds=settings.analysis_lease_seconds,
            )

        return renew

    async def call_agent(state: AnalysisWorkflowState) -> AnalysisWorkflowState:
        workflow_run_id = state["workflow_run_id"]
        await _require_step(session, workflow_run_id, lease_owner, "call_analysis_agent")
        facts = AnalysisFacts.model_validate(state["facts"])
        invocation = await client.request(
            facts,
            call_type=AgentCallType.PRIMARY,
            before_http_attempt=before_http_attempt(workflow_run_id),
        )
        if invocation.error_code == "LEASE_LOST":
            raise LeaseLostError()
        return {
            "drafts": (
                [draft.model_dump(mode="json") for draft in invocation.response.candidates]
                if invocation.response is not None
                else []
            ),
            "call_records": [_record_state(record) for record in invocation.records],
            "error_code": invocation.error_code,
        }

    async def validate_and_reconcile(state: AnalysisWorkflowState) -> AnalysisWorkflowState:
        workflow_run_id = state["workflow_run_id"]
        await _require_step(session, workflow_run_id, lease_owner, "validate_and_reconcile")
        facts = AnalysisFacts.model_validate(state["facts"])
        records = [_record_from_state(record) for record in state.get("call_records", [])]
        error_code = state.get("error_code")
        drafts: list[AgentCandidateDraft] | None = None
        if error_code is None:
            try:
                response = AgentAnalysisResponse(candidates=state["drafts"])
                drafts = validate_agent_response(facts, response)
            except (AgentSchemaError, ValueError):
                error_code = "DEEPSEEK_SCHEMA_INVALID"

        if error_code == "DEEPSEEK_SCHEMA_INVALID":
            repair = await client.request(
                facts,
                call_type=AgentCallType.SCHEMA_REPAIR,
                before_http_attempt=before_http_attempt(workflow_run_id),
            )
            if repair.error_code == "LEASE_LOST":
                raise LeaseLostError()
            records.extend(repair.records)
            error_code = repair.error_code
            if repair.response is not None:
                try:
                    drafts = validate_agent_response(facts, repair.response)
                    error_code = None
                except AgentSchemaError:
                    error_code = "DEEPSEEK_SCHEMA_INVALID"

        if drafts is None:
            drafts = build_degraded_drafts(facts)
            records.append(_degradation_record(facts, settings, error_code))
            quality_status = WorkflowQuality.DEGRADED
        else:
            quality_status = WorkflowQuality.NORMAL
        candidates = _merge_candidates(facts, drafts)
        return {
            "drafts": [draft.model_dump(mode="json") for draft in drafts],
            "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
            "call_records": [_record_state(record) for record in records],
            "quality_status": quality_status.value,
            "error_code": error_code,
        }

    async def persist_results(state: AnalysisWorkflowState) -> AnalysisWorkflowState:
        workflow_run_id = state["workflow_run_id"]
        if not await _renew_or_lose(
            session,
            workflow_run_id,
            lease_owner,
            settings.analysis_lease_seconds,
        ):
            raise LeaseLostError()
        try:
            persisted = await persist_analysis_completion(
                session,
                workflow_run_id=workflow_run_id,
                lease_owner=lease_owner,
                candidates=[
                    AnalysisCandidateView.model_validate(candidate)
                    for candidate in state["candidates"]
                ],
                calls=[_record_from_state(record) for record in state["call_records"]],
                quality_status=WorkflowQuality(state["quality_status"]),
                quality={
                    "status": state["quality_status"],
                    "error_code": state.get("error_code"),
                },
            )
        except SQLAlchemyError as error:
            raise _WorkflowFailure("DATABASE_ERROR") from error
        if not persisted:
            raise LeaseLostError()
        return {}

    graph = StateGraph(AnalysisWorkflowState)
    graph.add_node("load_run", load_run)
    graph.add_node("collect_facts", gather_facts)
    graph.add_node("call_analysis_agent", call_agent)
    graph.add_node("validate_and_reconcile", validate_and_reconcile)
    graph.add_node("persist_results", persist_results)
    graph.add_edge(START, "load_run")
    graph.add_edge("load_run", "collect_facts")
    graph.add_edge("collect_facts", "call_analysis_agent")
    graph.add_edge("call_analysis_agent", "validate_and_reconcile")
    graph.add_edge("validate_and_reconcile", "persist_results")
    graph.add_edge("persist_results", END)
    return graph.compile(checkpointer=checkpointer)


async def _fail_current_run(
    session_factory: async_sessionmaker[AsyncSession],
    workflow_run_id: str,
    lease_owner: str,
    error_code: str,
) -> None:
    async with session_factory() as session:
        await fail_analysis_run(
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
    transport: httpx.AsyncBaseTransport | None = None,
) -> str | None:
    async with session_factory() as session:
        run = await claim_next_analysis_run(
            session, lease_owner=lease_owner, lease_seconds=settings.analysis_lease_seconds
        )
        if run is None:
            return None
        try:
            graph = build_analysis_graph(
                session=session,
                settings=settings,
                lease_owner=lease_owner,
                checkpointer=checkpointer,
                transport=transport,
            )
            await graph.ainvoke(
                {"workflow_run_id": run.id},
                config={"configurable": {"thread_id": run.id}},
            )
        except LeaseLostError:
            return run.id
        except _WorkflowFailure as error:
            await _fail_current_run(session_factory, run.id, lease_owner, error.error_code)
        except CheckpointFailure:
            await _fail_current_run(session_factory, run.id, lease_owner, "CHECKPOINT_ERROR")
        except Exception:
            await _fail_current_run(session_factory, run.id, lease_owner, "CHECKPOINT_ERROR")
        return run.id
