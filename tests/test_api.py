import os

from fastapi.testclient import TestClient

os.environ.setdefault("SEARCH_API_TOKEN", "test-token-at-least-twenty-characters")

from iai_search_service.main import create_app
from iai_search_service.models import ResearchResponse


class StubResearchService:
    async def research(self, request):
        return ResearchResponse(
            request_id="req-1",
            query=request.task,
            strategy_used="search_and_extract",
            sources=[],
            errors=[],
            duration_ms=12,
        )


def test_health_is_public():
    client = TestClient(create_app(service=StubResearchService(), api_token="secret"))
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/openapi.json").status_code == 404


def test_research_requires_bearer_token():
    client = TestClient(create_app(service=StubResearchService(), api_token="secret"))
    response = client.post("/v1/research", json={"task": "open source search"})
    assert response.status_code == 401


def test_research_returns_structured_response():
    client = TestClient(create_app(service=StubResearchService(), api_token="secret"))
    response = client.post(
        "/v1/research",
        headers={"Authorization": "Bearer secret"},
        json={"task": "open source search", "max_sources": 3},
    )
    assert response.status_code == 200
    assert response.json() == {
        "request_id": "req-1",
        "query": "open source search",
        "strategy_used": "search_and_extract",
        "sources": [],
        "errors": [],
        "duration_ms": 12,
    }


def test_research_rejects_empty_work():
    client = TestClient(create_app(service=StubResearchService(), api_token="secret"))
    response = client.post(
        "/v1/research",
        headers={"Authorization": "Bearer secret"},
        json={"task": "", "urls": []},
    )
    assert response.status_code == 422
