import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import and_, delete, func, not_, or_, select, text, update

import backend.optimization_worker as optimization_worker
from backend.common import (
    AgentCallType,
    ComplianceRiskLevel,
    KnowledgeVersionStatus,
    UserRole,
    UserStatus,
    WorkflowQuality,
    WorkflowStatus,
    WorkflowType,
)
from backend.compliance_agent import ComplianceAgentResponse
from backend.config import Settings
from backend.database import async_session_factory
from backend.models import (
    AgentCall,
    AnalysisCandidate,
    ComplianceReview,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    Product,
    ProductProposal,
    ProductSku,
    ProposalRevision,
    Store,
    User,
    UserStoreScope,
    WorkflowRun,
)
from backend.optimization_agent import OptimizationAgentCallRecord
from backend.optimization_runs import (
    claim_next_optimization_run,
    defer_optimization_manual,
    fail_optimization_run,
    finalize_optimization_draft,
    load_owned_optimization_context,
    persist_compliance_failure,
    persist_compliance_review,
    persist_optimization_revision,
)
from backend.optimization_validation import validate_optimization_output
from backend.schemas import (
    CanonicalRuleCitation,
    DescriptionSection,
    EvidenceRef,
    OptimizationChange,
    OptimizationProposalOutput,
    OutputCitation,
    ProductMetrics,
    TrustedOptimizationInput,
)


if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
        reason="explicit PostgreSQL integration opt-in required",
    ),
]

_CHECKPOINT_KEYS = {
    "workflow_run_id", "iteration", "revision_id", "review_id", "review_passed",
    "review_quality_status", "error_code", "next_node",
}


@dataclass
class _OwnedIds:
    threads: set[str] = field(default_factory=set)
    scopes: set[tuple[str, str]] = field(default_factory=set)
    checkpoint_tables_ready: bool = False
    stores: set[str] = field(default_factory=set)
    users: set[str] = field(default_factory=set)
    products: set[str] = field(default_factory=set)
    skus: set[str] = field(default_factory=set)
    analysis_runs: set[str] = field(default_factory=set)
    optimization_runs: set[str] = field(default_factory=set)
    candidates: set[str] = field(default_factory=set)
    proposals: set[str] = field(default_factory=set)
    revisions: set[str] = field(default_factory=set)
    reviews: set[str] = field(default_factory=set)
    calls: set[str] = field(default_factory=set)
    documents: set[str] = field(default_factory=set)
    versions: set[str] = field(default_factory=set)
    chunks: set[str] = field(default_factory=set)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        jwt_secret_key="task-nine-postgres-local-jwt-secret-at-least-32",
        deepseek_api_key="mock-transport-only-key",
        deepseek_base_url="https://mock.deepseek.invalid",
        optimization_lease_seconds=60,
    )


async def _external_claimable_run_exists(owned_workflow_ids: set[str]) -> bool:
    eligible = or_(
        WorkflowRun.status == WorkflowStatus.ACCEPTED,
        and_(WorkflowRun.status == WorkflowStatus.PROCESSING, WorkflowRun.lease_expires_at < func.now()),
    )
    async with async_session_factory() as session:
        return (
            await session.scalar(
                select(WorkflowRun.id).where(
                    WorkflowRun.workflow_type == WorkflowType.OPTIMIZATION,
                    WorkflowRun.attempt_count < 3,
                    eligible,
                    not_(WorkflowRun.id.in_(owned_workflow_ids)),
                ).limit(1)
            )
        ) is not None


async def _assert_no_external_claimable(owned: _OwnedIds) -> None:
    if await _external_claimable_run_exists(owned.optimization_runs):
        pytest.skip("external optimization rows are claimable")


async def _seed_chain(owned: _OwnedIds, prefix: str) -> dict[str, str]:
    ids = {key: str(uuid4()) for key in (
        "store", "user", "product", "sku", "analysis", "candidate", "optimization", "proposal",
        "document", "version", "chunk",
    )}
    owned.stores.add(ids["store"])
    owned.users.add(ids["user"])
    owned.scopes.add((ids["user"], ids["store"]))
    owned.products.add(ids["product"])
    owned.skus.add(ids["sku"])
    owned.analysis_runs.add(ids["analysis"])
    owned.optimization_runs.add(ids["optimization"])
    owned.threads.add(ids["optimization"])
    owned.candidates.add(ids["candidate"])
    owned.proposals.add(ids["proposal"])
    owned.documents.add(ids["document"])
    owned.versions.add(ids["version"])
    owned.chunks.add(ids["chunk"])
    async with async_session_factory() as session:
        session.add_all((
            Store(id=ids["store"], name=f"{prefix}-store", code=f"t9-{ids['store'][:8]}", enabled=True),
            User(
                id=ids["user"], username=f"t9-{ids['user'][:12]}", password_hash="task9-postgres",
                role=UserRole.OPERATOR, status=UserStatus.ACTIVE,
            ),
        ))
        await session.flush()
        session.add(UserStoreScope(user_id=ids["user"], store_id=ids["store"]))
        session.add(Product(
            id=ids["product"], store_id=ids["store"], code=f"t9-{ids['product'][:8]}", title="手机",
            category="数码", brand="", selling_points=["续航持久"], description="普通手机",
            search_keywords=["手机"], attributes={"颜色": "黑色"}, current_version=7, enabled=True,
        ))
        await session.flush()
        session.add(ProductSku(
            id=ids["sku"], product_id=ids["product"], code=f"SKU-{ids['sku'][:8]}", spec={"颜色": "黑色"},
            price=Decimal("100.00"), current_stock=8,
        ))
        session.add_all((
            WorkflowRun(
                id=ids["analysis"], workflow_type=WorkflowType.ANALYSIS, store_id=ids["store"],
                created_by=ids["user"], start_date=date(2026, 8, 1), end_date=date(2026, 8, 1),
                status=WorkflowStatus.COMPLETED, quality_status=WorkflowQuality.NORMAL,
                current_step="product_selected",
            ),
            WorkflowRun(
                id=ids["optimization"], workflow_type=WorkflowType.OPTIMIZATION, store_id=ids["store"],
                created_by=ids["user"], start_date=None, end_date=None, status=WorkflowStatus.ACCEPTED,
                quality_status=WorkflowQuality.NORMAL,
            ),
        ))
        await session.flush()
        metrics = ProductMetrics(
            product_id=ids["product"], product_code=f"t9-{ids['product'][:8]}", impressions=100,
            clicks=20, orders=4, units=4, revenue=Decimal("400.00"), refunds=0,
            ctr=Decimal("0.2000"), conversion_rate=Decimal("0.2000"),
            refund_rate=Decimal("0.0000"), average_order_value=Decimal("100.0000"),
        )
        session.add(AnalysisCandidate(
            id=ids["candidate"], workflow_run_id=ids["analysis"], product_id=ids["product"], rank=1,
            product_code=metrics.product_code or "", anomaly_types=["low_conversion"],
            metrics=metrics.model_dump(mode="json"), business_impact=Decimal("1.00"),
            evidence=["clicks=20"], impact_explanation="测试", reason="测试", recommended_action="测试",
            confidence=Decimal("0.8000"),
        ))
        session.add(ProductProposal(
            id=ids["proposal"], analysis_run_id=ids["analysis"], analysis_candidate_id=ids["candidate"],
            optimization_run_id=ids["optimization"], store_id=ids["store"], product_id=ids["product"],
            base_product_version=7, selection_idempotency_hash="a" * 64,
        ))
        document = KnowledgeDocument(
            id=ids["document"], name="通用规则", category="通用规则", enabled=True,
            created_by=ids["user"], idempotency_key=f"t9-{ids['document']}",
        )
        session.add(document)
        await session.flush()
        version = KnowledgeDocumentVersion(
            id=ids["version"], document_id=document.id, version_number=1,
            sha256=uuid4().hex * 2, original_filename="task9.md", mime_type="text/markdown",
            storage_path="D:/E-commerce_operations_runtime/task9-postgres.md",
            status=KnowledgeVersionStatus.ACTIVE,
        )
        session.add(version)
        await session.flush()
        document.current_version_id = version.id
        session.add(KnowledgeChunk(
            id=ids["chunk"], version_id=version.id, chunk_index=0, chunk_hash=uuid4().hex * 2,
            canonical_text="商品描述应当真实准确。", chunk_metadata={}, token_count=1,
        ))
        await session.commit()
    return ids


def _citation(ids: dict[str, str]) -> CanonicalRuleCitation:
    return CanonicalRuleCitation(
        document_id=ids["document"], version_id=ids["version"], chunk_id=ids["chunk"],
        document_name="通用规则", version_number=1, category="通用规则",
        canonical_text="商品描述应当真实准确。", active=True, applicable=True,
    )


def _trusted(context, citation: CanonicalRuleCitation) -> TrustedOptimizationInput:
    return TrustedOptimizationInput(
        store_id=context.store_id, product_id=context.product_id,
        base_product_version=context.base_product_version, title=context.title, category=context.category,
        brand=context.brand, selling_points=list(context.selling_points), description=context.description,
        search_keywords=list(context.search_keywords), attributes=context.attributes, skus=list(context.skus),
        candidate_metrics=context.candidate_metrics, candidate_evidence=list(context.candidate_evidence),
        rag_quality="normal", canonical_rule_citations=[citation],
    )


def _output(citation: CanonicalRuleCitation) -> OptimizationProposalOutput:
    evidence = [EvidenceRef(kind="citation", value=citation.chunk_id)]
    description = [DescriptionSection(heading="商品详情", body="适合日常使用", evidence=evidence)]
    return OptimizationProposalOutput(
        title="手机", selling_points=["续航持久"], description=description, keywords=["手机"],
        attribute_completions=[],
        changes=[OptimizationChange(
            field="description", current_value="普通手机", suggested_value=description,
            reason="补充商品详情", evidence=evidence,
        )],
        citations=[OutputCitation(chunk_id=citation.chunk_id)], price_suggestions=[], sku_suggestions=[],
    )


def _passing_compliance(citation: CanonicalRuleCitation) -> ComplianceAgentResponse:
    return ComplianceAgentResponse(
        passed=True, risk_level=ComplianceRiskLevel.LOW, violations=[], required_changes=[],
        citations=[OutputCitation(chunk_id=citation.chunk_id)], confidence=Decimal("0.8"), degraded=False,
    )


def _optimization_call(iteration: int = 0) -> OptimizationAgentCallRecord:
    return OptimizationAgentCallRecord(
        node_name="call_product_optimization_agent", call_type=AgentCallType.PRIMARY,
        iteration=iteration, attempt=1, model="deepseek-v4-flash",
        prompt_version="product-optimization-v1", status="success", input_hash="b" * 64,
        prompt_tokens=3, completion_tokens=5, total_tokens=8, duration_ms=1,
        estimated_cost=Decimal("0.000001"), error_code=None,
    )


class _TypedLoader:
    def __init__(self, citation: CanonicalRuleCitation) -> None:
        self.citation = citation
        self.calls = 0

    async def __call__(self, context, before_external_attempt) -> TrustedOptimizationInput:
        self.calls += 1
        await before_external_attempt()
        return _trusted(context, self.citation)


def _transport(citation: CanonicalRuleCitation, requests: list[dict[str, object]]) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        requests.append(payload)
        content = (
            _passing_compliance(citation).model_dump_json()
            if "candidate_output" in payload else _output(citation).model_dump_json()
        )
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        })

    return httpx.MockTransport(handler)


class _SaverProxy(BaseCheckpointSaver):
    def __init__(self, saver: AsyncPostgresSaver) -> None:
        super().__init__(serde=saver.serde)
        self._saver = saver

    @property
    def config_specs(self):
        return self._saver.config_specs

    def get_next_version(self, current, channel):
        return self._saver.get_next_version(current, channel)

    async def aget_tuple(self, *args, **kwargs):
        return await self._saver.aget_tuple(*args, **kwargs)

    async def aput(self, *args, **kwargs):
        return await self._saver.aput(*args, **kwargs)

    async def aput_writes(self, *args, **kwargs):
        return await self._saver.aput_writes(*args, **kwargs)


class _CancelAfterRevisionSaver(_SaverProxy):
    def __init__(self, saver: AsyncPostgresSaver) -> None:
        super().__init__(saver)
        self.cancelled = False

    async def aput(self, config, checkpoint, metadata, new_versions):
        values = checkpoint.get("channel_values", {})
        if values.get("revision_id") and values.get("review_id") is None and not self.cancelled:
            self.cancelled = True
            raise asyncio.CancelledError()
        return await super().aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(self, *args, **kwargs):
        writes = kwargs.get("writes") or args[1]
        if not self.cancelled and any(channel == "revision_id" and value for channel, value in writes):
            self.cancelled = True
            raise asyncio.CancelledError()
        return await super().aput_writes(*args, **kwargs)


class _FailingCheckpointSaver(_SaverProxy):
    def __init__(self, saver: AsyncPostgresSaver, phase: str) -> None:
        super().__init__(saver)
        self.phase = phase

    async def aget_tuple(self, *args, **kwargs):
        if self.phase == "get":
            raise RuntimeError("checkpoint get failed")
        return await super().aget_tuple(*args, **kwargs)

    async def aput(self, *args, **kwargs):
        if self.phase == "put":
            raise RuntimeError("checkpoint put failed")
        return await super().aput(*args, **kwargs)


class _OwnerReplacingCheckpointSaver(_SaverProxy):
    def __init__(self, saver: AsyncPostgresSaver, workflow_run_id: str) -> None:
        super().__init__(saver)
        self.workflow_run_id = workflow_run_id
        self.replaced = False

    async def aget_tuple(self, *args, **kwargs):
        if not self.replaced:
            self.replaced = True
            async with async_session_factory() as replacement:
                await replacement.execute(
                    update(WorkflowRun).where(WorkflowRun.id == self.workflow_run_id).values(
                        lease_owner="replacement-owner"
                    )
                )
                await replacement.commit()
            raise RuntimeError("checkpoint get lost owner")
        return await super().aget_tuple(*args, **kwargs)


async def _record_children(owned: _OwnedIds) -> None:
    async with async_session_factory() as session:
        owned.calls.update((await session.scalars(
            select(AgentCall.id).where(AgentCall.workflow_run_id.in_(owned.optimization_runs))
        )).all())
        owned.reviews.update((await session.scalars(
            select(ComplianceReview.id).where(ComplianceReview.proposal_id.in_(owned.proposals))
        )).all())
        owned.revisions.update((await session.scalars(
            select(ProposalRevision.id).where(ProposalRevision.proposal_id.in_(owned.proposals))
        )).all())


async def _cleanup(owned: _OwnedIds) -> None:
    if not owned.users:
        return
    await _record_children(owned)
    async with async_session_factory() as session:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            if owned.checkpoint_tables_ready and owned.threads:
                await session.execute(
                    text(f"DELETE FROM {table} WHERE thread_id = ANY(:thread_ids)"),
                    {"thread_ids": sorted(owned.threads)},
                )
        if owned.calls:
            await session.execute(delete(AgentCall).where(AgentCall.id.in_(owned.calls)))
        if owned.reviews:
            await session.execute(delete(ComplianceReview).where(ComplianceReview.id.in_(owned.reviews)))
        if owned.proposals:
            await session.execute(
                update(ProductProposal).where(ProductProposal.id.in_(owned.proposals)).values(current_revision_id=None)
            )
        if owned.revisions:
            await session.execute(delete(ProposalRevision).where(ProposalRevision.id.in_(owned.revisions)))
        if owned.proposals:
            await session.execute(delete(ProductProposal).where(ProductProposal.id.in_(owned.proposals)))
        if owned.candidates:
            await session.execute(delete(AnalysisCandidate).where(AnalysisCandidate.id.in_(owned.candidates)))
        if owned.optimization_runs:
            await session.execute(delete(WorkflowRun).where(WorkflowRun.id.in_(owned.optimization_runs)))
        if owned.analysis_runs:
            await session.execute(delete(WorkflowRun).where(WorkflowRun.id.in_(owned.analysis_runs)))
        if owned.skus:
            await session.execute(delete(ProductSku).where(ProductSku.id.in_(owned.skus)))
        if owned.products:
            await session.execute(delete(Product).where(Product.id.in_(owned.products)))
        for user_id, store_id in owned.scopes:
            await session.execute(
                delete(UserStoreScope).where(
                    UserStoreScope.user_id == user_id,
                    UserStoreScope.store_id == store_id,
                )
            )
        if owned.stores:
            await session.execute(delete(Store).where(Store.id.in_(owned.stores)))
        if owned.chunks:
            await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.id.in_(owned.chunks)))
        if owned.documents:
            await session.execute(
                update(KnowledgeDocument).where(KnowledgeDocument.id.in_(owned.documents)).values(current_version_id=None)
            )
        if owned.versions:
            await session.execute(delete(KnowledgeDocumentVersion).where(KnowledgeDocumentVersion.id.in_(owned.versions)))
        if owned.documents:
            await session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id.in_(owned.documents)))
        if owned.users:
            await session.execute(delete(User).where(User.id.in_(owned.users)))
        await session.commit()


async def _claim(owned: _OwnedIds, workflow_run_id: str, lease_owner: str) -> None:
    await _assert_no_external_claimable(owned)
    async with async_session_factory() as session:
        claim = await claim_next_optimization_run(session, lease_owner=lease_owner, lease_seconds=60)
    assert claim is not None and claim.workflow_run_id == workflow_run_id


async def _make_only_claimable(owned: _OwnedIds, workflow_run_id: str) -> None:
    async with async_session_factory() as session:
        await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id.in_(owned.optimization_runs - {workflow_run_id}))
            .values(
                status=WorkflowStatus.FAILED,
                lease_owner=None,
                lease_expires_at=None,
                current_step="failed",
                error_code="OPTIMIZATION_DATABASE_ERROR",
            )
        )
        await session.execute(
            update(WorkflowRun).where(WorkflowRun.id == workflow_run_id).values(
                status=WorkflowStatus.ACCEPTED,
                lease_owner=None,
                lease_expires_at=None,
                current_step=None,
                error_code=None,
            )
        )
        await session.commit()


async def _run_and_read(workflow_run_id: str) -> WorkflowRun:
    async with async_session_factory() as session:
        run = await session.get(WorkflowRun, workflow_run_id, populate_existing=True)
        assert run is not None
        return run


async def _counts(owned: _OwnedIds) -> tuple[int, int, int]:
    async with async_session_factory() as session:
        return (
            await session.scalar(select(func.count(ProposalRevision.id)).where(ProposalRevision.proposal_id.in_(owned.proposals))) or 0,
            await session.scalar(select(func.count(ComplianceReview.id)).where(ComplianceReview.proposal_id.in_(owned.proposals))) or 0,
            await session.scalar(select(func.count(AgentCall.id)).where(AgentCall.workflow_run_id.in_(owned.optimization_runs))) or 0,
        )


async def test_postgres_claims_are_skip_locked_type_isolated_and_stale_owner_is_read_only() -> None:
    if await _external_claimable_run_exists(set()):
        pytest.skip("external optimization rows are claimable")
    owned = _OwnedIds()
    try:
        first, second = await asyncio.gather(
            _seed_chain(owned, f"task9-pg-{uuid4().hex[:10]}-a"),
            _seed_chain(owned, f"task9-pg-{uuid4().hex[:10]}-b"),
        )
        await _assert_no_external_claimable(owned)
        async with async_session_factory() as session_a, async_session_factory() as session_b:
            claims = await asyncio.gather(
                claim_next_optimization_run(session_a, lease_owner="task9-pg-a", lease_seconds=60),
                claim_next_optimization_run(session_b, lease_owner="task9-pg-b", lease_seconds=60),
            )
        assert {claim.workflow_run_id for claim in claims if claim is not None} == {
            first["optimization"], second["optimization"],
        }
        async with async_session_factory() as session:
            stale = await load_owned_optimization_context(
                session, workflow_run_id=first["optimization"], lease_owner="not-the-owner"
            )
        assert stale.disposition == "lease_lost"

        expired = await _seed_chain(owned, f"task9-pg-{uuid4().hex[:10]}-expired")
        exhausted = await _seed_chain(owned, f"task9-pg-{uuid4().hex[:10]}-exhausted")
        async with async_session_factory() as session:
            await session.execute(
                update(WorkflowRun).where(WorkflowRun.id == expired["optimization"]).values(
                    status=WorkflowStatus.PROCESSING, lease_owner="expired-owner",
                    lease_expires_at=func.now() - text("interval '1 second'"), attempt_count=2,
                )
            )
            await session.execute(
                update(WorkflowRun).where(WorkflowRun.id == exhausted["optimization"]).values(
                    status=WorkflowStatus.PROCESSING, lease_owner="exhausted-owner",
                    lease_expires_at=func.now() - text("interval '1 second'"), attempt_count=3,
                )
            )
            await session.commit()
        await _assert_no_external_claimable(owned)
        async with async_session_factory() as session:
            recovered = await claim_next_optimization_run(session, lease_owner="task9-pg-recovery", lease_seconds=60)
            exhausted_run = await session.get(WorkflowRun, exhausted["optimization"], populate_existing=True)
            exhausted_analysis = await session.get(WorkflowRun, exhausted["analysis"], populate_existing=True)
        assert recovered is not None and recovered.workflow_run_id == expired["optimization"] and recovered.attempt_count == 3
        assert exhausted_run is not None and (exhausted_run.status, exhausted_run.error_code) == (
            WorkflowStatus.FAILED, "LEASE_ATTEMPTS_EXHAUSTED"
        )
        assert exhausted_analysis is not None and (
            exhausted_analysis.workflow_type,
            exhausted_analysis.status,
            exhausted_analysis.current_step,
            exhausted_analysis.error_code,
        ) == (
            WorkflowType.ANALYSIS,
            WorkflowStatus.COMPLETED,
            "product_selected",
            None,
        )
    finally:
        await _cleanup(owned)


async def test_postgres_worker_recovers_same_thread_after_revision_checkpoint_cancel_without_duplication() -> None:
    if await _external_claimable_run_exists(set()):
        pytest.skip("external optimization rows are claimable")
    owned = _OwnedIds()
    settings = _settings()
    requests: list[dict[str, object]] = []
    try:
        ids = await _seed_chain(owned, f"task9-pg-{uuid4().hex[:10]}-checkpoint")
        citation = _citation(ids)
        loader = _TypedLoader(citation)
        async with AsyncPostgresSaver.from_conn_string(settings.langgraph_database_url) as saver:
            await saver.setup()
            owned.checkpoint_tables_ready = True
            await _assert_no_external_claimable(owned)
            with pytest.raises(asyncio.CancelledError):
                await optimization_worker.run_once(
                    async_session_factory, settings=settings, lease_owner="checkpoint-owner",
                    checkpointer=_CancelAfterRevisionSaver(saver), trusted_input_loader=loader,
                    transport=_transport(citation, requests), max_agent_attempts=1,
                )
            saved = await saver.aget_tuple({"configurable": {"thread_id": ids["optimization"]}})
            assert saved is not None
            values = saved.checkpoint["channel_values"]
            user_channels = {
                key for key in values
                if key != "__start__" and not key.startswith("branch:") and not key.startswith("start:")
            }
            assert user_channels == _CHECKPOINT_KEYS
            assert await _counts(owned) == (1, 0, 1)
            interrupted = await _run_and_read(ids["optimization"])
            assert (interrupted.status, interrupted.lease_owner) == (WorkflowStatus.PROCESSING, "checkpoint-owner")
            async with async_session_factory() as session:
                await session.execute(
                    update(WorkflowRun).where(WorkflowRun.id == ids["optimization"]).values(
                        lease_expires_at=func.now() - text("interval '1 second'")
                    )
                )
                await session.commit()
            await _assert_no_external_claimable(owned)
            resumed = await optimization_worker.run_once(
                async_session_factory, settings=settings, lease_owner="reclaimed-owner", checkpointer=saver,
                trusted_input_loader=loader, transport=_transport(citation, requests), max_agent_attempts=1,
            )
        assert resumed == ids["optimization"]
        assert ["candidate_output" in request for request in requests] == [False, True]
        assert await _counts(owned) == (1, 1, 2)
        finished = await _run_and_read(ids["optimization"])
        assert (finished.status, finished.quality_status, finished.error_code) == (
            WorkflowStatus.DRAFT_READY, WorkflowQuality.NORMAL, None
        )
    finally:
        await _cleanup(owned)


async def test_postgres_checkpoint_get_put_failures_and_replaced_owner_are_guarded() -> None:
    if await _external_claimable_run_exists(set()):
        pytest.skip("external optimization rows are claimable")
    owned = _OwnedIds()
    settings = _settings()
    try:
        get_ids = await _seed_chain(owned, f"task9-pg-{uuid4().hex[:10]}-get")
        put_ids = await _seed_chain(owned, f"task9-pg-{uuid4().hex[:10]}-put")
        stale_ids = await _seed_chain(owned, f"task9-pg-{uuid4().hex[:10]}-stale")
        async with AsyncPostgresSaver.from_conn_string(settings.langgraph_database_url) as saver:
            await saver.setup()
            owned.checkpoint_tables_ready = True
            for ids, phase in ((get_ids, "get"), (put_ids, "put")):
                await _make_only_claimable(owned, ids["optimization"])
                await _assert_no_external_claimable(owned)
                processed = await optimization_worker.run_once(
                    async_session_factory, settings=settings, lease_owner=f"{phase}-owner",
                    checkpointer=_FailingCheckpointSaver(saver, phase),
                    trusted_input_loader=_TypedLoader(_citation(ids)), max_agent_attempts=1,
                )
                assert processed == ids["optimization"]
                failed = await _run_and_read(ids["optimization"])
                assert (failed.status, failed.error_code, failed.lease_owner) == (
                    WorkflowStatus.FAILED, "OPTIMIZATION_CHECKPOINT_ERROR", None
                )
            await _make_only_claimable(owned, stale_ids["optimization"])
            await _assert_no_external_claimable(owned)
            processed = await optimization_worker.run_once(
                async_session_factory, settings=settings, lease_owner="old-checkpoint-owner",
                checkpointer=_OwnerReplacingCheckpointSaver(saver, stale_ids["optimization"]),
                trusted_input_loader=_TypedLoader(_citation(stale_ids)), max_agent_attempts=1,
            )
            assert processed == stale_ids["optimization"]
        stale = await _run_and_read(stale_ids["optimization"])
        assert (stale.status, stale.lease_owner, stale.error_code) == (
            WorkflowStatus.PROCESSING, "replacement-owner", None
        )
        assert await _counts(owned) == (0, 0, 0)
    finally:
        await _cleanup(owned)


@pytest.mark.parametrize("mutation", ["product_version", "citation_disabled", "citation_current_version"])
async def test_postgres_persistence_rechecks_current_product_and_citation_facts(mutation: str) -> None:
    if await _external_claimable_run_exists(set()):
        pytest.skip("external optimization rows are claimable")
    owned = _OwnedIds()
    try:
        ids = await _seed_chain(owned, f"task9-pg-{uuid4().hex[:10]}-{mutation}")
        await _claim(owned, ids["optimization"], "fact-owner")
        async with async_session_factory() as session:
            loaded = await load_owned_optimization_context(
                session, workflow_run_id=ids["optimization"], lease_owner="fact-owner"
            )
        assert loaded.disposition == "ready" and loaded.context is not None
        citation = _citation(ids)
        async with async_session_factory() as session:
            if mutation == "product_version":
                await session.execute(update(Product).where(Product.id == ids["product"]).values(current_version=8))
            elif mutation == "citation_disabled":
                await session.execute(update(KnowledgeDocument).where(KnowledgeDocument.id == ids["document"]).values(enabled=False))
            else:
                alternate = str(uuid4())
                owned.versions.add(alternate)
                session.add(KnowledgeDocumentVersion(
                    id=alternate, document_id=ids["document"], version_number=2, sha256=uuid4().hex * 2,
                    original_filename="task9-next.md", mime_type="text/markdown",
                    storage_path="D:/E-commerce_operations_runtime/task9-next.md",
                    status=KnowledgeVersionStatus.ACTIVE,
                ))
                await session.flush()
                await session.execute(
                    update(KnowledgeDocument).where(KnowledgeDocument.id == ids["document"]).values(current_version_id=alternate)
                )
            await session.commit()
        async with async_session_factory() as session:
            result = await persist_optimization_revision(
                session, workflow_run_id=ids["optimization"], lease_owner="fact-owner", iteration=0,
                trusted=_trusted(loaded.context, citation), output=_output(citation),
                canonical_citations=[citation], calls=[_optimization_call()],
            )
        expected = "PRODUCT_VERSION_CONFLICT" if mutation == "product_version" else "OPTIMIZATION_FACT_ERROR"
        assert (result.disposition, result.error_code) == ("failed", expected)
        assert await _counts(owned) == (0, 0, 0)
    finally:
        await _cleanup(owned)


async def test_postgres_old_owner_has_zero_context_immutable_and_terminal_writes() -> None:
    if await _external_claimable_run_exists(set()):
        pytest.skip("external optimization rows are claimable")
    owned = _OwnedIds()
    try:
        ids = await _seed_chain(owned, f"task9-pg-{uuid4().hex[:10]}-old-owner")
        async with async_session_factory() as session:
            await session.execute(
                update(WorkflowRun).where(WorkflowRun.id == ids["optimization"]).values(
                    status=WorkflowStatus.PROCESSING, attempt_count=1, lease_owner="replacement-owner",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                )
            )
            await session.commit()
        async with async_session_factory() as session:
            current = await load_owned_optimization_context(
                session, workflow_run_id=ids["optimization"], lease_owner="replacement-owner"
            )
        assert current.disposition == "ready" and current.context is not None
        citation = _citation(ids)
        trusted = _trusted(current.context, citation)
        output = _output(citation)
        deterministic = validate_optimization_output(trusted, output)
        semantic = _passing_compliance(citation)
        async with async_session_factory() as session:
            context = await load_owned_optimization_context(
                session, workflow_run_id=ids["optimization"], lease_owner="old-owner"
            )
            revision = await persist_optimization_revision(
                session, workflow_run_id=ids["optimization"], lease_owner="old-owner", iteration=0,
                trusted=trusted, output=output, canonical_citations=[citation], calls=(),
            )
            normal_review = await persist_compliance_review(
                session, workflow_run_id=ids["optimization"], lease_owner="old-owner", revision_id=str(uuid4()),
                iteration=0, deterministic=deterministic, semantic=semantic, required_changes=[],
                canonical_citations=[citation], calls=(),
            )
            failure_review = await persist_compliance_failure(
                session, workflow_run_id=ids["optimization"], lease_owner="old-owner", revision_id=str(uuid4()),
                iteration=0, deterministic=deterministic, error_code="DEEPSEEK_TIMEOUT", required_changes=[],
                canonical_citations=[citation], calls=(),
            )
            deferred = await defer_optimization_manual(
                session, workflow_run_id=ids["optimization"], lease_owner="old-owner",
                error_code="DEEPSEEK_TIMEOUT", iteration=0,
            )
            finalized = await finalize_optimization_draft(
                session, workflow_run_id=ids["optimization"], lease_owner="old-owner"
            )
            failed = await fail_optimization_run(
                session, workflow_run_id=ids["optimization"], lease_owner="old-owner",
                error_code="OPTIMIZATION_FACT_ERROR",
            )
        assert all(result.disposition == "lease_lost" for result in (
            context, revision, normal_review, failure_review, deferred, finalized, failed,
        ))
        run = await _run_and_read(ids["optimization"])
        assert (run.status, run.lease_owner, run.error_code) == (
            WorkflowStatus.PROCESSING, "replacement-owner", None
        )
        assert await _counts(owned) == (0, 0, 0)
    finally:
        await _cleanup(owned)
