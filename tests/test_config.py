"""Tests for configuration normalization, queries and manual sync control."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from game_activity.service import GameActivityService, normalize_config

LOGGER = logging.getLogger("test_config")
TZ = timezone(timedelta(hours=8))
NOW = int(datetime(2026, 11, 19, 12, 0, tzinfo=TZ).timestamp())


def make_service(data_root, config=None, **kwargs) -> GameActivityService:
    service = GameActivityService(
        config=config or {},
        data_root=data_root / "plugin_data",
        logger=LOGGER,
        clock=lambda: NOW,
    )
    for key, value in kwargs.items():
        setattr(service, key, value)
    return service


def make_activity(
    key: str = "starrail#a1",
    *,
    source_id: str = "starrail",
    game_id: str = "starrail",
    name: str = "活动",
    status: str = "active",
    start: int = NOW - 86400,
    end: int = NOW + 5 * 86400,
    removed_at: int | None = None,
) -> dict:
    return {
        "key": key,
        "raw_id": key.split("#", 1)[-1],
        "source_id": source_id,
        "game_id": game_id,
        "name": name,
        "description": "描述",
        "kind": None,
        "start_iso": datetime.fromtimestamp(start, TZ).isoformat(),
        "end_iso": datetime.fromtimestamp(end, TZ).isoformat(),
        "start_epoch": start,
        "end_epoch": end,
        "cover_url": "",
        "content_hash": "h",
        "status": status,
        "first_seen_at": start,
        "last_seen_at": start,
        "removed_at": removed_at,
    }


def snapshot(activities: list[dict], game_id: str = "starrail") -> dict:
    return {
        "schema_version": 1,
        "game_id": game_id,
        "game_name": game_id,
        "last_sync_at": NOW,
        "activities": activities,
    }


def test_normalize_config_defaults() -> None:
    config = normalize_config({})
    assert config["enabled"] is True
    assert config["sync_interval_minutes"] == 30
    assert config["remind_days"] == [1, 3]
    assert config["target_umos"] == []
    assert config["retention_days"] == 7
    assert config["poll_seconds"] == 30


def test_normalize_config_clamps_invalid_values() -> None:
    config = normalize_config(
        {
            "general": {
                "enabled": "yes",
                "sync_interval_minutes": 1,
                "request_timeout_seconds": 999,
                "max_response_kb": 10,
                "use_t2i": 1,
            },
            "reminder": {
                "remind_days_before": "bad,-3",
                "max_grace_hours": 9999,
                "max_sends_per_tick": 0,
                "target_umos": ["umo_a", "", 5],
            },
            "view": {"poll_seconds": 1, "show_covers": False},
            "advanced": {"retention_days": 999, "allow_insecure_http": True},
        },
    )
    assert config["enabled"] is True
    assert config["sync_interval_minutes"] == 5
    assert config["request_timeout_seconds"] == 60
    assert config["max_response_kb"] == 256
    assert config["use_t2i"] is False
    assert config["remind_days"] == [1]
    assert config["max_grace_hours"] == 168
    assert config["max_sends_per_tick"] == 1
    assert config["target_umos"] == ["umo_a", "5"]
    assert config["poll_seconds"] == 10
    assert config["show_covers"] is False
    assert config["retention_days"] == 90
    assert config["allow_insecure_http"] is True


def test_service_defaults_to_six_sources(data_root) -> None:
    service = make_service(data_root)
    assert len(service.sources) == 6
    assert all(source.enabled for source in service.sources)


def test_activities_dto_filters_and_clamps(data_root) -> None:
    service = make_service(
        data_root,
        snapshots={
            "starrail": snapshot(
                [
                    make_activity(),
                    make_activity(
                        "starrail#a2",
                        name="即将结束",
                        end=NOW + 3600,
                    ),
                    make_activity(
                        "starrail#a3",
                        name="已下架",
                        removed_at=NOW - 60,
                    ),
                ],
            ),
        },
    )
    total, rows = service.activities_dto(limit=1)
    assert total == 3
    assert len(rows) == 1
    keys = {"key", "is_primary", "merged_sources", "status"}
    assert keys <= set(rows[0])
    _total, ending = service.activities_dto(status="ending_soon")
    assert [row["name"] for row in ending] == ["即将结束"]
    _total, searched = service.activities_dto(query="即将")
    assert [row["name"] for row in searched] == ["即将结束"]
    _total, other_game = service.activities_dto(game="genshin")
    assert other_game == []


def test_overview_counts_and_source_health(data_root) -> None:
    service = make_service(
        data_root,
        snapshots={
            "starrail": snapshot(
                [
                    make_activity(),
                    make_activity("starrail#a2", end=NOW + 3600),
                    make_activity("starrail#a3", removed_at=NOW - 60),
                ],
            ),
        },
        source_states={
            "starrail": {"status": "ok", "last_error": ""},
            "genshin": {"status": "error", "last_error": "boom"},
        },
    )
    overview = service.overview_dto()
    assert overview["schema_version"] == 1
    assert overview["counts"]["activities"] == 2
    assert overview["counts"]["active"] == 1
    assert overview["counts"]["ending_soon"] == 1
    assert overview["counts"]["removed"] == 1
    assert overview["counts"]["sources"] == 6
    assert overview["source_health"]["total"] == 6
    assert overview["source_health"]["ok"] == 1
    assert overview["source_health"]["error"] == 1
    assert overview["source_health"]["disabled"] == 0
    assert overview["poll_seconds"] == 30


def test_sources_dto_marks_disabled_and_state(data_root) -> None:
    service = make_service(data_root)
    service.sources[0].enabled = False
    service.source_states = {"genshin": {"status": "ok", "etag": '"1"'}}
    rows = service.sources_dto()
    by_id = {row["source_id"]: row for row in rows}
    assert by_id["genshin"]["enabled"] is False
    assert by_id["genshin"]["status"] == "disabled"
    assert by_id["genshin"]["etag_present"] is True
    assert by_id["starrail"]["status"] == "stale"


def test_reminders_dto_and_next_reminder(data_root) -> None:
    activity = make_activity()
    service = make_service(data_root, snapshots={"starrail": snapshot([activity])})
    service.store.save_state(
        "reminders",
        {
            "schema_version": 1,
            "items": {
                "starrail#a1#1": {
                    "key": "starrail#a1#1",
                    "game_id": "starrail",
                    "source_id": "starrail",
                    "raw_id": "a1",
                    "activity_key": "starrail#a1",
                    "threshold_days": 1,
                    "fire_at_epoch": NOW + 60,
                    "status": "pending",
                    "attempts": 0,
                    "last_error": "",
                    "sent_at": None,
                    "targets": {},
                },
            },
        },
    )
    payload = service.reminders_dto()
    assert payload["subscriptions"]["count"] == 0
    assert payload["jobs"][0]["activity_name"] == "活动"
    assert payload["jobs"][0]["status"] == "pending"
    next_reminder = service.overview_dto()["next_reminder"]
    assert next_reminder["threshold_days"] == 1
    assert next_reminder["fire_at_iso"].endswith("+08:00")


def test_subscribe_and_unsubscribe(data_root) -> None:
    service = make_service(data_root)
    service.subscribe("umo_a", "starrail")
    service.subscribe("umo_a", "starrail")
    service.subscribe("umo_b", "genshin")
    assert service.subscriptions["starrail"] == ["umo_a"]
    assert service.unsubscribe("umo_a", "starrail") == 1
    assert "starrail" not in service.subscriptions
    assert service.unsubscribe("umo_b") == 1
    assert service.subscriptions == {}


def test_health_dto_and_known_games(data_root) -> None:
    service = make_service(data_root)
    health = service.health_dto()
    assert health["status"] == "ok"
    assert health["data_dir_writable"] is True
    assert "starrail" in service.known_games()


def test_manual_sync_single_flight_and_rate_limit(data_root) -> None:
    service = make_service(data_root)
    calls: list[str | None] = []

    async def fake_sync(game=None, *, manual=False):
        calls.append(game)
        await asyncio.sleep(0.05)
        service.sync_status = "idle"
        return {"sources": 0, "succeeded": 0, "failed": 0}

    service.sync = fake_sync  # type: ignore[assignment]

    async def scenario():
        first = await service.start_manual_sync("starrail")
        second = await service.start_manual_sync("starrail")
        assert first == (True, 0)
        assert second == (False, 0)
        assert service._sync_task is not None
        await service._sync_task
        third = await service.start_manual_sync(None)
        assert third[0] is False
        assert third[1] > 0

    asyncio.run(scenario())
    assert calls == ["starrail"]


def test_stop_cancels_in_flight_manual_sync(data_root) -> None:
    service = make_service(data_root)
    service.started = True

    async def slow_sync(game=None, *, manual=False):
        await asyncio.sleep(60)
        return {"sources": 0, "succeeded": 0, "failed": 0}

    service.sync = slow_sync  # type: ignore[assignment]

    async def scenario():
        started, _retry = await service.start_manual_sync("starrail")
        assert started is True
        task = service._sync_task
        assert task is not None
        assert task in service._transient_tasks
        await service.stop()
        assert task.done()
        assert task.cancelled()
        assert service._sync_task is None
        assert service._transient_tasks == set()

    asyncio.run(scenario())


def test_send_message_without_context_returns_false(data_root) -> None:
    service = make_service(data_root)
    assert asyncio.run(service._send_message("umo", "text")) is False


def test_merged_activities_deduplicates_sources(data_root) -> None:
    service = make_service(
        data_root,
        snapshots={
            "starrail": snapshot(
                [
                    make_activity("source_a#a1", source_id="source_a"),
                    make_activity("source_b#a1", source_id="source_b"),
                ],
            ),
        },
    )
    service.sources = service.sources[:1] + [
        source
        for source in service.sources
        if source.source_id != "starrail"
    ]
    service.sources[0].source_id = "source_a"
    service.sources[0].game_id = "starrail"
    rows = service.merged_activities()
    assert len(rows) == 1
    assert rows[0]["merged_sources"] == ["source_a#a1", "source_b#a1"]
