import json
from dataclasses import dataclass

from backend.knowledge_search import KnowledgeSearchHit, RetrievalPath


@dataclass(frozen=True)
class RetrievalQueryOutcome:
    query_id: str
    hits: list[KnowledgeSearchHit]
    elapsed_ms: float


@dataclass(frozen=True)
class RetrievalMetrics:
    recall_at_10: float
    mrr: float
    citation_document_version_accuracy: float
    latency_ms: float


def _matches(hit: KnowledgeSearchHit, query: dict[str, object]) -> bool:
    return (
        hit.document_name == query["expected_document_name"]
        and hit.version_number == query["expected_version_number"]
        and query["expected_section"] in hit.chunk_metadata.get("heading_path", [])
    )


def evaluate_retrieval(
    queries: list[dict[str, object]],
    outcomes: dict[RetrievalPath, dict[str, RetrievalQueryOutcome]],
) -> dict[RetrievalPath, RetrievalMetrics]:
    results = {}
    for path in ("dense", "sparse", "hybrid", "hybrid_rerank"):
        relevant = reciprocal_rank = citations = elapsed = 0.0
        for query in queries:
            outcome = outcomes[path][str(query["id"])]
            elapsed += outcome.elapsed_ms
            matches = [_matches(hit, query) for hit in outcome.hits[:10]]
            if any(matches):
                relevant += 1
                reciprocal_rank += 1 / (matches.index(True) + 1)
            if outcome.hits and (
                outcome.hits[0].document_name == query["expected_document_name"]
                and outcome.hits[0].version_number == query["expected_version_number"]
            ):
                citations += 1
        count = len(queries)
        results[path] = RetrievalMetrics(
            recall_at_10=relevant / count if count else 0.0,
            mrr=reciprocal_rank / count if count else 0.0,
            citation_document_version_accuracy=citations / count if count else 0.0,
            latency_ms=elapsed / count if count else 0.0,
        )
    return results


def select_calibration(
    candidates: list[dict[str, object]], metrics_by_candidate: dict[str, RetrievalMetrics]
) -> dict[str, object]:
    return min(
        candidates,
        key=lambda candidate: (
            -metrics_by_candidate[str(candidate["id"])].recall_at_10,
            -metrics_by_candidate[str(candidate["id"])].mrr,
            int(candidate["candidate_limit"]),
            json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ),
    )
