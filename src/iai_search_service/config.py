from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SEARCH_", extra="ignore")

    api_token: str = Field(min_length=20)
    searxng_url: str = "http://searxng:8080"
    max_concurrent_requests: int = Field(default=2, ge=1, le=8)
    source_cache_ttl_seconds: int = Field(default=21_600, ge=0, le=86_400)
    source_cache_max_entries: int = Field(default=256, ge=0, le=2_000)
    source_cache_max_bytes: int = Field(default=16_000_000, ge=0, le=64_000_000)
    query_cache_ttl_seconds: int = Field(default=300, ge=0, le=3_600)
    query_cache_max_entries: int = Field(default=128, ge=0, le=1_000)
    query_cache_max_bytes: int = Field(default=2_000_000, ge=0, le=16_000_000)
