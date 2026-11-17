"""Tests for fetching, retries and SSRF protection (no network)."""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import pytest

from game_activity.fetch import (
    FetchError,
    ValidatedResolver,
    _check_resolved,
    classify_status,
    fetch_source,
    fetch_with_retry,
    validate_source_url,
)
from game_activity.models import SourceConfig


def make_cfg(url: str = "https://example.com/activity.json") -> SourceConfig:
    return SourceConfig(
        source_id="demo",
        game_id="demo",
        game_name="Demo",
        url=url,
        adapter="starrail_assistant_v1",
    )


def run(coro):
    return asyncio.run(coro)


class FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, size: int):
        for chunk in self._chunks:
            yield chunk


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        body: bytes = b"{}",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent([body] if body else [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False


class FakeSession:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any):
        self.calls.append((url, kwargs))
        if not self._responses:
            raise AssertionError("unexpected request")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeResolver:
    def __init__(self, error: FetchError | None = None) -> None:
        self.error = error
        self.prefetched: list[tuple[str, int]] = []

    async def prefetch(self, host: str, port: int) -> list[dict[str, Any]]:
        self.prefetched.append((host, port))
        if self.error:
            raise self.error
        return []


def fetch(session, *, cache=None, advanced=None, resolver=None):
    return fetch_source(
        session,
        make_cfg(),
        cache or {},
        advanced=advanced or {},
        resolver=resolver or FakeResolver(),
    )


def test_not_modified_skips_parsing() -> None:
    session = FakeSession([FakeResponse(304)])
    result = run(fetch(session, cache={"etag": 'W/"abc"'}))
    assert result.not_modified is True
    assert result.payload is None
    _, kwargs = session.calls[0]
    assert kwargs["headers"]["If-None-Match"] == 'W/"abc"'
    assert kwargs["allow_redirects"] is False


def test_200_json_returns_payload_and_cache_headers() -> None:
    body = json.dumps({"activities": [{"name": "x"}]}).encode()
    session = FakeSession(
        [
            FakeResponse(
                200,
                body,
                {
                    "Content-Type": "application/json; charset=utf-8",
                    "ETag": '"v1"',
                    "Last-Modified": "Wed, 01 Oct 2025 00:00:00 GMT",
                },
            ),
        ],
    )
    result = run(fetch(session))
    assert result.not_modified is False
    assert result.payload == {"activities": [{"name": "x"}]}
    assert result.etag == '"v1"'
    assert result.last_modified == "Wed, 01 Oct 2025 00:00:00 GMT"
    assert result.byte_count == len(body)


def test_html_error_page_is_permanent() -> None:
    session = FakeSession(
        [FakeResponse(200, b"<html>", {"Content-Type": "text/html; charset=utf-8"})],
    )
    with pytest.raises(FetchError) as excinfo:
        run(fetch(session))
    assert excinfo.value.kind == "permanent"
    assert excinfo.value.message == "invalid_content_type"


def test_missing_content_type_is_allowed() -> None:
    session = FakeSession([FakeResponse(200, b"{}")])
    assert run(fetch(session)).payload == {}


def test_oversized_response_is_aborted() -> None:
    session = FakeSession([FakeResponse(200, b"x" * 4096)])
    with pytest.raises(FetchError) as excinfo:
        run(fetch(session, advanced={"max_response_kb": 1}))
    assert excinfo.value.message == "response_too_large"


def test_invalid_json_is_permanent() -> None:
    session = FakeSession([FakeResponse(200, b"{not json")])
    with pytest.raises(FetchError) as excinfo:
        run(fetch(session))
    assert excinfo.value.message == "invalid_json"


def test_non_dict_root_is_permanent() -> None:
    session = FakeSession([FakeResponse(200, b"[]")])
    with pytest.raises(FetchError) as excinfo:
        run(fetch(session))
    assert excinfo.value.message == "invalid_json_root"


def test_transient_error_is_retried_then_succeeds() -> None:
    session = FakeSession([FakeResponse(503), FakeResponse(200, b'{"ok": true}')])
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    result = run(
        fetch_with_retry(
            session,
            make_cfg(),
            {},
            advanced={},
            resolver=FakeResolver(),
            sleep=fake_sleep,
            rng=lambda: 0.0,
        ),
    )
    assert result.payload == {"ok": True}
    assert len(session.calls) == 2
    assert sleeps == [1.0]


def test_permanent_error_is_not_retried() -> None:
    session = FakeSession([FakeResponse(404)])
    with pytest.raises(FetchError) as excinfo:
        run(
            fetch_with_retry(
                session,
                make_cfg(),
                {},
                advanced={},
                resolver=FakeResolver(),
            ),
        )
    assert excinfo.value.kind == "permanent"
    assert len(session.calls) == 1


def test_429_honors_retry_after_up_to_30_seconds() -> None:
    session = FakeSession(
        [
            FakeResponse(429, b"", {"Retry-After": "999"}),
            FakeResponse(200, b"{}"),
        ],
    )
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    run(
        fetch_with_retry(
            session,
            make_cfg(),
            {},
            advanced={},
            resolver=FakeResolver(),
            sleep=fake_sleep,
        ),
    )
    assert sleeps == [30.0]


def test_retry_budget_is_exhausted_on_persistent_transient() -> None:
    session = FakeSession([FakeResponse(500), FakeResponse(500), FakeResponse(500)])
    with pytest.raises(FetchError) as excinfo:
        run(
            fetch_with_retry(
                session,
                make_cfg(),
                {},
                advanced={},
                resolver=FakeResolver(),
                sleep=lambda _delay: asyncio.sleep(0),
                rng=lambda: 0.0,
            ),
        )
    assert excinfo.value.kind == "transient"
    assert len(session.calls) == 3


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/a.json",
        "http://example.com/a.json",
        "https://user:pass@example.com/a.json",
        "https://example.com/a.json#frag",
        "https://example.com:8443/a.json",
        "https:///a.json",
    ],
)
def test_unsafe_urls_are_blocked(url: str) -> None:
    with pytest.raises(FetchError) as excinfo:
        validate_source_url(url)
    assert excinfo.value.kind == "blocked"


def test_http_allowed_only_when_configured() -> None:
    validate_source_url("http://example.com/a.json", allow_insecure_http=True)


def test_blocked_url_never_calls_session() -> None:
    session = FakeSession([])

    async def call_with_http() -> None:
        await fetch_source(
            session,
            make_cfg("http://example.com/a.json"),
            {},
            advanced={},
            resolver=FakeResolver(),
        )

    with pytest.raises(FetchError) as excinfo:
        run(call_with_http())
    assert excinfo.value.kind == "blocked"
    assert session.calls == []


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.1.1", "::1"])
def test_resolver_rejects_private_literal_ips(address: str) -> None:
    resolver = ValidatedResolver()
    with pytest.raises(FetchError) as excinfo:
        run(resolver.prefetch(address, 443))
    assert excinfo.value.kind == "blocked"


def test_check_resolved_rejects_any_private_address() -> None:
    infos = [
        (socket.AF_INET, ("93.184.216.34", 443)),
        (socket.AF_INET, ("10.0.0.1", 443)),
    ]
    with pytest.raises(FetchError):
        _check_resolved("example.com", 443, infos)


def test_check_resolved_deduplicates_public_addresses() -> None:
    infos = [
        (socket.AF_INET, ("93.184.216.34", 443)),
        (socket.AF_INET, ("93.184.216.34", 443)),
        (socket.AF_INET6, ("2606:2800:220:1::1", 443, 0, 0)),
    ]
    results = _check_resolved("example.com", 443, infos)
    assert len(results) == 2
    assert {item["family"] for item in results} == {socket.AF_INET, socket.AF_INET6}


def test_resolver_caches_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = ValidatedResolver()
    calls = {"count": 0}

    async def fake_validate(host: str, port: int) -> list[dict[str, Any]]:
        calls["count"] += 1
        return [{"host": "93.184.216.34", "family": socket.AF_INET}]

    monkeypatch.setattr(resolver, "_resolve_and_validate", fake_validate)

    async def scenario() -> None:
        await resolver.resolve("example.com", 443)
        await resolver.resolve("example.com", 443)

    run(scenario())
    assert calls["count"] == 1


def test_classify_status_mapping() -> None:
    assert classify_status(302).kind == "permanent"
    assert classify_status(404).kind == "permanent"
    assert classify_status(429).kind == "transient"
    assert classify_status(503).kind == "transient"
