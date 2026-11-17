"""Tests for snapshot diffing, tombstones and cross-source merging."""

from __future__ import annotations

from game_activity.diff import apply_source_items, derive_status, recompute_statuses
from game_activity.merge import merge_activities, primary_source
from game_activity.models import NormalizedActivity, SourceConfig

NOW = 1_800_000_000
DAY = 86400


def make_cfg(
    source_id: str = "starrail",
    game_id: str = "starrail",
    priority: int = 100,
) -> SourceConfig:
    return SourceConfig(
        source_id=source_id,
        game_id=game_id,
        game_name=game_id,
        url="https://example.com/a.json",
        adapter="starrail_assistant_v1",
        priority=priority,
    )


def make_item(
    name: str = "活动",
    *,
    start: int = NOW - DAY,
    end: int = NOW + 10 * DAY,
    description: str = "描述",
    kind: str | None = None,
    cover: str = "",
) -> NormalizedActivity:
    return NormalizedActivity(
        name=name,
        description=description,
        kind=kind,
        start_epoch=start,
        end_epoch=end,
        start_iso=f"iso-start-{start}",
        end_iso=f"iso-end-{end}",
        cover_url=cover,
    )


def apply(
    snapshot: dict,
    items: list[NormalizedActivity],
    *,
    cfg: SourceConfig | None = None,
    now: int = NOW,
    retention_days: int = 7,
    remind_days=(3, 1),
):
    return apply_source_items(
        snapshot,
        cfg or make_cfg(),
        items,
        now=now,
        retention_days=retention_days,
        remind_days=list(remind_days),
    )


def test_added_items_get_identity_and_first_seen() -> None:
    snapshot, stats = apply({}, [make_item("新活动")])
    assert stats["added"] == 1
    activity = snapshot["activities"][0]
    assert activity["key"].startswith("starrail#a")
    assert activity["first_seen_at"] == NOW
    assert activity["last_seen_at"] == NOW
    assert snapshot["game_id"] == "starrail"
    assert snapshot["schema_version"] == 1


def test_reapplying_same_items_reports_no_events() -> None:
    snapshot, _ = apply({}, [make_item("活动")])
    again, stats = apply(snapshot, [make_item("活动")], now=NOW + 60)
    assert stats["added"] == 0
    assert stats["changed"] == 0
    assert stats["renamed"] == 0
    assert stats["removed"] == 0
    assert again["activities"][0]["first_seen_at"] == NOW
    assert again["activities"][0]["last_seen_at"] == NOW + 60


def test_content_change_is_reported_as_changed() -> None:
    snapshot, _ = apply({}, [make_item("活动")])
    _updated, stats = apply(snapshot, [make_item("活动", description="新描述")])
    assert stats["changed"] == 1
    assert stats["added"] == 0
    assert stats["removed"] == 0


def test_rename_keeps_raw_id() -> None:
    snapshot, _ = apply({}, [make_item("abcdefghij")])
    raw_id = snapshot["activities"][0]["raw_id"]
    updated, stats = apply(snapshot, [make_item("abcdefghijx")])
    assert stats["renamed"] == 1
    assert stats["added"] == 0
    assert stats["removed"] == 0
    assert updated["activities"][0]["raw_id"] == raw_id
    assert updated["activities"][0]["name"] == "abcdefghijx"


def test_disappeared_item_becomes_tombstone_then_is_purged() -> None:
    snapshot, _ = apply({}, [make_item("旧活动"), make_item("保留活动", start=NOW + 5 * DAY)])
    removed_at = NOW + DAY
    updated, stats = apply(
        snapshot,
        [make_item("保留活动", start=NOW + 5 * DAY)],
        now=removed_at,
    )
    assert stats["removed"] == 1
    tombstones = [item for item in updated["activities"] if item["status"] == "removed"]
    assert len(tombstones) == 1
    assert tombstones[0]["removed_at"] == removed_at

    purged, _stats = apply(
        updated,
        [make_item("保留活动", start=NOW + 5 * DAY)],
        now=removed_at + 8 * DAY,
        retention_days=7,
    )
    assert not [item for item in purged["activities"] if item["status"] == "removed"]


def test_removed_item_revives_with_original_identity() -> None:
    snapshot, _ = apply({}, [make_item("回归活动"), make_item("占位", start=NOW + 5 * DAY)])
    first_raw = next(
        item["raw_id"] for item in snapshot["activities"] if item["name"] == "回归活动"
    )
    removed_snapshot, _ = apply(
        snapshot,
        [make_item("占位", start=NOW + 5 * DAY)],
        now=NOW + DAY,
    )
    revived, stats = apply(
        removed_snapshot,
        [make_item("回归活动"), make_item("占位", start=NOW + 5 * DAY)],
        now=NOW + 2 * DAY,
    )
    activity = next(
        item for item in revived["activities"] if item["name"] == "回归活动"
    )
    assert activity["raw_id"] == first_raw
    assert activity["removed_at"] is None
    assert activity["first_seen_at"] == NOW
    assert stats["removed"] == 0


def test_empty_upstream_never_wipes_existing_snapshot() -> None:
    snapshot, _ = apply({}, [make_item("活动")])
    unchanged, stats = apply(snapshot, [])
    assert stats["skipped_empty"] == 1
    assert stats["removed"] == 0
    assert unchanged == snapshot


def test_other_sources_and_games_are_preserved() -> None:
    genshin_cfg = make_cfg("genshin", "genshin")
    snapshot, _ = apply({}, [make_item("原神活动")], cfg=genshin_cfg)
    snapshot, _ = apply(snapshot, [make_item("星铁活动")])
    assert {item["source_id"] for item in snapshot["activities"]} == {
        "genshin",
        "starrail",
    }


def test_derive_status_priority() -> None:
    remind = [3, 1]
    assert (
        derive_status({"start_epoch": NOW - DAY, "end_epoch": NOW + DAY}, NOW, remind)
        == "ending_soon"
    )
    assert (
        derive_status({"start_epoch": NOW - DAY, "end_epoch": NOW + 5 * DAY}, NOW, remind)
        == "active"
    )
    assert (
        derive_status({"start_epoch": NOW + DAY, "end_epoch": NOW + 5 * DAY}, NOW, remind)
        == "upcoming"
    )
    assert (
        derive_status({"start_epoch": NOW - 5 * DAY, "end_epoch": NOW - DAY}, NOW, remind)
        == "ended"
    )
    assert (
        derive_status(
            {"start_epoch": NOW - DAY, "end_epoch": NOW + DAY, "removed_at": NOW},
            NOW,
            remind,
        )
        == "removed"
    )


def test_recompute_statuses_does_not_mutate_input() -> None:
    activity = {"start_epoch": NOW - DAY, "end_epoch": NOW + 10 * DAY, "status": "ended"}
    result = recompute_statuses([activity], NOW, [1])
    assert result[0]["status"] == "active"
    assert activity["status"] == "ended"


def merged_row(
    name: str,
    game_id: str,
    source_id: str,
    *,
    start: int = NOW - DAY,
    first_seen: int = NOW,
    removed_at: int | None = None,
) -> dict:
    return {
        "key": f"{source_id}#a1",
        "raw_id": "a1",
        "source_id": source_id,
        "game_id": game_id,
        "name": name,
        "description": "",
        "kind": None,
        "start_iso": "",
        "end_iso": "",
        "start_epoch": start,
        "end_epoch": start + 10 * DAY,
        "cover_url": "",
        "content_hash": "h",
        "status": "active",
        "first_seen_at": first_seen,
        "last_seen_at": first_seen,
        "removed_at": removed_at,
    }


def test_merge_groups_similar_cross_source_activities() -> None:
    rows = [
        merged_row("星穹铁道活动测试", "starrail", "source_b", first_seen=NOW + 10),
        merged_row("星穹铁道活动测试", "starrail", "source_a", first_seen=NOW),
    ]
    result = merge_activities(rows, {"source_a": 100, "source_b": 100}, NOW, [1])
    assert len(result) == 1
    assert result[0]["source_id"] == "source_a"
    assert result[0]["merged_sources"] == ["source_a#a1", "source_b#a1"]


def test_merge_respects_priority_over_first_seen() -> None:
    rows = [
        merged_row("同名活动", "starrail", "source_a", first_seen=NOW),
        merged_row("同名活动", "starrail", "source_b", first_seen=NOW + 10),
    ]
    result = merge_activities(rows, {"source_a": 200, "source_b": 50}, NOW, [1])
    assert result[0]["source_id"] == "source_b"


def test_merge_does_not_cross_games_or_far_starts() -> None:
    rows = [
        merged_row("同名活动", "starrail", "source_a"),
        merged_row("同名活动", "genshin", "source_b"),
        merged_row("同名活动", "starrail", "source_c", start=NOW + 48 * 3600),
    ]
    result = merge_activities(
        rows,
        {"source_a": 100, "source_b": 100, "source_c": 100},
        NOW,
        [1],
    )
    assert len(result) == 3


def test_removed_rows_are_not_merged() -> None:
    rows = [
        merged_row("同名活动", "starrail", "source_a"),
        merged_row("同名活动", "starrail", "source_b", removed_at=NOW),
    ]
    result = merge_activities(rows, {"source_a": 100, "source_b": 100}, NOW, [1])
    assert len(result) == 2
    assert sum(1 for row in result if row["status"] == "removed") == 1


def test_primary_source_tie_break() -> None:
    group = [
        merged_row("同名活动", "starrail", "source_b", first_seen=NOW),
        merged_row("同名活动", "starrail", "source_a", first_seen=NOW),
    ]
    winner = primary_source(group, {"source_a": 100, "source_b": 100})
    assert winner["source_id"] == "source_a"
