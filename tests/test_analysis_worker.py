import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import backend.analysis_worker as analysis_worker
from backend.analysis_agent import AgentCallRecord, collect_analysis_facts
from backend.analysis_runs import (
    claim_next_analysis_run,
    create_analysis_run,
    fail_analysis_run,
    persist_analysis_completion,
    renew_analysis_lease,
    update_analysis_step,
)
from backend.common import AgentCallType, WorkflowQuality, WorkflowStatus
from backend.config import Settings
from backend.database import Base
from backend.models import AgentCall, AnalysisCandidate, Store, User, WorkflowRun
from backend.schemas import AnalysisCandidateView
from backend.seed import seed_demo_data


START_DATE = date(2026, 7, 26)
END_DATE = date(2026, 8, 24)


def test_worker_help_does_not_load_settings_or_include_validation_inputs(monkeypatch) -> None:
    result = subprocess.run(
        [sys.executable, "scripts/run_analysis_worker.py", "--help"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parents[1],
        env={
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"}
        },
        check=False,
    )

    assert result.returncode == 0
    assert "--once" in result.stdout
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)
    assert "input_value" not in str(error.value)


@pytest_asyncio.fixture
async def worker_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seeded_worker(worker_factory) -> dict[str, object]:
    async with worker_factory() as session:
        await seed_demo_data(session)
        store_id = await session.scalar(select(Store.id).where(Store.code == "flagship"))
        user_id = await session.scalar(select(User.id).where(User.username == "operator"))
        assert store_id is not None
        assert user_id is not None
        await session.commit()
    return {"factory": worker_factory, "store_id": store_id, "user_id": user_id}


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        jwt_secret_key="test-only-secret-at-least-32-characters",
        deepseek_api_key="mock-key",
        deepseek_base_url="https://mock.deepseek.invalid",
        analysis_lease_seconds=60,
    )


def _completion(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    facts = json.loads(body["messages"][1]["content"])["facts"]
    candidates = []
    for rank, candidate in enumerate(facts["candidates"], start=1):
        candidates.append(
            {
                "product_id": candidate["product_id"],
                "rank": rank,
                "impact_explanation": f"影响说明 {rank}",
                "reason": f"原因 {rank}",
                "recommended_action": f"建议 {rank}",
                "confidence": "0.8000",
            }
        )
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": json.dumps({"candidates": candidates})}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        },
    )


@pytest.fixture
def valid_transport() -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/chat/completions"
        return _completion(request)

    return httpx.MockTransport(handler)


async def _new_run(data: dict[str, object]) -> str:
    factory = data["factory"]
    assert isinstance(factory, async_sessionmaker)
    async with factory() as session:
        run = await create_analysis_run(
            session,
            store_id=str(data["store_id"]),
            created_by=str(data["user_id"]),
            start_date=START_DATE,
            end_date=END_DATE,
        )
        await session.commit()
        return run.id


async def _read_run(factory: async_sessionmaker[AsyncSession], run_id: str) -> WorkflowRun:
    async with factory() as session:
        run = await session.get(WorkflowRun, run_id)
        assert run is not None
        return run


async def test_run_once_processes_one_accepted_run_with_trusted_candidates(
    seeded_worker, settings, valid_transport
) -> None:
    factory = seeded_worker["factory"]
    assert isinstance(factory, async_sessionmaker)
    async with factory() as session:
        facts = await collect_analysis_facts(
            session, str(seeded_worker["store_id"]), START_DATE, END_DATE
        )
    assert len(facts.candidates) == 5
    run_id = await _new_run(seeded_worker)
    untouched_run_id = await _new_run(seeded_worker)
    async with factory() as session:
        run = await session.get(WorkflowRun, run_id)
        untouched = await session.get(WorkflowRun, untouched_run_id)
        assert run is not None and untouched is not None
        run.created_at = datetime(2026, 8, 25, tzinfo=UTC)
        untouched.created_at = datetime(2026, 8, 25, 0, 0, 1, tzinfo=UTC)
        await session.commit()

    processed_id = await analysis_worker.run_once(
        factory,
        settings=settings,
        lease_owner="worker-a",
        checkpointer=InMemorySaver(),
        transport=valid_transport,
    )

    assert processed_id == run_id
    run = await _read_run(factory, run_id)
    untouched = await _read_run(factory, untouched_run_id)
    assert (run.status, run.quality_status, run.attempt_count) == (
        WorkflowStatus.AWAITING_SELECTION,
        WorkflowQuality.NORMAL,
        1,
    )
    assert untouched.status is WorkflowStatus.ACCEPTED
    async with factory() as session:
        persisted = list(
            await session.scalars(
                select(AnalysisCandidate)
                .where(AnalysisCandidate.workflow_run_id == run_id)
                .order_by(AnalysisCandidate.rank)
            )
        )
    assert len(persisted) == 5
    trusted = {candidate.product_id: candidate for candidate in facts.candidates}
    for rank, candidate in enumerate(persisted, start=1):
        fact = trusted[candidate.product_id]
        assert (
            candidate.product_code,
            candidate.metrics,
            candidate.anomaly_types,
            candidate.business_impact,
            candidate.evidence,
        ) == (
            fact.product_code,
            fact.metrics.model_dump(mode="json"),
            fact.anomaly_types,
            fact.business_impact,
            fact.evidence,
        )
        assert (
            candidate.impact_explanation,
            candidate.reason,
            candidate.recommended_action,
            candidate.confidence,
        ) == (f"影响说明 {rank}", f"原因 {rank}", f"建议 {rank}", Decimal("0.8000"))


async def test_claim_orders_accepted_reclaims_expired_and_never_claims_terminal_or_fourth_attempt(
    seeded_worker
) -> None:
    factory = seeded_worker["factory"]
    assert isinstance(factory, async_sessionmaker)
    accepted_id = await _new_run(seeded_worker)
    expired_id = await _new_run(seeded_worker)
    exhausted_id = await _new_run(seeded_worker)
    terminal_id = await _new_run(seeded_worker)
    async with factory() as session:
        expired = await session.get(WorkflowRun, expired_id)
        exhausted = await session.get(WorkflowRun, exhausted_id)
        terminal = await session.get(WorkflowRun, terminal_id)
        accepted = await session.get(WorkflowRun, accepted_id)
        assert accepted is not None and expired is not None and exhausted is not None and terminal is not None
        for run, attempts in ((expired, 2), (exhausted, 3)):
            run.status = WorkflowStatus.PROCESSING
            run.lease_owner = "old-worker"
            run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
            run.attempt_count = attempts
        terminal.status = WorkflowStatus.AWAITING_SELECTION
        ordered_runs = (accepted, expired, exhausted, terminal)
        for seconds, run in enumerate(ordered_runs):
            run.created_at = datetime(2026, 8, 25, 1, 0, seconds, tzinfo=UTC)
        await session.commit()

    async with factory() as session:
        first = await claim_next_analysis_run(session, lease_owner="worker-a", lease_seconds=60)
        second = await claim_next_analysis_run(session, lease_owner="worker-b", lease_seconds=60)
        third = await claim_next_analysis_run(session, lease_owner="worker-c", lease_seconds=60)
    assert first is not None and first.id == accepted_id
    assert second is not None and second.id == expired_id and second.attempt_count == 3
    assert third is None
    exhausted = await _read_run(factory, exhausted_id)
    terminal = await _read_run(factory, terminal_id)
    assert (exhausted.status, exhausted.error_code, exhausted.lease_owner) == (
        WorkflowStatus.FAILED,
        "LEASE_ATTEMPTS_EXHAUSTED",
        None,
    )
    assert terminal.status is WorkflowStatus.AWAITING_SELECTION


async def test_stale_owner_cannot_renew_persist_or_fail_and_completion_is_idempotent(
    seeded_worker, settings, valid_transport
) -> None:
    factory = seeded_worker["factory"]
    assert isinstance(factory, async_sessionmaker)
    run_id = await _new_run(seeded_worker)
    await analysis_worker.run_once(
        factory,
        settings=settings,
        lease_owner="worker-a",
        checkpointer=InMemorySaver(),
        transport=valid_transport,
    )
    async with factory() as session:
        candidates = [
            AnalysisCandidateView.model_validate(candidate)
            for candidate in (
                await session.scalars(
                    select(AnalysisCandidate).where(AnalysisCandidate.workflow_run_id == run_id)
                )
            )
        ]
        calls = [
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
            for call in (
                await session.scalars(select(AgentCall).where(AgentCall.workflow_run_id == run_id))
            )
        ]
        before = (
            await session.scalar(select(func.count(AnalysisCandidate.id))),
            await session.scalar(select(func.count(AgentCall.id))),
        )
        assert all(call.estimated_cost is None for call in calls)
        await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id)
            .values(
                status=WorkflowStatus.PROCESSING,
                lease_owner="worker-a",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
        )
        await session.commit()
        assert await persist_analysis_completion(
            session,
            workflow_run_id=run_id,
            lease_owner="worker-a",
            candidates=candidates,
            calls=calls,
            quality_status=WorkflowQuality.NORMAL,
            quality={},
        )
        assert (
            await session.scalar(select(func.count(AnalysisCandidate.id))),
            await session.scalar(select(func.count(AgentCall.id))),
        ) == before
        await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id)
            .values(
                status=WorkflowStatus.PROCESSING,
                lease_owner="replacement-worker",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
        )
        await session.commit()
        assert not await renew_analysis_lease(
            session, workflow_run_id=run_id, lease_owner="worker-a", lease_seconds=60
        )
        assert not await update_analysis_step(
            session,
            workflow_run_id=run_id,
            lease_owner="worker-a",
            current_step="stale-step",
        )
        assert not await persist_analysis_completion(
            session,
            workflow_run_id=run_id,
            lease_owner="worker-a",
            candidates=candidates,
            calls=calls,
            quality_status=WorkflowQuality.NORMAL,
            quality={},
        )
        assert not await fail_analysis_run(
            session, workflow_run_id=run_id, lease_owner="worker-a", error_code="CHECKPOINT_ERROR"
        )
    async with factory() as session:
        after = (
            await session.scalar(select(func.count(AnalysisCandidate.id))),
            await session.scalar(select(func.count(AgentCall.id))),
        )
    refreshed = await _read_run(factory, run_id)
    assert before == after
    assert (refreshed.status, refreshed.lease_owner) == (
        WorkflowStatus.PROCESSING,
        "replacement-worker",
    )


async def test_worker_renews_before_each_retry_and_schema_repair_request(
    seeded_worker, settings, monkeypatch
) -> None:
    factory = seeded_worker["factory"]
    assert isinstance(factory, async_sessionmaker)
    events: list[str] = []
    original_renew = analysis_worker.renew_analysis_lease

    async def recording_renew(session, *, workflow_run_id, lease_owner, lease_seconds):
        events.append("renew")
        return await original_renew(
            session,
            workflow_run_id=workflow_run_id,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
        )

    monkeypatch.setattr(analysis_worker, "renew_analysis_lease", recording_renew)
    responses = [429, 429, 200]

    async def retries(request: httpx.Request) -> httpx.Response:
        events.append("post")
        status = responses.pop(0)
        return _completion(request) if status == 200 else httpx.Response(status)

    retry_run = await _new_run(seeded_worker)
    await analysis_worker.run_once(
        factory,
        settings=settings,
        lease_owner="worker-a",
        checkpointer=InMemorySaver(),
        transport=httpx.MockTransport(retries),
    )
    assert events == ["renew", "post", "renew", "post", "renew", "post", "renew"]
    assert (await _read_run(factory, retry_run)).status is WorkflowStatus.AWAITING_SELECTION

    events.clear()
    response_count = 0

    async def repair(request: httpx.Request) -> httpx.Response:
        nonlocal response_count
        response_count += 1
        events.append("post")
        if response_count == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": "{"}}]})
        return _completion(request)

    repair_run = await _new_run(seeded_worker)
    await analysis_worker.run_once(
        factory,
        settings=settings,
        lease_owner="worker-b",
        checkpointer=InMemorySaver(),
        transport=httpx.MockTransport(repair),
    )
    assert events == ["renew", "post", "renew", "post", "renew"]
    async with factory() as session:
        rows = list(
            await session.scalars(
                select(AgentCall).where(AgentCall.workflow_run_id == repair_run).order_by(AgentCall.id)
            )
        )
    assert {(row.call_type, row.node_name) for row in rows} == {
        (AgentCallType.PRIMARY, "call_analysis_agent"),
        (AgentCallType.SCHEMA_REPAIR, "validate_and_reconcile"),
    }


@pytest.mark.parametrize(
    ("name", "settings_change", "responses", "expected_calls"),
    [
        ("missing", {"deepseek_api_key": None}, [], 0),
        ("unauthorized", {}, [401], 1),
        ("forbidden", {}, [403], 1),
        ("timeout", {}, ["timeout", "timeout", "timeout"], 3),
        ("transport", {}, ["transport", "transport", "transport"], 3),
        ("rate_limited", {}, [429, 429, 429], 3),
        ("server_error", {}, [500, 500, 500], 3),
    ],
)
async def test_llm_failures_degrade_to_five_chinese_candidates(
    seeded_worker, settings, name, settings_change, responses, expected_calls
) -> None:
    factory = seeded_worker["factory"]
    assert isinstance(factory, async_sessionmaker)
    calls = 0

    async def failure(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        outcome = responses.pop(0)
        if outcome == "timeout":
            raise httpx.ReadTimeout("mock timeout")
        if outcome == "transport":
            raise httpx.ConnectError("mock transport")
        return httpx.Response(outcome)

    run_id = await _new_run(seeded_worker)
    await analysis_worker.run_once(
        factory,
        settings=settings.model_copy(update=settings_change),
        lease_owner=f"worker-{name}",
        checkpointer=InMemorySaver(),
        transport=httpx.MockTransport(failure),
    )
    run = await _read_run(factory, run_id)
    async with factory() as session:
        candidates = list(
            await session.scalars(
                select(AnalysisCandidate)
                .where(AnalysisCandidate.workflow_run_id == run_id)
                .order_by(AnalysisCandidate.rank)
            )
        )
        audit_rows = list(
            await session.scalars(select(AgentCall).where(AgentCall.workflow_run_id == run_id))
        )
    assert calls == expected_calls
    assert (run.status, run.quality_status) == (
        WorkflowStatus.AWAITING_SELECTION,
        WorkflowQuality.DEGRADED,
    )
    assert len(candidates) == 5
    assert all("模型解释暂不可用" in candidate.impact_explanation for candidate in candidates)
    assert any(row.attempt == 0 and row.node_name == "validate_and_reconcile" for row in audit_rows)
    assert {row.call_type for row in audit_rows} == {AgentCallType.PRIMARY}
    assert {column.name for column in AgentCall.__table__.columns}.isdisjoint(
        {"api_key", "authorization", "prompt", "raw_response", "chain_of_thought"}
    )


async def test_schema_errors_repair_once_then_degrade_and_lease_loss_stops_without_writes(
    seeded_worker, settings, monkeypatch
) -> None:
    factory = seeded_worker["factory"]
    assert isinstance(factory, async_sessionmaker)
    schema_posts = 0

    async def invalid_schema(_request: httpx.Request) -> httpx.Response:
        nonlocal schema_posts
        schema_posts += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]})

    schema_run = await _new_run(seeded_worker)
    await analysis_worker.run_once(
        factory,
        settings=settings,
        lease_owner="worker-schema",
        checkpointer=InMemorySaver(),
        transport=httpx.MockTransport(invalid_schema),
    )
    assert schema_posts == 2
    async with factory() as session:
        schema_calls = list(
            await session.scalars(select(AgentCall).where(AgentCall.workflow_run_id == schema_run))
        )
    assert [row.call_type for row in schema_calls].count(AgentCallType.SCHEMA_REPAIR) == 1
    assert (await _read_run(factory, schema_run)).quality_status is WorkflowQuality.DEGRADED

    posts = 0

    async def no_post(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return httpx.Response(500)

    async def lose_lease(session, *, workflow_run_id, lease_owner, lease_seconds):
        await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == workflow_run_id)
            .values(lease_owner="replacement-worker")
        )
        await session.commit()
        return False

    monkeypatch.setattr(analysis_worker, "renew_analysis_lease", lose_lease)
    lost_run = await _new_run(seeded_worker)
    result = await analysis_worker.run_once(
        factory,
        settings=settings,
        lease_owner="worker-old",
        checkpointer=InMemorySaver(),
        transport=httpx.MockTransport(no_post),
    )
    assert result == lost_run
    assert posts == 0
    lost = await _read_run(factory, lost_run)
    assert (lost.status, lost.lease_owner) == (WorkflowStatus.PROCESSING, "replacement-worker")
    async with factory() as session:
        assert await session.scalar(
            select(func.count(AnalysisCandidate.id)).where(AnalysisCandidate.workflow_run_id == lost_run)
        ) == 0


async def test_fact_and_checkpoint_failures_are_terminal_only_for_the_current_lease_owner(
    seeded_worker, settings, valid_transport, monkeypatch
) -> None:
    factory = seeded_worker["factory"]
    assert isinstance(factory, async_sessionmaker)

    async def bad_facts(*_args, **_kwargs):
        raise RuntimeError("deterministic fact collection failed")

    monkeypatch.setattr(analysis_worker, "collect_analysis_facts", bad_facts)
    fact_run = await _new_run(seeded_worker)
    await analysis_worker.run_once(
        factory,
        settings=settings,
        lease_owner="worker-facts",
        checkpointer=InMemorySaver(),
        transport=valid_transport,
    )
    failed = await _read_run(factory, fact_run)
    assert (failed.status, failed.error_code) == (WorkflowStatus.FAILED, "FACT_COLLECTION_ERROR")

    monkeypatch.setattr(analysis_worker, "collect_analysis_facts", collect_analysis_facts)

    class FailingReadSaver(InMemorySaver):
        def get_tuple(self, _config):
            raise RuntimeError("checkpoint read failed")

    checkpoint_run = await _new_run(seeded_worker)
    await analysis_worker.run_once(
        factory,
        settings=settings,
        lease_owner="worker-checkpoint",
        checkpointer=FailingReadSaver(),
        transport=valid_transport,
    )
    checkpoint_failed = await _read_run(factory, checkpoint_run)
    assert (checkpoint_failed.status, checkpoint_failed.error_code) == (
        WorkflowStatus.FAILED,
        "CHECKPOINT_ERROR",
    )

    class FailingWriteSaver(InMemorySaver):
        def put_writes(self, *_args, **_kwargs):
            raise RuntimeError("checkpoint consistency write failed")

    checkpoint_write_run = await _new_run(seeded_worker)
    await analysis_worker.run_once(
        factory,
        settings=settings,
        lease_owner="worker-checkpoint-write",
        checkpointer=FailingWriteSaver(),
        transport=valid_transport,
    )
    checkpoint_write_failed = await _read_run(factory, checkpoint_write_run)
    assert (checkpoint_write_failed.status, checkpoint_write_failed.error_code) == (
        WorkflowStatus.FAILED,
        "CHECKPOINT_ERROR",
    )

    class StaleReadSaver(InMemorySaver):
        async def aget_tuple(self, config):
            async with factory() as replacement_session:
                await replacement_session.execute(
                    update(WorkflowRun)
                    .where(WorkflowRun.id == config["configurable"]["thread_id"])
                    .values(lease_owner="new-checkpoint-owner")
                )
                await replacement_session.commit()
            raise analysis_worker.CheckpointFailure()

    stale_run = await _new_run(seeded_worker)
    await analysis_worker.run_once(
        factory,
        settings=settings,
        lease_owner="worker-checkpoint-old",
        checkpointer=StaleReadSaver(),
        transport=valid_transport,
    )
    stale = await _read_run(factory, stale_run)
    assert (stale.status, stale.lease_owner) == (WorkflowStatus.PROCESSING, "new-checkpoint-owner")


async def test_unexpected_non_checkpoint_runtime_is_not_misreported(
    seeded_worker, settings, valid_transport, monkeypatch
) -> None:
    factory = seeded_worker["factory"]
    assert isinstance(factory, async_sessionmaker)

    def unexpected_merge(*_args, **_kwargs):
        raise RuntimeError("unexpected worker defect")

    monkeypatch.setattr(analysis_worker, "_merge_candidates", unexpected_merge)
    run_id = await _new_run(seeded_worker)
    with pytest.raises(RuntimeError, match="unexpected worker defect"):
        await analysis_worker.run_once(
            factory,
            settings=settings,
            lease_owner="worker-unexpected",
            checkpointer=InMemorySaver(),
            transport=valid_transport,
        )
    run = await _read_run(factory, run_id)
    assert (run.status, run.error_code) == (WorkflowStatus.PROCESSING, None)


async def test_final_checkpoint_save_failure_fails_before_completion(
    seeded_worker, settings, valid_transport
) -> None:
    class RecordingFailingSaver(InMemorySaver):
        def __init__(self) -> None:
            super().__init__()
            self.save_count = 0

        def put(self, *args, **kwargs):
            self.save_count += 1
            if self.save_count == 7:
                raise RuntimeError("final checkpoint save failed")
            return super().put(*args, **kwargs)

    factory = seeded_worker["factory"]
    assert isinstance(factory, async_sessionmaker)
    saver = RecordingFailingSaver()
    run_id = await _new_run(seeded_worker)
    processed_id = await analysis_worker.run_once(
        factory,
        settings=settings,
        lease_owner="worker-recording",
        checkpointer=saver,
        transport=valid_transport,
    )
    assert processed_id == run_id
    run = await _read_run(factory, run_id)
    assert saver.save_count == 7
    assert (run.status, run.error_code, run.lease_owner) == (
        WorkflowStatus.FAILED,
        "CHECKPOINT_ERROR",
        None,
    )
    async with factory() as session:
        assert await session.scalar(
            select(func.count(AnalysisCandidate.id)).where(AnalysisCandidate.workflow_run_id == run_id)
        ) == 5
