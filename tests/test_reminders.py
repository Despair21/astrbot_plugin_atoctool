"""Tests for reminder fire-time computation and delivery state machine."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from game_activity.reminders import (
    ReminderEngine,
    compute_fire_at,
    parse_remind_days,
    parse_remind_time,
)
from game_activity.store import Store

LOGGER = logging.getLogger("test_reminders")
TZ = timezone(timedelta(hours=8))


def now_dt() -> datetime:
    return datetime(2026, 11, 19, 12, 0, tzinfo=TZ)


NOW = int(now_dt().timestamp())


def make_activity(
    *,
    key: str = "starrail#a1",
    game_id: str = "starrail",
    source_id: str = "starrail",
    raw_id: str = "a1",
    name: str = "活动",
    end_iso: str = "2026-11-20T03:59:59+08:00",
    removed_at: int | None = None,
) -> dict:
    end_epoch = int(datetime.fromisoformat(end_iso).timestamp())
    return {
        "key": key,
        "raw_id": raw_id,
        "source_id": source_id,
        "game_id": game_id,
        "name": name,
        "description": "",
        "kind": None,
        "start_iso": "2026-11-01T10:00:00+08:00",
        "end_iso": end_iso,
        "start_epoch": int(datetime(2026, 11, 1, 10, 0, tzinfo=TZ).timestamp()),
        "end_epoch": end_epoch,
        "cover_url": "",
        "content_hash": "h",
        "status": "active",
        "first_seen_at": NOW - 86400,
        "last_seen_at": NOW,
        "removed_at": removed_at,
    }


def make_config(**overrides) -> dict:
    config = {
        "enabled": True,
        "remind_days": [1],
        "remind_time": "00:00",
        "max_grace_hours": 48,
        "max_sends_per_tick": 5,
        "target_umos": [],
    }
    config.update(overrides)
    return config


class Sender:
    def __init__(self, behavior=None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.behavior = behavior or (lambda umo: True)

    async def __call__(self, umo: str, text: str) -> bool:
        self.calls.append((umo, text))
        result = self.behavior(umo)
        if isinstance(result, Exception):
            raise result
        return result


def make_engine(data_root) -> tuple[ReminderEngine, Store]:
    store = Store(data_root / "plugin_data", LOGGER)
    return ReminderEngine(store, LOGGER), store


def tick(
    engine: ReminderEngine,
    activities: list[dict],
    subscriptions: dict,
    *,
    config: dict | None = None,
    sender: Sender | None = None,
    now: int = NOW,
    timezones: dict[str, str] | None = None,
) -> dict:
    sender = sender or Sender()
    return asyncio.run(
        engine.tick(
            now=now,
            activities=activities,
            subscriptions=subscriptions,
            config=config or make_config(),
            send_message=sender,
            timezones=timezones or {},
        ),
    )


def jobs_of(store: Store) -> dict:
    return store.load_state("reminders", {"items": {}})["items"]


def test_compute_fire_at_uses_source_offset() -> None:
    end_iso = "2026-11-20T03:59:59+08:00"
    end_epoch = int(datetime.fromisoformat(end_iso).timestamp())
    fire_at = compute_fire_at(end_iso, 1, "10:00", end_epoch)
    expected = int(datetime(2026, 11, 19, 10, 0, tzinfo=TZ).timestamp())
    assert fire_at == expected


def test_compute_fire_at_never_fires_after_end() -> None:
    end_iso = "2026-11-20T09:00:00+08:00"
    end_epoch = int(datetime.fromisoformat(end_iso).timestamp())
    fire_at = compute_fire_at(end_iso, 0, "10:00", end_epoch)
    assert fire_at == end_epoch - 60


def test_compute_fire_at_handles_dst_gap() -> None:
    try:
        berlin = ZoneInfo("Europe/Berlin")
    except (ZoneInfoNotFoundError, ValueError):
        pytest.skip("tzdata is unavailable")
    end_iso = "2026-04-01T12:00:00+02:00"
    end_epoch = int(datetime.fromisoformat(end_iso).timestamp())
    fire_at = compute_fire_at(end_iso, 3, "02:30", end_epoch, tz=berlin)
    expected = int(datetime(2026, 3, 29, 3, 30, tzinfo=berlin).timestamp())
    assert fire_at == expected


def test_parse_remind_time_and_days() -> None:
    assert parse_remind_time("07:05") == (7, 5)
    assert parse_remind_time("25:99") == (10, 0)
    assert parse_remind_time(None) == (10, 0)
    assert parse_remind_days("3,1,1,x,-2,0") == [0, 1, 3]
    assert parse_remind_days(["2", "bad"]) == [2]
    assert parse_remind_days(None) == []


def test_delivers_once_to_all_subscribers(data_root) -> None:
    engine, store = make_engine(data_root)
    sender = Sender()
    stats = tick(
        engine,
        [make_activity()],
        {"starrail": ["umo_a", "umo_b"]},
        sender=sender,
    )
    assert stats["sent"] == 2
    assert len(sender.calls) == 2
    jobs = jobs_of(store)
    job = next(iter(jobs.values()))
    assert job["status"] == "sent"
    assert all(t["status"] == "sent" for t in job["targets"].values())
    assert sender.calls[0][1].startswith("活动即将结束提醒")
    assert "活动" in sender.calls[0][1]

    sender2 = Sender()
    stats2 = tick(
        engine,
        [make_activity()],
        {"starrail": ["umo_a", "umo_b"]},
        sender=sender2,
        now=NOW + 60,
    )
    assert stats2["sent"] == 0
    assert sender2.calls == []


def test_partial_failure_retries_only_failed_target(data_root) -> None:
    engine, _store = make_engine(data_root)

    def behavior(umo: str):
        return RuntimeError("boom") if umo == "umo_b" else True

    sender = Sender(behavior)
    stats = tick(
        engine,
        [make_activity()],
        {"starrail": ["umo_a", "umo_b"]},
        sender=sender,
    )
    assert stats["sent"] == 1
    assert [umo for umo, _ in sender.calls] == ["umo_a", "umo_b"]

    sender2 = Sender()
    stats2 = tick(
        engine,
        [make_activity()],
        {"starrail": ["umo_a", "umo_b"]},
        sender=sender2,
        now=NOW + 200,
    )
    assert [umo for umo, _ in sender2.calls] == ["umo_b"]
    assert stats2["sent"] == 1


def test_platform_false_marks_target_failed(data_root) -> None:
    engine, store = make_engine(data_root)
    sender = Sender(lambda umo: False)
    tick(engine, [make_activity()], {"starrail": ["umo_a"]}, sender=sender)
    job = next(iter(jobs_of(store).values()))
    assert job["status"] == "failed"
    assert job["targets"]["umo_a"]["status"] == "failed"
    assert job["targets"]["umo_a"]["last_error"] == "platform_not_found"


def test_invalid_session_marks_target_failed(data_root) -> None:
    engine, store = make_engine(data_root)
    sender = Sender(lambda umo: ValueError("bad session"))
    tick(engine, [make_activity()], {"starrail": ["umo_a"]}, sender=sender)
    target = next(iter(jobs_of(store).values()))["targets"]["umo_a"]
    assert target["status"] == "failed"
    assert target["last_error"] == "invalid_session"


def test_transient_failure_exhausts_after_three_attempts(data_root) -> None:
    engine, store = make_engine(data_root)
    sender = Sender(lambda umo: RuntimeError("network"))
    tick(engine, [make_activity()], {"starrail": ["umo_a"]}, sender=sender)
    tick(
        engine,
        [make_activity()],
        {"starrail": ["umo_a"]},
        sender=sender,
        now=NOW + 121,
    )
    tick(
        engine,
        [make_activity()],
        {"starrail": ["umo_a"]},
        sender=sender,
        now=NOW + 500,
    )
    target = next(iter(jobs_of(store).values()))["targets"]["umo_a"]
    assert target["status"] == "failed"
    assert target["attempts"] == 3
    assert target["last_error"].startswith("transient_exhausted")


def test_qq_official_target_skipped_without_sending(data_root) -> None:
    engine, store = make_engine(data_root)
    sender = Sender()
    tick(
        engine,
        [make_activity()],
        {"starrail": ["qq_official:FriendMessage:1"]},
        sender=sender,
    )
    assert sender.calls == []
    target = next(iter(jobs_of(store).values()))["targets"][
        "qq_official:FriendMessage:1"
    ]
    assert target["status"] == "skipped"
    assert target["last_error"] == "platform_unsupported"


def test_grace_expired_target_is_skipped(data_root) -> None:
    engine, store = make_engine(data_root)
    sender = Sender()
    tick(
        engine,
        [make_activity()],
        {"starrail": ["umo_a"]},
        config=make_config(max_grace_hours=0),
        sender=sender,
        now=NOW + 60,
    )
    assert sender.calls == []
    target = next(iter(jobs_of(store).values()))["targets"]["umo_a"]
    assert target["status"] == "skipped"
    assert target["last_error"] == "grace_expired"


def test_no_targets_skips_job_without_sending(data_root) -> None:
    engine, store = make_engine(data_root)
    sender = Sender()
    tick(engine, [make_activity()], {}, sender=sender)
    assert sender.calls == []
    job = next(iter(jobs_of(store).values()))
    assert job["status"] == "skipped"
    assert job["last_error"] == "no_targets"


def test_max_sends_per_tick_defers_remaining_targets(data_root) -> None:
    engine, _store = make_engine(data_root)
    sender = Sender()
    tick(
        engine,
        [make_activity()],
        {"starrail": ["umo_a", "umo_b"]},
        config=make_config(max_sends_per_tick=1),
        sender=sender,
    )
    assert [umo for umo, _ in sender.calls] == ["umo_a"]
    sender2 = Sender()
    tick(
        engine,
        [make_activity()],
        {"starrail": ["umo_a", "umo_b"]},
        config=make_config(max_sends_per_tick=1),
        sender=sender2,
        now=NOW + 60,
    )
    assert [umo for umo, _ in sender2.calls] == ["umo_b"]


def test_fixed_target_umos_are_merged_with_subscriptions(data_root) -> None:
    engine, _store = make_engine(data_root)
    sender = Sender()
    tick(
        engine,
        [make_activity()],
        {"starrail": ["umo_a"]},
        config=make_config(target_umos=["umo_c", "umo_a"]),
        sender=sender,
    )
    assert [umo for umo, _ in sender.calls] == ["umo_a", "umo_c"]


def test_activity_ended_before_due_is_skipped(data_root) -> None:
    engine, store = make_engine(data_root)
    sender = Sender()
    config = make_config(remind_time="23:59")
    tick(engine, [make_activity()], {"starrail": ["umo_a"]}, config=config, sender=sender)
    assert sender.calls == []
    tick(
        engine,
        [make_activity()],
        {"starrail": ["umo_a"]},
        config=config,
        sender=sender,
        now=int(datetime(2026, 11, 20, 5, 0, tzinfo=TZ).timestamp()),
    )
    job = next(iter(jobs_of(store).values()))
    assert sender.calls == []
    assert job["status"] == "skipped"
    assert job["targets"]["umo_a"]["last_error"] == "activity_ended"


def test_terminal_jobs_are_pruned_after_retention(data_root) -> None:
    engine, store = make_engine(data_root)
    sender = Sender()
    tick(engine, [make_activity()], {"starrail": ["umo_a"]}, sender=sender)
    assert jobs_of(store)
    tick(
        engine,
        [make_activity(end_iso="2026-12-30T03:59:59+08:00")],
        {"starrail": ["umo_a"]},
        sender=sender,
        now=NOW + 31 * 86400,
    )
    assert jobs_of(store) == {}


def test_disabled_reminders_do_nothing(data_root) -> None:
    engine, store = make_engine(data_root)
    sender = Sender()
    stats = tick(
        engine,
        [make_activity()],
        {"starrail": ["umo_a"]},
        config=make_config(enabled=False),
        sender=sender,
    )
    assert stats == {"created": 0, "sent": 0, "skipped": 0, "failed": 0, "pending": 0}
    assert sender.calls == []
    assert jobs_of(store) == {}
