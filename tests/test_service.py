import httpx

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


async def test_allowed_domains_filter_search_results():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "searxng":
            return httpx.Response(200, json={"results": [
                {"title": "Allowed", "url": "https://docs.example.com/a", "content": "ok"},
                {"title": "Blocked", "url": "https://other.example/b", "content": "no"},
            ]})
        return page_response(200, text="<p>Allowed domain body text.</p>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ResearchService("http://searxng:8080", client=client, resolver=public_resolver)
        result = await service.research(ResearchRequest(
            task="query", max_sources=3, allowed_domains=["example.com"]
        ))

    assert [source.url for source in result.sources] == ["https://docs.example.com/a"]


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
