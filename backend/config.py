from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env.local", extra="ignore")

    app_env: str = "development"
    database_url: str = "postgresql+asyncpg://ecommerce:ecommerce@localhost:5434/ecommerce"
    jwt_secret_key: SecretStr
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
