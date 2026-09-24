import importlib
import io
import json

import pytest


def test_probe_rejects_successful_http_responses_without_usable_sources(monkeypatch):
    monkeypatch.setenv("SEARCH_API_TOKEN", "test-token-not-secret")
    bench = importlib.import_module("scripts.deployment_benchmark")

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    monkeypatch.setattr(bench.urllib.request, "urlopen", lambda *args, **kwargs: Response(b'{"results": []}'))

    with pytest.raises(ValueError, match="short_search returned no results"):
        bench.probe()


def test_live_probe_checks_agent_search_and_extract_routes_without_logging_content(monkeypatch):
    monkeypatch.setenv("SEARCH_API_TOKEN", "test-token-not-secret")
    bench = importlib.import_module("scripts.deployment_benchmark")
    seen = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()

    def urlopen(request, timeout):
        assert request.headers["Authorization"] == "Bearer test-token-not-secret"
        seen.append((request.full_url, json.loads(request.data)))
        if request.full_url.endswith("/search"):
            return Response(json.dumps({"results": [{"url": "https://source.example", "content": "secret raw content"}]}).encode())
        return Response(json.dumps({"results": [{"url": "https://docs.searxng.org", "raw_content": "secret raw content"}],
                                    "failed_results": [{"url": "https://example.org", "error": "HTTP 403"}]}).encode())

    monkeypatch.setattr(bench.urllib.request, "urlopen", urlopen)
    result = bench.probe()
    assert [url.rsplit("/", 1)[-1] for url, _ in seen] == ["search", "search", "extract"]
    assert all(isinstance(body, dict) for _, body in seen)
    assert result[0]["results"] == 1
    assert result[0]["hosts"] == ["source.example"]
    assert result[1]["results"] == 1
    assert result[2]["hosts"] == ["docs.searxng.org"]
    assert result[2]["failed_results"] == ["HTTP 403"]
    assert "secret raw content" not in str(result)
