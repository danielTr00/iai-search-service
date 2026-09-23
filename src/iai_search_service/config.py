from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SEARCH_", extra="ignore")

    api_token: str = Field(min_length=20)
    searxng_url: str = "http://searxng:8080"
    max_concurrent_requests: int = Field(default=2, ge=1, le=8)
