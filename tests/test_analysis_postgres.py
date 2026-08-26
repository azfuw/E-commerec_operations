import asyncio
import json
import os
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import delete, func, select, text, update

import backend.analysis_agent as analysis_agent
import backend.analysis_worker as analysis_worker
from backend.analysis_agent import AgentCallRecord, collect_analysis_facts
from backend.analysis_runs import (
    claim_next_analysis_run,
    create_analysis_run,
    finalize_analysis_run,
    persist_analysis_completion,
)
from backend.common import AgentCallType, WorkflowQuality, WorkflowStatus
from backend.config import Settings
from backend.database import async_session_factory
from backend.models import AgentCall, AnalysisCandidate, Store, User, WorkflowRun
from backend.schemas import AnalysisCandidateView
from backend.seed import seed_demo_data


if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


START_DATE = date(2026, 7, 26)
END_DATE = date(2026, 8, 24)
PROHIBITED_AUDIT_COLUMNS = {
    "api_key",
    "authorization",
    "prompt",
    "raw_response",
    "chain_of_thought",
}


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        jwt_secret_key="task-five-postgres-local-jwt-secret-at-least-32",
        deepseek_api_key="test-only-transport-token",
        deepseek_base_url="https://mock.deepseek.invalid",
        analysis_lease_seconds=60,
    )


def _completion(request: httpx.Request) -> httpx.Response:
    facts = json.loads(json.loads(request.content)["messages"][1]["content"])["facts"]
    candidates = [
        {
            "product_id": candidate["product_id"],
            "rank": rank,
            "impact_explanation": f"影响说明 {rank}",
            "reason": f"原因 {rank}",
            "recommended_action": f"建议 {rank}",
            "confidence": "0.8000",
        }
        for rank, candidate in enumerate(facts["candidates"], start=1)
    ]
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": json.dumps({"candidates": candidates})}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        },
    )


class _FailingSaver(BaseCheckpointSaver):
    def __init__(self, saver: AsyncPostgresSaver, phase: str) -> None:
        super().__init__(serde=saver.serde)
        self._saver = saver
        self._phase = phase

    @property
    def config_specs(self):
        return self._saver.config_specs

    def get_next_version(self, current, channel):
        return self._saver.get_next_version(current, channel)

    async def aget_tuple(self, *args, **kwargs):
        if self._phase == "read":
            raise analysis_worker.CheckpointFailure()
        return await self._saver.aget_tuple(*args, **kwargs)

    async def aput(self, *args, **kwargs):
        if self._phase == "save":
            raise analysis_worker.CheckpointFailure()
        return await self._saver.aput(*args, **kwargs)

    async def aput_writes(self, *args, **kwargs):
        if self._phase == "consistency":
            raise analysis_worker.CheckpointFailure()
        return await self._saver.aput_writes(*args, **kwargs)


@pytest.mark.postgres_integration
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="explicit PostgreSQL integration opt-in required",
)
async def test_postgres_analysis_vertical_slice(monkeypatch) -> None:
    settings = _settings()
    workflow_ids: list[str] = []

    async def create_run(store_id: str, user_id: str) -> str:
        async with async_session_factory() as session:
            run = await create_analysis_run(
                session,
                store_id=store_id,
                created_by=user_id,
                start_date=START_DATE,
                end_date=END_DATE,
            )
            workflow_ids.append(run.id)
            await session.commit()
            return run.id

    async def read_run(workflow_run_id: str) -> WorkflowRun:
        async with async_session_factory() as session:
            run = await session.get(WorkflowRun, workflow_run_id)
            assert run is not None
            return run

    async def cleanup() -> None:
        if not workflow_ids:
            return
        async with async_session_factory() as session:
            await session.execute(delete(AgentCall).where(AgentCall.workflow_run_id.in_(workflow_ids)))
            await session.execute(
                delete(AnalysisCandidate).where(AnalysisCandidate.workflow_run_id.in_(workflow_ids))
            )
            await session.execute(delete(WorkflowRun).where(WorkflowRun.id.in_(workflow_ids)))
            await session.commit()

    try:
        async with async_session_factory() as session:
            await seed_demo_data(session)
            store_id = await session.scalar(select(Store.id).where(Store.code == "flagship"))
            user_id = await session.scalar(select(User.id).where(User.username == "operator"))
            assert store_id is not None and user_id is not None
            await session.commit()

        first_id = await create_run(store_id, user_id)
        second_id = await create_run(store_id, user_id)
        async with async_session_factory() as session_a, async_session_factory() as session_b:
            first_claim, second_claim = await asyncio.gather(
                claim_next_analysis_run(session_a, lease_owner="pg-worker-a", lease_seconds=60),
                claim_next_analysis_run(session_b, lease_owner="pg-worker-b", lease_seconds=60),
            )
        assert {run.id for run in (first_claim, second_claim) if run is not None} == {
            first_id,
            second_id,
        }

        recovery_id = await create_run(store_id, user_id)
        exhausted_id = await create_run(store_id, user_id)
        awaiting_id = await create_run(store_id, user_id)
        failed_id = await create_run(store_id, user_id)
        async with async_session_factory() as session:
            await session.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == recovery_id)
                .values(
                    status=WorkflowStatus.PROCESSING,
                    lease_owner="expired-owner",
                    lease_expires_at=func.now() - text("interval '1 second'"),
                    attempt_count=2,
                )
            )
            await session.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == exhausted_id)
                .values(
                    status=WorkflowStatus.PROCESSING,
                    lease_owner="exhausted-owner",
                    lease_expires_at=func.now() - text("interval '1 second'"),
                    attempt_count=3,
                )
            )
            await session.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == awaiting_id)
                .values(status=WorkflowStatus.AWAITING_SELECTION)
            )
            await session.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == failed_id)
                .values(status=WorkflowStatus.FAILED, error_code="CHECKPOINT_ERROR")
            )
            await session.commit()

        async with async_session_factory() as session:
            recovered = await claim_next_analysis_run(
                session, lease_owner="recovery-owner", lease_seconds=60
            )
            assert recovered is not None and recovered.id == recovery_id
            assert recovered.attempt_count == 3
            assert not await finalize_analysis_run(
                session,
                workflow_run_id=recovery_id,
                lease_owner="expired-owner",
                candidate_count=0,
                quality_status=WorkflowQuality.NORMAL,
                quality={},
            )
            await session.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id.in_([first_id, second_id, recovery_id]))
                .values(status=WorkflowStatus.AWAITING_SELECTION, lease_owner=None, lease_expires_at=None)
            )
            await session.commit()

        async with async_session_factory() as session:
            assert await claim_next_analysis_run(session, lease_owner="terminal-worker", lease_seconds=60) is None
        exhausted = await read_run(exhausted_id)
        assert (exhausted.status, exhausted.error_code, exhausted.lease_owner) == (
            WorkflowStatus.FAILED,
            "LEASE_ATTEMPTS_EXHAUSTED",
            None,
        )
        assert (await read_run(awaiting_id)).status is WorkflowStatus.AWAITING_SELECTION
        assert (await read_run(failed_id)).status is WorkflowStatus.FAILED

        async with AsyncPostgresSaver.from_conn_string(settings.langgraph_database_url) as checkpointer:
            await checkpointer.setup()
            renew_events: list[str] = []
            original_renew = analysis_worker.renew_analysis_lease

            async def record_renew(session, *, workflow_run_id, lease_owner, lease_seconds):
                renew_events.append("renew")
                return await original_renew(
                    session,
                    workflow_run_id=workflow_run_id,
                    lease_owner=lease_owner,
                    lease_seconds=lease_seconds,
                )

            monkeypatch.setattr(analysis_worker, "renew_analysis_lease", record_renew)
            retry_statuses = [429, 429, 200]

            async def retry_transport(_request: httpx.Request) -> httpx.Response:
                renew_events.append("post")
                status = retry_statuses.pop(0)
                return _completion(_request) if status == 200 else httpx.Response(status)

            retry_id = await create_run(store_id, user_id)
            assert await analysis_worker.run_once(
                async_session_factory,
                settings=settings,
                lease_owner="retry-worker",
                checkpointer=checkpointer,
                transport=httpx.MockTransport(retry_transport),
            ) == retry_id
            assert renew_events[:6] == ["renew", "post", "renew", "post", "renew", "post"]
            assert (await read_run(retry_id)).status is WorkflowStatus.AWAITING_SELECTION

            renew_events.clear()
            repair_calls = 0

            async def repair_transport(request: httpx.Request) -> httpx.Response:
                nonlocal repair_calls
                repair_calls += 1
                renew_events.append("post")
                if repair_calls == 1:
                    return httpx.Response(200, json={"choices": [{"message": {"content": "{"}}]})
                return _completion(request)

            repair_id = await create_run(store_id, user_id)
            assert await analysis_worker.run_once(
                async_session_factory,
                settings=settings,
                lease_owner="repair-worker",
                checkpointer=checkpointer,
                transport=httpx.MockTransport(repair_transport),
            ) == repair_id
            assert renew_events[:4] == ["renew", "post", "renew", "post"]

            posts_after_loss = 0

            async def lose_lease(session, *, workflow_run_id, lease_owner, lease_seconds):
                await session.execute(
                    update(WorkflowRun)
                    .where(WorkflowRun.id == workflow_run_id)
                    .values(lease_owner="replacement-owner")
                )
                await session.commit()
                return False

            async def no_post(_request: httpx.Request) -> httpx.Response:
                nonlocal posts_after_loss
                posts_after_loss += 1
                return httpx.Response(500)

            monkeypatch.setattr(analysis_worker, "renew_analysis_lease", lose_lease)
            lost_id = await create_run(store_id, user_id)
            assert await analysis_worker.run_once(
                async_session_factory,
                settings=settings,
                lease_owner="old-worker",
                checkpointer=checkpointer,
                transport=httpx.MockTransport(no_post),
            ) == lost_id
            lost = await read_run(lost_id)
            assert posts_after_loss == 0
            assert (lost.status, lost.lease_owner) == (
                WorkflowStatus.PROCESSING,
                "replacement-owner",
            )

            monkeypatch.setattr(analysis_worker, "renew_analysis_lease", original_renew)
            tool_spies = {}
            for name in (
                "get_store_summary",
                "find_anomalous_products",
                "get_product_metrics",
                "compare_store_products",
                "get_inventory_risk",
            ):
                spy = AsyncMock(wraps=getattr(analysis_agent, name))
                monkeypatch.setattr(analysis_agent, name, spy)
                tool_spies[name] = spy

            recovery_run_id = await create_run(store_id, user_id)
            async with async_session_factory() as session:
                claimed = await claim_next_analysis_run(
                    session, lease_owner="checkpoint-worker", lease_seconds=60
                )
                assert claimed is not None and claimed.id == recovery_run_id
                graph = analysis_worker.build_analysis_graph(
                    session=session,
                    settings=settings,
                    lease_owner="checkpoint-worker",
                    checkpointer=checkpointer,
                    transport=httpx.MockTransport(_completion),
                )
                graph.interrupt_after_nodes = ["collect_facts"]
                await graph.ainvoke(
                    {"workflow_run_id": recovery_run_id},
                    config={"configurable": {"thread_id": recovery_run_id}},
                )

            saved = await checkpointer.aget_tuple(
                {"configurable": {"thread_id": recovery_run_id}}
            )
            assert saved is not None
            checkpoint_state = json.dumps(
                saved.checkpoint["channel_values"], default=str, ensure_ascii=False
            )
            assert "facts" in checkpoint_state
            assert all(
                marker not in checkpoint_state
                for marker in ("Authorization", "Bearer ", "test-only-transport-token", "raw_response")
            )
            assert all(spy.await_count > 0 for spy in tool_spies.values())

            async with async_session_factory() as session:
                resumed = analysis_worker.build_analysis_graph(
                    session=session,
                    settings=settings,
                    lease_owner="checkpoint-worker",
                    checkpointer=checkpointer,
                    transport=httpx.MockTransport(_completion),
                )
                state = await resumed.ainvoke(
                    None,
                    config={"configurable": {"thread_id": recovery_run_id}},
                )
                assert await finalize_analysis_run(
                    session,
                    workflow_run_id=recovery_run_id,
                    lease_owner="checkpoint-worker",
                    candidate_count=len(state["candidates"]),
                    quality_status=WorkflowQuality(state["quality_status"]),
                    quality={"status": state["quality_status"], "error_code": state.get("error_code")},
                )

            recovered_run = await read_run(recovery_run_id)
            assert (recovered_run.status, recovered_run.quality_status) == (
                WorkflowStatus.AWAITING_SELECTION,
                WorkflowQuality.NORMAL,
            )
            async with async_session_factory() as session:
                expected_facts = await collect_analysis_facts(session, store_id, START_DATE, END_DATE)
                persisted_candidates = list(
                    await session.scalars(
                        select(AnalysisCandidate)
                        .where(AnalysisCandidate.workflow_run_id == recovery_run_id)
                        .order_by(AnalysisCandidate.rank)
                    )
                )
                persisted_calls = list(
                    await session.scalars(
                        select(AgentCall).where(AgentCall.workflow_run_id == recovery_run_id)
                    )
                )
                assert len(persisted_candidates) == len(expected_facts.candidates) == 5
                trusted = {candidate.product_id: candidate for candidate in expected_facts.candidates}
                for candidate in persisted_candidates:
                    fact = trusted[candidate.product_id]
                    assert (
                        candidate.product_code,
                        candidate.anomaly_types,
                        candidate.metrics,
                        candidate.business_impact,
                        candidate.evidence,
                    ) == (
                        fact.product_code,
                        fact.anomaly_types,
                        fact.metrics.model_dump(mode="json"),
                        fact.business_impact,
                        fact.evidence,
                    )
                assert {column.name for column in AgentCall.__table__.columns}.isdisjoint(
                    PROHIBITED_AUDIT_COLUMNS
                )
                candidate_count = await session.scalar(
                    select(func.count(AnalysisCandidate.id)).where(
                        AnalysisCandidate.workflow_run_id == recovery_run_id
                    )
                )
                call_count = await session.scalar(
                    select(func.count(AgentCall.id)).where(AgentCall.workflow_run_id == recovery_run_id)
                )
                replay_candidates = [
                    AnalysisCandidateView.model_validate(candidate) for candidate in persisted_candidates
                ]
                replay_calls = [
                    AgentCallRecord(
                        node_name=call.node_name,
                        call_type=call.call_type,
                        attempt=call.attempt,
                        model=call.model,
                        prompt_version=call.prompt_version,
                        status=call.status,
                        input_hash=call.input_hash,
                        prompt_tokens=call.prompt_tokens,
                        completion_tokens=call.completion_tokens,
                        total_tokens=call.total_tokens,
                        duration_ms=call.duration_ms,
                        estimated_cost=call.estimated_cost,
                        error_code=call.error_code,
                    )
                    for call in persisted_calls
                ]
                await session.execute(
                    update(WorkflowRun)
                    .where(WorkflowRun.id == recovery_run_id)
                    .values(
                        status=WorkflowStatus.PROCESSING,
                        lease_owner="replay-worker",
                        lease_expires_at=func.now() + text("interval '1 minute'"),
                    )
                )
                await session.commit()
                assert await persist_analysis_completion(
                    session,
                    workflow_run_id=recovery_run_id,
                    lease_owner="replay-worker",
                    candidates=replay_candidates,
                    calls=replay_calls,
                    quality_status=WorkflowQuality.NORMAL,
                    quality={},
                )
                assert await session.scalar(
                    select(func.count(AnalysisCandidate.id)).where(
                        AnalysisCandidate.workflow_run_id == recovery_run_id
                    )
                ) == candidate_count
                assert await session.scalar(
                    select(func.count(AgentCall.id)).where(AgentCall.workflow_run_id == recovery_run_id)
                ) == call_count

            for phase in ("read", "save", "consistency"):
                checkpoint_failure_id = await create_run(store_id, user_id)
                assert await analysis_worker.run_once(
                    async_session_factory,
                    settings=settings,
                    lease_owner=f"checkpoint-{phase}",
                    checkpointer=_FailingSaver(checkpointer, phase),
                    transport=httpx.MockTransport(_completion),
                ) == checkpoint_failure_id
                failed = await read_run(checkpoint_failure_id)
                assert (failed.status, failed.error_code) == (
                    WorkflowStatus.FAILED,
                    "CHECKPOINT_ERROR",
                )

            stale_id = await create_run(store_id, user_id)

            class _StaleReadSaver(_FailingSaver):
                async def aget_tuple(self, config):
                    async with async_session_factory() as replacement_session:
                        await replacement_session.execute(
                            update(WorkflowRun)
                            .where(WorkflowRun.id == config["configurable"]["thread_id"])
                            .values(lease_owner="replacement-checkpoint-owner")
                        )
                        await replacement_session.commit()
                    raise analysis_worker.CheckpointFailure()

            assert await analysis_worker.run_once(
                async_session_factory,
                settings=settings,
                lease_owner="stale-checkpoint-owner",
                checkpointer=_StaleReadSaver(checkpointer, "read"),
                transport=httpx.MockTransport(_completion),
            ) == stale_id
            stale = await read_run(stale_id)
            assert (stale.status, stale.lease_owner) == (
                WorkflowStatus.PROCESSING,
                "replacement-checkpoint-owner",
            )
    finally:
        await cleanup()
