import asyncio
import importlib
import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.common import (
    KnowledgeVersionStatus,
    UserRole,
    UserStatus,
    WorkflowQuality,
    WorkflowStatus,
    WorkflowType,
)
from backend.config import Settings
from backend.database import Base, async_session_factory
from backend.knowledge_index import IndexedChunk, KnowledgeDependencyError
from backend.knowledge_search import (
    KnowledgeSearchHit,
    KnowledgeSearchOutcome,
    get_knowledge_search_dependencies,
)
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
from backend.optimization_runs import (
    OwnedOptimizationContext,
    load_owned_optimization_context,
    persist_optimization_revision,
)
from backend.optimization_worker import OptimizationLeaseLost, TrustedInputLoadFailure
from backend.schemas import (
    CanonicalRuleCitation,
    OptimizationProposalOutput,
    OutputCitation,
    ProductMetrics,
    TrustedProductSku,
)

import backend.optimization_trusted_input as trusted_input


_OPT_IN = pytest.mark.skipif(
    os.getenv("RUN_KNOWLEDGE_INTEGRATION") != "1",
    reason="explicit PostgreSQL, Milvus, and local-model integration opt-in required",
)


@pytest_asyncio.fixture
async def rag_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _context(*, title: str = "智能手机", category: str = "数码") -> OwnedOptimizationContext:
    return OwnedOptimizationContext(
        workflow_run_id="optimization-rag-run",
        proposal_id="proposal-rag",
        analysis_run_id="analysis-rag",
        analysis_candidate_id="candidate-rag",
        store_id="store-rag",
        product_id="product-rag",
        created_by="operator-rag",
        base_product_version=7,
        title=title,
        category=category,
        brand="示例品牌",
        selling_points=("长续航", "轻薄机身"),
        description="不应进入检索查询的完整详情。",
        search_keywords=("不应进入",),
        attributes={"颜色": "黑色"},
        skus=(
            TrustedProductSku(
                id="sku-rag-1", code="PHONE-BLACK", spec={"颜色": "黑色"},
                price=Decimal("100.00"), stock=8,
            ),
        ),
        candidate_metrics=ProductMetrics(
            product_id="product-rag", product_code="PHONE-RAG", impressions=100, clicks=20,
            orders=4, units=4, revenue=Decimal("400.00"), refunds=0,
            ctr=Decimal("0.2000"), conversion_rate=Decimal("0.2000"),
            refund_rate=Decimal("0.0000"), average_order_value=Decimal("100.0000"),
        ),
        candidate_evidence=("clicks=20",),
        current_revision_id=None,
        current_revision_iteration=None,
        current_proposal_output=None,
        current_canonical_citations=(),
        current_review_id=None,
        current_review_passed=None,
        current_review_quality_status=None,
        current_review_error_code=None,
        current_required_changes=(),
    )


async def _document(
    session: AsyncSession,
    *,
    document_id: str,
    version_id: str,
    chunk_id: str,
    name: str,
    category: str,
    text: str,
    enabled: bool = True,
    current: bool = True,
    version_number: int = 1,
) -> KnowledgeChunk:
    user = await session.get(User, "knowledge-rag-user")
    if user is None:
        session.add(User(
            id="knowledge-rag-user", username="knowledge-rag-user", password_hash="hash", role=UserRole.ADMIN,
        ))
        await session.flush()
    document = KnowledgeDocument(
        id=document_id, name=name, category=category, enabled=enabled,
        created_by="knowledge-rag-user", idempotency_key=f"key-{document_id}",
    )
    session.add(document)
    await session.flush()
    version = KnowledgeDocumentVersion(
        id=version_id, document_id=document.id, version_number=version_number,
        sha256=(version_id.replace("-", "x") + "x" * 64)[:64], original_filename="rules.md",
        mime_type="text/markdown", storage_path="d:/test/rules.md", status=KnowledgeVersionStatus.ACTIVE,
    )
    session.add(version)
    await session.flush()
    chunk = KnowledgeChunk(
        id=chunk_id, version_id=version.id, chunk_index=0,
        chunk_hash=(chunk_id.replace("-", "x") + "x" * 64)[:64], canonical_text=text,
        chunk_metadata={}, token_count=1,
    )
    session.add(chunk)
    await session.flush()
    if current:
        document.current_version_id = version.id
    await session.commit()
    return chunk


def _hit(
    *, chunk_id: str, name: str, category: str, text: str, version_number: int = 1
) -> KnowledgeSearchHit:
    return KnowledgeSearchHit(
        chunk_id=chunk_id, document_name=name, version_number=version_number, category=category,
        canonical_text=text, chunk_metadata={}, dense_score=0.9, sparse_score=0.8,
        fusion_score=0.7, reranker_score=0.6, final_score=0.6,
    )


async def test_loader_builds_bounded_query_and_db_canonical_trusted_input(
    rag_session_factory, monkeypatch
) -> None:
    async with rag_session_factory() as session:
        await _document(
            session, document_id="doc-product", version_id="version-product", chunk_id="chunk-product",
            name="数码规则", category="数码", text="数码商品规则。",
        )
        await _document(
            session, document_id="doc-general", version_id="version-general", chunk_id="chunk-general",
            name="通用规则", category="通用规则", text="通用商品规则。",
        )
    calls: list[dict[str, object]] = []
    renewals = 0

    async def fake_search(session, **kwargs):
        calls.append(kwargs)
        await kwargs["before_external_attempt"]()
        return KnowledgeSearchOutcome("normal", [
            _hit(chunk_id="chunk-product", name="数码规则", category="数码", text="数码商品规则。"),
            _hit(chunk_id="chunk-general", name="通用规则", category="通用规则", text="通用商品规则。"),
        ])

    async def renew() -> None:
        nonlocal renewals
        renewals += 1

    monkeypatch.setattr(trusted_input, "search_active_knowledge", fake_search)
    context = _context()
    actual = await trusted_input.load_optimization_trusted_input(
        context, renew, session_factory=rag_session_factory, settings=object(),
    )

    assert calls == [{
        "query": "智能手机 数码 示例品牌 长续航 轻薄机身 标题 卖点 详情 属性 SKU 价格 合规",
        "categories": ["数码", "通用规则"],
        "top_k": 10,
        "retrieval_path": "hybrid_rerank",
        "load_dependencies": trusted_input.get_knowledge_search_dependencies,
        "before_external_attempt": renew,
    }]
    assert renewals == 1
    assert actual.store_id == context.store_id and actual.product_id == context.product_id
    assert actual.base_product_version == context.base_product_version
    assert actual.title == context.title and actual.category == context.category and actual.brand == context.brand
    assert actual.selling_points == list(context.selling_points)
    assert actual.description == context.description and actual.search_keywords == list(context.search_keywords)
    assert actual.attributes == context.attributes and actual.skus == list(context.skus)
    assert actual.candidate_metrics == context.candidate_metrics
    assert actual.candidate_evidence == list(context.candidate_evidence)
    assert actual.rag_quality == "normal"
    assert [citation.chunk_id for citation in actual.canonical_rule_citations] == [
        "chunk-general", "chunk-product"
    ]
    assert all(citation.active and citation.applicable for citation in actual.canonical_rule_citations)


async def test_loader_discards_noncurrent_inapplicable_and_mismatched_hits_and_downgrades_quality(
    rag_session_factory, monkeypatch
) -> None:
    async with rag_session_factory() as session:
        await _document(
            session, document_id="doc-accepted", version_id="version-accepted", chunk_id="chunk-accepted",
            name="数码规则", category="数码", text="可用规则。",
        )
        await _document(
            session, document_id="doc-disabled", version_id="version-disabled", chunk_id="chunk-disabled",
            name="禁用规则", category="数码", text="禁用规则。", enabled=False,
        )
        await _document(
            session, document_id="doc-other", version_id="version-other", chunk_id="chunk-other",
            name="其他规则", category="服饰", text="其他规则。",
        )
        await _document(
            session, document_id="doc-old", version_id="version-old", chunk_id="chunk-old",
            name="旧规则", category="数码", text="旧规则。", current=False,
        )

    async def fake_search(session, **kwargs):
        return KnowledgeSearchOutcome("normal", [
            _hit(chunk_id="chunk-disabled", name="禁用规则", category="数码", text="禁用规则。"),
            _hit(chunk_id="chunk-accepted", name="数码规则", category="数码", text="可用规则。"),
            _hit(chunk_id="chunk-other", name="其他规则", category="服饰", text="其他规则。"),
            _hit(chunk_id="chunk-old", name="旧规则", category="数码", text="旧规则。"),
            _hit(chunk_id="orphan", name="孤儿", category="数码", text="孤儿规则。"),
            _hit(chunk_id="chunk-accepted", name="数码规则", category="数码", text="被篡改。"),
        ])

    monkeypatch.setattr(trusted_input, "search_active_knowledge", fake_search)
    actual = await trusted_input.load_optimization_trusted_input(
        _context(), _no_op_renewal, session_factory=rag_session_factory, settings=object(),
    )

    assert actual.rag_quality == "low_confidence"
    assert [citation.chunk_id for citation in actual.canonical_rule_citations] == ["chunk-accepted"]


async def test_loader_rejects_cross_document_current_version_pointer(rag_session_factory, monkeypatch) -> None:
    async with rag_session_factory() as session:
        await _document(
            session, document_id="doc-version-owner", version_id="version-cross", chunk_id="chunk-cross",
            name="版本所有者", category="数码", text="跨文档规则。", current=False,
        )
        foreign = KnowledgeDocument(
            id="doc-current-owner", name="当前指针所有者", category="数码", enabled=True,
            created_by="knowledge-rag-user", idempotency_key="cross-current-pointer",
            current_version_id="version-cross",
        )
        session.add(foreign)
        await session.commit()

    async def fake_search(session, **kwargs):
        return KnowledgeSearchOutcome("normal", [
            _hit(
                chunk_id="chunk-cross", name="当前指针所有者", category="数码", text="跨文档规则。"
            ),
        ])

    monkeypatch.setattr(trusted_input, "search_active_knowledge", fake_search)
    actual = await trusted_input.load_optimization_trusted_input(
        _context(), _no_op_renewal, session_factory=rag_session_factory, settings=object(),
    )

    assert actual.rag_quality == "zero_hit"
    assert actual.canonical_rule_citations == []


@pytest.mark.parametrize(
    ("outcome_quality", "hits", "expected_quality"),
    (
        ("zero_hit", [], "zero_hit"),
        ("low_confidence", [_hit(chunk_id="chunk-a", name="数码规则", category="数码", text="规则。")], "low_confidence"),
    ),
)
async def test_loader_preserves_safe_empty_and_low_quality_results(
    rag_session_factory, monkeypatch, outcome_quality, hits, expected_quality
) -> None:
    async with rag_session_factory() as session:
        await _document(
            session, document_id="doc-a", version_id="version-a", chunk_id="chunk-a",
            name="数码规则", category="数码", text="规则。",
        )

    async def fake_search(session, **kwargs):
        return KnowledgeSearchOutcome(outcome_quality, hits)

    monkeypatch.setattr(trusted_input, "search_active_knowledge", fake_search)
    actual = await trusted_input.load_optimization_trusted_input(
        _context(), _no_op_renewal, session_factory=rag_session_factory, settings=object(),
    )

    assert actual.rag_quality == expected_quality


@pytest.mark.parametrize(
    ("source_code", "expected_code"),
    (
        ("KNOWLEDGE_MODEL_UNAVAILABLE", "KNOWLEDGE_MODEL_UNAVAILABLE"),
        ("KNOWLEDGE_DEPENDENCY_TIMEOUT", "KNOWLEDGE_DEPENDENCY_TIMEOUT"),
        ("KNOWLEDGE_CALIBRATION_UNAVAILABLE", "KNOWLEDGE_DEPENDENCY_ERROR"),
        ("KNOWLEDGE_MILVUS_UNAVAILABLE", "KNOWLEDGE_DEPENDENCY_ERROR"),
    ),
)
async def test_loader_maps_only_knowledge_dependency_codes(
    rag_session_factory, monkeypatch, source_code, expected_code
) -> None:
    async def fake_search(session, **kwargs):
        raise KnowledgeDependencyError(source_code, retryable=True)

    monkeypatch.setattr(trusted_input, "search_active_knowledge", fake_search)
    with pytest.raises(TrustedInputLoadFailure) as raised:
        await trusted_input.load_optimization_trusted_input(
            _context(), _no_op_renewal, session_factory=rag_session_factory, settings=object(),
        )

    assert raised.value.error_code == expected_code


@pytest.mark.parametrize("error", [SQLAlchemyError("database"), asyncio.CancelledError(), OptimizationLeaseLost()])
async def test_loader_propagates_fact_cancellation_and_lease_failures(
    rag_session_factory, monkeypatch, error
) -> None:
    async def fake_search(session, **kwargs):
        raise error

    monkeypatch.setattr(trusted_input, "search_active_knowledge", fake_search)
    with pytest.raises(type(error)):
        await trusted_input.load_optimization_trusted_input(
            _context(), _no_op_renewal, session_factory=rag_session_factory, settings=object(),
        )


async def _no_op_renewal() -> None:
    return None


def test_optimization_worker_cli_import_is_side_effect_free() -> None:
    module = importlib.import_module("scripts.run_optimization_worker")
    assert callable(module.main)


async def test_optimization_worker_cli_binds_real_loader_for_one_once_call(monkeypatch) -> None:
    module = importlib.import_module("scripts.run_optimization_worker")
    from backend import config, database, optimization_worker
    from langgraph.checkpoint.postgres import aio as checkpoint_aio

    settings = Settings(
        _env_file=None,
        jwt_secret_key="test-only-secret-at-least-32-characters",
        langgraph_database_url="postgresql://test.invalid/optimization",
    )
    factory = object()
    calls: list[tuple[object, dict[str, object]]] = []

    class FakeSaver:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def setup(self) -> None:
            calls.append(("setup", {}))

    saver = FakeSaver()

    class FakeAsyncPostgresSaver:
        @classmethod
        def from_conn_string(cls, value):
            calls.append(("connection", {"value": value}))
            return saver

    async def fake_run_once(session_factory, **kwargs) -> str:
        calls.append((session_factory, kwargs))
        return "optimization-rag-run"

    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(database, "async_session_factory", factory)
    monkeypatch.setattr(optimization_worker, "run_once", fake_run_once)
    monkeypatch.setattr(checkpoint_aio, "AsyncPostgresSaver", FakeAsyncPostgresSaver)
    await module.main(True)

    assert calls[0] == ("connection", {"value": settings.langgraph_database_url})
    assert calls[1] == ("setup", {})
    assert calls[2][0] is factory
    kwargs = calls[2][1]
    loader = kwargs["trusted_input_loader"]
    assert isinstance(loader, partial)
    assert loader.func is trusted_input.load_optimization_trusted_input
    assert loader.keywords == {"session_factory": factory, "settings": settings}
    assert kwargs["settings"] is settings and kwargs["checkpointer"] is saver


@_OPT_IN
async def test_real_local_rag_loader_returns_only_current_applicable_citations(monkeypatch) -> None:
    settings = Settings()
    user_id = str(uuid4())
    chain_ids = {key: str(uuid4()) for key in (
        "store", "product", "sku", "analysis", "candidate", "optimization", "proposal",
    )}
    document_ids = [str(uuid4()) for _ in range(4)]
    version_ids = [str(uuid4()) for _ in range(5)]
    chunk_ids = [str(uuid4()) for _ in range(5)]
    orphan_id = str(uuid4())
    index = None
    get_knowledge_search_dependencies.cache_clear()
    try:
        models, index, _ = get_knowledge_search_dependencies()
        await index.ensure_collection()
        documents = [
            (document_ids[0], version_ids[0], chunk_ids[0], "数码规则", "数码", "数码商品应如实描述。", True),
            (document_ids[1], version_ids[1], chunk_ids[1], "通用规则", "通用规则", "商品宣传不得夸大。", True),
            (document_ids[2], version_ids[2], chunk_ids[2], "非适用规则", "服饰", "服饰商品规则。", True),
            (document_ids[3], version_ids[3], chunk_ids[3], "停用规则", "数码", "停用商品规则。", False),
        ]
        async with async_session_factory() as session:
            current_documents: list[tuple[KnowledgeDocument, str]] = []
            session.add(User(
                id=user_id, username=f"task9-rag-{user_id}", password_hash="task9-rag",
                role=UserRole.OPERATOR, status=UserStatus.ACTIVE,
            ))
            session.add(Store(
                id=chain_ids["store"], name="Task 9 RAG 店铺", code=f"t9-rag-{chain_ids['store'][:8]}",
                enabled=True,
            ))
            await session.flush()
            session.add(UserStoreScope(user_id=user_id, store_id=chain_ids["store"]))
            session.add(Product(
                id=chain_ids["product"], store_id=chain_ids["store"], code=f"t9-rag-{chain_ids['product'][:8]}",
                title="智能手机", category="数码", brand="示例品牌", selling_points=["长续航", "轻薄机身"],
                description="不应进入检索查询的完整详情。", search_keywords=["不应进入"],
                attributes={"颜色": "黑色"}, current_version=7, enabled=True,
            ))
            await session.flush()
            session.add(ProductSku(
                id=chain_ids["sku"], product_id=chain_ids["product"], code="PHONE-BLACK",
                spec={"颜色": "黑色"}, price=Decimal("100.00"), current_stock=8,
            ))
            metrics = ProductMetrics(
                product_id=chain_ids["product"], product_code=f"t9-rag-{chain_ids['product'][:8]}",
                impressions=100, clicks=20, orders=4, units=4, revenue=Decimal("400.00"), refunds=0,
                ctr=Decimal("0.2000"), conversion_rate=Decimal("0.2000"),
                refund_rate=Decimal("0.0000"), average_order_value=Decimal("100.0000"),
            )
            session.add_all((
                WorkflowRun(
                    id=chain_ids["analysis"], workflow_type=WorkflowType.ANALYSIS,
                    store_id=chain_ids["store"], created_by=user_id,
                    start_date=date(2026, 8, 1), end_date=date(2026, 8, 1),
                    status=WorkflowStatus.COMPLETED, quality_status=WorkflowQuality.NORMAL,
                    current_step="product_selected",
                ),
                WorkflowRun(
                    id=chain_ids["optimization"], workflow_type=WorkflowType.OPTIMIZATION,
                    store_id=chain_ids["store"], created_by=user_id, start_date=None, end_date=None,
                    status=WorkflowStatus.PROCESSING, quality_status=WorkflowQuality.NORMAL, attempt_count=1,
                    lease_owner="task9-rag-owner", lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                ),
            ))
            await session.flush()
            session.add(AnalysisCandidate(
                id=chain_ids["candidate"], workflow_run_id=chain_ids["analysis"],
                product_id=chain_ids["product"], rank=1, product_code=metrics.product_code or "",
                anomaly_types=["low_conversion"], metrics=metrics.model_dump(mode="json"),
                business_impact=Decimal("1.00"), evidence=["clicks=20"], impact_explanation="测试",
                reason="测试", recommended_action="测试", confidence=Decimal("0.8000"),
            ))
            session.add(ProductProposal(
                id=chain_ids["proposal"], analysis_run_id=chain_ids["analysis"],
                analysis_candidate_id=chain_ids["candidate"], optimization_run_id=chain_ids["optimization"],
                store_id=chain_ids["store"], product_id=chain_ids["product"], base_product_version=7,
                selection_idempotency_hash="a" * 64,
            ))
            for document_id, version_id, chunk_id, name, category, canonical_text, enabled in documents:
                document = KnowledgeDocument(
                    id=document_id, name=name, category=category, enabled=enabled,
                    created_by=user_id, idempotency_key=f"task9-rag-{document_id}",
                )
                session.add(document)
                await session.flush()
                version = KnowledgeDocumentVersion(
                    id=version_id, document_id=document.id, version_number=1,
                    sha256=uuid4().hex * 2, original_filename="task9-rag.md",
                    mime_type="text/markdown", storage_path="D:/E-commerce_operations_runtime/task9-rag.md",
                    status=KnowledgeVersionStatus.ACTIVE,
                )
                session.add(version)
                await session.flush()
                chunk = KnowledgeChunk(
                    id=chunk_id, version_id=version.id, chunk_index=0,
                    chunk_hash=uuid4().hex * 2, canonical_text=canonical_text, chunk_metadata={}, token_count=1,
                )
                session.add(chunk)
                await session.flush()
                current_documents.append((document, version_id))
            old = KnowledgeDocumentVersion(
                id=version_ids[4], document_id=document_ids[0], version_number=2,
                sha256=uuid4().hex * 2, original_filename="task9-old.md",
                mime_type="text/markdown", storage_path="D:/E-commerce_operations_runtime/task9-old.md",
                status=KnowledgeVersionStatus.ACTIVE,
            )
            session.add(old)
            await session.flush()
            for document, version_id in current_documents:
                document.current_version_id = version_id
            await session.commit()
        vectors = await models.embed_documents([
            *(document[5] for document in documents), "历史版本规则。", "孤儿向量规则。"
        ])
        assert len(vectors) == 6
        await index.upsert([
            IndexedChunk(
                chunk_id=chunk_id, document_id=document_id, version_id=version_id,
                category=category, vector=vector,
            )
            for (document_id, version_id, chunk_id, _name, category, _text, _enabled), vector
            in zip(documents, vectors[:4], strict=True)
        ] + [
            IndexedChunk(
                chunk_id=chunk_ids[4], document_id=document_ids[0], version_id=version_ids[4],
                category="数码", vector=vectors[4],
            ),
            IndexedChunk(
                chunk_id=orphan_id, document_id=str(uuid4()), version_id=str(uuid4()),
                category="数码", vector=vectors[5],
            ),
        ])
        async with async_session_factory() as session:
            loaded = await load_owned_optimization_context(
                session,
                workflow_run_id=chain_ids["optimization"],
                lease_owner="task9-rag-owner",
            )
        assert loaded.disposition == "ready" and loaded.context is not None
        phases = iter(("dependencies", "embedding", "dense", "sparse", "rerank"))
        renewals: list[str] = []

        async def renew() -> None:
            renewals.append(next(phases))

        actual = await trusted_input.load_optimization_trusted_input(
            loaded.context, renew, session_factory=async_session_factory, settings=settings,
        )

        assert actual.rag_quality in {"normal", "low_confidence"}
        assert [citation.chunk_id for citation in actual.canonical_rule_citations] == sorted(chunk_ids[:2])
        assert renewals == ["dependencies", "embedding", "dense", "sparse", "rerank"]
        expected = {
            chunk_id: (document_id, version_id, name, 1, category, text)
            for document_id, version_id, chunk_id, name, category, text, _enabled in documents[:2]
        }
        assert [
            (
                citation.document_id, citation.version_id, citation.document_name,
                citation.version_number, citation.category, citation.canonical_text,
                citation.active, citation.applicable,
            )
            for citation in actual.canonical_rule_citations
        ] == [(*expected[chunk_id], True, True) for chunk_id in sorted(expected)]
        get_knowledge_search_dependencies.cache_clear()
        dependency_calls = 0
        original_dependencies = get_knowledge_search_dependencies

        def counted_dependencies():
            nonlocal dependency_calls
            dependency_calls += 1
            return original_dependencies()

        async def lose_lease() -> None:
            raise OptimizationLeaseLost()

        monkeypatch.setattr(trusted_input, "get_knowledge_search_dependencies", counted_dependencies)
        with pytest.raises(OptimizationLeaseLost):
            await trusted_input.load_optimization_trusted_input(
                loaded.context, lose_lease, session_factory=async_session_factory, settings=settings,
            )
        assert dependency_calls == 0

        async with async_session_factory() as session:
            await session.execute(
                update(KnowledgeDocument)
                .where(KnowledgeDocument.id == document_ids[0])
                .values(enabled=False)
            )
            await session.commit()
        rejected_output = OptimizationProposalOutput(
            title=actual.title, selling_points=actual.selling_points, description=[],
            keywords=actual.search_keywords, attribute_completions=[], changes=[],
            citations=[OutputCitation(chunk_id=citation.chunk_id) for citation in actual.canonical_rule_citations],
            price_suggestions=[], sku_suggestions=[],
        )
        async with async_session_factory() as session:
            rejected = await persist_optimization_revision(
                session,
                workflow_run_id=chain_ids["optimization"],
                lease_owner="task9-rag-owner",
                iteration=0,
                trusted=actual,
                output=rejected_output,
                canonical_citations=actual.canonical_rule_citations,
                calls=(),
            )
            revisions = list(await session.scalars(
                select(ProposalRevision.id).where(ProposalRevision.proposal_id == chain_ids["proposal"])
            ))
            calls = list(await session.scalars(
                select(AgentCall.id).where(AgentCall.workflow_run_id == chain_ids["optimization"])
            ))
        assert (rejected.disposition, rejected.error_code) == ("failed", "OPTIMIZATION_FACT_ERROR")
        assert not revisions and not calls
    finally:
        if index is not None:
            await index.delete_chunk_ids(chunk_ids=[*chunk_ids, orphan_id])
        async with async_session_factory() as session:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                if await session.scalar(text("SELECT to_regclass(:table_name)"), {"table_name": table}):
                    await session.execute(
                        text(f"DELETE FROM {table} WHERE thread_id = :thread_id"),
                        {"thread_id": chain_ids["optimization"]},
                    )
            await session.execute(
                delete(AgentCall).where(AgentCall.workflow_run_id == chain_ids["optimization"])
            )
            await session.execute(
                delete(ComplianceReview).where(ComplianceReview.proposal_id == chain_ids["proposal"])
            )
            await session.execute(
                update(ProductProposal)
                .where(ProductProposal.id == chain_ids["proposal"])
                .values(current_revision_id=None)
            )
            await session.execute(
                delete(ProposalRevision).where(ProposalRevision.proposal_id == chain_ids["proposal"])
            )
            await session.execute(delete(ProductProposal).where(ProductProposal.id == chain_ids["proposal"]))
            await session.execute(delete(AnalysisCandidate).where(AnalysisCandidate.id == chain_ids["candidate"]))
            await session.execute(delete(WorkflowRun).where(WorkflowRun.id == chain_ids["optimization"]))
            await session.execute(delete(WorkflowRun).where(WorkflowRun.id == chain_ids["analysis"]))
            await session.execute(delete(ProductSku).where(ProductSku.id == chain_ids["sku"]))
            await session.execute(delete(Product).where(Product.id == chain_ids["product"]))
            await session.execute(
                delete(UserStoreScope).where(
                    UserStoreScope.user_id == user_id,
                    UserStoreScope.store_id == chain_ids["store"],
                )
            )
            await session.execute(delete(Store).where(Store.id == chain_ids["store"]))
            await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.id.in_(chunk_ids)))
            await session.execute(
                update(KnowledgeDocument)
                .where(KnowledgeDocument.id.in_(document_ids))
                .values(current_version_id=None)
            )
            await session.execute(delete(KnowledgeDocumentVersion).where(KnowledgeDocumentVersion.id.in_(version_ids)))
            await session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id.in_(document_ids)))
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()
        get_knowledge_search_dependencies.cache_clear()
