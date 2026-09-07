import asyncio
import importlib.util
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import and_, delete, func, not_, or_, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

import backend.manual_review_runs as manual_runs
import backend.manual_review_worker as manual_worker
from backend.approvals import (
    ApprovalDomainError,
    approve_proposal,
    reject_proposal,
    request_proposal_changes,
    submit_proposal,
)
from backend.common import (
    AgentCallType,
    ApprovalActionType,
    AuditEventType,
    ComplianceRiskLevel,
    KnowledgeVersionStatus,
    ProposalRevisionOrigin,
    UserRole,
    UserStatus,
    WorkflowQuality,
    WorkflowStatus,
    WorkflowType,
)
from backend.compliance_agent import ComplianceAgentCallRecord, ComplianceAgentResponse
from backend.config import Settings
from backend.database import async_session_factory, engine
from backend.manual_review_runs import (
    claim_next_manual_review_run,
    fail_manual_review_run,
    finalize_manual_review,
    load_owned_manual_review_context,
    persist_manual_compliance_review,
)
from backend.manual_reviews import (
    ManualReviewDomainError,
    create_manual_revision,
)
from backend.models import (
    AgentCall,
    AnalysisCandidate,
    ApprovalAction,
    AuditEvent,
    ComplianceReview,
    InventorySnapshot,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    ManualReviewRun,
    Product,
    ProductProposal,
    ProductSku,
    PlatformDelivery,
    PlatformWebhookReceipt,
    ProposalRevision,
    PublishRecord,
    Store,
    User,
    UserStoreScope,
    WorkflowRun,
)
from backend.optimization_validation import validate_optimization_output
from backend.schemas import (
    CanonicalRuleCitation,
    DescriptionSection,
    EvidenceRef,
    ManualRevisionRequest,
    OptimizationChange,
    OptimizationProposalOutput,
    OutputCitation,
    ProductMetrics,
    ProposalActionRequest,
    ProposalCommentActionRequest,
    TrustedOptimizationInput,
    TrustedProductSku,
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


@dataclass
class _OwnedIds:
    threads: set[str] = field(default_factory=set)
    checkpoint_tables_ready: bool = False
    users: set[str] = field(default_factory=set)
    scopes: set[tuple[str, str]] = field(default_factory=set)
    stores: set[str] = field(default_factory=set)
    products: set[str] = field(default_factory=set)
    skus: set[str] = field(default_factory=set)
    inventory: set[str] = field(default_factory=set)
    analysis_runs: set[str] = field(default_factory=set)
    optimization_runs: set[str] = field(default_factory=set)
    manual_workflows: set[str] = field(default_factory=set)
    candidates: set[str] = field(default_factory=set)
    proposals: set[str] = field(default_factory=set)
    revisions: set[str] = field(default_factory=set)
    reviews: set[str] = field(default_factory=set)
    calls: set[str] = field(default_factory=set)
    manual_runs: set[str] = field(default_factory=set)
    actions: set[str] = field(default_factory=set)
    publishes: set[str] = field(default_factory=set)
    deliveries: set[str] = field(default_factory=set)
    receipts: set[str] = field(default_factory=set)
    audits: set[str] = field(default_factory=set)
    documents: set[str] = field(default_factory=set)
    versions: set[str] = field(default_factory=set)
    chunks: set[str] = field(default_factory=set)
    unrelated_before: dict[str, tuple[object, ...]] | None = None


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        jwt_secret_key="task-eleven-postgres-local-jwt-secret-at-least-32",
        deepseek_api_key="mock-transport-only-key",
        deepseek_base_url="https://mock.deepseek.invalid",
        optimization_lease_seconds=60,
    )


def _migration():
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "0005_manual_review_approval_publish.py"
    )
    spec = importlib.util.spec_from_file_location("task11_migration_0005", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _external_claimable_manual_exists(owned_ids: set[str]) -> bool:
    potentially_mutated = or_(
        and_(
            WorkflowRun.status == WorkflowStatus.ACCEPTED,
            WorkflowRun.attempt_count < 3,
        ),
        and_(
            WorkflowRun.status == WorkflowStatus.PROCESSING,
            WorkflowRun.lease_expires_at < func.now(),
        ),
    )
    async with async_session_factory() as session:
        return (
            await session.scalar(
                select(WorkflowRun.id)
                .where(
                    WorkflowRun.workflow_type == WorkflowType.MANUAL_REVIEW,
                    potentially_mutated,
                    not_(WorkflowRun.id.in_(owned_ids)),
                )
                .limit(1)
            )
        ) is not None


async def _assert_no_external_claimable(owned: _OwnedIds) -> None:
    if await _external_claimable_manual_exists(owned.manual_workflows):
        pytest.skip("external manual-review rows may be claimed or exhausted")


def _citation(ids: dict[str, str]) -> CanonicalRuleCitation:
    return CanonicalRuleCitation(
        document_id=ids["document"],
        version_id=ids["version"],
        chunk_id=ids["chunk"],
        document_name="通用规则",
        version_number=1,
        category="通用规则",
        canonical_text="商品文案应有依据。",
        active=True,
        applicable=True,
    )


def _parent_output(ids: dict[str, str]) -> OptimizationProposalOutput:
    evidence = [
        EvidenceRef(kind="fact", value="product.description"),
        EvidenceRef(kind="citation", value=ids["chunk"]),
    ]
    return OptimizationProposalOutput(
        title="原商品标题",
        selling_points=["棉质家居设计"],
        description=[
            DescriptionSection(
                heading="商品详情",
                body="原始详情",
                evidence=evidence,
            )
        ],
        keywords=["家居"],
        attribute_completions=[],
        changes=[],
        citations=[OutputCitation(chunk_id=ids["chunk"])],
        price_suggestions=[],
        sku_suggestions=[],
    )


def _manual_request(
    ids: dict[str, str], *, title: str = "人工修订商品标题"
) -> ManualRevisionRequest:
    description = [
        DescriptionSection(
            heading="商品详情",
            body="人工修订后的商品详情",
            evidence=[
                EvidenceRef(kind="fact", value="product.description"),
                EvidenceRef(kind="citation", value=ids["chunk"]),
            ],
        )
    ]
    return ManualRevisionRequest(
        parent_revision_id=ids["parent"],
        base_product_version=7,
        title=title,
        selling_points=["棉质家居设计"],
        description=description,
        keywords=["家居"],
        attribute_completions=[],
        changes=[
            OptimizationChange(
                field="title",
                current_value="原商品标题",
                suggested_value=title,
                reason="人工修订标题",
                evidence=[EvidenceRef(kind="fact", value="product.title")],
            ),
            OptimizationChange(
                field="description",
                current_value="原始详情",
                suggested_value=description,
                reason="人工修订详情",
                evidence=[EvidenceRef(kind="citation", value=ids["chunk"])],
            ),
        ],
    )


async def _seed_base(
    owned: _OwnedIds, prefix: str, *, role: UserRole = UserRole.SUPERVISOR
) -> dict[str, str]:
    if owned.unrelated_before is None:
        owned.unrelated_before = await _unrelated_snapshot(owned)
    ids = {
        key: str(uuid4())
        for key in (
            "user",
            "store",
            "product",
            "sku",
            "inventory",
            "analysis",
            "candidate",
            "optimization",
            "proposal",
            "parent",
            "parent_review",
            "document",
            "version",
            "chunk",
        )
    }
    owned.users.add(ids["user"])
    owned.stores.add(ids["store"])
    owned.scopes.add((ids["user"], ids["store"]))
    owned.products.add(ids["product"])
    owned.skus.add(ids["sku"])
    owned.inventory.add(ids["inventory"])
    owned.analysis_runs.add(ids["analysis"])
    owned.optimization_runs.add(ids["optimization"])
    owned.candidates.add(ids["candidate"])
    owned.proposals.add(ids["proposal"])
    owned.revisions.add(ids["parent"])
    owned.reviews.add(ids["parent_review"])
    owned.documents.add(ids["document"])
    owned.versions.add(ids["version"])
    owned.chunks.add(ids["chunk"])
    async with async_session_factory() as session:
        session.add_all(
            [
                User(
                    id=ids["user"],
                    username=f"t11-{ids['user'][:12]}",
                    password_hash="task11-postgres",
                    role=role,
                    status=UserStatus.ACTIVE,
                ),
                Store(
                    id=ids["store"],
                    name=f"{prefix}-store",
                    code=f"t11-{ids['store'][:8]}",
                    enabled=True,
                ),
            ]
        )
        await session.flush()
        session.add(UserStoreScope(user_id=ids["user"], store_id=ids["store"]))
        product = Product(
            id=ids["product"],
            store_id=ids["store"],
            code=f"t11-{ids['product'][:8]}",
            title="原商品标题",
            category="家居",
            brand="好物品牌",
            selling_points=["棉质家居设计"],
            description="原始详情",
            search_keywords=["家居"],
            attributes={"材质": "棉"},
            current_version=7,
            enabled=True,
        )
        session.add(product)
        await session.flush()
        session.add(
            ProductSku(
                id=ids["sku"],
                product_id=product.id,
                code=f"SKU-{ids['sku'][:8]}",
                spec={"颜色": "红"},
                price=Decimal("100.00"),
                current_stock=10,
            )
        )
        await session.flush()
        session.add(
            InventorySnapshot(
                id=ids["inventory"],
                store_id=ids["store"],
                sku_id=ids["sku"],
                snapshot_date=date(2026, 8, 31),
                on_hand=10,
                inbound=3,
            )
        )
        analysis = WorkflowRun(
            id=ids["analysis"],
            workflow_type=WorkflowType.ANALYSIS,
            store_id=ids["store"],
            created_by=ids["user"],
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 2),
            status=WorkflowStatus.COMPLETED,
            quality_status=WorkflowQuality.NORMAL,
            current_step="product_selected",
        )
        optimization = WorkflowRun(
            id=ids["optimization"],
            workflow_type=WorkflowType.OPTIMIZATION,
            store_id=ids["store"],
            created_by=ids["user"],
            status=WorkflowStatus.DRAFT_READY,
            quality_status=WorkflowQuality.NORMAL,
            current_step="draft_ready",
            input={
                "proposal_id": ids["proposal"],
                "source_analysis_run_id": ids["analysis"],
                "analysis_candidate_id": ids["candidate"],
                "product_id": ids["product"],
                "store_id": ids["store"],
            },
        )
        session.add_all([analysis, optimization])
        await session.flush()
        metrics = ProductMetrics(
            product_id=ids["product"],
            product_code=product.code,
            impressions=100,
            clicks=20,
            orders=4,
            units=4,
            revenue=Decimal("400.00"),
            refunds=0,
            ctr=Decimal("0.2000"),
            conversion_rate=Decimal("0.2000"),
            refund_rate=Decimal("0.0000"),
            average_order_value=Decimal("100.0000"),
        )
        session.add(
            AnalysisCandidate(
                id=ids["candidate"],
                workflow_run_id=analysis.id,
                product_id=product.id,
                rank=1,
                product_code=product.code,
                anomaly_types=["low_conversion"],
                metrics=metrics.model_dump(mode="json"),
                business_impact=Decimal("100.00"),
                evidence=["orders=4"],
                impact_explanation="影响说明",
                reason="原因",
                recommended_action="建议",
                confidence=Decimal("0.8000"),
            )
        )
        proposal = ProductProposal(
            id=ids["proposal"],
            analysis_run_id=ids["analysis"],
            analysis_candidate_id=ids["candidate"],
            optimization_run_id=ids["optimization"],
            store_id=ids["store"],
            product_id=ids["product"],
            base_product_version=7,
            selection_idempotency_hash=uuid4().hex * 2,
        )
        session.add(proposal)
        await session.flush()
        citation = _citation(ids)
        parent = ProposalRevision(
            id=ids["parent"],
            proposal_id=proposal.id,
            iteration=0,
            revision_number=1,
            origin=ProposalRevisionOrigin.AGENT,
            created_by=ids["user"],
            parent_revision_id=None,
            base_product_version=7,
            trusted_fact_hash="b" * 64,
            proposal_output=_parent_output(ids).model_dump(mode="json"),
            citations=[citation.model_dump(mode="json")],
        )
        session.add(parent)
        await session.flush()
        proposal.current_revision_id = parent.id
        session.add(
            ComplianceReview(
                id=ids["parent_review"],
                proposal_id=proposal.id,
                proposal_revision_id=parent.id,
                iteration=0,
                deterministic_checks={"passed": True, "violations": []},
                semantic_review={"passed": True, "violations": []},
                passed=True,
                risk_level=ComplianceRiskLevel.LOW,
                required_changes=[],
                citations=[citation.model_dump(mode="json")],
                quality_status=WorkflowQuality.NORMAL,
            )
        )
        document = KnowledgeDocument(
            id=ids["document"],
            name="通用规则",
            category="通用规则",
            enabled=True,
            created_by=ids["user"],
            idempotency_key=f"t11-{ids['document']}",
        )
        session.add(document)
        await session.flush()
        version = KnowledgeDocumentVersion(
            id=ids["version"],
            document_id=document.id,
            version_number=1,
            sha256="c" * 64,
            original_filename="task11.txt",
            mime_type="text/plain",
            storage_path="D:/E-commerce_operations_runtime/task11.txt",
            status=KnowledgeVersionStatus.ACTIVE,
        )
        session.add(version)
        await session.flush()
        session.add(
            KnowledgeChunk(
                id=ids["chunk"],
                version_id=version.id,
                chunk_index=0,
                chunk_hash="d" * 64,
                canonical_text="商品文案应有依据。",
                chunk_metadata={},
                token_count=8,
            )
        )
        document.current_version_id = version.id
        await session.commit()
    return ids


async def _record_children(owned: _OwnedIds) -> None:
    if not owned.proposals:
        return
    async with async_session_factory() as session:
        owned.revisions.update(
            (
                await session.scalars(
                    select(ProposalRevision.id).where(
                        ProposalRevision.proposal_id.in_(owned.proposals)
                    )
                )
            ).all()
        )
        owned.reviews.update(
            (
                await session.scalars(
                    select(ComplianceReview.id).where(
                        ComplianceReview.proposal_id.in_(owned.proposals)
                    )
                )
            ).all()
        )
        owned.manual_runs.update(
            (
                await session.scalars(
                    select(ManualReviewRun.id).where(
                        ManualReviewRun.proposal_id.in_(owned.proposals)
                    )
                )
            ).all()
        )
        owned.manual_workflows.update(
            (
                await session.scalars(
                    select(ManualReviewRun.workflow_run_id).where(
                        ManualReviewRun.proposal_id.in_(owned.proposals)
                    )
                )
            ).all()
        )
        owned.threads.update(owned.manual_workflows)
        owned.calls.update(
            (
                await session.scalars(
                    select(AgentCall.id).where(
                        AgentCall.workflow_run_id.in_(owned.manual_workflows)
                    )
                )
            ).all()
        )
        owned.actions.update(
            (
                await session.scalars(
                    select(ApprovalAction.id).where(
                        ApprovalAction.proposal_id.in_(owned.proposals)
                    )
                )
            ).all()
        )
        owned.publishes.update(
            (
                await session.scalars(
                    select(PublishRecord.id).where(
                        PublishRecord.proposal_id.in_(owned.proposals)
                    )
                )
            ).all()
        )
        owned.deliveries.update(
            (
                await session.scalars(
                    select(PlatformDelivery.id).where(
                        PlatformDelivery.publish_record_id.in_(owned.publishes)
                    )
                )
            ).all()
        )
        owned.receipts.update(
            (
                await session.scalars(
                    select(PlatformWebhookReceipt.id).where(
                        PlatformWebhookReceipt.platform_delivery_id.in_(
                            owned.deliveries
                        )
                    )
                )
            ).all()
        )
        owned.audits.update(
            (
                await session.scalars(
                    select(AuditEvent.id).where(AuditEvent.store_id.in_(owned.stores))
                )
            ).all()
        )


async def _checkpoint_rows(
    session, table: str, owned_threads: set[str]
) -> tuple[object, ...]:
    exists = await session.scalar(
        text("SELECT to_regclass(:table_name)"), {"table_name": table}
    )
    if exists is None:
        return ()
    columns = {
        "checkpoints": "thread_id, checkpoint_ns, checkpoint_id",
        "checkpoint_blobs": "thread_id, checkpoint_ns, channel, version",
        "checkpoint_writes": (
            "thread_id, checkpoint_ns, checkpoint_id, task_id, idx"
        ),
    }[table]
    statement = f"SELECT {columns} FROM {table}"
    params: dict[str, object] = {}
    if owned_threads:
        statement += " WHERE NOT (thread_id = ANY(:thread_ids))"
        params["thread_ids"] = sorted(owned_threads)
    statement += f" ORDER BY {columns}"
    return tuple((await session.execute(text(statement), params)).all())


async def _unrelated_snapshot(owned: _OwnedIds) -> dict[str, tuple[object, ...]]:
    model_ids = {
        "users": (User, User.id, owned.users),
        "stores": (Store, Store.id, owned.stores),
        "products": (Product, Product.id, owned.products),
        "skus": (ProductSku, ProductSku.id, owned.skus),
        "inventory": (InventorySnapshot, InventorySnapshot.id, owned.inventory),
        "workflows": (
            WorkflowRun,
            WorkflowRun.id,
            owned.analysis_runs | owned.optimization_runs | owned.manual_workflows,
        ),
        "candidates": (AnalysisCandidate, AnalysisCandidate.id, owned.candidates),
        "proposals": (ProductProposal, ProductProposal.id, owned.proposals),
        "revisions": (ProposalRevision, ProposalRevision.id, owned.revisions),
        "reviews": (ComplianceReview, ComplianceReview.id, owned.reviews),
        "calls": (AgentCall, AgentCall.id, owned.calls),
        "manual_runs": (ManualReviewRun, ManualReviewRun.id, owned.manual_runs),
        "actions": (ApprovalAction, ApprovalAction.id, owned.actions),
        "publishes": (PublishRecord, PublishRecord.id, owned.publishes),
        "deliveries": (PlatformDelivery, PlatformDelivery.id, owned.deliveries),
        "receipts": (
            PlatformWebhookReceipt,
            PlatformWebhookReceipt.id,
            owned.receipts,
        ),
        "audits": (AuditEvent, AuditEvent.id, owned.audits),
        "documents": (KnowledgeDocument, KnowledgeDocument.id, owned.documents),
        "versions": (
            KnowledgeDocumentVersion,
            KnowledgeDocumentVersion.id,
            owned.versions,
        ),
        "chunks": (KnowledgeChunk, KnowledgeChunk.id, owned.chunks),
    }
    snapshot: dict[str, tuple[object, ...]] = {}
    async with async_session_factory() as session:
        for name, (model, column, ids) in model_ids.items():
            statement = select(column)
            if ids:
                statement = statement.where(not_(column.in_(ids)))
            snapshot[name] = tuple(
                sorted((await session.scalars(statement)).all())
            )
        scope_statement = select(UserStoreScope.user_id, UserStoreScope.store_id)
        if owned.scopes:
            scope_statement = scope_statement.where(
                not_(
                    sa.tuple_(UserStoreScope.user_id, UserStoreScope.store_id).in_(
                        owned.scopes
                    )
                )
            )
        snapshot["scopes"] = tuple(
            sorted((await session.execute(scope_statement)).all())
        )
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            snapshot[table] = await _checkpoint_rows(
                session, table, owned.threads
            )
    return snapshot


async def _assert_owned_absent(owned: _OwnedIds) -> None:
    checks = (
        (User, User.id, owned.users),
        (Store, Store.id, owned.stores),
        (Product, Product.id, owned.products),
        (ProductSku, ProductSku.id, owned.skus),
        (InventorySnapshot, InventorySnapshot.id, owned.inventory),
        (
            WorkflowRun,
            WorkflowRun.id,
            owned.analysis_runs | owned.optimization_runs | owned.manual_workflows,
        ),
        (AnalysisCandidate, AnalysisCandidate.id, owned.candidates),
        (ProductProposal, ProductProposal.id, owned.proposals),
        (ProposalRevision, ProposalRevision.id, owned.revisions),
        (ComplianceReview, ComplianceReview.id, owned.reviews),
        (AgentCall, AgentCall.id, owned.calls),
        (ManualReviewRun, ManualReviewRun.id, owned.manual_runs),
        (ApprovalAction, ApprovalAction.id, owned.actions),
        (PublishRecord, PublishRecord.id, owned.publishes),
        (PlatformDelivery, PlatformDelivery.id, owned.deliveries),
        (PlatformWebhookReceipt, PlatformWebhookReceipt.id, owned.receipts),
        (AuditEvent, AuditEvent.id, owned.audits),
        (KnowledgeDocument, KnowledgeDocument.id, owned.documents),
        (
            KnowledgeDocumentVersion,
            KnowledgeDocumentVersion.id,
            owned.versions,
        ),
        (KnowledgeChunk, KnowledgeChunk.id, owned.chunks),
    )
    async with async_session_factory() as session:
        for model, column, ids in checks:
            if ids:
                assert int(
                    await session.scalar(
                        select(func.count()).select_from(model).where(column.in_(ids))
                    )
                    or 0
                ) == 0
        for user_id, store_id in owned.scopes:
            assert (
                await session.scalar(
                    select(UserStoreScope.user_id).where(
                        UserStoreScope.user_id == user_id,
                        UserStoreScope.store_id == store_id,
                    )
                )
            ) is None
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            exists = await session.scalar(
                text("SELECT to_regclass(:table_name)"), {"table_name": table}
            )
            if exists is not None and owned.threads:
                assert int(
                    await session.scalar(
                        text(
                            f"SELECT count(*) FROM {table} "
                            "WHERE thread_id = ANY(:thread_ids)"
                        ),
                        {"thread_ids": sorted(owned.threads)},
                    )
                    or 0
                ) == 0


async def _cleanup(owned: _OwnedIds) -> None:
    await _record_children(owned)
    async with async_session_factory() as session:
        if owned.checkpoint_tables_ready and owned.threads:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                await session.execute(
                    text(f"DELETE FROM {table} WHERE thread_id = ANY(:thread_ids)"),
                    {"thread_ids": sorted(owned.threads)},
                )
        if owned.audits:
            await session.execute(delete(AuditEvent).where(AuditEvent.id.in_(owned.audits)))
        if owned.receipts:
            await session.execute(
                delete(PlatformWebhookReceipt).where(
                    PlatformWebhookReceipt.id.in_(owned.receipts)
                )
            )
        if owned.deliveries:
            await session.execute(
                delete(PlatformDelivery).where(
                    PlatformDelivery.id.in_(owned.deliveries)
                )
            )
        if owned.publishes:
            await session.execute(
                delete(PublishRecord).where(PublishRecord.id.in_(owned.publishes))
            )
        if owned.actions:
            await session.execute(
                delete(ApprovalAction).where(ApprovalAction.id.in_(owned.actions))
            )
        if owned.calls:
            await session.execute(delete(AgentCall).where(AgentCall.id.in_(owned.calls)))
        if owned.reviews:
            await session.execute(
                delete(ComplianceReview).where(ComplianceReview.id.in_(owned.reviews))
            )
        if owned.proposals:
            await session.execute(
                update(ProductProposal)
                .where(ProductProposal.id.in_(owned.proposals))
                .values(
                    current_revision_id=None,
                    submitted_revision_id=None,
                    active_manual_review_run_id=None,
                )
            )
        if owned.manual_runs:
            await session.execute(
                delete(ManualReviewRun).where(ManualReviewRun.id.in_(owned.manual_runs))
            )
        if owned.revisions:
            await session.execute(
                delete(ProposalRevision).where(ProposalRevision.id.in_(owned.revisions))
            )
        if owned.proposals:
            await session.execute(
                delete(ProductProposal).where(ProductProposal.id.in_(owned.proposals))
            )
        if owned.candidates:
            await session.execute(
                delete(AnalysisCandidate).where(AnalysisCandidate.id.in_(owned.candidates))
            )
        workflow_ids = (
            owned.manual_workflows | owned.optimization_runs | owned.analysis_runs
        )
        if workflow_ids:
            await session.execute(delete(WorkflowRun).where(WorkflowRun.id.in_(workflow_ids)))
        if owned.inventory:
            await session.execute(
                delete(InventorySnapshot).where(InventorySnapshot.id.in_(owned.inventory))
            )
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
        if owned.chunks:
            await session.execute(
                delete(KnowledgeChunk).where(KnowledgeChunk.id.in_(owned.chunks))
            )
        if owned.documents:
            await session.execute(
                update(KnowledgeDocument)
                .where(KnowledgeDocument.id.in_(owned.documents))
                .values(current_version_id=None)
            )
        if owned.versions:
            await session.execute(
                delete(KnowledgeDocumentVersion).where(
                    KnowledgeDocumentVersion.id.in_(owned.versions)
                )
            )
        if owned.documents:
            await session.execute(
                delete(KnowledgeDocument).where(KnowledgeDocument.id.in_(owned.documents))
            )
        if owned.stores:
            await session.execute(delete(Store).where(Store.id.in_(owned.stores)))
        if owned.users:
            await session.execute(delete(User).where(User.id.in_(owned.users)))
        await session.commit()
    await _assert_owned_absent(owned)
    assert owned.unrelated_before is not None
    assert await _unrelated_snapshot(owned) == owned.unrelated_before


async def _create_manual(
    owned: _OwnedIds,
    ids: dict[str, str],
    *,
    key: str,
    title: str = "人工修订商品标题",
):
    async with async_session_factory() as session:
        result = await create_manual_revision(
            session,
            actor_id=ids["user"],
            proposal_id=ids["proposal"],
            request=_manual_request(ids, title=title),
            idempotency_key=key,
            request_id=f"request-{uuid4().hex[:16]}",
        )
    ids["manual_revision"] = result.revision_id
    ids["manual_workflow"] = result.workflow_run_id
    owned.revisions.add(result.revision_id)
    owned.manual_workflows.add(result.workflow_run_id)
    owned.threads.add(result.workflow_run_id)
    await _record_children(owned)
    async with async_session_factory() as session:
        manual_id = await session.scalar(
            select(ManualReviewRun.id).where(
                ManualReviewRun.workflow_run_id == result.workflow_run_id
            )
        )
    assert manual_id is not None
    ids["manual_run"] = manual_id
    owned.manual_runs.add(manual_id)
    return result


def _trusted(context) -> TrustedOptimizationInput:
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
        rag_quality="normal",
        canonical_rule_citations=list(context.canonical_citations),
    )


def _passing_response(context) -> ComplianceAgentResponse:
    return ComplianceAgentResponse(
        passed=True,
        risk_level=ComplianceRiskLevel.LOW,
        violations=[],
        required_changes=[],
        citations=[
            OutputCitation(chunk_id=context.canonical_citations[0].chunk_id)
        ],
        confidence=Decimal("0.9000"),
        degraded=False,
    )


def _compliance_calls() -> list[ComplianceAgentCallRecord]:
    return [
        ComplianceAgentCallRecord(
            node_name="call_product_compliance_agent",
            call_type=AgentCallType.PRIMARY,
            iteration=0,
            attempt=1,
            model="deepseek-v4-flash",
            prompt_version="product-compliance-v1",
            status="success",
            input_hash="4" * 64,
            prompt_tokens=3,
            completion_tokens=5,
            total_tokens=8,
            duration_ms=10,
            estimated_cost=Decimal("0.000001"),
            error_code=None,
        )
    ]


async def _claim(
    owned: _OwnedIds, workflow_run_id: str, lease_owner: str
):
    await _assert_no_external_claimable(owned)
    async with async_session_factory() as session:
        claim = await claim_next_manual_review_run(
            session, lease_owner=lease_owner, lease_seconds=60
        )
    assert claim is not None and claim.workflow_run_id == workflow_run_id
    return claim


async def _load_ready(workflow_run_id: str, lease_owner: str):
    async with async_session_factory() as session:
        loaded = await load_owned_manual_review_context(
            session,
            workflow_run_id=workflow_run_id,
            lease_owner=lease_owner,
        )
    assert loaded.disposition == "ready" and loaded.context is not None
    return loaded.context


async def _persist_pass(workflow_run_id: str, lease_owner: str):
    context = await _load_ready(workflow_run_id, lease_owner)
    trusted = _trusted(context)
    deterministic = validate_optimization_output(trusted, context.proposal_output)
    async with async_session_factory() as session:
        result = await persist_manual_compliance_review(
            session,
            workflow_run_id=workflow_run_id,
            lease_owner=lease_owner,
            trusted=trusted,
            deterministic=deterministic,
            response=_passing_response(context),
            calls=_compliance_calls(),
            error_code=None,
        )
    return result, context, trusted, deterministic


class _TrustedLoader:
    def __init__(self) -> None:
        self.requests = 0

    async def __call__(self, context, before_external) -> TrustedOptimizationInput:
        await before_external()
        return _trusted(context)


def _transport(requests: list[dict[str, object]]) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        requests.append(payload)
        chunk_id = payload["candidate_output"]["citations"][0]["chunk_id"]
        response = ComplianceAgentResponse(
            passed=True,
            risk_level=ComplianceRiskLevel.LOW,
            violations=[],
            required_changes=[],
            citations=[OutputCitation(chunk_id=chunk_id)],
            confidence=Decimal("0.9000"),
            degraded=False,
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": response.model_dump_json()}}],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 5,
                    "total_tokens": 8,
                },
            },
        )

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


class _CancelAfterReviewSaver(_SaverProxy):
    def __init__(self, saver: AsyncPostgresSaver, revision_id: str) -> None:
        super().__init__(saver)
        self.revision_id = revision_id
        self.cancelled = False

    async def _cancel_if_review_exists(self) -> None:
        if self.cancelled:
            return
        async with async_session_factory() as session:
            review_id = await session.scalar(
                select(ComplianceReview.id).where(
                    ComplianceReview.proposal_revision_id == self.revision_id,
                    ComplianceReview.iteration.is_(None),
                )
            )
        if review_id is not None:
            self.cancelled = True
            raise asyncio.CancelledError()

    async def aput(self, *args, **kwargs):
        await self._cancel_if_review_exists()
        return await super().aput(*args, **kwargs)

    async def aput_writes(self, *args, **kwargs):
        await self._cancel_if_review_exists()
        return await super().aput_writes(*args, **kwargs)


class _FailingSaver(_SaverProxy):
    def __init__(self, saver: AsyncPostgresSaver, method: str) -> None:
        super().__init__(saver)
        self.method = method

    async def aget_tuple(self, *args, **kwargs):
        if self.method == "aget_tuple":
            raise RuntimeError("checkpoint read failed")
        return await super().aget_tuple(*args, **kwargs)

    async def aput(self, *args, **kwargs):
        if self.method == "aput":
            raise RuntimeError("checkpoint put failed")
        return await super().aput(*args, **kwargs)

    async def aput_writes(self, *args, **kwargs):
        if self.method == "aput_writes":
            raise RuntimeError("checkpoint writes failed")
        return await super().aput_writes(*args, **kwargs)


class _OwnerReplacingSaver(_SaverProxy):
    def __init__(self, saver: AsyncPostgresSaver, workflow_run_id: str) -> None:
        super().__init__(saver)
        self.workflow_run_id = workflow_run_id
        self.replaced = False

    async def aget_tuple(self, *args, **kwargs):
        if not self.replaced:
            self.replaced = True
            async with async_session_factory() as session:
                await session.execute(
                    update(WorkflowRun)
                    .where(WorkflowRun.id == self.workflow_run_id)
                    .values(
                        lease_owner="replacement-owner",
                        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                    )
                )
                await session.commit()
            raise RuntimeError("checkpoint read lost owner")
        return await super().aget_tuple(*args, **kwargs)


async def _workflow_snapshot(ids: dict[str, str]) -> tuple[object, ...]:
    async with async_session_factory() as session:
        manual = await session.get(WorkflowRun, ids["manual_workflow"])
        original = await session.get(WorkflowRun, ids["optimization"])
        proposal = await session.get(ProductProposal, ids["proposal"])
        assert manual is not None and original is not None and proposal is not None
        return (
            manual.status,
            manual.quality_status,
            manual.current_step,
            manual.error_code,
            manual.lease_owner,
            manual.lease_expires_at,
            manual.attempt_count,
            original.status,
            original.quality_status,
            original.current_step,
            original.error_code,
            proposal.active_manual_review_run_id,
            int(
                await session.scalar(
                    select(func.count(ComplianceReview.id)).where(
                        ComplianceReview.proposal_revision_id
                        == ids["manual_revision"]
                    )
                )
                or 0
            ),
            int(
                await session.scalar(
                    select(func.count(AgentCall.id)).where(
                        AgentCall.workflow_run_id == ids["manual_workflow"]
                    )
                )
                or 0
            ),
            int(
                await session.scalar(
                    select(func.count(AuditEvent.id)).where(
                        AuditEvent.workflow_run_id == ids["manual_workflow"]
                    )
                )
                or 0
            ),
        )


async def _prepare_draft(
    owned: _OwnedIds, prefix: str
) -> dict[str, str]:
    ids = await _seed_base(owned, prefix)
    await _create_manual(owned, ids, key=f"{prefix}-manual")
    await _claim(owned, ids["manual_workflow"], f"{prefix}-owner")
    persisted, _, _, _ = await _persist_pass(
        ids["manual_workflow"], f"{prefix}-owner"
    )
    assert persisted.disposition == "created"
    async with async_session_factory() as session:
        terminal = await finalize_manual_review(
            session,
            workflow_run_id=ids["manual_workflow"],
            lease_owner=f"{prefix}-owner",
        )
    assert terminal.disposition == "draft_ready"
    await _record_children(owned)
    return ids


async def _submit(
    ids: dict[str, str], *, key: str
):
    request = ProposalActionRequest(revision_id=ids["manual_revision"])
    async with async_session_factory() as session:
        result = await submit_proposal(
            session,
            actor_id=ids["user"],
            proposal_id=ids["proposal"],
            request=request,
            idempotency_key=key,
            request_id=f"request-{uuid4().hex[:16]}",
        )
    return result


async def _chain_snapshot(ids: dict[str, str]) -> dict[str, object]:
    async with async_session_factory() as session:
        product = await session.get(Product, ids["product"])
        proposal = await session.get(ProductProposal, ids["proposal"])
        original = await session.get(WorkflowRun, ids["optimization"])
        assert product is not None and proposal is not None and original is not None
        manual_rows = (
            await session.execute(
                select(
                    ManualReviewRun.id,
                    ManualReviewRun.workflow_run_id,
                    ManualReviewRun.proposal_revision_id,
                    WorkflowRun.status,
                    WorkflowRun.quality_status,
                    WorkflowRun.current_step,
                    WorkflowRun.error_code,
                    WorkflowRun.lease_owner,
                    WorkflowRun.lease_expires_at,
                    WorkflowRun.attempt_count,
                )
                .join(
                    WorkflowRun,
                    WorkflowRun.id == ManualReviewRun.workflow_run_id,
                )
                .where(ManualReviewRun.proposal_id == ids["proposal"])
                .order_by(ManualReviewRun.id)
            )
        ).all()
        revisions = (
            await session.execute(
                select(
                    ProposalRevision.id,
                    ProposalRevision.revision_number,
                    ProposalRevision.origin,
                    ProposalRevision.parent_revision_id,
                    ProposalRevision.trusted_fact_hash,
                )
                .where(ProposalRevision.proposal_id == ids["proposal"])
                .order_by(ProposalRevision.revision_number, ProposalRevision.id)
            )
        ).all()
        reviews = (
            await session.execute(
                select(
                    ComplianceReview.id,
                    ComplianceReview.proposal_revision_id,
                    ComplianceReview.passed,
                    ComplianceReview.quality_status,
                    ComplianceReview.error_code,
                )
                .where(ComplianceReview.proposal_id == ids["proposal"])
                .order_by(ComplianceReview.id)
            )
        ).all()
        calls = (
            await session.execute(
                select(
                    AgentCall.id,
                    AgentCall.workflow_run_id,
                    AgentCall.call_type,
                    AgentCall.attempt,
                    AgentCall.status,
                    AgentCall.error_code,
                )
                .where(
                    AgentCall.workflow_run_id.in_(
                        select(ManualReviewRun.workflow_run_id).where(
                            ManualReviewRun.proposal_id == ids["proposal"]
                        )
                    )
                )
                .order_by(AgentCall.id)
            )
        ).all()
        actions = (
            await session.execute(
                select(
                    ApprovalAction.id,
                    ApprovalAction.proposal_revision_id,
                    ApprovalAction.action,
                    ApprovalAction.comment,
                    ApprovalAction.request_hash,
                )
                .where(ApprovalAction.proposal_id == ids["proposal"])
                .order_by(ApprovalAction.id)
            )
        ).all()
        publishes = (
            await session.execute(
                select(
                    PublishRecord.id,
                    PublishRecord.proposal_revision_id,
                    PublishRecord.approval_action_id,
                    PublishRecord.base_product_version,
                    PublishRecord.published_product_version,
                )
                .where(PublishRecord.proposal_id == ids["proposal"])
                .order_by(PublishRecord.id)
            )
        ).all()
        audits = (
            await session.execute(
                select(
                    AuditEvent.id,
                    AuditEvent.event_type,
                    AuditEvent.outcome,
                    AuditEvent.error_code,
                    AuditEvent.approval_action_id,
                    AuditEvent.publish_record_id,
                    AuditEvent.details,
                )
                .where(AuditEvent.proposal_id == ids["proposal"])
                .order_by(AuditEvent.id)
            )
        ).all()
        return {
            "product": (
                product.store_id,
                product.title,
                tuple(product.selling_points),
                product.description,
                tuple(product.search_keywords),
                dict(product.attributes),
                product.current_version,
            ),
            "proposal": (
                proposal.current_revision_id,
                proposal.submitted_revision_id,
                proposal.active_manual_review_run_id,
                proposal.base_product_version,
            ),
            "original": (
                original.store_id,
                original.status,
                original.quality_status,
                original.current_step,
                original.error_code,
                original.lease_owner,
                original.lease_expires_at,
                original.attempt_count,
            ),
            "manual_rows": tuple(manual_rows),
            "revisions": tuple(revisions),
            "reviews": tuple(reviews),
            "calls": tuple(calls),
            "actions": tuple(actions),
            "publishes": tuple(publishes),
            "audits": tuple(
                (*row[:-1], json.dumps(row[-1], sort_keys=True, ensure_ascii=False))
                for row in audits
            ),
        }


async def _fresh_guarded_replay(
    owned: _OwnedIds, ids: dict[str, str], invocation
) -> None:
    foreign_store = str(uuid4())
    owned.stores.add(foreign_store)
    async with async_session_factory() as session:
        session.add(
            Store(
                id=foreign_store,
                name="Task 11 replay ownership guard",
                code=f"t11-{foreign_store[:8]}",
                enabled=True,
            )
        )
        await session.commit()

    async def mutate(case: str) -> None:
        async with async_session_factory() as session:
            if case == "actor":
                await session.execute(
                    update(User)
                    .where(User.id == ids["user"])
                    .values(status=UserStatus.DISABLED)
                )
            elif case == "store":
                await session.execute(
                    update(Store)
                    .where(Store.id == ids["store"])
                    .values(enabled=False)
                )
            elif case == "scope":
                await session.execute(
                    delete(UserStoreScope).where(
                        UserStoreScope.user_id == ids["user"],
                        UserStoreScope.store_id == ids["store"],
                    )
                )
            else:
                await session.execute(
                    update(Product)
                    .where(Product.id == ids["product"])
                    .values(store_id=foreign_store)
                )
            await session.commit()

    async def restore(case: str) -> None:
        async with async_session_factory() as session:
            if case == "actor":
                await session.execute(
                    update(User)
                    .where(User.id == ids["user"])
                    .values(status=UserStatus.ACTIVE)
                )
            elif case == "store":
                await session.execute(
                    update(Store)
                    .where(Store.id == ids["store"])
                    .values(enabled=True)
                )
            elif case == "scope":
                session.add(
                    UserStoreScope(
                        user_id=ids["user"], store_id=ids["store"]
                    )
                )
            else:
                await session.execute(
                    update(Product)
                    .where(Product.id == ids["product"])
                    .values(store_id=ids["store"])
                )
            await session.commit()

    for case in ("actor", "store", "scope", "ownership"):
        await mutate(case)
        mutated = await _chain_snapshot(ids)
        try:
            with pytest.raises(
                (ManualReviewDomainError, ApprovalDomainError)
            ) as denied:
                await invocation()
            assert (denied.value.code, denied.value.status_code) == (
                "PROPOSAL_NOT_FOUND",
                404,
            )
            assert await _chain_snapshot(ids) == mutated
        finally:
            await restore(case)


async def test_postgres_manual_claim_skip_locked_recovery_and_exhaustion_are_atomic(
    monkeypatch,
) -> None:
    if await _external_claimable_manual_exists(set()):
        pytest.skip("external manual-review rows are claimable")
    owned = _OwnedIds()
    try:
        first = await _seed_base(owned, f"task11-{uuid4().hex[:8]}-first")
        second = await _seed_base(owned, f"task11-{uuid4().hex[:8]}-second")
        expired = await _seed_base(owned, f"task11-{uuid4().hex[:8]}-expired")
        exhausted = await _seed_base(owned, f"task11-{uuid4().hex[:8]}-exhausted")
        for ids, key in (
            (first, "claim-first"),
            (second, "claim-second"),
            (expired, "claim-expired"),
            (exhausted, "claim-exhausted"),
        ):
            await _create_manual(owned, ids, key=key)
        async with async_session_factory() as session:
            analysis_control = str(uuid4())
            optimization_control = str(uuid4())
            owned.analysis_runs.add(analysis_control)
            owned.optimization_runs.add(optimization_control)
            session.add_all(
                [
                    WorkflowRun(
                        id=analysis_control,
                        workflow_type=WorkflowType.ANALYSIS,
                        store_id=first["store"],
                        created_by=first["user"],
                        start_date=date(2026, 8, 1),
                        end_date=date(2026, 8, 1),
                        status=WorkflowStatus.ACCEPTED,
                        quality_status=WorkflowQuality.NORMAL,
                    ),
                    WorkflowRun(
                        id=optimization_control,
                        workflow_type=WorkflowType.OPTIMIZATION,
                        store_id=first["store"],
                        created_by=first["user"],
                        status=WorkflowStatus.ACCEPTED,
                        quality_status=WorkflowQuality.NORMAL,
                    ),
                ]
            )
            await session.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == expired["manual_workflow"])
                .values(
                    status=WorkflowStatus.PROCESSING,
                    lease_owner="expired-owner",
                    lease_expires_at=func.now() - text("interval '1 second'"),
                    attempt_count=2,
                )
            )
            await session.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == exhausted["manual_workflow"])
                .values(
                    status=WorkflowStatus.PROCESSING,
                    lease_owner="exhausted-owner",
                    lease_expires_at=func.now() - text("interval '1 second'"),
                    attempt_count=3,
                )
            )
            await session.commit()
        await _assert_no_external_claimable(owned)
        async with async_session_factory() as session_a, async_session_factory() as session_b:
            claims = await asyncio.gather(
                claim_next_manual_review_run(
                    session_a, lease_owner="claim-owner-a", lease_seconds=60
                ),
                claim_next_manual_review_run(
                    session_b, lease_owner="claim-owner-b", lease_seconds=60
                ),
            )
        claim_ids = {claim.workflow_run_id for claim in claims if claim is not None}
        assert len(claim_ids) == 2
        assert claim_ids <= {
            first["manual_workflow"],
            second["manual_workflow"],
            expired["manual_workflow"],
        }
        await _assert_no_external_claimable(owned)
        async with async_session_factory() as session:
            third = await claim_next_manual_review_run(
                session, lease_owner="claim-owner-c", lease_seconds=60
            )
        assert third is not None
        assert {
            *claim_ids,
            third.workflow_run_id,
        } == {
            first["manual_workflow"],
            second["manual_workflow"],
            expired["manual_workflow"],
        }
        assert third.attempt_count == (
            3 if third.workflow_run_id == expired["manual_workflow"] else 1
        )
        async with async_session_factory() as session:
            exhausted_manual = await session.get(
                WorkflowRun, exhausted["manual_workflow"]
            )
            exhausted_original = await session.get(
                WorkflowRun, exhausted["optimization"]
            )
            exhausted_proposal = await session.get(
                ProductProposal, exhausted["proposal"]
            )
            controls = {
                run.id: (run.workflow_type, run.status, run.error_code)
                for run in (
                    await session.scalars(
                        select(WorkflowRun).where(
                            WorkflowRun.id.in_([analysis_control, optimization_control])
                        )
                    )
                ).all()
            }
            failed_audits = list(
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.workflow_run_id == exhausted["manual_workflow"],
                        AuditEvent.event_type == AuditEventType.MANUAL_REVIEW_FAILED,
                    )
                )
            )
        assert exhausted_manual is not None and exhausted_original is not None
        assert exhausted_proposal is not None
        assert (
            exhausted_manual.status,
            exhausted_manual.quality_status,
            exhausted_manual.error_code,
            exhausted_manual.lease_owner,
            exhausted_manual.lease_expires_at,
        ) == (
            WorkflowStatus.FAILED,
            WorkflowQuality.DEGRADED,
            "LEASE_ATTEMPTS_EXHAUSTED",
            None,
            None,
        )
        assert (
            exhausted_original.status,
            exhausted_original.quality_status,
            exhausted_original.current_step,
            exhausted_original.error_code,
            exhausted_original.lease_owner,
            exhausted_original.lease_expires_at,
        ) == (
            WorkflowStatus.FAILED,
            WorkflowQuality.DEGRADED,
            "manual_review_failed",
            "LEASE_ATTEMPTS_EXHAUSTED",
            None,
            None,
        )
        assert exhausted_proposal.active_manual_review_run_id is None
        assert len(failed_audits) == 1
        assert (
            failed_audits[0].proposal_id,
            failed_audits[0].proposal_revision_id,
            failed_audits[0].workflow_run_id,
            failed_audits[0].error_code,
            failed_audits[0].outcome.value,
            failed_audits[0].details,
        ) == (
            exhausted["proposal"],
            exhausted["manual_revision"],
            exhausted["manual_workflow"],
            "LEASE_ATTEMPTS_EXHAUSTED",
            "failed",
            {
                "from_status": "processing",
                "to_status": "failed",
                "workflow_type": "manual_review",
                "quality_status": "degraded",
                "current_step": "failed",
            },
        )
        assert controls == {
            analysis_control: (
                WorkflowType.ANALYSIS,
                WorkflowStatus.ACCEPTED,
                None,
            ),
            optimization_control: (
                WorkflowType.OPTIMIZATION,
                WorkflowStatus.ACCEPTED,
                None,
            ),
        }

        rollback = await _seed_base(owned, f"task11-{uuid4().hex[:8]}-rollback")
        await _create_manual(owned, rollback, key="claim-rollback")
        async with async_session_factory() as session:
            await session.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == rollback["manual_workflow"])
                .values(
                    status=WorkflowStatus.PROCESSING,
                    lease_owner="rollback-owner",
                    lease_expires_at=func.now() - text("interval '1 second'"),
                    attempt_count=3,
                )
            )
            await session.commit()
        before = await _workflow_snapshot(rollback)

        def fail_audit(*_args, **_kwargs):
            raise SQLAlchemyError("injected exhaustion audit failure")

        monkeypatch.setattr(manual_runs, "add_audit_event", fail_audit)
        await _assert_no_external_claimable(owned)
        async with async_session_factory() as session:
            with pytest.raises(
                SQLAlchemyError, match="injected exhaustion audit failure"
            ):
                await claim_next_manual_review_run(
                    session, lease_owner="unused-owner", lease_seconds=60
                )
        assert await _workflow_snapshot(rollback) == before
    finally:
        await _cleanup(owned)


async def test_postgres_manual_worker_resumes_same_thread_after_review_checkpoint_cancel(
) -> None:
    if await _external_claimable_manual_exists(set()):
        pytest.skip("external manual-review rows are claimable")
    owned = _OwnedIds()
    try:
        ids = await _seed_base(owned, f"task11-{uuid4().hex[:8]}-resume")
        await _create_manual(owned, ids, key="worker-resume")
        requests: list[dict[str, object]] = []
        async with AsyncPostgresSaver.from_conn_string(
            _settings().langgraph_database_url
        ) as saver:
            await saver.setup()
            owned.checkpoint_tables_ready = True
            boundary = _CancelAfterReviewSaver(saver, ids["manual_revision"])
            await _assert_no_external_claimable(owned)
            with pytest.raises(asyncio.CancelledError):
                await manual_worker.run_once(
                    async_session_factory,
                    settings=_settings(),
                    lease_owner="checkpoint-owner-a",
                    checkpointer=boundary,
                    trusted_input_loader=_TrustedLoader(),
                    transport=_transport(requests),
                )
            saved = await saver.aget_tuple(
                {"configurable": {"thread_id": ids["manual_workflow"]}}
            )
            assert saved is not None and boundary.cancelled
            async with async_session_factory() as session:
                review_count = int(
                    await session.scalar(
                        select(func.count(ComplianceReview.id)).where(
                            ComplianceReview.proposal_revision_id
                            == ids["manual_revision"]
                        )
                    )
                    or 0
                )
                call_count = int(
                    await session.scalar(
                        select(func.count(AgentCall.id)).where(
                            AgentCall.workflow_run_id == ids["manual_workflow"]
                        )
                    )
                    or 0
                )
                await session.execute(
                    update(WorkflowRun)
                    .where(WorkflowRun.id == ids["manual_workflow"])
                    .values(lease_expires_at=func.now() - text("interval '1 second'"))
                )
                await session.commit()
            assert (review_count, call_count, len(requests)) == (1, 1, 1)
            resumed_requests: list[dict[str, object]] = []
            await _assert_no_external_claimable(owned)
            processed = await manual_worker.run_once(
                async_session_factory,
                settings=_settings(),
                lease_owner="checkpoint-owner-b",
                checkpointer=saver,
                trusted_input_loader=_TrustedLoader(),
                transport=_transport(resumed_requests),
            )
        assert processed == ids["manual_workflow"]
        assert resumed_requests == []
        async with async_session_factory() as session:
            manual = await session.get(WorkflowRun, ids["manual_workflow"])
            original = await session.get(WorkflowRun, ids["optimization"])
            proposal = await session.get(ProductProposal, ids["proposal"])
            calls = list(
                await session.scalars(
                    select(AgentCall).where(
                        AgentCall.workflow_run_id == ids["manual_workflow"]
                    )
                )
            )
            completed_audits = int(
                await session.scalar(
                    select(func.count(AuditEvent.id)).where(
                        AuditEvent.workflow_run_id == ids["manual_workflow"],
                        AuditEvent.event_type
                        == AuditEventType.MANUAL_REVIEW_COMPLETED,
                    )
                )
                or 0
            )
        assert manual is not None and original is not None and proposal is not None
        assert (manual.status, original.status, proposal.active_manual_review_run_id) == (
            WorkflowStatus.COMPLETED,
            WorkflowStatus.DRAFT_READY,
            None,
        )
        assert [
            (call.call_type, call.iteration, call.attempt) for call in calls
        ] == [(AgentCallType.PRIMARY, 0, 1)]
        assert completed_audits == 1
    finally:
        await _cleanup(owned)


@pytest.mark.parametrize(
    "checkpoint_case", ["aget_tuple", "aput", "aput_writes", "owner"]
)
async def test_postgres_checkpoint_failure_and_owner_replacement_are_guarded(
    checkpoint_case: str,
) -> None:
    if await _external_claimable_manual_exists(set()):
        pytest.skip("external manual-review rows are claimable")
    owned = _OwnedIds()
    try:
        ids = await _seed_base(
            owned, f"task11-{uuid4().hex[:8]}-{checkpoint_case}"
        )
        await _create_manual(owned, ids, key=f"checkpoint-{checkpoint_case}")
        async with AsyncPostgresSaver.from_conn_string(
            _settings().langgraph_database_url
        ) as saver:
            await saver.setup()
            owned.checkpoint_tables_ready = True
            boundary = (
                _OwnerReplacingSaver(saver, ids["manual_workflow"])
                if checkpoint_case == "owner"
                else _FailingSaver(saver, checkpoint_case)
            )
            await _assert_no_external_claimable(owned)
            processed = await manual_worker.run_once(
                async_session_factory,
                settings=_settings(),
                lease_owner="checkpoint-old-owner",
                checkpointer=boundary,
                trusted_input_loader=_TrustedLoader(),
            )
        assert processed == ids["manual_workflow"]
        async with async_session_factory() as session:
            run = await session.get(WorkflowRun, ids["manual_workflow"])
            review_count = int(
                await session.scalar(
                    select(func.count(ComplianceReview.id)).where(
                        ComplianceReview.proposal_revision_id
                        == ids["manual_revision"]
                    )
                )
                or 0
            )
            call_count = int(
                await session.scalar(
                    select(func.count(AgentCall.id)).where(
                        AgentCall.workflow_run_id == ids["manual_workflow"]
                    )
                )
                or 0
            )
        assert run is not None and (review_count, call_count) == (0, 0)
        if checkpoint_case == "owner":
            assert (run.status, run.lease_owner, run.error_code) == (
                WorkflowStatus.PROCESSING,
                "replacement-owner",
                None,
            )
        else:
            assert (run.status, run.lease_owner, run.error_code) == (
                WorkflowStatus.FAILED,
                None,
                "MANUAL_REVIEW_CHECKPOINT_ERROR",
            )
    finally:
        await _cleanup(owned)


async def test_postgres_manual_review_exact_replay_nonexact_conflict_and_old_owner_zero_writes(
) -> None:
    if await _external_claimable_manual_exists(set()):
        pytest.skip("external manual-review rows are claimable")
    owned = _OwnedIds()
    try:
        exact = await _seed_base(owned, f"task11-{uuid4().hex[:8]}-exact")
        await _create_manual(owned, exact, key="review-exact")
        await _claim(owned, exact["manual_workflow"], "review-owner")
        context = await _load_ready(exact["manual_workflow"], "review-owner")
        trusted = _trusted(context)
        deterministic = validate_optimization_output(trusted, context.proposal_output)

        async def persist_exact():
            async with async_session_factory() as session:
                return await persist_manual_compliance_review(
                    session,
                    workflow_run_id=exact["manual_workflow"],
                    lease_owner="review-owner",
                    trusted=trusted,
                    deterministic=deterministic,
                    response=_passing_response(context),
                    calls=_compliance_calls(),
                    error_code=None,
                )

        first, second = await asyncio.gather(persist_exact(), persist_exact())
        assert {first.disposition, second.disposition} == {"created", "replayed"}
        assert first.review_id == second.review_id
        replay = await persist_exact()
        assert (replay.disposition, replay.review_id) == (
            "replayed",
            first.review_id,
        )
        async with async_session_factory() as session:
            terminal = await finalize_manual_review(
                session,
                workflow_run_id=exact["manual_workflow"],
                lease_owner="review-owner",
            )
            review_count = int(
                await session.scalar(
                    select(func.count(ComplianceReview.id)).where(
                        ComplianceReview.proposal_revision_id
                        == exact["manual_revision"]
                    )
                )
                or 0
            )
            call_count = int(
                await session.scalar(
                    select(func.count(AgentCall.id)).where(
                        AgentCall.workflow_run_id == exact["manual_workflow"]
                    )
                )
                or 0
            )
        assert terminal.disposition == "draft_ready"
        assert (review_count, call_count) == (1, 1)

        conflict = await _seed_base(
            owned, f"task11-{uuid4().hex[:8]}-nonexact"
        )
        await _create_manual(owned, conflict, key="review-nonexact")
        await _claim(owned, conflict["manual_workflow"], "nonexact-owner")
        conflict_context = await _load_ready(
            conflict["manual_workflow"], "nonexact-owner"
        )
        conflict_trusted = _trusted(conflict_context)
        conflict_deterministic = validate_optimization_output(
            conflict_trusted, conflict_context.proposal_output
        )

        async def persist_body(confidence: Decimal):
            async with async_session_factory() as session:
                return await persist_manual_compliance_review(
                    session,
                    workflow_run_id=conflict["manual_workflow"],
                    lease_owner="nonexact-owner",
                    trusted=conflict_trusted,
                    deterministic=conflict_deterministic,
                    response=_passing_response(conflict_context).model_copy(
                        update={"confidence": confidence}
                    ),
                    calls=_compliance_calls(),
                    error_code=None,
                )

        race = await asyncio.gather(
            persist_body(Decimal("0.9000")),
            persist_body(Decimal("0.8000")),
        )
        created = [result for result in race if result.disposition == "created"]
        nonexact = [result for result in race if result.disposition == "failed"]
        assert len(created) == len(nonexact) == 1
        assert nonexact[0].error_code == "MANUAL_REVIEW_REPLAY_CONFLICT"
        async with async_session_factory() as session:
            run = await session.get(WorkflowRun, conflict["manual_workflow"])
            original = await session.get(WorkflowRun, conflict["optimization"])
            proposal = await session.get(ProductProposal, conflict["proposal"])
            reviews = list(
                await session.scalars(
                    select(ComplianceReview).where(
                        ComplianceReview.proposal_revision_id
                        == conflict["manual_revision"]
                    )
                )
            )
            calls = list(
                await session.scalars(
                    select(AgentCall).where(
                        AgentCall.workflow_run_id == conflict["manual_workflow"]
                    )
                )
            )
            audits = list(
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.workflow_run_id == conflict["manual_workflow"]
                    )
                )
            )
        assert run is not None and original is not None and proposal is not None
        assert (run.status, original.status, proposal.active_manual_review_run_id) == (
            WorkflowStatus.FAILED,
            WorkflowStatus.FAILED,
            None,
        )
        assert len(reviews) == len(calls) == 1
        assert reviews[0].id == created[0].review_id
        assert (
            calls[0].workflow_run_id,
            calls[0].node_name,
            calls[0].call_type,
            calls[0].iteration,
            calls[0].attempt,
            calls[0].status,
            calls[0].error_code,
        ) == (
            conflict["manual_workflow"],
            "call_product_compliance_agent",
            AgentCallType.PRIMARY,
            0,
            1,
            "success",
            None,
        )
        assert [audit.event_type for audit in audits].count(
            AuditEventType.MANUAL_REVISION_CREATED
        ) == 1
        assert [audit.event_type for audit in audits].count(
            AuditEventType.MANUAL_REVIEW_CLAIMED
        ) == 1
        failed_audits = [
            audit
            for audit in audits
            if audit.event_type == AuditEventType.MANUAL_REVIEW_FAILED
        ]
        assert len(failed_audits) == 1
        assert failed_audits[0].error_code == "MANUAL_REVIEW_REPLAY_CONFLICT"

        stale = await _seed_base(owned, f"task11-{uuid4().hex[:8]}-old-owner")
        await _create_manual(owned, stale, key="old-owner")
        await _claim(owned, stale["manual_workflow"], "current-owner")
        stale_context = await _load_ready(stale["manual_workflow"], "current-owner")
        stale_trusted = _trusted(stale_context)
        stale_deterministic = validate_optimization_output(
            stale_trusted, stale_context.proposal_output
        )
        async with async_session_factory() as session:
            await session.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == stale["manual_workflow"])
                .values(
                    lease_owner="replacement-owner",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                )
            )
            await session.commit()
        before = await _workflow_snapshot(stale)
        async with async_session_factory() as session:
            context_result = await load_owned_manual_review_context(
                session,
                workflow_run_id=stale["manual_workflow"],
                lease_owner="current-owner",
            )
            persistence_result = await persist_manual_compliance_review(
                session,
                workflow_run_id=stale["manual_workflow"],
                lease_owner="current-owner",
                trusted=stale_trusted,
                deterministic=stale_deterministic,
                response=_passing_response(stale_context),
                calls=_compliance_calls(),
                error_code=None,
            )
            final_result = await finalize_manual_review(
                session,
                workflow_run_id=stale["manual_workflow"],
                lease_owner="current-owner",
            )
            fail_result = await fail_manual_review_run(
                session,
                workflow_run_id=stale["manual_workflow"],
                lease_owner="current-owner",
                error_code="MANUAL_REVIEW_DATABASE_ERROR",
            )
        assert context_result.disposition == "lease_lost"
        assert persistence_result.disposition == "lease_lost"
        assert final_result.disposition == "lease_lost"
        assert fail_result.disposition == "lease_lost"
        assert await _workflow_snapshot(stale) == before
    finally:
        await _cleanup(owned)


async def _insert_later_active_manual(
    owned: _OwnedIds, ids: dict[str, str]
) -> None:
    later_revision = str(uuid4())
    later_workflow = str(uuid4())
    later_manual = str(uuid4())
    owned.revisions.add(later_revision)
    owned.manual_workflows.add(later_workflow)
    owned.threads.add(later_workflow)
    owned.manual_runs.add(later_manual)
    async with async_session_factory() as session:
        first_revision = await session.get(
            ProposalRevision, ids["manual_revision"]
        )
        proposal = await session.get(ProductProposal, ids["proposal"])
        assert first_revision is not None and proposal is not None
        session.add(
            ProposalRevision(
                id=later_revision,
                proposal_id=proposal.id,
                iteration=None,
                revision_number=first_revision.revision_number + 1,
                origin=ProposalRevisionOrigin.MANUAL,
                created_by=ids["user"],
                parent_revision_id=first_revision.id,
                base_product_version=7,
                trusted_fact_hash="e" * 64,
                proposal_output=first_revision.proposal_output,
                citations=first_revision.citations,
            )
        )
        session.add(
            WorkflowRun(
                id=later_workflow,
                workflow_type=WorkflowType.MANUAL_REVIEW,
                store_id=ids["store"],
                created_by=ids["user"],
                status=WorkflowStatus.ACCEPTED,
                quality_status=WorkflowQuality.NORMAL,
                current_step="accepted",
                input={
                    "manual_review_run_id": later_manual,
                    "proposal_id": ids["proposal"],
                    "proposal_revision_id": later_revision,
                    "parent_revision_id": first_revision.id,
                    "product_id": ids["product"],
                    "store_id": ids["store"],
                },
            )
        )
        await session.flush()
        session.add(
            ManualReviewRun(
                id=later_manual,
                workflow_run_id=later_workflow,
                proposal_id=ids["proposal"],
                proposal_revision_id=later_revision,
                submitted_by=ids["user"],
                idempotency_key_hash="f" * 64,
                request_hash="a" * 64,
            )
        )
        await session.flush()
        proposal.current_revision_id = later_revision
        proposal.active_manual_review_run_id = later_manual
        await session.commit()


async def test_postgres_manual_revision_active_race_and_post_success_replay(
) -> None:
    owned = _OwnedIds()
    try:
        ids = await _seed_base(owned, f"task11-{uuid4().hex[:8]}-manual-replay")
        request = _manual_request(ids)

        async def create(key: str, body: ManualRevisionRequest = request):
            async with async_session_factory() as session:
                return await create_manual_revision(
                    session,
                    actor_id=ids["user"],
                    proposal_id=ids["proposal"],
                    request=body,
                    idempotency_key=key,
                    request_id=f"request-{uuid4().hex[:16]}",
                )

        first, replay = await asyncio.gather(create("manual-race"), create("manual-race"))
        assert first.revision_id == replay.revision_id
        assert {first.created, replay.created} == {True, False}
        ids["manual_revision"] = first.revision_id
        ids["manual_workflow"] = first.workflow_run_id
        owned.revisions.add(first.revision_id)
        owned.manual_workflows.add(first.workflow_run_id)
        owned.threads.add(first.workflow_run_id)
        await _record_children(owned)
        async with async_session_factory() as session:
            manual_id = await session.scalar(
                select(ManualReviewRun.id).where(
                    ManualReviewRun.workflow_run_id == first.workflow_run_id
                )
            )
        assert manual_id is not None
        ids["manual_run"] = manual_id
        owned.manual_runs.add(manual_id)

        async def replay_original():
            return await create("manual-race")

        await _insert_later_active_manual(owned, ids)
        await _fresh_guarded_replay(owned, ids, replay_original)
        replay_before = await _chain_snapshot(ids)
        advanced = await replay_original()
        assert (
            advanced.revision_id,
            advanced.workflow_run_id,
            advanced.created,
        ) == (first.revision_id, first.workflow_run_id, False)
        assert await _chain_snapshot(ids) == replay_before
        with pytest.raises(ManualReviewDomainError) as changed:
            await create(
                "manual-race",
                _manual_request(ids, title="同键不同人工标题"),
            )
        assert (changed.value.code, changed.value.status_code) == (
            "IDEMPOTENCY_REPLAY_CONFLICT",
            409,
        )

        competing = await _seed_base(
            owned, f"task11-{uuid4().hex[:8]}-manual-active-race"
        )

        async def competing_create(key: str):
            try:
                async with async_session_factory() as session:
                    return await create_manual_revision(
                        session,
                        actor_id=competing["user"],
                        proposal_id=competing["proposal"],
                        request=_manual_request(competing),
                        idempotency_key=key,
                        request_id=f"request-{uuid4().hex[:16]}",
                    )
            except ManualReviewDomainError as error:
                return error

        outcomes = await asyncio.gather(
            competing_create("active-a"), competing_create("active-b")
        )
        assert sum(not isinstance(value, ManualReviewDomainError) for value in outcomes) == 1
        errors = [value for value in outcomes if isinstance(value, ManualReviewDomainError)]
        assert len(errors) == 1 and errors[0].code == "MANUAL_REVIEW_ACTIVE"
        await _record_children(owned)
        async with async_session_factory() as session:
            active_count = int(
                await session.scalar(
                    select(func.count(ManualReviewRun.id)).where(
                        ManualReviewRun.proposal_id == competing["proposal"]
                    )
                )
                or 0
            )
        assert active_count == 1
    finally:
        await _cleanup(owned)


async def test_postgres_submit_reject_and_request_changes_replay_terminal_chains(
) -> None:
    owned = _OwnedIds()
    try:
        reject_ids = await _prepare_draft(
            owned, f"task11-{uuid4().hex[:8]}-reject"
        )
        submitted = await _submit(reject_ids, key="submit-reject")

        async def replay_submit():
            return await _submit(reject_ids, key="submit-reject")

        await _fresh_guarded_replay(owned, reject_ids, replay_submit)
        submit_replay = await replay_submit()
        assert submit_replay.action.id == submitted.action.id
        assert submit_replay.created is False
        alternate_revision = str(uuid4())
        owned.revisions.add(alternate_revision)
        async with async_session_factory() as session:
            source = await session.get(
                ProposalRevision, reject_ids["manual_revision"]
            )
            assert source is not None
            session.add(
                ProposalRevision(
                    id=alternate_revision,
                    proposal_id=reject_ids["proposal"],
                    iteration=None,
                    revision_number=source.revision_number + 1,
                    origin=ProposalRevisionOrigin.MANUAL,
                    created_by=reject_ids["user"],
                    parent_revision_id=source.id,
                    base_product_version=source.base_product_version,
                    trusted_fact_hash="9" * 64,
                    proposal_output=source.proposal_output,
                    citations=source.citations,
                )
            )
            await session.commit()
        with pytest.raises(ApprovalDomainError) as submit_conflict:
            async with async_session_factory() as session:
                await submit_proposal(
                    session,
                    actor_id=reject_ids["user"],
                    proposal_id=reject_ids["proposal"],
                    request=ProposalActionRequest(revision_id=alternate_revision),
                    idempotency_key="submit-reject",
                    request_id="submit-conflict-request",
                )
        assert (submit_conflict.value.code, submit_conflict.value.status_code) == (
            "IDEMPOTENCY_REPLAY_CONFLICT",
            409,
        )
        reject_request = ProposalCommentActionRequest(
            revision_id=reject_ids["manual_revision"], comment="拒绝原因"
        )

        async def reject():
            async with async_session_factory() as session:
                return await reject_proposal(
                    session,
                    actor_id=reject_ids["user"],
                    proposal_id=reject_ids["proposal"],
                    request=reject_request,
                    idempotency_key="reject-terminal",
                    request_id=f"request-{uuid4().hex[:16]}",
                )

        rejected = await reject()
        await _fresh_guarded_replay(owned, reject_ids, reject)
        rejected_replay = await reject()
        assert rejected_replay.action.id == rejected.action.id
        assert rejected_replay.created is False
        async with async_session_factory() as session:
            proposal = await session.get(ProductProposal, reject_ids["proposal"])
            run = await session.get(WorkflowRun, reject_ids["optimization"])
        assert proposal is not None and run is not None
        assert (run.status, proposal.submitted_revision_id) == (
            WorkflowStatus.REJECTED,
            reject_ids["manual_revision"],
        )

        changes_ids = await _prepare_draft(
            owned, f"task11-{uuid4().hex[:8]}-changes"
        )
        await _submit(changes_ids, key="submit-changes")
        changes_request = ProposalCommentActionRequest(
            revision_id=changes_ids["manual_revision"], comment="请继续修改"
        )

        async def request_changes():
            async with async_session_factory() as session:
                return await request_proposal_changes(
                    session,
                    actor_id=changes_ids["user"],
                    proposal_id=changes_ids["proposal"],
                    request=changes_request,
                    idempotency_key="changes-terminal",
                    request_id=f"request-{uuid4().hex[:16]}",
                )

        changed = await request_changes()
        await _fresh_guarded_replay(owned, changes_ids, request_changes)
        changed_replay = await request_changes()
        assert changed_replay.action.id == changed.action.id
        assert changed_replay.created is False
        async with async_session_factory() as session:
            proposal = await session.get(ProductProposal, changes_ids["proposal"])
            run = await session.get(WorkflowRun, changes_ids["optimization"])
        assert proposal is not None and run is not None
        assert (run.status, proposal.submitted_revision_id) == (
            WorkflowStatus.PENDING_MANUAL,
            None,
        )
        await _record_children(owned)
    finally:
        await _cleanup(owned)


async def test_postgres_migration_backfill_guards_and_unique_schema_are_lossless(
) -> None:
    migration = _migration()
    schema = f"task11_{uuid4().hex}"
    quoted_schema = f'"{schema}"'
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(text(f"CREATE SCHEMA {quoted_schema}"))
            await connection.execute(
                text(f"SET LOCAL search_path TO {quoted_schema}")
            )
            await connection.execute(
                text(
                    "CREATE TABLE workflow_runs ("
                    "id varchar(36) PRIMARY KEY, workflow_type varchar(32) NOT NULL, "
                    "created_by varchar(36) NOT NULL)"
                )
            )
            await connection.execute(
                text(
                    "CREATE TABLE product_proposals ("
                    "id varchar(36) PRIMARY KEY, optimization_run_id varchar(36) NOT NULL)"
                )
            )
            await connection.execute(
                text(
                    "CREATE TABLE proposal_revisions ("
                    "id varchar(36) PRIMARY KEY, proposal_id varchar(36) NOT NULL, "
                    "iteration integer, revision_number integer, origin varchar(16), "
                    "created_by varchar(36), parent_revision_id varchar(36), "
                    "CONSTRAINT uq_task11_proposal_revision_number "
                    "UNIQUE (proposal_id, revision_number))"
                )
            )
            for table in (
                "manual_review_runs",
                "approval_actions",
                "publish_records",
                "audit_events",
            ):
                await connection.execute(
                    text(f"CREATE TABLE {table} (id varchar(36) PRIMARY KEY)")
                )
            await connection.execute(
                text(
                    "INSERT INTO workflow_runs (id, workflow_type, created_by) "
                    "VALUES ('optimization-1', 'optimization', 'user-1')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO product_proposals (id, optimization_run_id) "
                    "VALUES ('proposal-1', 'optimization-1')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO proposal_revisions "
                    "(id, proposal_id, iteration) VALUES "
                    "('revision-0', 'proposal-1', 0), "
                    "('revision-1', 'proposal-1', 1), "
                    "('revision-2', 'proposal-1', 2)"
                )
            )
            await connection.run_sync(migration._backfill_agent_revisions)
            rows = (
                await connection.execute(
                    text(
                        "SELECT id, iteration, revision_number, origin, created_by, "
                        "parent_revision_id FROM proposal_revisions ORDER BY iteration"
                    )
                )
            ).all()
            assert rows == [
                ("revision-0", 0, 1, "agent", "user-1", None),
                ("revision-1", 1, 2, "agent", "user-1", "revision-0"),
                ("revision-2", 2, 3, "agent", "user-1", "revision-1"),
            ]

            savepoint = await connection.begin_nested()
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text(
                        "INSERT INTO proposal_revisions "
                        "(id, proposal_id, iteration, revision_number, origin, created_by) "
                        "VALUES ('duplicate-number', 'proposal-1', NULL, 1, "
                        "'manual', 'user-1')"
                    )
                )
            if savepoint.is_active:
                await savepoint.rollback()

            malformed_shapes = {
                "missing_parent": (
                    "INSERT INTO product_proposals (id, optimization_run_id) "
                    "VALUES ('proposal-a', 'optimization-1')",
                    "INSERT INTO proposal_revisions (id, proposal_id, iteration) "
                    "VALUES ('missing-parent', 'proposal-a', 1)",
                ),
                "duplicate_iteration": (
                    "INSERT INTO product_proposals (id, optimization_run_id) "
                    "VALUES ('proposal-a', 'optimization-1')",
                    "INSERT INTO proposal_revisions (id, proposal_id, iteration) "
                    "VALUES ('duplicate-a', 'proposal-a', 0), "
                    "('duplicate-b', 'proposal-a', 0)",
                ),
                "cross_proposal_parent": (
                    "INSERT INTO product_proposals (id, optimization_run_id) "
                    "VALUES ('proposal-a', 'optimization-1'), "
                    "('proposal-b', 'optimization-1')",
                    "INSERT INTO proposal_revisions "
                    "(id, proposal_id, iteration, parent_revision_id) VALUES "
                    "('parent-a', 'proposal-a', 0, NULL), "
                    "('child-b', 'proposal-b', 1, 'parent-a')",
                ),
            }
            for proposal_sql, revision_sql in malformed_shapes.values():
                await connection.execute(text("DELETE FROM proposal_revisions"))
                await connection.execute(text("DELETE FROM product_proposals"))
                await connection.execute(text(proposal_sql))
                await connection.execute(text(revision_sql))
                with pytest.raises(
                    RuntimeError, match="cannot backfill proposal revisions"
                ):
                    await connection.run_sync(
                        migration._backfill_agent_revisions
                    )
            await connection.execute(text("DELETE FROM proposal_revisions"))
            await connection.execute(text("DELETE FROM product_proposals"))
            await connection.execute(
                text(
                    "INSERT INTO product_proposals (id, optimization_run_id) "
                    "VALUES ('malformed-proposal', 'optimization-1')"
                )
            )
            await connection.run_sync(migration._guard_stage_five_downgrade)
            stage_five_facts = [
                (
                    "proposal_revisions",
                    "INSERT INTO proposal_revisions "
                    "(id, proposal_id, iteration, revision_number, origin, created_by) "
                    "VALUES ('manual-fact', 'malformed-proposal', NULL, 1, "
                    "'manual', 'user-1')",
                ),
                (
                    "workflow_runs",
                    "INSERT INTO workflow_runs (id, workflow_type, created_by) "
                    "VALUES ('manual-workflow-fact', 'manual_review', 'user-1')",
                ),
                (
                    "manual_review_runs",
                    "INSERT INTO manual_review_runs (id) VALUES ('manual-run-fact')",
                ),
                (
                    "approval_actions",
                    "INSERT INTO approval_actions (id) VALUES ('approval-fact')",
                ),
                (
                    "publish_records",
                    "INSERT INTO publish_records (id) VALUES ('publish-fact')",
                ),
                (
                    "audit_events",
                    "INSERT INTO audit_events (id) VALUES ('audit-fact')",
                ),
            ]
            for table, statement in stage_five_facts:
                await connection.execute(text(statement))
                with pytest.raises(
                    RuntimeError,
                    match="cannot downgrade manual review approval publish",
                ):
                    await connection.run_sync(
                        migration._guard_stage_five_downgrade
                    )
                if table == "proposal_revisions":
                    await connection.execute(
                        text("DELETE FROM proposal_revisions WHERE id = 'manual-fact'")
                    )
                elif table == "workflow_runs":
                    await connection.execute(
                        text("DELETE FROM workflow_runs WHERE id = 'manual-workflow-fact'")
                    )
                else:
                    await connection.execute(text(f"DELETE FROM {table}"))
        finally:
            if transaction.is_active:
                await transaction.rollback()
            await connection.execute(
                text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE")
            )
            await connection.commit()


async def _publish_snapshot(ids: dict[str, str]) -> tuple[object, ...]:
    async with async_session_factory() as session:
        product = await session.get(Product, ids["product"])
        sku = await session.get(ProductSku, ids["sku"])
        inventory = await session.get(InventorySnapshot, ids["inventory"])
        assert product is not None and sku is not None and inventory is not None
        return (
            product.title,
            tuple(product.selling_points),
            product.description,
            tuple(product.search_keywords),
            dict(product.attributes),
            product.current_version,
            sku.code,
            dict(sku.spec),
            sku.price,
            sku.current_stock,
            inventory.on_hand,
            inventory.inbound,
        )


async def test_postgres_concurrent_approve_replays_once_and_preserves_nonlisting_facts(
) -> None:
    owned = _OwnedIds()
    try:
        ids = await _prepare_draft(
            owned, f"task11-{uuid4().hex[:8]}-approve-race"
        )
        await _submit(ids, key="approve-race-submit")
        before = await _publish_snapshot(ids)
        request = ProposalActionRequest(revision_id=ids["manual_revision"])

        async def approve(
            key: str, action_request: ProposalActionRequest = request
        ):
            async with async_session_factory() as session:
                return await approve_proposal(
                    session,
                    actor_id=ids["user"],
                    proposal_id=ids["proposal"],
                    request=action_request,
                    idempotency_key=key,
                    request_id=f"request-{uuid4().hex[:16]}",
                )

        race_keys = ("approve-race-a", "approve-race-b")
        first, second = await asyncio.gather(
            approve(race_keys[0]), approve(race_keys[1])
        )
        assert first.publish_record.id == second.publish_record.id
        assert first.action.id == second.action.id
        assert first.platform_delivery is not None
        assert second.platform_delivery is not None
        assert first.platform_delivery.id == second.platform_delivery.id
        assert {first.created, second.created} == {True, False}
        winner_key = race_keys[0] if first.created else race_keys[1]
        same_key_replay = await approve(winner_key)
        assert same_key_replay.publish_record.id == first.publish_record.id
        assert same_key_replay.created is False

        async def new_key_completed_replay():
            return await approve("approve-after-completed")

        await _fresh_guarded_replay(owned, ids, new_key_completed_replay)
        replay_before = await _chain_snapshot(ids)
        completed_replay = await new_key_completed_replay()
        assert completed_replay.publish_record.id == first.publish_record.id
        assert completed_replay.created is False
        assert await _chain_snapshot(ids) == replay_before

        alternate_revision = str(uuid4())
        owned.revisions.add(alternate_revision)
        async with async_session_factory() as session:
            source = await session.get(
                ProposalRevision, ids["manual_revision"]
            )
            assert source is not None
            session.add(
                ProposalRevision(
                    id=alternate_revision,
                    proposal_id=ids["proposal"],
                    iteration=None,
                    revision_number=source.revision_number + 1,
                    origin=ProposalRevisionOrigin.MANUAL,
                    created_by=ids["user"],
                    parent_revision_id=source.id,
                    base_product_version=source.base_product_version,
                    trusted_fact_hash="8" * 64,
                    proposal_output=source.proposal_output,
                    citations=source.citations,
                )
            )
            await session.commit()
        conflict_before = await _chain_snapshot(ids)
        with pytest.raises(ApprovalDomainError) as same_key_conflict:
            await approve(
                winner_key,
                ProposalActionRequest(revision_id=alternate_revision),
            )
        assert (
            same_key_conflict.value.code,
            same_key_conflict.value.status_code,
        ) == ("IDEMPOTENCY_REPLAY_CONFLICT", 409)
        assert await _chain_snapshot(ids) == conflict_before
        after = await _publish_snapshot(ids)
        assert after[:6] == (
            "人工修订商品标题",
            ("棉质家居设计",),
            "商品详情\n人工修订后的商品详情",
            ("家居",),
            {"材质": "棉"},
            8,
        )
        assert after[6:] == before[6:]
        async with async_session_factory() as session:
            publish_count = int(
                await session.scalar(
                    select(func.count(PublishRecord.id)).where(
                        PublishRecord.proposal_id == ids["proposal"]
                    )
                )
                or 0
            )
            approve_count = int(
                await session.scalar(
                    select(func.count(ApprovalAction.id)).where(
                        ApprovalAction.proposal_id == ids["proposal"],
                        ApprovalAction.action == ApprovalActionType.APPROVE,
                    )
                )
                or 0
            )
            delivery_count = int(
                await session.scalar(
                    select(func.count(PlatformDelivery.id)).where(
                        PlatformDelivery.publish_record_id
                        == first.publish_record.id
                    )
                )
                or 0
            )
            publish_audits = list(
                await session.scalars(
                    select(AuditEvent.event_type).where(
                        AuditEvent.proposal_id == ids["proposal"],
                        AuditEvent.event_type.in_(
                            [
                                AuditEventType.PROPOSAL_APPROVED,
                                AuditEventType.SIMULATED_PUBLISH_COMPLETED,
                                AuditEventType.PLATFORM_DELIVERY_ENQUEUED,
                            ]
                        ),
                    )
                )
            )
            run = await session.get(WorkflowRun, ids["optimization"])
        assert (publish_count, approve_count, delivery_count) == (1, 1, 1)
        assert publish_audits.count(AuditEventType.PROPOSAL_APPROVED) == 1
        assert publish_audits.count(
            AuditEventType.SIMULATED_PUBLISH_COMPLETED
        ) == 1
        assert publish_audits.count(AuditEventType.PLATFORM_DELIVERY_ENQUEUED) == 1
        assert run is not None and (
            run.status,
            run.current_step,
            run.error_code,
        ) == (WorkflowStatus.COMPLETED, "simulated_published", None)
        await _record_children(owned)
    finally:
        await _cleanup(owned)


@pytest.mark.parametrize("terminal_action", ["reject", "request_changes"])
async def test_postgres_approve_conflicts_with_other_terminal_writers(
    terminal_action: str,
) -> None:
    owned = _OwnedIds()
    try:
        ids = await _prepare_draft(
            owned, f"task11-{uuid4().hex[:8]}-{terminal_action}-race"
        )
        await _submit(ids, key=f"{terminal_action}-race-submit")
        before = await _publish_snapshot(ids)
        chain_before = await _chain_snapshot(ids)
        approve_request = ProposalActionRequest(revision_id=ids["manual_revision"])
        terminal_request = ProposalCommentActionRequest(
            revision_id=ids["manual_revision"], comment="并发终态意见"
        )

        async def approve():
            try:
                async with async_session_factory() as session:
                    return await approve_proposal(
                        session,
                        actor_id=ids["user"],
                        proposal_id=ids["proposal"],
                        request=approve_request,
                        idempotency_key=f"approve-{terminal_action}-race",
                        request_id=f"request-{uuid4().hex[:16]}",
                    )
            except ApprovalDomainError as error:
                return error

        async def terminal():
            try:
                async with async_session_factory() as session:
                    service = (
                        reject_proposal
                        if terminal_action == "reject"
                        else request_proposal_changes
                    )
                    return await service(
                        session,
                        actor_id=ids["user"],
                        proposal_id=ids["proposal"],
                        request=terminal_request,
                        idempotency_key=f"{terminal_action}-approve-race",
                        request_id=f"request-{uuid4().hex[:16]}",
                    )
            except ApprovalDomainError as error:
                return error

        outcomes = await asyncio.gather(approve(), terminal())
        assert sum(not isinstance(value, ApprovalDomainError) for value in outcomes) == 1
        errors = [value for value in outcomes if isinstance(value, ApprovalDomainError)]
        assert len(errors) == 1
        assert (errors[0].code, errors[0].status_code) == (
            "APPROVAL_ACTION_CONFLICT",
            409,
        )
        after = await _publish_snapshot(ids)
        chain_after = await _chain_snapshot(ids)
        async with async_session_factory() as session:
            publish_count = int(
                await session.scalar(
                    select(func.count(PublishRecord.id)).where(
                        PublishRecord.proposal_id == ids["proposal"]
                    )
                )
                or 0
            )
            terminal_count = int(
                await session.scalar(
                    select(func.count(ApprovalAction.id)).where(
                        ApprovalAction.proposal_id == ids["proposal"],
                        ApprovalAction.action.in_(
                            [
                                ApprovalActionType.APPROVE,
                                ApprovalActionType.REJECT,
                                ApprovalActionType.REQUEST_CHANGES,
                            ]
                        ),
                    )
                )
                or 0
            )
            run = await session.get(WorkflowRun, ids["optimization"])
        assert terminal_count == 1 and run is not None
        assert after[6:] == before[6:]
        if publish_count == 1:
            assert after[5] == 8 and run.status is WorkflowStatus.COMPLETED
            assert len(chain_after["audits"]) - len(chain_before["audits"]) == 2
        else:
            assert after == before
            expected = (
                WorkflowStatus.REJECTED
                if terminal_action == "reject"
                else WorkflowStatus.PENDING_MANUAL
            )
            assert run.status is expected
            assert len(chain_after["audits"]) - len(chain_before["audits"]) == 1
        await _record_children(owned)
    finally:
        await _cleanup(owned)


async def test_postgres_version_conflict_and_faulted_approve_roll_back_every_write(
    monkeypatch,
) -> None:
    owned = _OwnedIds()
    try:
        conflict = await _prepare_draft(
            owned, f"task11-{uuid4().hex[:8]}-version-conflict"
        )
        await _submit(conflict, key="version-conflict-submit")
        async with async_session_factory() as session:
            await session.execute(
                update(Product)
                .where(Product.id == conflict["product"])
                .values(current_version=8)
            )
            await session.commit()
        conflict_chain = await _chain_snapshot(conflict)
        conflict_listing = await _publish_snapshot(conflict)
        with pytest.raises(ApprovalDomainError) as version_error:
            async with async_session_factory() as session:
                await approve_proposal(
                    session,
                    actor_id=conflict["user"],
                    proposal_id=conflict["proposal"],
                    request=ProposalActionRequest(
                        revision_id=conflict["manual_revision"]
                    ),
                    idempotency_key="version-conflict-approve",
                    request_id="version-conflict-request",
                )
        assert (version_error.value.code, version_error.value.status_code) == (
            "PRODUCT_VERSION_CONFLICT",
            409,
        )
        assert await _chain_snapshot(conflict) == conflict_chain
        assert await _publish_snapshot(conflict) == conflict_listing
        async with async_session_factory() as session:
            assert (
                await session.scalar(
                    select(func.count(PublishRecord.id)).where(
                        PublishRecord.proposal_id == conflict["proposal"]
                    )
                )
                == 0
            )

        rollback = await _prepare_draft(
            owned, f"task11-{uuid4().hex[:8]}-approve-rollback"
        )
        await _submit(rollback, key="approve-rollback-submit")
        before = await _publish_snapshot(rollback)
        rollback_chain = await _chain_snapshot(rollback)
        async with async_session_factory() as session:
            action_count = int(
                await session.scalar(
                    select(func.count(ApprovalAction.id)).where(
                        ApprovalAction.proposal_id == rollback["proposal"]
                    )
                )
                or 0
            )
            audit_count = int(
                await session.scalar(
                    select(func.count(AuditEvent.id)).where(
                        AuditEvent.proposal_id == rollback["proposal"]
                    )
                )
                or 0
            )
            original_flush = session.flush
            flush_count = 0

            async def fail_fourth_flush(*args, **kwargs):
                nonlocal flush_count
                flush_count += 1
                if flush_count == 4:
                    raise SQLAlchemyError("injected approval audit failure")
                return await original_flush(*args, **kwargs)

            monkeypatch.setattr(session, "flush", fail_fourth_flush)
            with pytest.raises(ApprovalDomainError) as fault:
                await approve_proposal(
                    session,
                    actor_id=rollback["user"],
                    proposal_id=rollback["proposal"],
                    request=ProposalActionRequest(
                        revision_id=rollback["manual_revision"]
                    ),
                    idempotency_key="approve-rollback",
                    request_id="approve-rollback-request",
                )
        assert fault.value.code == "PROPOSAL_DATA_INCONSISTENT"
        assert await _publish_snapshot(rollback) == before
        assert await _chain_snapshot(rollback) == rollback_chain
        async with async_session_factory() as session:
            assert (
                int(
                    await session.scalar(
                        select(func.count(ApprovalAction.id)).where(
                            ApprovalAction.proposal_id == rollback["proposal"]
                        )
                    )
                    or 0
                ),
                int(
                    await session.scalar(
                        select(func.count(PublishRecord.id)).where(
                            PublishRecord.proposal_id == rollback["proposal"]
                        )
                    )
                    or 0
                ),
                int(
                    await session.scalar(
                        select(func.count(AuditEvent.id)).where(
                            AuditEvent.proposal_id == rollback["proposal"]
                        )
                    )
                    or 0
                ),
            ) == (action_count, 0, audit_count)
        await _record_children(owned)
    finally:
        await _cleanup(owned)
