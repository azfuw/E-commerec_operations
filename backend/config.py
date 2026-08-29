from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env.local", env_ignore_empty=True, extra="ignore", hide_input_in_errors=True
    )

    app_env: str = "development"
    database_url: str = "postgresql+asyncpg://ecommerce:ecommerce@localhost:5434/ecommerce"
    jwt_secret_key: SecretStr
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60
    deepseek_api_key: SecretStr | None = None
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_timeout_seconds: float = 30.0
    deepseek_price_per_million_tokens: Decimal | None = None
    langgraph_database_url: str = "postgresql://ecommerce:ecommerce@localhost:5434/ecommerce"
    analysis_lease_seconds: int = 60
    optimization_lease_seconds: int = 60
    knowledge_upload_dir: Path = Path("data/uploads/knowledge")
    knowledge_embedding_model_path: Path = Path("model/bge-m3")
    knowledge_reranker_model_path: Path = Path("model/bge-reranker-v2-m3")
    milvus_uri: str = "http://localhost:19530"
    milvus_collection: str = "knowledge_chunks"
    knowledge_lease_seconds: int = 60
    knowledge_dependency_timeout_seconds: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
