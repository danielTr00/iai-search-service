from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit


Resolver = Callable[[str, int], Awaitable[list[str]]]


class UnsafeUrlError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedPublicUrl:
    url: str
    host: str
    host_header: str
    addresses: tuple[str, ...]


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return bool(ip.is_global)


async def _default_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


async def resolve_public_url(url: str, *, resolver: Resolver | None = None) -> ResolvedPublicUrl:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeUrlError("only absolute HTTP(S) URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("URL credentials are not allowed")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeUrlError("local targets are not allowed")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = [host] if ipaddress.ip_address(host) else []
    except ValueError:
        addresses = await (resolver or _default_resolver)(host, port)
    if not addresses or any(not _is_public(address) for address in addresses):
        raise UnsafeUrlError("target must resolve only to public addresses")
    return ResolvedPublicUrl(
        url=url,
        host=host,
        host_header=parsed.netloc,
        addresses=tuple(addresses),
    )


async def validate_public_url(url: str, *, resolver: Resolver | None = None) -> str:
    return (await resolve_public_url(url, resolver=resolver)).url
