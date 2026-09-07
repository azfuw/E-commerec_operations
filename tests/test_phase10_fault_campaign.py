import json

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import backend.manual_review_worker as manual_worker
from backend.common import PlatformDeliveryStatus, WorkflowStatus
from backend.config import Settings
from backend.database import Base
from backend.deepseek_runtime import DeepSeekJsonRuntime
from backend.knowledge_index import KnowledgeDependencyError
from backend.knowledge_search import search_active_knowledge
from backend.models import AuditEvent, PlatformWebhookReceipt
from backend.platform_client import CommercePlatformClient
from backend.platform_delivery_worker import run_once as run_platform_delivery_once
from backend.platform_webhooks import PlatformWebhookError
from tests.support.platform_simulator import create_platform_simulator
from tests.test_knowledge_search import _Index, _Models, _seed
from tests.test_manual_review_worker import (
    _ManualLoader,
    _ManualRecordingSaver,
    _MutateAtNodeSaver,
    _manual_worker_counts,
    _manual_worker_run,
    _manual_worker_settings,
    _manual_worker_snapshot,
    _manual_worker_transport,
    _passing_response,
    _seed_manual_worker,
    _trusted_seed,
    manual_worker_factory,
)
from tests.test_platform_delivery_runs import (
    load_delivery,
    make_delivery_due,
    platform_delivery_factory,
    seed_pending_delivery,
)
from tests.test_platform_delivery_worker import _DisconnectAfterAccept, delivery_settings
from tests.test_platform_webhooks import _count, _receive, webhook_session


FAULT_EXPECTATIONS = {
    "deepseek_timeout": ("DEEPSEEK_TIMEOUT", 1),
    "deepseek_rate_limit": ("DEEPSEEK_RATE_LIMIT", 1),
    "deepseek_invalid_json": ("DEEPSEEK_SCHEMA_INVALID", 2),
    "milvus_timeout": ("KNOWLEDGE_DEPENDENCY_TIMEOUT", 0),
    "milvus_zero_hit": ("zero_hit", 0),
    "platform_accepted_disconnect": ("succeeded", 1),
    "platform_three_5xx": ("failed", 3),
    "worker_stale_owner": ("processing", 0),
    "webhook_bad_signature": ("PLATFORM_WEBHOOK_SIGNATURE_INVALID", 0),
    "webhook_duplicate": ("accepted", 1),
}

COUNT_SEMANTICS = {
    "deepseek_timeout": "outbound HTTP attempts",
    "deepseek_rate_limit": "outbound HTTP attempts",
    "deepseek_invalid_json": "primary plus schema-repair HTTP attempts",
    "milvus_timeout": "manufactured knowledge hits",
    "milvus_zero_hit": "returned knowledge hits",
    "platform_accepted_disconnect": "remote platform mutations",
    "platform_three_5xx": "claimed delivery attempts",
    "worker_stale_owner": "terminal writes by the stale owner",
    "webhook_bad_signature": "durable receipt and audit writes",
    "webhook_duplicate": "durable webhook receipts",
}

FORBIDDEN = (
    "authorization",
    "api_key",
    "prompt",
    "raw_response",
    "traceback",
    "connection_string",
)


async def _persisted_values(session: AsyncSession) -> dict[str, list[list[object]]]:
    values: dict[str, list[list[object]]] = {}
    for table in sorted(Base.metadata.tables.values(), key=lambda item: item.name):
        rows = (await session.execute(select(table))).all()
        if rows:
            values[table.name] = [list(row) for row in rows]
    return values


async def _deepseek_fault(fault: str) -> tuple[str, int, object]:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if fault == "deepseek_timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(429)

    runtime = DeepSeekJsonRuntime(
        Settings(
            _env_file=None,
            jwt_secret_key="synthetic-test-secret-at-least-32-characters",
            deepseek_api_key="synthetic-test-key",
            deepseek_base_url="https://mock.deepseek.invalid",
        ),
        transport=httpx.MockTransport(handler),
    )
    result = await runtime.request(
        system_prompt="synthetic system instruction",
        user_payload={"synthetic_id": "phase10-fault-campaign"},
        parse_response=json.loads,
        max_attempts=1,
    )
    assert attempts == len(result.records) == 1
    assert result.error_code is not None
    evidence = [
        [
            record.attempt,
            record.model,
            record.status,
            record.input_hash,
            record.error_code,
        ]
        for record in result.records
    ]
    return result.error_code, attempts, evidence


async def _deepseek_invalid_json(factory) -> tuple[str, int, object]:
    await _seed_manual_worker(factory)
    requests: list[dict[str, object]] = []
    await manual_worker.run_once(
        factory,
        settings=_manual_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_ManualRecordingSaver(),
        trusted_input_loader=_ManualLoader(_trusted_seed()),
        transport=_manual_worker_transport(
            _passing_response(),
            requests,
            schema_invalid_primary=True,
            schema_invalid_repair=True,
        ),
    )
    run = await _manual_worker_run(factory)
    assert run.status is WorkflowStatus.COMPLETED
    assert len(requests) == 2
    assert (await _manual_worker_counts(factory))[2] == 2
    async with factory() as session:
        evidence = await _persisted_values(session)
    assert run.error_code is not None
    return run.error_code, len(requests), evidence


async def _knowledge_fault(fault: str, session: AsyncSession) -> tuple[str, int, object]:
    await _seed(session)
    models = _Models(
        error=(
            KnowledgeDependencyError("KNOWLEDGE_DEPENDENCY_TIMEOUT", retryable=True)
            if fault == "milvus_timeout"
            else None
        )
    )
    index = _Index(dense=[], sparse=[])
    try:
        outcome = await search_active_knowledge(
            session,
            query="合成规则",
            categories=None,
            top_k=3,
            retrieval_path="hybrid",
            load_dependencies=lambda: (
                models,
                index,
                {"candidate_limit": 3, "rrf_k": 60, "threshold": 0.0},
            ),
        )
    except KnowledgeDependencyError as error:
        assert fault == "milvus_timeout"
        assert len(models.embed_queries) == 1
        assert index.dense_calls == index.sparse_calls == []
        return error.code, 0, await _persisted_values(session)

    assert fault == "milvus_zero_hit"
    assert len(models.embed_queries) == 1
    assert len(index.dense_calls) == len(index.sparse_calls) == 1
    assert outcome.hits == []
    return outcome.quality_status, len(outcome.hits), await _persisted_values(session)


async def _platform_fault(fault: str, factory, settings: Settings) -> tuple[str, int, object]:
    delivery_id = await seed_pending_delivery(factory, suffix=fault[-10:])
    app = create_platform_simulator(
        fault=None if fault == "platform_accepted_disconnect" else "server_error"
    )
    transport = (
        _DisconnectAfterAccept(app)
        if fault == "platform_accepted_disconnect"
        else httpx.ASGITransport(app=app)
    )
    client = CommercePlatformClient(settings, transport=transport)
    try:
        attempts = 2 if fault == "platform_accepted_disconnect" else 3
        for attempt in range(attempts):
            if attempt:
                await make_delivery_due(factory, delivery_id)
            assert await run_platform_delivery_once(
                factory,
                settings=settings,
                lease_owner=f"worker-{attempt + 1}",
                client=client,
            ) == delivery_id
    finally:
        await client.aclose()

    row = await load_delivery(factory, delivery_id)
    assert len(app.state.write_idempotency_keys) == attempts
    assert set(app.state.write_idempotency_keys) == {"a" * 64}
    async with factory() as session:
        evidence = await _persisted_values(session)
    if fault == "platform_accepted_disconnect":
        assert row.status is PlatformDeliveryStatus.SUCCEEDED
        assert row.attempt_count == 2
        assert app.state.mutation_count == 1
        assert row.external_operation_id == next(iter(app.state.operations.values()))[1]
        return row.status.value, app.state.mutation_count, evidence

    assert row.status is PlatformDeliveryStatus.FAILED
    assert row.attempt_count == attempts == 3
    assert app.state.mutation_count == 0
    return row.status.value, row.attempt_count, evidence


async def _stale_worker(factory) -> tuple[str, int, object]:
    await _seed_manual_worker(factory)
    saver = _MutateAtNodeSaver(factory, "call_compliance_agent", "owner")
    requests: list[dict[str, object]] = []
    assert await manual_worker.run_once(
        factory,
        settings=_manual_worker_settings(),
        lease_owner="worker-a",
        checkpointer=saver,
        trusted_input_loader=_ManualLoader(_trusted_seed()),
        transport=_manual_worker_transport(_passing_response(), requests),
    ) == "manual-workflow-1"
    assert saver.mutated and saver.snapshot is not None
    current = await _manual_worker_snapshot(factory)
    stale_terminal_writes = int(current != saver.snapshot)
    assert stale_terminal_writes == 0
    assert requests == []
    run = await _manual_worker_run(factory)
    assert (run.status, run.lease_owner) == (WorkflowStatus.PROCESSING, "worker-b")
    async with factory() as session:
        evidence = await _persisted_values(session)
    return run.status.value, stale_terminal_writes, evidence


async def _webhook_fault(fault: str, session: AsyncSession) -> tuple[str, int, object]:
    if fault == "webhook_bad_signature":
        try:
            await _receive(session, body=b"not-json", signature="0" * 64)
        except PlatformWebhookError as error:
            writes = await _count(session, PlatformWebhookReceipt) + await _count(
                session, AuditEvent
            )
            assert writes == 0
            return error.code, writes, await _persisted_values(session)
        raise AssertionError("bad signature was accepted")

    assert await _receive(session) is True
    assert await _receive(session) is False
    receipts = await _count(session, PlatformWebhookReceipt)
    audits = await _count(session, AuditEvent)
    assert receipts == audits == 1
    return "accepted", receipts, await _persisted_values(session)


@pytest.mark.parametrize("fault", FAULT_EXPECTATIONS)
async def test_phase10_fault_campaign(
    fault: str,
    session: AsyncSession,
    platform_delivery_factory,
    delivery_settings: Settings,
    webhook_session: AsyncSession,
    manual_worker_factory,
) -> None:
    if fault in {"deepseek_timeout", "deepseek_rate_limit"}:
        actual = await _deepseek_fault(fault)
    elif fault == "deepseek_invalid_json":
        actual = await _deepseek_invalid_json(manual_worker_factory)
    elif fault in {"milvus_timeout", "milvus_zero_hit"}:
        actual = await _knowledge_fault(fault, session)
    elif fault in {"platform_accepted_disconnect", "platform_three_5xx"}:
        actual = await _platform_fault(fault, platform_delivery_factory, delivery_settings)
    elif fault == "worker_stale_owner":
        actual = await _stale_worker(manual_worker_factory)
    else:
        actual = await _webhook_fault(fault, webhook_session)

    outcome, count, persisted_values = actual
    assert (outcome, count) == FAULT_EXPECTATIONS[fault]
    assert COUNT_SEMANTICS[fault]
    serialized = json.dumps(
        persisted_values, ensure_ascii=False, sort_keys=True, default=str
    ).lower()
    assert not any(forbidden in serialized for forbidden in FORBIDDEN)
