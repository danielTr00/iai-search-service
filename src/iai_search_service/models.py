from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ResearchRequest(BaseModel):
    task: str = Field(default="", max_length=2_000)
    urls: list[str] = Field(default_factory=list, max_length=8)
    search_depth: Literal["advanced", "basic", "fast", "ultra-fast"] = "basic"
    depth: Literal["quick", "standard"] | None = Field(default=None, exclude=True)
    max_sources: int = Field(default=3, ge=1, le=8)
    freshness: Literal["any", "day", "week", "month", "year"] = "any"
    topic: Literal["general", "news"] = "general"
    extract_depth: Literal["basic", "advanced"] = "basic"
    content_format: Literal["text", "markdown"] = "text"
    allowed_domains: list[str] = Field(default_factory=list, max_length=20)
    excluded_domains: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("urls")
    @classmethod
    def limit_urls(cls, values: list[str]) -> list[str]:
        if any(len(url) > 2_048 for url in values):
            raise ValueError("URL exceeds 2048 characters")
        return values

    @field_validator("task")
    @classmethod
    def strip_task(cls, value: str) -> str:
        return value.strip()

    @field_validator("allowed_domains", "excluded_domains")
    @classmethod
    def normalize_domains(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            domain = value.strip().lower().rstrip(".")
            if not domain or len(domain) > 253 or "/" in domain or ":" in domain:
                raise ValueError("allowed domains must be plain host names")
            normalized.append(domain)
        return normalized

    @model_validator(mode="after")
    def require_work(self) -> "ResearchRequest":
        if self.depth is not None:
            self.search_depth = "basic"
        if not self.task and not self.urls:
            raise ValueError("task or urls is required")
        return self


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2_000)
    search_depth: Literal["advanced", "basic", "fast", "ultra-fast"] = "basic"
    topic: Literal["general", "news"] = "general"
    time_range: Literal["day", "week", "month", "year"] | None = None
    max_results: int = Field(default=5, ge=1, le=8)
    include_domains: list[str] = Field(default_factory=list, max_length=20)
    exclude_domains: list[str] = Field(default_factory=list, max_length=20)
    include_raw_content: bool = False

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query is required")
        return value

    @field_validator("include_domains", "exclude_domains")
    @classmethod
    def normalize_search_domains(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            domain = value.strip().lower().rstrip(".")
            if not domain or len(domain) > 253 or "/" in domain or ":" in domain:
                raise ValueError("domains must be plain host names")
            normalized.append(domain)
        return normalized


class SearchResult(BaseModel):
    title: str
    url: str
    content: str
    score: float = 0.0
    raw_content: str | None = None


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
    response_time: float
    request_id: str


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    urls: str | list[str]
    extract_depth: Literal["basic", "advanced"] = "basic"
    content_format: Literal["text", "markdown"] = Field(default="markdown", alias="format")

    @field_validator("urls")
    @classmethod
    def validate_urls(cls, value: str | list[str]) -> str | list[str]:
        urls = [value] if isinstance(value, str) else value
        if not 1 <= len(urls) <= 8 or any(
            not isinstance(url, str) or not url.strip() or len(url) > 2_048 for url in urls
        ):
            raise ValueError("urls must contain 1 to 8 non-empty URL strings")
        return value


class ExtractResult(BaseModel):
    url: str
    raw_content: str


class ExtractFailure(BaseModel):
    url: str
    error: str


class ExtractResponse(BaseModel):
    results: list[ExtractResult]
    failed_results: list[ExtractFailure]
    response_time: float
    request_id: str


class ResearchError(BaseModel):
    stage: Literal["search", "validation", "fetch", "extract"]
    message: str
    url: str | None = None


class ResearchSource(BaseModel):
    title: str | None = None
    url: str
    snippet: str | None = None
    content: str | None = None
    status: Literal["extracted", "snippet_only", "validation_error", "fetch_error", "extract_error"]


class ResearchResponse(BaseModel):
    request_id: str
    query: str
    strategy_used: Literal["search_only", "search_and_extract", "extract_urls"]
    sources: list[ResearchSource]
    errors: list[ResearchError]
    duration_ms: int
