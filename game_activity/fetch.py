"""Async HTTP fetching with conditional caching, retries and SSRF protection."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import random
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from .models import SourceConfig

_ALLOWED_CONTENT_TYPES = {"application/json", "text/json", "text/plain"}
_ALLOWED_PORTS = {80, 443}
_MAX_CHUNK = 64 * 1024


class FetchError(Exception):
    """A classified fetch failure.

    Attributes:
        kind: ``transient`` (retry), ``permanent`` (do not retry) or ``blocked``
            (unsafe URL/address, never retried).
        message: Short stable error summary stored in source state.
        http_status: HTTP status code when the failure came from a response.
        retry_after: Seconds requested by a ``Retry-After`` header.
    """

    def __init__(
        self,
        kind: str,
        message: str,
        http_status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.http_status = http_status
        self.retry_after = retry_after


@dataclass(slots=True)
class FetchResult:
    """Outcome of one source fetch."""

    not_modified: bool
    http_status: int
    payload: dict[str, Any] | None
    etag: str | None = None
    last_modified: str | None = None
    byte_count: int = 0


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_reserved,
            ip.is_multicast,
            ip.is_unspecified,
        ),
    )


def validate_source_url(url: str, allow_insecure_http: bool = False) -> None:
    """Reject URLs that do not meet the fetch security policy.

    Args:
        url: Source URL from configuration.
        allow_insecure_http: Whether plain HTTP sources are allowed.

    Raises:
        FetchError: With kind ``blocked`` when the URL is unsafe.
    """
    parts = urlsplit(str(url or "").strip())
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise FetchError("blocked", "blocked_scheme")
    if scheme == "http" and not allow_insecure_http:
        raise FetchError("blocked", "blocked_insecure_http")
    if parts.username or parts.password:
        raise FetchError("blocked", "blocked_userinfo")
    if parts.fragment:
        raise FetchError("blocked", "blocked_fragment")
    if not parts.hostname:
        raise FetchError("blocked", "blocked_missing_host")
    try:
        port = parts.port
    except ValueError as exc:
        raise FetchError("blocked", "blocked_invalid_port") from exc
    if port is None:
        port = 443 if scheme == "https" else 80
    if port not in _ALLOWED_PORTS:
        raise FetchError("blocked", "blocked_port")


def _check_resolved(
    hostname: str,
    port: int,
    infos: list[tuple[int, tuple]],
) -> list[dict[str, Any]]:
    """Validate resolved addresses and convert them to aiohttp resolve results.

    Args:
        hostname: Original hostname used for SNI/Host.
        port: Destination port.
        infos: ``getaddrinfo``-shaped results.

    Returns:
        Deduplicated resolve results pointing at validated IP addresses.

    Raises:
        FetchError: With kind ``blocked`` when any address is not public.
    """
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for family, sockaddr in infos:
        address = str(sockaddr[0])
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise FetchError("blocked", "blocked_unparsable_address") from exc
        if _is_forbidden_ip(ip):
            raise FetchError("blocked", f"blocked_ip:{address}")
        key = (address, family)
        if key in seen:
            continue
        seen.add(key)
        results.append(
            {
                "hostname": hostname,
                "host": address,
                "port": port,
                "family": family,
                "proto": socket.IPPROTO_TCP,
                "flags": 0,
            },
        )
    if not results:
        raise FetchError("transient", "dns_empty")
    return results


class ValidatedResolver(AbstractResolver):
    """Resolve and pin source hosts to pre-validated public IP addresses."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def prefetch(self, host: str, port: int) -> list[dict[str, Any]]:
        """Resolve, validate and cache every address of ``host`` once."""
        key = (host, port)
        if key in self._cache:
            return self._cache[key]
        async with self._lock:
            if key in self._cache:
                return self._cache[key]
            results = await self._resolve_and_validate(host, port)
            self._cache[key] = results
            return results

    async def _resolve_and_validate(self, host: str, port: int) -> list[dict[str, Any]]:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            if _is_forbidden_ip(literal):
                raise FetchError("blocked", f"blocked_ip:{host}")
            family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
            return [
                {
                    "hostname": host,
                    "host": host,
                    "port": port,
                    "family": family,
                    "proto": socket.IPPROTO_TCP,
                    "flags": 0,
                },
            ]
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except OSError as exc:
            raise FetchError("transient", f"dns_failed:{host}") from exc
        return _check_resolved(host, port, [(info[0], info[4]) for info in infos])

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        """Return cached validated addresses for aiohttp's connector."""
        results = self._cache.get((host, port))
        if results is None:
            results = await self._resolve_and_validate(host, port)
            self._cache[(host, port)] = results
        if family not in (0, socket.AF_UNSPEC):
            filtered = [item for item in results if item["family"] == family]
            if not filtered:
                raise OSError(f"no validated address for family {family}")
            return filtered
        return results

    async def close(self) -> None:
        """Drop the cached resolutions."""
        self._cache.clear()


async def read_capped(resp: aiohttp.ClientResponse, max_bytes: int) -> bytes:
    """Read a response body, aborting once ``max_bytes`` is exceeded."""
    total = 0
    chunks: list[bytes] = []
    async for chunk in resp.content.iter_chunked(_MAX_CHUNK):
        total += len(chunk)
        if total > max_bytes:
            raise FetchError("permanent", "response_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def classify_status(status: int) -> FetchError:
    """Map an unexpected HTTP status code to a classified fetch error."""
    if 300 <= status < 400:
        return FetchError("permanent", f"unexpected_redirect:{status}", status)
    if status == 429 or status >= 500:
        return FetchError("transient", f"http_{status}", status)
    return FetchError("permanent", f"http_{status}", status)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None


def _content_type_allowed(content_type: str) -> bool:
    if not content_type:
        return True
    base = content_type.split(";", 1)[0].strip().lower()
    if not base:
        return True
    if "text/html" in base:
        return False
    return base in _ALLOWED_CONTENT_TYPES


async def fetch_source(
    session: aiohttp.ClientSession,
    cfg: SourceConfig,
    cache: dict[str, Any],
    *,
    advanced: dict[str, Any],
    resolver: ValidatedResolver,
) -> FetchResult:
    """Fetch one source with conditional headers and response validation.

    Args:
        session: Shared aiohttp session.
        cfg: Source configuration (URL is expected to be already normalized).
        cache: Per-source conditional cache entry (``etag``/``last_modified``).
        advanced: Advanced config (``allow_insecure_http``, UA, size limit).
        resolver: SSRF-safe resolver used for the pinned connection.

    Raises:
        FetchError: Classified failure; callers decide retry/stale behavior.
    """
    allow_insecure = bool(advanced.get("allow_insecure_http", False))
    validate_source_url(cfg.url, allow_insecure)
    parts = urlsplit(cfg.url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    await resolver.prefetch(host, port)

    headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": str(advanced.get("http_user_agent") or "astrbot-game-activity"),
    }
    if cache.get("etag"):
        headers["If-None-Match"] = str(cache["etag"])
    if cache.get("last_modified"):
        headers["If-Modified-Since"] = str(cache["last_modified"])

    try:
        async with session.get(
            cfg.url,
            headers=headers,
            allow_redirects=False,
        ) as resp:
            if resp.status == 304:
                return FetchResult(
                    not_modified=True,
                    http_status=304,
                    payload=None,
                    etag=cache.get("etag"),
                    last_modified=cache.get("last_modified"),
                )
            if resp.status != 200:
                error = classify_status(resp.status)
                error.retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                raise error
            content_type = resp.headers.get("Content-Type", "")
            if not _content_type_allowed(content_type):
                raise FetchError("permanent", "invalid_content_type")
            max_bytes = int(advanced.get("max_response_kb", 4096)) * 1024
            raw = await read_capped(resp, max_bytes)
            try:
                text = raw.decode("utf-8").lstrip("\ufeff")
            except UnicodeDecodeError as exc:
                raise FetchError("permanent", "invalid_utf8") from exc
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise FetchError("permanent", "invalid_json") from exc
            if not isinstance(payload, dict):
                raise FetchError("permanent", "invalid_json_root")
            return FetchResult(
                not_modified=False,
                http_status=200,
                payload=payload,
                etag=resp.headers.get("ETag"),
                last_modified=resp.headers.get("Last-Modified"),
                byte_count=len(raw),
            )
    except FetchError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise FetchError("transient", f"network:{type(exc).__name__}") from exc


async def fetch_with_retry(
    session: aiohttp.ClientSession,
    cfg: SourceConfig,
    cache: dict[str, Any],
    *,
    advanced: dict[str, Any],
    resolver: ValidatedResolver,
    attempts: int = 3,
    sleep=asyncio.sleep,
    rng=random.random,
) -> FetchResult:
    """Fetch a source, retrying only transient failures with backoff."""
    last_error: FetchError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fetch_source(
                session,
                cfg,
                cache,
                advanced=advanced,
                resolver=resolver,
            )
        except FetchError as exc:
            last_error = exc
            if exc.kind != "transient" or attempt == attempts:
                raise
            delay = 1.0 * (2 ** (attempt - 1)) + rng() * 0.3
            if exc.http_status == 429 and exc.retry_after is not None:
                delay = min(exc.retry_after, 30.0)
            await sleep(delay)
    assert last_error is not None
    raise last_error
