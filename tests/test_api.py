"""Tests for Web API validation, error markers and command rendering."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from game_activity import webapi
from game_activity.commands import (
    HELP_TEXT,
    render_detail,
    render_list,
    render_status,
    render_subscription_change,
    render_subscriptions,
    resolve_game,
)
from game_activity.webapi import parse_game, parse_limit

PLUGIN = "astrbot_plugin_game_activity"


class FakeQuery:
    def __init__(self, data: dict) -> None:
        self._data = data

    def get(self, key: str, default=None):
        return self._data.get(key, default)


class FakeRequest:
    def __init__(
        self,
        *,
        username: str | None = "tester",
        query: dict | None = None,
        body=None,
    ) -> None:
        self.username = username
        self.query = FakeQuery(query or {})
        self._body = body

    async def json(self, default=None):
        if self._body is None:
            return default
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeResponse:
    def __init__(self, payload: dict, status_code: int) -> None:
        self.body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.status_code = status_code


class FakeLogger:
    def exception(self, *args, **kwargs) -> None:
        return None


class FakeContext:
    def __init__(self) -> None:
        self.routes: list[tuple] = []

    def register_web_api(self, route, handler, methods, desc) -> None:
        self.routes.append((route, handler, methods, desc))


class FakeService:
    def __init__(self) -> None:
        self.sources = [SimpleNamespace(game_id="starrail")]
        self.sync_result: tuple[bool, int] = (True, 0)

    def health_dto(self) -> dict:
        return {"schema_version": 1, "status": "ok"}

    def overview_dto(self) -> dict:
        return {"schema_version": 1, "counts": {}}

    def games_dto(self) -> list:
        return [{"game_id": "starrail"}]

    def sources_dto(self) -> list:
        return [{"source_id": "starrail"}]

    def activities_dto(self, **kwargs) -> tuple[int, list]:
        return 1, [{"key": "starrail#a1"}]

    def reminders_dto(self, limit: int) -> dict:
        return {"schema_version": 1, "jobs": [], "subscriptions": {"count": 0}}

    async def start_manual_sync(self, game):
        return self.sync_result


def fake_json_response(data=None, *, status_code: int = 200, headers=None):
    return FakeResponse(data if isinstance(data, dict) else {}, status_code)


def fake_error_response(message, *, status_code=400, data=None, headers=None):
    return FakeResponse(
        {"status": "error", "message": message, "data": data},
        status_code,
    )


WEB = (FakeLogger(), fake_error_response, fake_json_response, FakeRequest())


def make_api(service=None) -> tuple[FakeContext, FakeService]:
    service = service or FakeService()
    context = FakeContext()
    webapi.register_web_apis(context, PLUGIN, service, web=WEB)
    return context, service


def call(context: FakeContext, suffix: str):
    route, handler, _methods, _desc = next(
        entry for entry in context.routes if entry[0] == f"/{PLUGIN}/{suffix}"
    )
    return asyncio.run(handler())


def body_of(response: FakeResponse) -> dict:
    return json.loads(response.body)


def use_request(**kwargs) -> None:
    webapi.request = FakeRequest(**kwargs)


def test_registers_seven_prefixed_routes() -> None:
    context, _service = make_api()
    assert len(context.routes) == 7
    assert {entry[0] for entry in context.routes} == {
        f"/{PLUGIN}/health",
        f"/{PLUGIN}/overview",
        f"/{PLUGIN}/games",
        f"/{PLUGIN}/activities",
        f"/{PLUGIN}/sources",
        f"/{PLUGIN}/sync",
        f"/{PLUGIN}/reminders",
    }
    methods = {entry[0].rsplit("/", 1)[-1]: entry[2] for entry in context.routes}
    assert methods["sync"] == ["POST"]
    assert methods["overview"] == ["GET"]


def test_unauthorized_returns_401() -> None:
    context, _service = make_api()
    use_request(username=None)
    response = call(context, "overview")
    assert response.status_code == 401
    assert body_of(response)["message"].startswith("unauthorized:")


def test_overview_success() -> None:
    context, _service = make_api()
    use_request()
    response = call(context, "overview")
    assert response.status_code == 200
    assert body_of(response)["schema_version"] == 1


def test_activities_invalid_limit_and_status() -> None:
    context, _service = make_api()
    use_request(query={"limit": "abc"})
    response = call(context, "activities")
    assert response.status_code == 400
    assert body_of(response)["message"].startswith("invalid_parameter:")

    use_request(query={"status": "nope"})
    response = call(context, "activities")
    assert response.status_code == 400


def test_activities_unknown_game_returns_404() -> None:
    context, _service = make_api()
    use_request(query={"game": "nope"})
    response = call(context, "activities")
    assert response.status_code == 404
    assert body_of(response)["message"].startswith("unknown_game:")


def test_activities_success_returns_schema_version() -> None:
    context, _service = make_api()
    use_request(query={"game": "starrail", "limit": "10"})
    response = call(context, "activities")
    payload = body_of(response)
    assert response.status_code == 200
    assert payload["schema_version"] == 1
    assert payload["total"] == 1


def test_sync_conflict_rate_limit_and_success() -> None:
    context, service = make_api()
    use_request(body={"game": "starrail"})

    service.sync_result = (False, 0)
    response = call(context, "sync")
    assert response.status_code == 409
    assert body_of(response)["message"].startswith("sync_already_running:")

    service.sync_result = (False, 15)
    response = call(context, "sync")
    payload = body_of(response)
    assert response.status_code == 429
    assert payload["message"].startswith("sync_rate_limited:")
    assert payload["data"]["retry_after_seconds"] == 15

    service.sync_result = (True, 0)
    response = call(context, "sync")
    payload = body_of(response)
    assert response.status_code == 200
    assert payload["started"] is True
    assert payload["requested_at"].endswith("+00:00")


def test_sync_invalid_body() -> None:
    context, _service = make_api()
    use_request(body=["not", "dict"])
    response = call(context, "sync")
    assert response.status_code == 400


def test_reminders_invalid_limit() -> None:
    context, _service = make_api()
    use_request(query={"limit": "x"})
    response = call(context, "reminders")
    assert response.status_code == 400


def test_parse_helpers() -> None:
    assert parse_limit(None, 50, 1, 200) == 50
    assert parse_limit("0", 50, 1, 200) == 1
    assert parse_limit("999", 50, 1, 200) == 200
    assert parse_limit("x", 50, 1, 200) is None
    assert parse_game("") is None
    assert parse_game("starrail") == "starrail"
    with pytest.raises(ValueError):
        parse_game("Bad Game")


def test_resolve_game_by_id_and_name() -> None:
    known = {"starrail", "genshin"}
    assert resolve_game("starrail", known) == "starrail"
    assert resolve_game("崩坏：星穹铁道", known) == "starrail"
    assert resolve_game("", known) is None
    with pytest.raises(ValueError):
        resolve_game("原神", {"starrail"})


def test_render_list_groups_and_truncates() -> None:
    activities = [
        {
            "key": "starrail#a1",
            "game_id": "starrail",
            "name": "活动一",
            "status": "active",
            "end_epoch": 10,
            "end_iso": "2026-11-20T03:59:59+08:00",
        },
        {
            "key": "starrail#a2",
            "game_id": "starrail",
            "name": "已下架",
            "status": "removed",
            "end_epoch": 5,
            "end_iso": "",
        },
    ]
    text = render_list(activities)
    assert "活动一" in text
    assert "已下架" not in text
    assert "崩坏：星穹铁道" in text
    assert render_list([]).startswith("暂无活动数据")


def test_render_detail_and_status() -> None:
    detail = render_detail(
        {
            "key": "starrail#a1",
            "game_id": "starrail",
            "name": "活动",
            "status": "ending_soon",
            "start_iso": "2026-11-01T10:00:00+08:00",
            "end_iso": "2026-11-20T03:59:59+08:00",
            "source_id": "starrail",
            "description": "说明",
            "merged_sources": ["starrail#a1", "starrail#a2"],
        },
    )
    assert detail is not None
    assert "即将结束" in detail
    assert "合并来源数：2" in detail
    assert render_detail(None) is None

    status_text = render_status(
        {
            "sync_status": "idle",
            "last_sync_at": 0,
            "source_health": {"ok": 1, "stale": 0, "error": 0, "disabled": 0},
        },
        [
            {
                "source_id": "starrail",
                "status": "error",
                "consecutive_failures": 3,
                "last_error": "boom",
            },
        ],
    )
    assert "同步状态：空闲" in status_text
    assert "连续失败 3 次" in status_text


def test_render_subscriptions_and_changes() -> None:
    assert "尚未订阅" in render_subscriptions({}, "umo_a")
    text = render_subscriptions({"starrail": ["umo_a"]}, "umo_a")
    assert "崩坏：星穹铁道" in text
    assert render_subscription_change("starrail", True).startswith("已订阅")
    assert render_subscription_change("starrail", False).startswith("已取消订阅")
    assert "/活动 列表" in HELP_TEXT
