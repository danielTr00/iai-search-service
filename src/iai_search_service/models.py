from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ResearchRequest(BaseModel):
    task: str = Field(default="", max_length=2_000)
    urls: list[str] = Field(default_factory=list, max_length=8)
    depth: Literal["quick", "standard"] = "standard"
    max_sources: int = Field(default=3, ge=1, le=8)
    freshness: Literal["any", "day", "month", "year"] = "any"
    allowed_domains: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("task")
    @classmethod
    def strip_task(cls, value: str) -> str:
        return value.strip()

    @field_validator("allowed_domains")
    @classmethod
    def normalize_domains(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            domain = value.strip().lower().rstrip(".")
            if not domain or "/" in domain or ":" in domain:
                raise ValueError("allowed domains must be plain host names")
            normalized.append(domain)
        return normalized

    @model_validator(mode="after")
    def require_work(self) -> "ResearchRequest":
        if not self.task and not self.urls:
            raise ValueError("task or urls is required")
        return self


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
    strategy_used: Literal["search_and_extract", "extract_urls"]
    sources: list[ResearchSource]
    errors: list[ResearchError]
    duration_ms: int
