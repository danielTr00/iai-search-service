from __future__ import annotations

import asyncio
import ipaddress
import time
import uuid
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import httpx
import trafilatura

from .models import ResearchError, ResearchRequest, ResearchResponse, ResearchSource
from .security import ResolvedPublicUrl, Resolver, UnsafeUrlError, resolve_public_url


_MAX_BODY_BYTES = 2_000_000
_MAX_CONTENT_CHARS = 24_000
_MAX_REDIRECTS = 3


class ResearchService:
    def __init__(
        self,
        searxng_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
        max_concurrent_requests: int = 2,
    ) -> None:
        self.searxng_url = searxng_url.rstrip("/")
        self._client = client
        self._resolver = resolver
        self._request_slots = asyncio.Semaphore(max_concurrent_requests)

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
                strategy = "search_and_extract"
            candidates = self._filter_domains(candidates, request.allowed_domains)
            sources, errors = await self._extract_candidates(candidates[: request.max_sources])
        return ResearchResponse(
            request_id=request_id,
            query=request.task,
            strategy_used=strategy,
            sources=sources,
            errors=errors,
            duration_ms=round((time.monotonic() - started) * 1000),
        )

    async def _search(self, request: ResearchRequest) -> list[dict]:
        params: dict[str, str | int] = {
            "q": request.task,
            "format": "json",
            "language": "auto",
            "safesearch": 1,
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
        return list(payload.get("results") or [])[: max(request.max_sources * 2, request.max_sources)]

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

    async def _extract_candidates(self, candidates: list[dict]) -> tuple[list[ResearchSource], list[ResearchError]]:
        completed = await asyncio.gather(*(self._extract_candidate(item) for item in candidates))
        sources = [item[0] for item in completed]
        errors = [item[1] for item in completed if item[1] is not None]
        return sources, errors

    async def _extract_candidate(self, candidate: dict) -> tuple[ResearchSource, ResearchError | None]:
        url = str(candidate.get("url") or "")
        title = candidate.get("title")
        snippet = candidate.get("content") or candidate.get("snippet")
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
            html, final_url = await self._fetch_html(url, initial=resolved)
        except UnsafeUrlError as exc:
            message = str(exc)
        except httpx.HTTPError:
            message = "connection failed"
        except ValueError as exc:
            message = str(exc)
        else:
            message = None
        if message is not None:
            return (
                ResearchSource(title=title, url=url, snippet=snippet, status="fetch_error"),
                ResearchError(stage="fetch", url=url, message=message),
            )
        content = await asyncio.to_thread(
            trafilatura.extract,
            html,
            url=final_url,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
        if not content:
            content = await asyncio.to_thread(trafilatura.html2txt, html)
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
    ) -> tuple[str, str]:
        current = url
        resolved = initial
        for _ in range(_MAX_REDIRECTS + 1):
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
                            current = urljoin(current, location)
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
