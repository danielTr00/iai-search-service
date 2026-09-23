from __future__ import annotations

import hmac
from typing import Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, status

from .config import Settings
from .models import ResearchRequest, ResearchResponse
from .service import ResearchService


class Researcher(Protocol):
    async def research(self, request: ResearchRequest) -> ResearchResponse: ...


def create_app(*, service: Researcher | None = None, api_token: str | None = None) -> FastAPI:
    if service is None or api_token is None:
        settings = Settings()
        service = service or ResearchService(
            settings.searxng_url,
            max_concurrent_requests=settings.max_concurrent_requests,
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

    return app


app = create_app()
