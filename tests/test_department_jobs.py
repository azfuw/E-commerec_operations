"""Queued jobs and writeback use current database department authorization."""

from datetime import date

import httpx
import pytest
from fastapi import HTTPException
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select, update

from backend.common import KnowledgeVersionStatus, UserDepartment, UserRole, WorkflowStatus
from backend.models import AgentCall, AnalysisCandidate, KnowledgeChunk, KnowledgeDocumentVersion, ProposalRevision, User, WorkflowRun
from tests.test_analysis_worker import _completion, _new_run, seeded_worker, settings, worker_factory
from tests.test_auth_and_scope import auth_data
from tests.test_platform_delivery_runs import platform_delivery_factory
from tests.test_platform_delivery_worker import delivery_settings


@pytest.mark.parametrize("revoke_when", ["queued", "running"])
async def test_analysis_worker_stops_after_department_change(seeded_worker, settings, revoke_when):
    from backend.analysis_worker import run_once

    factory = seeded_worker["factory"]
    run_id = await _new_run(seeded_worker)
    async def revoke():
        async with factory() as session:
            await session.execute(update(User).where(User.id == seeded_worker["user_id"]).values(department=UserDepartment.LOGISTICS))
            await session.commit()
    if revoke_when == "queued":
        await revoke()
    requests = []
    async def handler(request):
        requests.append(request)
        await revoke()
        return _completion(request)
    assert await run_once(factory, settings=settings, lease_owner="department-worker", checkpointer=InMemorySaver(), transport=httpx.MockTransport(handler)) == run_id
    async with factory() as session:
        run = await session.get(WorkflowRun, run_id)
        assert (run.status, run.error_code) == (WorkflowStatus.FAILED, "ANALYSIS_AUTHORIZATION_CHANGED")
        assert await session.scalar(select(func.count()).select_from(AnalysisCandidate).where(AnalysisCandidate.workflow_run_id == run_id)) == 0
        assert await session.scalar(select(func.count()).select_from(AgentCall).where(AgentCall.workflow_run_id == run_id)) == 0
    assert len(requests) == (0 if revoke_when == "queued" else 1)


async def test_internal_analysis_creation_denies_logistics(session, auth_data):
    from backend.analysis_runs import create_analysis_run

    auth_data["operator"].department = UserDepartment.LOGISTICS
    await session.commit()
    with pytest.raises(HTTPException) as error:
        await create_analysis_run(session, store_id="flagship", created_by="operator-user", start_date=date(2026, 9, 1), end_date=date(2026, 9, 2))
    assert error.value.status_code == 403
    assert await session.scalar(select(func.count()).select_from(WorkflowRun)) == 0


@pytest.mark.parametrize("boundary", ["load", "renew", "persist"])
async def test_optimization_revalidates_department_at_execution_and_writeback(session, boundary):
    from backend.optimization_runs import load_owned_optimization_context, persist_optimization_revision, renew_optimization_lease
    from tests.test_optimization_worker import _chain, _citation, _optimization_calls, _output, _trusted

    chain = await _chain(session, live=True)
    trusted = _trusted(chain)
    await session.execute(update(User).where(User.id == "user-1").values(department=UserDepartment.LOGISTICS).execution_options(synchronize_session=False))
    await session.commit()
    if boundary == "load":
        result = await load_owned_optimization_context(session, workflow_run_id="optimization-1", lease_owner="worker-a")
        assert result.error_code == "OPTIMIZATION_AUTHORIZATION_CHANGED"
    elif boundary == "renew":
        assert not await renew_optimization_lease(session, workflow_run_id="optimization-1", lease_owner="worker-a", lease_seconds=60)
    else:
        result = await persist_optimization_revision(session, workflow_run_id="optimization-1", lease_owner="worker-a", iteration=0,
            trusted=trusted, output=_output(), canonical_citations=[_citation()], calls=_optimization_calls(0))
        assert result.error_code == "OPTIMIZATION_AUTHORIZATION_CHANGED"
    assert (await session.get(WorkflowRun, "optimization-1", populate_existing=True)).status == WorkflowStatus.FAILED
    assert await session.scalar(select(func.count()).select_from(ProposalRevision)) == 0


@pytest.mark.parametrize("boundary", ["load", "renew", "persist"])
async def test_manual_review_revalidates_department_at_execution_and_writeback(session, boundary):
    import backend.manual_review_runs as runs
    from backend.optimization_validation import validate_optimization_output
    from tests.test_manual_review_worker import _seed_manual_chain, _trusted_seed, _proposal_output, _passing_response, _manual_calls

    await _seed_manual_chain(session)
    await session.execute(update(User).where(User.id == "operator-1").values(department=UserDepartment.LOGISTICS).execution_options(synchronize_session=False))
    await session.commit()
    if boundary == "load":
        result = await runs.load_owned_manual_review_context(session, workflow_run_id="manual-workflow-1", lease_owner="worker-a")
        assert result.error_code == "MANUAL_REVIEW_AUTHORIZATION_CHANGED"
    elif boundary == "renew":
        assert not await runs.renew_manual_review_lease(session, workflow_run_id="manual-workflow-1", lease_owner="worker-a", lease_seconds=60)
    else:
        trusted = _trusted_seed()
        result = await runs.persist_manual_compliance_review(session, workflow_run_id="manual-workflow-1", lease_owner="worker-a", trusted=trusted,
            deterministic=validate_optimization_output(trusted, _proposal_output()), response=_passing_response(), calls=_manual_calls(), error_code=None)
        assert result.error_code == "MANUAL_REVIEW_AUTHORIZATION_CHANGED"
    assert (await session.get(WorkflowRun, "manual-workflow-1", populate_existing=True)).status == WorkflowStatus.FAILED
    assert await session.scalar(select(func.count()).select_from(AgentCall)) == 0


@pytest.mark.parametrize("boundary", ["renew", "chunks", "activate"])
async def test_knowledge_writeback_rechecks_department(session, boundary):
    from backend.knowledge_runs import activate_knowledge_version, claim_next_knowledge_version, renew_knowledge_lease, upsert_knowledge_chunks
    from tests.test_knowledge_runs import _add_admin, _document, _version, _draft

    admin = await _add_admin(session)
    session.add(_document("department-document", admin.id))
    await session.flush()
    session.add(_version("department-version", "department-document", 1))
    await session.commit()
    await claim_next_knowledge_version(session, lease_owner="department-worker", lease_seconds=60)
    await session.execute(update(User).where(User.id == admin.id).values(department=UserDepartment.LOGISTICS, role=UserRole.SUPERVISOR).execution_options(synchronize_session=False))
    await session.commit()
    kwargs = dict(version_id="department-version", lease_owner="department-worker")
    if boundary == "renew":
        assert not await renew_knowledge_lease(session, **kwargs, lease_seconds=60)
    elif boundary == "chunks":
        assert not await upsert_knowledge_chunks(session, **kwargs, chunks=[_draft(0)])
    else:
        assert not await activate_knowledge_version(session, **kwargs)
    version = await session.get(KnowledgeDocumentVersion, "department-version", populate_existing=True)
    assert (version.status, version.error_code) == (KnowledgeVersionStatus.FAILED, "KNOWLEDGE_AUTHORIZATION_CHANGED")
    assert await session.scalar(select(func.count()).select_from(KnowledgeChunk)) == 0


@pytest.mark.parametrize("revoke_when", ["queued", "running"])
async def test_platform_delivery_rechecks_department(platform_delivery_factory, delivery_settings, revoke_when):
    from backend.common import PlatformDeliveryStatus
    from backend.platform_client import PlatformPublishResult
    from backend.platform_delivery_worker import run_once
    from tests.test_platform_delivery_runs import seed_pending_delivery, load_delivery

    factory = platform_delivery_factory
    delivery_id = await seed_pending_delivery(factory, suffix="department")
    async def revoke():
        async with factory() as session:
            await session.execute(update(User).where(User.id == "user-department").values(department=UserDepartment.LOGISTICS))
            await session.commit()
    if revoke_when == "queued":
        await revoke()
    class Client:
        calls = 0
        async def publish_listing(self, **kwargs):
            self.calls += 1
            await revoke()
            return PlatformPublishResult("operation-department")
    client = Client()
    assert await run_once(factory, settings=delivery_settings, lease_owner="department-worker", client=client) == delivery_id
    row = await load_delivery(factory, delivery_id)
    assert (row.status, row.error_code, row.external_operation_id) == (PlatformDeliveryStatus.FAILED, "PLATFORM_FORBIDDEN", None)
    assert client.calls == (0 if revoke_when == "queued" else 1)


@pytest.mark.parametrize("refresh", [False, True])
async def test_platform_rechecks_after_oauth_before_each_listing_write(platform_delivery_factory, delivery_settings, refresh):
    from backend.common import PlatformDeliveryStatus
    from backend.platform_client import CommercePlatformClient
    from backend.platform_delivery_worker import run_once
    from tests.test_platform_delivery_runs import seed_pending_delivery, load_delivery

    factory = platform_delivery_factory
    delivery_id = await seed_pending_delivery(factory, suffix="oauth-department")
    token_requests = 0
    listing_requests = []
    async def handler(request):
        nonlocal token_requests
        if request.url.path == "/oauth/token":
            token_requests += 1
            if token_requests == (2 if refresh else 1):
                async with factory() as session:
                    await session.execute(update(User).where(User.id == "user-oauth-department").values(department=UserDepartment.LOGISTICS))
                    await session.commit()
            return httpx.Response(200, json={"access_token": "test-oauth-token", "token_type": "Bearer"})
        assert request.method == "PUT"
        listing_requests.append(request)
        if refresh and len(listing_requests) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"external_operation_id": "operation-oauth"})
    client = CommercePlatformClient(delivery_settings, transport=httpx.MockTransport(handler))
    try:
        await run_once(factory, settings=delivery_settings, lease_owner="oauth-worker", client=client)
    finally:
        await client.aclose()
    assert len(listing_requests) == (1 if refresh else 0)
    row = await load_delivery(factory, delivery_id)
    assert (row.status, row.error_code) == (PlatformDeliveryStatus.FAILED, "PLATFORM_FORBIDDEN")
