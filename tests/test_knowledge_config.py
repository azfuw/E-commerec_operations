import json
import subprocess
from pathlib import Path

from backend.config import Settings


def test_knowledge_settings_have_local_nonsecret_defaults(monkeypatch) -> None:
    for name in (
        "KNOWLEDGE_UPLOAD_DIR",
        "KNOWLEDGE_EMBEDDING_MODEL_PATH",
        "KNOWLEDGE_RERANKER_MODEL_PATH",
        "MILVUS_URI",
        "MILVUS_COLLECTION",
        "KNOWLEDGE_LEASE_SECONDS",
        "KNOWLEDGE_DEPENDENCY_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None, jwt_secret_key="test-only-secret-at-least-32-characters")

    assert settings.knowledge_upload_dir == Path("data/uploads/knowledge")
    assert settings.knowledge_embedding_model_path == Path("model/bge-m3")
    assert settings.knowledge_reranker_model_path == Path("model/bge-reranker-v2-m3")
    assert (settings.milvus_uri, settings.milvus_collection) == (
        "http://localhost:19530",
        "knowledge_chunks",
    )
    assert settings.knowledge_lease_seconds == 60
    assert settings.knowledge_dependency_timeout_seconds == 30.0


def test_rendered_compose_has_milvus_infrastructure_without_knowledge_worker() -> None:
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=Path(__file__).parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    services = json.loads(result.stdout)["services"]

    assert {"postgres", "etcd", "minio", "milvus"} <= services.keys()
    assert "knowledge-worker" not in services
    assert services["etcd"]["healthcheck"]
    assert services["minio"]["healthcheck"]
    assert services["milvus"]["healthcheck"]
    assert services["milvus"]["depends_on"]["etcd"]["condition"] == "service_healthy"
    assert services["milvus"]["depends_on"]["minio"]["condition"] == "service_healthy"
