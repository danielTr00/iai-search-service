from __future__ import annotations

import hmac
from typing import Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, status

from .config import Settings
from .models import (
    ExtractFailure, ExtractRequest, ExtractResponse, ExtractResult,
    ResearchRequest, ResearchResponse, SearchRequest, SearchResponse, SearchResult,
)
from .service import ResearchService


class Researcher(Protocol):
    async def research(self, request: ResearchRequest) -> ResearchResponse: ...


def create_app(*, service: Researcher | None = None, api_token: str | None = None) -> FastAPI:
    if service is None or api_token is None:
        settings = Settings()
        service = service or ResearchService(
            settings.searxng_url,
            max_concurrent_requests=settings.max_concurrent_requests,
            source_cache_ttl_seconds=settings.source_cache_ttl_seconds,
            source_cache_max_entries=settings.source_cache_max_entries,
            source_cache_max_bytes=settings.source_cache_max_bytes,
            query_cache_ttl_seconds=settings.query_cache_ttl_seconds,
            query_cache_max_entries=settings.query_cache_max_entries,
            query_cache_max_bytes=settings.query_cache_max_bytes,
        )
        api_token = api_token or settings.api_token

    app = FastAPI(
        title="iAi Search Service",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def require_token(authorization: str | None = Header(default=None)) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
        supplied = authorization.removeprefix("Bearer ")
        if not hmac.compare_digest(supplied, api_token):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/research", response_model=ResearchResponse, dependencies=[Depends(require_token)])
    async def research(request: ResearchRequest) -> ResearchResponse:
        try:
            return await service.research(request)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"research backend failed: {type(exc).__name__}") from exc

    @app.post("/search", response_model=SearchResponse, dependencies=[Depends(require_token)])
    async def search(request: SearchRequest) -> SearchResponse:
        try:
            result = await service.research(
                ResearchRequest(
                    task=request.query,
                    search_depth=request.search_depth,
                    max_sources=request.max_results,
                    freshness=request.time_range or "any",
                    topic=request.topic,
                    allowed_domains=request.include_domains,
                    excluded_domains=request.exclude_domains,
                )
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"research backend failed: {type(exc).__name__}") from exc
        include_raw = bool(request.include_raw_content)
        return SearchResponse(
            query=result.query,
            results=[
                SearchResult(
                    title=source.title or source.url,
                    url=source.url,
                    content=source.content or source.snippet or "",
                    raw_content=source.content if include_raw else None,
                )
                for source in result.sources
            ],
            response_time=result.duration_ms / 1_000,
            request_id=result.request_id,
        )

    @app.post("/extract", response_model=ExtractResponse, dependencies=[Depends(require_token)])
    async def extract(request: ExtractRequest) -> ExtractResponse:
        urls = [request.urls] if isinstance(request.urls, str) else request.urls
        try:
            result = await service.research(ResearchRequest(
                urls=urls,
                max_sources=len(urls),
                extract_depth=request.extract_depth,
                content_format=request.content_format,
            ))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"research backend failed: {type(exc).__name__}") from exc
        failures = {error.url: error.message for error in result.errors}
        return ExtractResponse(
            results=[
                ExtractResult(url=source.url, raw_content=source.content)
                for source in result.sources if source.status == "extracted" and source.content is not None
            ],
            failed_results=[
                ExtractFailure(url=source.url, error=failures.get(source.url, source.status))
                for source in result.sources if source.status != "extracted"
            ],
            response_time=result.duration_ms / 1_000,
            request_id=result.request_id,
        )

    return app


app = create_app()
