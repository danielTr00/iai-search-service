from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import httpx
import trafilatura

from .cache import BoundedTTLCache
from .extract_worker import run_extraction
from .models import ResearchError, ResearchRequest, ResearchResponse, ResearchSource
from .security import ResolvedPublicUrl, Resolver, UnsafeUrlError, resolve_public_url


_MAX_BODY_BYTES = 2_000_000
_MAX_CONTENT_CHARS = 24_000
_MAX_REDIRECTS = 3
_SEARCH_STOPWORDS = frozenset({
    "find", "search", "research", "authoritative", "verifiable", "sources", "source",
    "relevant", "measurable", "developments", "focused", "focus", "include", "including", "return", "urls", "url",
    "publication", "dates", "claims", "specific", "reputable", "available", "if",
    "the", "a", "an", "in", "on", "of", "to", "for", "from", "through", "with",
    "and", "or", "about", "that", "which", "are", "is", "be", "by",
})


def _search_query(task: str) -> str:
    if len(task) <= 160:
        return task
    terms = [
        word for word in re.findall(r"[^\W_]+(?:-[^\W_]+)*", task)
        if word.casefold() not in _SEARCH_STOPWORDS
    ][:15]
    while terms and len(" ".join(terms)) > 150:
        terms.pop()
    return " ".join(terms) or task[:150]


def _is_source_page(item: dict) -> bool:
    parsed = urlsplit(str(item.get("url") or ""))
    host = (parsed.hostname or "").lower()
    if host == "accounts.google.com":
        return False
    return not (host in {"google.com", "www.google.com"} and parsed.path in {"/search", "/url"})


class ResearchService:
    def __init__(
        self,
        searxng_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
        max_concurrent_requests: int = 2,
        source_cache_ttl_seconds: int = 21_600,
        source_cache_max_entries: int = 256,
        source_cache_max_bytes: int = 16_000_000,
        query_cache_ttl_seconds: int = 300,
        query_cache_max_entries: int = 128,
        query_cache_max_bytes: int = 2_000_000,
    ) -> None:
        self.searxng_url = searxng_url.rstrip("/")
        self._client = client
        self._resolver = resolver
        self._request_slots = asyncio.Semaphore(max_concurrent_requests)
        self._source_cache = BoundedTTLCache(
            ttl_seconds=source_cache_ttl_seconds,
            max_entries=source_cache_max_entries,
            max_bytes=source_cache_max_bytes,
        )
        self._query_cache = BoundedTTLCache(
            ttl_seconds=query_cache_ttl_seconds,
            max_entries=query_cache_max_entries,
            max_bytes=query_cache_max_bytes,
        )

    async def research(self, request: ResearchRequest) -> ResearchResponse:
        started = time.monotonic()
        request_id = uuid.uuid4().hex[:12]
        async with self._request_slots:
            if request.urls:
                candidates = [
                    {"title": None, "url": url, "content": None}
                    for url in request.urls[: request.max_sources]
                ]
                strategy = "extract_urls"
            else:
                candidates = await self._search(request)
                strategy = (
                    "search_only"
                    if request.search_depth in {"fast", "ultra-fast"}
                    else "search_and_extract"
                )
            candidates = self._filter_domains(candidates, request.allowed_domains)
            candidates = self._exclude_domains(candidates, request.excluded_domains)
            if strategy == "search_only":
                sources = [
                    ResearchSource(
                        title=item.get("title"),
                        url=str(item.get("url") or ""),
                        snippet=item.get("content") or item.get("snippet"),
                        status="snippet_only",
                    )
                    for item in candidates[: request.max_sources]
                    if item.get("url")
                ]
                errors = []
            else:
                candidate_limit = request.max_sources * 2 if request.search_depth == "advanced" else request.max_sources
                sources, errors = await self._extract_candidates(
                    candidates[:candidate_limit],
                    request.allowed_domains,
                    request.excluded_domains,
                    request.extract_depth if request.urls else ("advanced" if request.search_depth == "advanced" else "basic"),
                    request.content_format,
                )
                if request.search_depth == "advanced":
                    sources = sorted(sources, key=lambda source: source.status != "extracted")[: request.max_sources]
        return ResearchResponse(
            request_id=request_id,
            query=request.task,
            strategy_used=strategy,
            sources=sources,
            errors=errors,
            duration_ms=round((time.monotonic() - started) * 1000),
        )

    async def _search(self, request: ResearchRequest) -> list[dict]:
        key = (request.task, request.topic, request.freshness, request.search_depth, request.max_sources)
        return await self._query_cache.get_or_load(
            key,
            lambda: self._search_uncached(request),
            cacheable=lambda results: bool(results),
            weight=lambda results: len(json.dumps(results, ensure_ascii=False).encode("utf-8")),
        )

    async def _search_uncached(self, request: ResearchRequest) -> list[dict]:
        params: dict[str, str | int] = {
            "q": _search_query(request.task),
            "format": "json",
            "language": "auto",
            "safesearch": 1,
            "categories": request.topic,
        }
        if request.freshness != "any":
            params["time_range"] = request.freshness
        response = await self._get_client().get(
            f"{self.searxng_url}/search",
            params=params,
            headers={
                "X-Forwarded-For": "127.0.0.1",
                "X-Real-IP": "127.0.0.1",
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        return [
            item for item in payload.get("results") or [] if _is_source_page(item)
        ][: max(request.max_sources * 2, request.max_sources)]

    @staticmethod
    def _filter_domains(candidates: list[dict], allowed_domains: list[str]) -> list[dict]:
        if not allowed_domains:
            return candidates
        filtered: list[dict] = []
        for candidate in candidates:
            host = (urlsplit(str(candidate.get("url") or "")).hostname or "").lower().rstrip(".")
            if any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains):
                filtered.append(candidate)
        return filtered

    @staticmethod
    def _exclude_domains(items: list[dict], excluded_domains: list[str]) -> list[dict]:
        if not excluded_domains:
            return items
        return [
            item
            for item in items
            if not any(
                (host := (urlsplit(str(item.get("url") or "")).hostname or "").lower().rstrip(".")) == domain
                or host.endswith(f".{domain}")
                for domain in excluded_domains
            )
        ]

    async def _extract_candidates(
        self,
        candidates: list[dict],
        allowed_domains: list[str],
        excluded_domains: list[str],
        extract_depth: str,
        content_format: str,
    ) -> tuple[list[ResearchSource], list[ResearchError]]:
        completed = await asyncio.gather(*(
            self._extract_candidate(item, allowed_domains, excluded_domains, extract_depth, content_format)
            for item in candidates
        ), return_exceptions=True)
        successful: list[tuple[ResearchSource, ResearchError | None]] = []
        for item in completed:
            if isinstance(item, BaseException):
                raise item
            successful.append(item)
        sources = [item[0] for item in successful]
        errors = [item[1] for item in successful if item[1] is not None]
        return sources, errors

    async def _extract_candidate(
        self,
        candidate: dict,
        allowed_domains: list[str],
        excluded_domains: list[str],
        extract_depth: str,
        content_format: str,
    ) -> tuple[ResearchSource, ResearchError | None]:
        url = str(candidate.get("url") or "")
        key = (
            url.split("#", 1)[0], extract_depth, content_format,
            tuple(allowed_domains), tuple(excluded_domains),
        )

        async def load() -> tuple[ResearchSource, ResearchError | None]:
            source, error = await self._extract_candidate_uncached(
                candidate, allowed_domains, excluded_domains, extract_depth, content_format,
            )
            if source.status == "extracted":
                source = source.model_copy(update={"title": None, "snippet": None})
            return source, error

        source, error = await self._source_cache.get_or_load(
            key,
            load,
            cacheable=lambda item: item[0].status == "extracted" and item[0].content is not None,
            weight=lambda item: len((item[0].content or "").encode("utf-8")) + len(item[0].url.encode("utf-8")),
        )
        return (
            source.model_copy(update={
                "title": candidate.get("title"),
                "snippet": candidate.get("content") or candidate.get("snippet"),
            }),
            error.model_copy(update={"url": url}) if error else None,
        )

    async def _extract_candidate_uncached(
        self,
        candidate: dict,
        allowed_domains: list[str],
        excluded_domains: list[str],
        extract_depth: str,
        content_format: str,
    ) -> tuple[ResearchSource, ResearchError | None]:
        url = str(candidate.get("url") or "")
        title = candidate.get("title")
        snippet = candidate.get("content") or candidate.get("snippet")
        if len(url) > 2_048 or len(url.encode("utf-8")) > 2_048:
            return (
                ResearchSource(title=title, url=url, snippet=snippet, status="validation_error"),
                ResearchError(stage="validation", url=url, message="URL exceeds size limit"),
            )
        try:
            resolved = await resolve_public_url(url, resolver=self._resolver)
        except UnsafeUrlError as exc:
            return (
                ResearchSource(title=title, url=url, snippet=snippet, status="validation_error"),
                ResearchError(stage="validation", url=url, message=str(exc)),
            )
        except (OSError, ValueError):
            return (
                ResearchSource(title=title, url=url, snippet=snippet, status="validation_error"),
                ResearchError(stage="validation", url=url, message="target did not resolve"),
            )
        try:
            html, final_url = await self._fetch_html(
                url,
                initial=resolved,
                allowed_domains=allowed_domains,
                excluded_domains=excluded_domains,
            )
        except UnsafeUrlError as exc:
            message = str(exc)
        except httpx.HTTPStatusError as exc:
            message = f"HTTP {exc.response.status_code}"
        except httpx.TimeoutException:
            message = "request timed out"
        except httpx.ConnectError:
            message = "connection error"
        except httpx.HTTPError:
            message = "request failed"
        except OSError:
            message = "target did not resolve"
        except ValueError as exc:
            message = str(exc)
        else:
            message = None
        if message is not None:
            return (
                ResearchSource(title=title, url=url, snippet=snippet, status="fetch_error"),
                ResearchError(stage="fetch", url=url, message=message),
            )
        content = await run_extraction(
            trafilatura.extract,
            html,
            url=final_url,
            include_comments=False,
            include_tables=extract_depth == "advanced",
            include_formatting=content_format == "markdown",
            output_format="markdown" if content_format == "markdown" else "txt",
            favor_precision=extract_depth == "basic",
            favor_recall=extract_depth == "advanced",
        )
        if not content:
            content = await run_extraction(trafilatura.html2txt, html)
        if not content:
            return (
                ResearchSource(title=title, url=final_url, snippet=snippet, status="extract_error"),
                ResearchError(stage="extract", url=final_url, message="no main text extracted"),
            )
        return (
            ResearchSource(
                title=title,
                url=final_url,
                snippet=snippet,
                content=content[:_MAX_CONTENT_CHARS],
                status="extracted",
            ),
            None,
        )

    async def _fetch_html(
        self,
        url: str,
        *,
        initial: ResolvedPublicUrl | None = None,
        allowed_domains: list[str] | None = None,
        excluded_domains: list[str] | None = None,
    ) -> tuple[str, str]:
        current = url.split("#", 1)[0]
        resolved = initial
        for _ in range(_MAX_REDIRECTS + 1):
            if not self._domain_is_permitted(current, allowed_domains or [], excluded_domains or []):
                raise UnsafeUrlError("target domain is not permitted")
            resolved = resolved or await resolve_public_url(current, resolver=self._resolver)
            redirected = False
            last_transport_error: httpx.TransportError | None = None
            for address in resolved.addresses:
                pinned_url = httpx.URL(current).copy_with(host=address)
                try:
                    async with self._get_client().stream(
                        "GET",
                        pinned_url,
                        headers={
                            "Connection": "close",
                            "Host": resolved.host_header,
                            "User-Agent": "iAi-search-service/0.1",
                        },
                        extensions={"sni_hostname": resolved.host},
                        follow_redirects=False,
                        timeout=15,
                    ) as response:
                        self._verify_connected_peer(response, resolved.addresses)
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise ValueError("redirect without location")
                            current = urljoin(current, location).split("#", 1)[0]
                            if len(current) > 2_048 or len(current.encode("utf-8")) > 2_048:
                                raise UnsafeUrlError("redirect URL exceeds size limit")
                            resolved = None
                            redirected = True
                            break
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "text/html").lower()
                        if "html" not in content_type and "text/plain" not in content_type:
                            raise ValueError("unsupported content type")
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > _MAX_BODY_BYTES:
                                raise ValueError("response body exceeds size limit")
                            chunks.append(chunk)
                        body = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                        return body, current
                except httpx.TransportError as exc:
                    last_transport_error = exc
                    continue
            if redirected:
                continue
            if last_transport_error is not None:
                raise last_transport_error
            raise ValueError("connection failed")
        raise ValueError("too many redirects")

    @staticmethod
    def _domain_is_permitted(url: str, allowed_domains: list[str], excluded_domains: list[str]) -> bool:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        if allowed_domains and not any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains):
            return False
        return not any(host == domain or host.endswith(f".{domain}") for domain in excluded_domains)

    @staticmethod
    def _verify_connected_peer(response: httpx.Response, allowed_addresses: tuple[str, ...]) -> None:
        stream = response.extensions.get("network_stream")
        if stream is None:
            raise UnsafeUrlError("connected peer could not be verified")
        peer = stream.get_extra_info("server_addr")
        if not peer:
            raise UnsafeUrlError("connected peer could not be verified")
        peer_address = str(peer[0]).split("%", 1)[0]
        allowed = {ipaddress.ip_address(address) for address in allowed_addresses}
        if ipaddress.ip_address(peer_address) not in allowed:
            raise UnsafeUrlError("connected peer did not match validated target")

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=0),
            )
        return self._client
