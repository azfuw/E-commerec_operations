from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from backend.common import AuditEventType, PlatformDeliveryStatus
from backend.config import Settings
from backend.models import AuditEvent, PlatformDelivery
from backend.platform_client import (
    CommercePlatformClient,
    PlatformClientError,
    PlatformPublishResult,
)
import backend.platform_delivery_worker as delivery_worker
from backend.platform_delivery_worker import PlatformDeliveryWorkerError, run_once
from scripts import run_platform_delivery_worker as worker_cli
from tests.support.platform_simulator import create_platform_simulator
from tests.test_platform_delivery_runs import (
    load_delivery,
    make_delivery_due,
    platform_delivery_factory,
    seed_pending_delivery,
)


@pytest.fixture
def delivery_settings() -> Settings:
    return Settings(
        _env_file=None,
        jwt_secret_key=SecretStr("unit-test-jwt"),
        platform_base_url="http://platform.test",
        platform_client_id=SecretStr("client-id"),
        platform_client_secret=SecretStr("client-secret"),
        platform_webhook_secret=SecretStr("webhook-secret"),
        platform_delivery_lease_seconds=60,
    )


class _TrackingFactory:
    def __init__(self, factory) -> None:
        self.factory = factory
        self.active_contexts = 0

    def __call__(self):
        @asynccontextmanager
        async def tracked():
            self.active_contexts += 1
            try:
                async with self.factory() as session:
                    yield session
            finally:
                self.active_contexts -= 1

        return tracked()


class _SuccessClient:
    def __init__(self, tracker: _TrackingFactory | None = None) -> None:
        self.tracker = tracker
        self.calls: list[dict[str, object]] = []

    async def publish_listing(self, **kwargs: object) -> PlatformPublishResult:
        if self.tracker is not None:
            assert self.tracker.active_contexts == 0
        self.calls.append(kwargs)
        return PlatformPublishResult("operation-1")


class _TransientClient:
    def __init__(self) -> None:
        self.calls = 0

    async def publish_listing(self, **_kwargs: object) -> PlatformPublishResult:
        self.calls += 1
        raise PlatformClientError("PLATFORM_SERVER_ERROR", True)


async def test_worker_success_uses_only_immutable_snapshot_and_completes_once(
    platform_delivery_factory, delivery_settings
) -> None:
    delivery_id = await seed_pending_delivery(
        platform_delivery_factory, suffix="snapshot"
    )
    tracker = _TrackingFactory(platform_delivery_factory)
    client = _SuccessClient(tracker)

    processed = await run_once(
        tracker,
        settings=delivery_settings,
        lease_owner="worker-1",
        client=client,
    )

    assert processed == delivery_id
    row = await load_delivery(platform_delivery_factory, delivery_id)
    assert (
        row.status,
        row.attempt_count,
        row.external_operation_id,
    ) == (PlatformDeliveryStatus.SUCCEEDED, 1, "operation-1")
    assert client.calls == [
        {
            "store_id": "store-snapshot",
            "product_id": "product-snapshot",
            "payload": {
                "title": "已审批标题",
                "selling_points": ["耐用", "易清洁"],
                "description": "已审批描述",
                "keywords": ["家居", "棉质"],
                "attribute_completions": {"材质": "棉", "可回收": True},
            },
            "idempotency_key": "a" * 64,
        }
    ]


async def test_transient_failures_retry_three_total_attempts_then_fail(
    platform_delivery_factory, delivery_settings
) -> None:
    delivery_id = await seed_pending_delivery(platform_delivery_factory)
    client = _TransientClient()
    for expected_attempt in (1, 2, 3):
        await make_delivery_due(platform_delivery_factory, delivery_id)
        assert await run_once(
            platform_delivery_factory,
            settings=delivery_settings,
            lease_owner=f"worker-{expected_attempt}",
            client=client,
        ) == delivery_id
    row = await load_delivery(platform_delivery_factory, delivery_id)
    assert (row.status, row.attempt_count, row.error_code) == (
        PlatformDeliveryStatus.FAILED,
        3,
        "PLATFORM_SERVER_ERROR",
    )
    assert client.calls == 3
    async with platform_delivery_factory() as session:
        audits = list(await session.scalars(select(AuditEvent)))
    assert [audit.event_type for audit in audits] == [
        AuditEventType.PLATFORM_DELIVERY_FAILED
    ]


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (PlatformClientError("PLATFORM_FORBIDDEN", False), "PLATFORM_FORBIDDEN"),
        (RuntimeError("private exception detail"), "PLATFORM_DELIVERY_FAILED"),
    ],
)
async def test_nonretryable_and_unexpected_errors_fail_once_with_safe_code(
    platform_delivery_factory, delivery_settings, error: Exception, expected_code: str
) -> None:
    delivery_id = await seed_pending_delivery(platform_delivery_factory)

    class FailingClient:
        async def publish_listing(self, **_kwargs: object) -> PlatformPublishResult:
            raise error

    assert await run_once(
        platform_delivery_factory,
        settings=delivery_settings,
        lease_owner="worker-1",
        client=FailingClient(),
    ) == delivery_id
    row = await load_delivery(platform_delivery_factory, delivery_id)
    assert (row.status, row.attempt_count, row.error_code) == (
        PlatformDeliveryStatus.FAILED,
        1,
        expected_code,
    )
    assert "private exception detail" not in repr(row.error_code)


class _DisconnectAfterAccept(httpx.AsyncBaseTransport):
    def __init__(self, app) -> None:
        self.inner = httpx.ASGITransport(app=app)
        self.disconnected = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        if request.method == "PUT" and not self.disconnected:
            await response.aread()
            await response.aclose()
            self.disconnected = True
            raise httpx.ReadError("private disconnect", request=request)
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


async def test_accepted_then_disconnect_replays_same_operation_once(
    platform_delivery_factory, delivery_settings
) -> None:
    delivery_id = await seed_pending_delivery(
        platform_delivery_factory, suffix="replay"
    )
    app = create_platform_simulator()
    client = CommercePlatformClient(
        delivery_settings, transport=_DisconnectAfterAccept(app)
    )
    try:
        assert await run_once(
            platform_delivery_factory,
            settings=delivery_settings,
            lease_owner="worker-1",
            client=client,
        ) == delivery_id
        first = await load_delivery(platform_delivery_factory, delivery_id)
        assert (first.status, first.attempt_count, first.error_code) == (
            PlatformDeliveryStatus.PENDING,
            1,
            "PLATFORM_CONNECTION_FAILED",
        )
        await make_delivery_due(platform_delivery_factory, delivery_id)

        assert await run_once(
            platform_delivery_factory,
            settings=delivery_settings,
            lease_owner="worker-2",
            client=client,
        ) == delivery_id
    finally:
        await client.aclose()

    row = await load_delivery(platform_delivery_factory, delivery_id)
    assert row.status is PlatformDeliveryStatus.SUCCEEDED
    assert row.attempt_count == 2
    assert row.external_operation_id is not None
    assert app.state.mutation_count == 1


async def test_worker_returns_none_without_calling_client(
    platform_delivery_factory, delivery_settings
) -> None:
    client = _SuccessClient()
    assert (
        await run_once(
            platform_delivery_factory,
            settings=delivery_settings,
            lease_owner="worker-1",
            client=client,
        )
        is None
    )
    assert client.calls == []


@pytest.mark.parametrize("failure", ["invalid-result", "completion-commit"])
async def test_remote_success_local_completion_failure_is_safe_and_replayable(
    platform_delivery_factory,
    delivery_settings,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    delivery_id = await seed_pending_delivery(platform_delivery_factory)

    if failure == "invalid-result":
        client = _SuccessClient()

        async def invalid_publish(**kwargs: object) -> PlatformPublishResult:
            client.calls.append(kwargs)
            return PlatformPublishResult("private operation id with spaces")

        client.publish_listing = invalid_publish
    else:
        client = _SuccessClient()

        async def fail_completion(*_args: object, **_kwargs: object) -> bool:
            raise SQLAlchemyError("private request and response body")

        monkeypatch.setattr(
            delivery_worker, "complete_platform_delivery", fail_completion
        )

    with pytest.raises(PlatformDeliveryWorkerError) as raised:
        await run_once(
            platform_delivery_factory,
            settings=delivery_settings,
            lease_owner="worker-1",
            client=client,
        )

    assert str(raised.value) == "PLATFORM_DELIVERY_FAILED"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    row = await load_delivery(platform_delivery_factory, delivery_id)
    assert (row.status, row.attempt_count, row.lease_owner) == (
        PlatformDeliveryStatus.PROCESSING,
        1,
        "worker-1",
    )
    async with platform_delivery_factory() as session:
        assert await session.scalar(select(func.count(AuditEvent.id))) == 0


async def test_claim_failure_raises_only_closed_worker_code(
    platform_delivery_factory,
    delivery_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery_id = await seed_pending_delivery(platform_delivery_factory)

    async def fail_claim(*_args: object, **_kwargs: object) -> None:
        raise SQLAlchemyError("private database URL")

    monkeypatch.setattr(delivery_worker, "claim_next_platform_delivery", fail_claim)
    with pytest.raises(PlatformDeliveryWorkerError) as raised:
        await run_once(
            platform_delivery_factory,
            settings=delivery_settings,
            lease_owner="worker-1",
            client=_SuccessClient(),
        )
    assert str(raised.value) == "PLATFORM_DELIVERY_FAILED"
    assert raised.value.__cause__ is raised.value.__context__ is None
    row = await load_delivery(platform_delivery_factory, delivery_id)
    assert (row.status, row.attempt_count) == (
        PlatformDeliveryStatus.PENDING,
        0,
    )


@pytest.mark.parametrize(
    "missing",
    [
        "RUN_PHASE10_PLATFORM_DELIVERY",
        "PLATFORM_BASE_URL",
        "PLATFORM_CLIENT_ID",
        "PLATFORM_CLIENT_SECRET",
        "PLATFORM_WEBHOOK_SECRET",
    ],
)
async def test_cli_refuses_missing_opt_in_or_platform_setting_before_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path, missing: str
) -> None:
    monkeypatch.chdir(tmp_path)
    values = {
        "RUN_PHASE10_PLATFORM_DELIVERY": "1",
        "JWT_SECRET_KEY": "unit-test-jwt-secret-at-least-32-characters",
        "PLATFORM_BASE_URL": "http://platform.test",
        "PLATFORM_CLIENT_ID": "client-id",
        "PLATFORM_CLIENT_SECRET": "client-secret",
        "PLATFORM_WEBHOOK_SECRET": "webhook-secret",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing)

    with pytest.raises(SystemExit, match="platform delivery is disabled or incomplete"):
        await worker_cli.main(True)


def test_cli_help_is_offline_and_safe() -> None:
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, "scripts/run_platform_delivery_worker.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert "--once" in completed.stdout
    assert "secret" not in completed.stdout.lower()


def test_cli_converts_unexpected_failure_to_closed_code_without_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = "private database URL and request body"

    def fail_run(coroutine) -> None:
        coroutine.close()
        raise RuntimeError(private)

    monkeypatch.setattr(worker_cli.asyncio, "run", fail_run)
    with pytest.raises(SystemExit) as raised:
        worker_cli.cli(["--once"])
    assert str(raised.value) == "PLATFORM_DELIVERY_FAILED"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert private not in repr(raised.value)
