import os

from fastapi.testclient import TestClient

os.environ.setdefault("SEARCH_API_TOKEN", "test-token-at-least-twenty-characters")

from iai_search_service.main import create_app
from iai_search_service.models import ResearchError, ResearchResponse, ResearchSource


class StubResearchService:
    def __init__(self):
        self.last_request = None

    async def research(self, request):
        self.last_request = request
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


def test_search_accepts_tavily_style_core_configuration():
    service = StubResearchService()
    client = TestClient(create_app(service=service, api_token="secret"))
    response = client.post(
        "/search",
        headers={"Authorization": "Bearer secret"},
        json={
            "query": "open source search",
            "search_depth": "advanced",
            "topic": "news",
            "max_results": 5,
            "time_range": "month",
            "include_domains": ["example.com"],
            "exclude_domains": ["blocked.example"],
            "include_raw_content": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "open source search"
    assert body["results"] == []
    assert body["response_time"] == 0.012
    assert body["request_id"] == "req-1"
    assert service.last_request.search_depth == "advanced"
    assert service.last_request.topic == "news"
    assert service.last_request.max_sources == 5
    assert service.last_request.freshness == "month"
    assert service.last_request.allowed_domains == ["example.com"]
    assert service.last_request.excluded_domains == ["blocked.example"]


def test_search_rejects_whitespace_query_and_malformed_domains_as_client_errors():
    client = TestClient(create_app(service=StubResearchService(), api_token="secret"))
    headers = {"Authorization": "Bearer secret"}

    assert client.post("/search", headers=headers, json={"query": "   "}).status_code == 422
    assert client.post(
        "/search",
        headers=headers,
        json={"query": "valid", "exclude_domains": ["https://example.com"]},
    ).status_code == 422
    assert client.post(
        "/search",
        headers=headers,
        json={"query": "valid", "include_raw_content": "markdown"},
    ).status_code == 422


def test_search_maps_extracted_content_to_tavily_style_results():
    class ContentService:
        async def research(self, request):
            return ResearchResponse(
                request_id="req-content",
                query=request.task,
                strategy_used="search_and_extract",
                sources=[ResearchSource(
                    title="Source",
                    url="https://example.com/source",
                    snippet="Short snippet",
                    content="Extracted page content",
                    status="extracted",
                )],
                errors=[],
                duration_ms=250,
            )

    client = TestClient(create_app(service=ContentService(), api_token="secret"))
    response = client.post(
        "/search",
        headers={"Authorization": "Bearer secret"},
        json={"query": "mapped content", "include_raw_content": True},
    )

    assert response.status_code == 200
    assert response.json()["results"] == [{
        "title": "Source",
        "url": "https://example.com/source",
        "content": "Extracted page content",
        "score": 0.0,
        "raw_content": "Extracted page content",
    }]


def test_extract_accepts_tavily_style_core_configuration():
    class ExtractService:
        def __init__(self):
            self.last_request = None

        async def research(self, request):
            self.last_request = request
            return ResearchResponse(
                request_id="req-extract",
                query=request.task,
                strategy_used="extract_urls",
                sources=[ResearchSource(
                    title=None,
                    url="https://example.com/source",
                    content="Extracted page content",
                    status="extracted",
                )],
                errors=[],
                duration_ms=500,
            )

    service = ExtractService()
    client = TestClient(create_app(service=service, api_token="secret"))
    response = client.post(
        "/extract",
        headers={"Authorization": "Bearer secret"},
        json={
            "urls": ["https://example.com/source"],
            "extract_depth": "advanced",
            "format": "text",
        },
    )

    assert response.status_code == 200
    assert service.last_request.extract_depth == "advanced"
    assert response.json() == {
        "results": [{"url": "https://example.com/source", "raw_content": "Extracted page content"}],
        "failed_results": [],
        "response_time": 0.5,
        "request_id": "req-extract",
    }


def test_extract_supports_single_url_and_returns_partial_failures():
    class MixedService:
        async def research(self, request):
            return ResearchResponse(
                request_id="mixed",
                query=request.task,
                strategy_used="extract_urls",
                sources=[ResearchSource(url="https://example.com/ok", content="Article", status="extracted"),
                         ResearchSource(url="https://example.com/missing", status="fetch_error")],
                errors=[ResearchError(stage="fetch", url="https://example.com/missing", message="connection failed")],
                duration_ms=100,
            )

    client = TestClient(create_app(service=MixedService(), api_token="secret"))
    headers = {"Authorization": "Bearer secret"}
    assert client.post("/extract", json={"urls": "https://example.com/ok"}).status_code == 401
    body = client.post("/extract", headers=headers, json={"urls": [
        "https://example.com/ok", "https://example.com/missing",
    ], "format": "text"}).json()
    assert body["results"] == [{"url": "https://example.com/ok", "raw_content": "Article"}]
    assert body["failed_results"] == [{"url": "https://example.com/missing", "error": "connection failed"}]
    assert client.post("/extract", headers=headers, json={"urls": []}).status_code == 422
    assert client.post("/extract", headers=headers, json={"urls": "https://example.com/ok", "query": "x"}).status_code == 422


def test_extract_and_search_reject_oversize_urls_and_domains():
    client = TestClient(create_app(service=StubResearchService(), api_token="secret"))
    headers = {"Authorization": "Bearer secret"}
    assert client.post("/extract", headers=headers, json={
        "urls": "https://example.com/" + "x" * 2_100,
    }).status_code == 422
    assert client.post("/search", headers=headers, json={
        "query": "public topic", "include_domains": ["a" * 300 + ".example"],
    }).status_code == 422
