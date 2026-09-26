import asyncio
import errno
import ssl
import threading

import httpx
import pytest

from iai_search_service.models import ResearchRequest
from iai_search_service.service import ResearchService


class PeerStream:
    def __init__(self, address="93.184.216.34"):
        self.address = address

    def get_extra_info(self, name):
        return (self.address, 443) if name == "server_addr" else None


def page_response(status_code=200, *, text="", headers=None, peer="93.184.216.34"):
    return httpx.Response(
        status_code,
        text=text,
        headers=headers,
        extensions={"network_stream": PeerStream(peer)},
    )


async def public_resolver(host, port):
    return ["93.184.216.34"]


async def test_search_then_extract_returns_sources_and_partial_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "searxng":
            assert request.headers["x-forwarded-for"] == "127.0.0.1"
            assert request.headers["x-real-ip"] == "127.0.0.1"
            return httpx.Response(200, json={"results": [
                {"title": "One", "url": "https://one.example/a", "content": "first snippet"},
                {"title": "Two", "url": "https://two.example/b", "content": "second snippet"},
            ]})
        if request.headers.get("host") == "one.example":
            return page_response(200, text="<html><main><h1>One</h1><p>Useful public source text.</p></main></html>")
        return page_response(503, text="unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService(
            searxng_url="http://searxng:8080",
            client=client,
            resolver=public_resolver,
            max_concurrent_requests=2,
        )
        result = await service.research(ResearchRequest(task="test query", max_sources=2))

    assert result.strategy_used == "search_and_extract"
    assert [source.status for source in result.sources] == ["extracted", "fetch_error"]
    assert "Useful public source text" in (result.sources[0].content or "")
    assert result.sources[0].snippet == "first snippet"
    assert result.errors[0].stage == "fetch"


async def test_extract_failure_reports_safe_http_status_and_transport_cause():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("host") == "denied.example":
            return page_response(403, text="Access denied")
        raise httpx.ConnectError("private proxy address/token=secret", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=public_resolver)
        result = await service.research(ResearchRequest(urls=[
            "https://denied.example/article", "https://offline.example/article",
        ]))

    assert [error.message for error in result.errors] == ["HTTP 403", "connection error"]
    assert "private proxy" not in str(result.errors)


@pytest.mark.parametrize("cause, expected", [
    (OSError(errno.ENETUNREACH, "sensitive network detail"), "network unreachable"),
    (OSError(errno.ECONNREFUSED, "sensitive upstream detail"), "connection refused"),
    (ssl.SSLCertVerificationError("sensitive TLS detail"), "TLS verification failed"),
])
async def test_connection_failure_reports_safe_network_cause(cause, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        try:
            raise cause
        except OSError as exc:
            raise httpx.ConnectError("private host and token", request=request) from exc

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=public_resolver)
        result = await service.research(ResearchRequest(urls=["https://offline.example/article"]))

    assert [error.message for error in result.errors] == [expected]
    assert "private host" not in str(result.errors)
    assert "sensitive" not in str(result.errors)


async def test_long_research_task_searches_with_concise_topic_terms():
    task = (
        "Find authoritative, verifiable sources relevant to measurable developments in the "
        "EU premium passenger vehicle market through 2030, focused on software-defined vehicles, "
        "automotive software, connectivity, automated driving, and regulatory requirements. "
        "Include sources from EU institutions, European Commission, UNECE, ACEA, BMW, "
        "Mercedes-Benz, reputable market research if available. Return URLs and publication dates."
    )
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["q"])
        return httpx.Response(200, json={"results": [{
            "title": "EU automotive software", "url": "https://europa.eu/article", "content": "official snippet",
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client)
        result = await service.research(ResearchRequest(task=task, search_depth="fast"))

    assert result.query == task
    assert len(result.sources) == 1
    assert len(seen) == 1
    assert len(seen[0]) <= 150
    assert "EU premium passenger vehicle" in seen[0]
    assert "software-defined vehicles" in seen[0]
    assert "regulatory requirements" in seen[0]
    assert "measurable developments" not in seen[0]
    assert "Find authoritative" not in seen[0]
    assert "Return URLs" not in seen[0]


async def test_search_discards_google_login_and_search_navigation_links():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [
            {"title": "Sign in", "url": "https://accounts.google.com/v3/signin", "content": "Google login"},
            {"title": "Search", "url": "https://www.google.com/search?q=vehicles", "content": "Google search"},
            {"title": "EU vehicle policy", "url": "https://digital-strategy.ec.europa.eu/vehicle", "content": "EU text"},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client)
        result = await service.research(ResearchRequest(task="EU vehicle policy", search_depth="fast"))

    assert [source.url for source in result.sources] == ["https://digital-strategy.ec.europa.eu/vehicle"]


async def test_fast_search_returns_snippets_without_fetching_pages():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "searxng"
        return httpx.Response(200, json={"results": [
            {"title": "Quick result", "url": "https://quick.example/a", "content": "Useful snippet"},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=public_resolver)
        result = await service.research(ResearchRequest(task="fast query", search_depth="fast"))

    assert result.strategy_used == "search_only"
    assert len(result.sources) == 1
    assert result.sources[0].status == "snippet_only"
    assert result.sources[0].snippet == "Useful snippet"
    assert result.sources[0].content is None
    assert result.errors == []


async def test_fast_search_timeout_covers_the_configured_searxng_engine_deadline():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"]["read"] >= 12
        return httpx.Response(200, json={"results": [{
            "title": "Result", "url": "https://example.com/a", "content": "snippet",
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client)
        result = await service.research(ResearchRequest(task="something", search_depth="fast"))

    assert result.sources[0].status == "snippet_only"


async def test_advanced_search_overfetches_to_prefer_an_extractable_result():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "searxng":
            return httpx.Response(200, json={"results": [
                {"title": "Unavailable", "url": "https://bad.example/a", "content": "bad"},
                {"title": "Available", "url": "https://good.example/b", "content": "good"},
            ]})
        if request.headers.get("host") == "bad.example":
            return page_response(503, text="unavailable")
        return page_response(
            200,
            text=(
                "<html><body><article><h1>Available source</h1>"
                "<p>This source contains enough useful content for advanced search extraction. "
                "It should be preferred over the unavailable first candidate.</p>"
                "</article></body></html>"
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=public_resolver)
        result = await service.research(ResearchRequest(
            task="advanced query",
            search_depth="advanced",
            max_sources=1,
        ))

    assert len(result.sources) == 1
    assert result.sources[0].title == "Available"
    assert result.sources[0].status == "extracted"


async def test_legacy_quick_depth_keeps_extracting_page_content():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "searxng":
            return httpx.Response(200, json={"results": [
                {"title": "Legacy", "url": "https://legacy.example/a", "content": "snippet"},
            ]})
        return page_response(
            200,
            text=(
                "<html><body><article><h1>Legacy source</h1>"
                "<p>Legacy extracted page content with enough useful detail for deterministic extraction. "
                "This confirms that the old quick depth remains backwards compatible.</p>"
                "</article></body></html>"
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=public_resolver)
        result = await service.research(ResearchRequest(task="legacy", depth="quick"))

    assert result.strategy_used == "search_and_extract"
    assert result.sources[0].status == "extracted"


async def test_known_urls_skip_search():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return page_response(
            200,
            text=(
                "<html><body><article><h1>Documentation</h1>"
                "<p>Direct URL content with enough useful detail for extraction. "
                "This paragraph describes the source and its relevant result.</p>"
                "</article></body></html>"
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService(
            searxng_url="http://searxng:8080",
            client=client,
            resolver=public_resolver,
        )
        result = await service.research(ResearchRequest(task="read", urls=["https://example.com/doc"]))

    assert result.strategy_used == "extract_urls"
    assert seen == ["https://93.184.216.34/doc"]
    assert result.sources[0].status == "extracted"


async def test_extract_depth_and_format_are_honored_for_known_urls():
    html = (
        "<html><body><article><h1>Research paper</h1>"
        "<p>This is a substantial and useful research paper about agent systems and web sources.</p>"
        "<p>The paper provides relevant facts for an agent reading the source directly.</p>"
        "<p>It also highlights <strong>important evidence</strong> for the reader.</p>"
        "<table><tr><th>Metric</th><th>Value</th></tr><tr><td>RareMetric123</td><td>42</td></tr></table>"
        "</article></body></html>"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: page_response(200, text=html)
    )) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=public_resolver)
        basic = await service.research(ResearchRequest(urls=["https://example.com/paper"]))
        advanced = await service.research(ResearchRequest(
            urls=["https://example.com/paper"], extract_depth="advanced", content_format="markdown",
        ))

    assert "RareMetric123" not in (basic.sources[0].content or "")
    assert "RareMetric123" in (advanced.sources[0].content or "")
    assert advanced.sources[0].content != basic.sources[0].content


async def test_repeated_source_reuses_extracted_content_across_requests():
    fetched = []
    html = (
        "<html><body><article><h1>Repeated article</h1>"
        "<p>This article provides enough useful public source content to extract.</p>"
        "<p>The second call must reuse the first extraction without fetching again.</p>"
        "</article></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "searxng":
            return httpx.Response(200, json={"results": [{
                "title": "Search title", "url": "https://example.com/article", "content": "Current snippet",
            }]})
        fetched.append(str(request.url))
        return page_response(200, text=html)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=public_resolver)
        direct = await service.research(ResearchRequest(urls=["https://example.com/article#section"]))
        searched = await service.research(ResearchRequest(task="repeated source"))

    assert len(fetched) == 1
    assert searched.sources[0].title == "Search title"
    assert searched.sources[0].snippet == "Current snippet"
    assert searched.sources[0].url == "https://example.com/article"
    assert searched.sources[0].content == direct.sources[0].content


async def test_concurrent_repeated_source_is_only_fetched_once():
    fetches = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fetches
        fetches += 1
        await asyncio.sleep(0.03)
        return page_response(200, text=(
            "<html><body><article><h1>Shared source</h1>"
            "<p>The same public content must be fetched only once for concurrent users.</p>"
            "</article></body></html>"
        ))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=public_resolver)
        results = await asyncio.gather(*(
            service.research(ResearchRequest(urls=["https://example.com/article"]))
            for _ in range(20)
        ))

    assert fetches == 1
    assert all(result.sources[0].status == "extracted" for result in results)


async def test_cancelled_request_does_not_keep_a_fetch_running_outside_the_request_limit():
    started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.set()
        try:
            if request.headers["host"] == "slow.example":
                await release.wait()
            return page_response(200, text=(
                "<html><body><article><h1>Public page</h1>"
                "<p>Enough meaningful source content to extract without error.</p>"
                "</article></body></html>"
            ))
        finally:
            active -= 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client,
                                  resolver=public_resolver, max_concurrent_requests=1)
        cancelled = asyncio.create_task(service.research(ResearchRequest(
            urls=["https://slow.example/one"],
        )))
        await started.wait()
        cancelled.cancel()
        try:
            await cancelled
        except asyncio.CancelledError:
            pass
        second = await asyncio.wait_for(service.research(ResearchRequest(
            urls=["https://fast.example/two"],
        )), timeout=1)
        release.set()

    assert peak == 1
    assert active == 0
    assert second.sources[0].status == "extracted"


async def test_cancelled_fetch_cleanup_holds_capacity_until_it_finishes():
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    finish_cleanup = asyncio.Event()
    entered_second = asyncio.Event()
    active = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        if request.headers["host"] == "slow.example":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await finish_cleanup.wait()
                active -= 1
        else:
            entered_second.set()
            active -= 1
        return page_response(200, text="<article>Content available after cleanup.</article>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client,
                                  resolver=public_resolver, max_concurrent_requests=1)
        first = asyncio.create_task(service.research(ResearchRequest(urls=["https://slow.example/a"])))
        await started.wait()
        first.cancel()
        await cleanup_started.wait()
        second = asyncio.create_task(service.research(ResearchRequest(urls=["https://fast.example/b"])))
        await asyncio.sleep(0.03)
        assert not entered_second.is_set()
        finish_cleanup.set()
        await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), timeout=2)

    assert peak == 1
    assert entered_second.is_set()


async def test_cancelled_extraction_holds_capacity_until_thread_finishes(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0

    def extract(html, *, url, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(active, peak)
        try:
            if url.endswith("/slow"):
                started.set()
                release.wait(timeout=2)
            return "Enough text for a source to be considered extracted."
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr("iai_search_service.service.trafilatura.extract", extract)

    def handler(request: httpx.Request) -> httpx.Response:
        return page_response(200, text="<article>Valid content for processing.</article>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client,
                                  resolver=public_resolver, max_concurrent_requests=1)
        first = asyncio.create_task(service.research(ResearchRequest(urls=["https://slow.example/slow"])))
        assert await asyncio.to_thread(started.wait, 1)
        first.cancel()
        second = asyncio.create_task(service.research(ResearchRequest(urls=["https://fast.example/fast"])))
        try:
            await asyncio.sleep(0.03)
            assert peak == 1
        finally:
            release.set()
        await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), timeout=2)

    assert active == 0


async def test_redirect_dns_failure_is_partial_and_waits_for_sibling_extraction(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    active = 0
    peak = 0

    def extract(html, *, url, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            if url.endswith("/slow"):
                started.set()
                release.wait(timeout=2)
            return "Enough content to be extracted as a public source."
        finally:
            active -= 1

    monkeypatch.setattr("iai_search_service.service.trafilatura.extract", extract)

    async def resolver(host, port):
        if host == "broken.example":
            await asyncio.to_thread(started.wait, 1)
            raise OSError("DNS unavailable")
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "redirect.example":
            return page_response(302, headers={"Location": "https://broken.example/target"})
        return page_response(200, text="<html><article><p>Some public source content.</p></article></html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=resolver,
                                  max_concurrent_requests=1)
        first = asyncio.create_task(service.research(ResearchRequest(urls=[
            "https://slow.example/slow", "https://redirect.example/redirect",
        ], max_sources=2)))
        try:
            result = await asyncio.wait_for(asyncio.shield(first), timeout=0.05)
        except asyncio.TimeoutError:
            release.set()
            result = await asyncio.wait_for(first, timeout=2)
        finally:
            release.set()

    assert [source.status for source in result.sources] == ["extracted", "fetch_error"]
    assert peak == 1
    assert active == 0


async def test_repeated_query_reuses_search_results_but_keeps_fresh_request_ids():
    searches = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal searches
        assert request.url.host == "searxng"
        searches += 1
        return httpx.Response(200, json={"results": [{
            "title": "Result", "url": "https://example.com/source", "content": "Snippet",
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=public_resolver)
        first = await service.research(ResearchRequest(task="same topic", search_depth="fast"))
        second = await service.research(ResearchRequest(task="same topic", search_depth="fast"))
        different = await service.research(ResearchRequest(task="different topic", search_depth="fast"))

    assert searches == 2
    assert first.sources[0].url == second.sources[0].url
    assert first.request_id != second.request_id
    assert different.query == "different topic"


async def test_oversize_upstream_search_hit_is_rejected_before_fetch():
    fetched = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "searxng":
            return httpx.Response(200, json={"results": [{
                "title": "Long", "url": "https://example.com/" + "x" * 2_100,
                "content": "short",
            }]})
        fetched.append(str(request.url))
        return page_response(200, text="<article>Should never load.</article>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=public_resolver)
        result = await service.research(ResearchRequest(task="find oversized link"))

    assert fetched == []
    assert result.sources[0].status == "validation_error"


async def test_redirected_unicode_url_is_charged_by_encoded_bytes_in_source_cache():
    fetched = []
    redirected = "https://example.com/" + "é" * 900

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        if request.headers["host"] == "example.com" and str(request.url).endswith("/start"):
            return page_response(302, headers={b"Location": redirected.encode("latin-1")})
        return page_response(200, text=(
            "<html><body><article><h1>Study</h1>"
            "<p>Evidence and research text with sufficient detail for extraction.</p>"
            "</article></body></html>"
        ))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=public_resolver,
                                  source_cache_max_bytes=1_600)
        for _ in range(2):
            result = await service.research(ResearchRequest(urls=["https://example.com/start"]))
            assert result.sources[0].status == "extracted"

    assert len(fetched) == 4


async def test_allowed_domains_filter_search_results():
    queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.params["q"])
        return httpx.Response(200, json={"results": [
            {"title": "Allowed", "url": "https://docs.example.com/a", "content": "ok"},
            {"title": "Blocked", "url": "https://other.example/b", "content": "no"},
            {"title": "Lookalike", "url": "https://notexample.com/b", "content": "no"},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=public_resolver)
        result = await service.research(ResearchRequest(
            task="query", max_sources=1, search_depth="fast", allowed_domains=["EXAMPLE.com."]
        ))

    assert len(queries) == 1
    assert queries[0] == "site:example.com query"
    assert [source.url for source in result.sources] == ["https://docs.example.com/a"]


async def test_allowed_result_after_unrelated_top_results_survives_candidate_cap():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [
            {"url": f"https://other{i}.example/a"} for i in range(4)
        ] + [{"url": "https://docs.example.com/a", "content": "allowed"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=public_resolver)
        result = await service.research(ResearchRequest(
            task="query", max_sources=1, search_depth="fast", allowed_domains=["example.com"]
        ))

    assert [source.url for source in result.sources] == ["https://docs.example.com/a"]


async def test_multiple_allowed_domains_filter_before_cap_without_changing_query():
    queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.params["q"])
        return httpx.Response(200, json={"results": [
            {"url": "https://blocked.example/a"},
            {"url": "https://docs.one.example/a"},
            {"url": "https://news.two.example/a"},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client)
        result = await service.research(ResearchRequest(
            task="query", max_sources=2, search_depth="fast",
            allowed_domains=["one.example", "two.example"]
        ))

    assert queries == ["query"]
    assert [source.url for source in result.sources] == [
        "https://docs.one.example/a", "https://news.two.example/a"
    ]


async def test_empty_domain_filtered_results_do_not_retry_unrestricted():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["q"])
        return httpx.Response(200, json={"results": [{"url": "https://blocked.example/a"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client)
        result = await service.research(ResearchRequest(
            task="query", search_depth="fast", allowed_domains=["allowed.example"]
        ))

    assert calls == ["site:allowed.example query"]
    assert result.sources == []


async def test_fetch_pins_validated_ip_and_preserves_host_header():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return page_response(200, text="<article>Public documentation body.</article>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=public_resolver)
        await service.research(ResearchRequest(task="read", urls=["https://rebind.example/doc"]))

    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["host"] == "rebind.example"
    assert requests[0].headers["connection"] == "close"
    assert requests[0].extensions["sni_hostname"] == "rebind.example"


async def test_network_errors_are_sanitized():
    async def broken_resolver(host, port):
        raise OSError("internal resolver detail")

    service = ResearchService("http://searxng:8080", resolver=broken_resolver)
    result = await service.research(ResearchRequest(task="read", urls=["https://bad.example/doc"]))
    assert result.errors[0].message == "target did not resolve"
    assert "internal resolver detail" not in result.errors[0].message


async def test_missing_or_mismatched_peer_metadata_fails_closed():
    responses = [
        httpx.Response(200, text="<p>missing peer</p>"),
        page_response(200, text="<p>wrong peer</p>", peer="93.184.216.35"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=public_resolver)
        missing = await service.research(ResearchRequest(task="x", urls=["https://one.example/"]))
        mismatch = await service.research(ResearchRequest(task="x", urls=["https://two.example/"]))

    assert missing.sources[0].status == "fetch_error"
    assert missing.errors[0].message == "connected peer could not be verified"
    assert mismatch.sources[0].status == "fetch_error"
    assert mismatch.errors[0].message == "connected peer did not match validated target"


async def test_redirect_to_private_target_is_rejected_before_second_request():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return page_response(302, headers={"location": "http://127.0.0.1/secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=public_resolver)
        result = await service.research(ResearchRequest(task="x", urls=["https://public.example/start"]))

    assert seen == ["https://93.184.216.34/start"]
    assert result.sources[0].status == "fetch_error"
    assert result.errors[0].message == "target must resolve only to public addresses"


async def test_redirect_to_excluded_domain_is_rejected_before_second_request():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return page_response(302, headers={"location": "https://blocked.example/secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=public_resolver)
        result = await service.research(ResearchRequest(
            task="x",
            urls=["https://public.example/start"],
            excluded_domains=["blocked.example"],
        ))

    assert seen == ["https://93.184.216.34/start"]
    assert result.sources[0].status == "fetch_error"
    assert result.errors[0].message == "target domain is not permitted"


async def test_cached_redirect_cannot_bypass_later_domain_restrictions():
    requested_hosts = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.headers["host"]
        requested_hosts.append(host)
        if host == "public.example":
            return page_response(302, headers={"location": "https://blocked.example/article"})
        return page_response(200, text=(
            "<html><body><article><h1>Redirected source</h1>"
            "<p>This is public, extractable content on the redirected domain.</p>"
            "</article></body></html>"
        ))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=public_resolver)
        first = await service.research(ResearchRequest(urls=["https://public.example/start"]))
        restricted = await service.research(ResearchRequest(
            urls=["https://public.example/start"], excluded_domains=["blocked.example"],
        ))

    assert first.sources[0].status == "extracted"
    assert restricted.sources[0].status == "fetch_error"
    assert requested_hosts == ["public.example", "blocked.example", "public.example"]


async def test_failed_extraction_is_retried_instead_of_cached():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return page_response(503, text="unavailable")
        return page_response(200, text=(
            "<html><body><article><h1>Recovered source</h1>"
            "<p>This content can be extracted after a transient upstream error.</p>"
            "</article></body></html>"
        ))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=public_resolver)
        first = await service.research(ResearchRequest(urls=["https://example.com/source"]))
        second = await service.research(ResearchRequest(urls=["https://example.com/source"]))

    assert first.sources[0].status == "fetch_error"
    assert second.sources[0].status == "extracted"
    assert calls == 2


async def test_ipv6_literal_is_dialed_with_bracketed_host_header():
    seen = []

    async def ipv6_resolver(host, port):
        return ["2001:4860:4860::8888"]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return page_response(
            200,
            text=(
                "<html><body><article><h1>IPv6 source</h1>"
                "<p>Public documentation body with sufficient text for deterministic extraction. "
                "The content confirms that the validated IPv6 address was used.</p>"
                "</article></body></html>"
            ),
            peer="2001:4860:4860::8888",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=ipv6_resolver)
        result = await service.research(ResearchRequest(task="x", urls=["https://v6.example:8443/doc"]))

    assert result.sources[0].status == "extracted"
    assert str(seen[0].url) == "https://[2001:4860:4860::8888]:8443/doc"
    assert seen[0].headers["host"] == "v6.example:8443"
    assert seen[0].extensions["sni_hostname"] == "v6.example"
