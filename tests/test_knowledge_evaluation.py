import pytest

from backend.knowledge_evaluation import (
    RetrievalQueryOutcome,
    RetrievalMetrics,
    evaluate_retrieval,
    select_calibration,
)
from backend.knowledge_search import KnowledgeSearchHit


def _hit(
    chunk_id: str, *, document_name: str = "项目演示规则", version_number: int = 1, heading: str = "标题关键词"
) -> KnowledgeSearchHit:
    return KnowledgeSearchHit(
        chunk_id=chunk_id,
        document_name=document_name,
        version_number=version_number,
        category="演示",
        canonical_text="项目演示规则正文",
        chunk_metadata={"heading_path": [heading], "paragraph_index": 0},
        dense_score=0.5,
        sparse_score=None,
        fusion_score=None,
        reranker_score=None,
        final_score=0.5,
    )


def test_evaluation_reports_each_path_recall_mrr_citation_and_latency() -> None:
    queries = [
        {
            "id": "q-title",
            "expected_document_name": "项目演示规则",
            "expected_version_number": 1,
            "expected_section": "标题关键词",
        }
    ]
    paths = ("dense", "sparse", "hybrid", "hybrid_rerank")
    outcomes = {
        path: {
            "q-title": RetrievalQueryOutcome(
                query_id="q-title",
                hits=[_hit("wrong", document_name="其他规则"), _hit("right")],
                elapsed_ms=12.5,
            )
        }
        for path in paths
    }

    metrics = evaluate_retrieval(queries, outcomes)

    assert set(metrics) == set(paths)
    for value in metrics.values():
        assert value == RetrievalMetrics(
            recall_at_10=1.0,
            mrr=0.5,
            citation_document_version_accuracy=0.0,
            latency_ms=12.5,
        )


def test_evaluation_matches_expected_section_within_full_heading() -> None:
    queries = [
        {
            "id": "q-title",
            "expected_document_name": "项目演示规则",
            "expected_version_number": 1,
            "expected_section": "标题关键词",
        }
    ]
    outcomes = {
        path: {
            "q-title": RetrievalQueryOutcome(
                query_id="q-title",
                hits=[_hit("right", heading="标题关键词｜项目演示规则：平台中立")],
                elapsed_ms=12.5,
            )
        }
        for path in ("dense", "sparse", "hybrid", "hybrid_rerank")
    }

    metrics = evaluate_retrieval(queries, outcomes)

    assert all(value.recall_at_10 == value.mrr == 1.0 for value in metrics.values())


def test_calibration_selection_prefers_metrics_then_lower_limit_then_json_order() -> None:
    candidates = [
        {"id": "wide", "candidate_limit": 20, "rrf_k": 60, "threshold": 0.1},
        {"id": "narrow-z", "candidate_limit": 10, "rrf_k": 60, "threshold": 0.2},
        {"id": "narrow-a", "candidate_limit": 10, "rrf_k": 60, "threshold": 0.1},
    ]
    metrics = {
        candidate["id"]: RetrievalMetrics(0.9, 0.7, 1.0, 12.0)
        for candidate in candidates
    }

    assert select_calibration(candidates, metrics) == candidates[2]
