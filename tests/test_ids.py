"""Tests for stable ids, content hashing and rename detection."""

from __future__ import annotations

from game_activity.ids import (
    assign_raw_ids,
    base_raw_id,
    content_hash,
    is_rename,
    name_similarity,
    normalize_name,
)
from game_activity.models import NormalizedActivity


def make_item(
    name: str,
    *,
    start: int = 1_700_000_000,
    end: int = 1_700_100_000,
    description: str = "",
    kind: str | None = None,
    cover: str = "",
) -> NormalizedActivity:
    return NormalizedActivity(
        name=name,
        description=description,
        kind=kind,
        start_epoch=start,
        end_epoch=end,
        start_iso="2026-11-01T10:00:00+08:00",
        end_iso="2026-11-10T10:00:00+08:00",
        cover_url=cover,
    )


def make_existing(
    raw_id: str,
    name: str,
    *,
    start: int = 1_700_000_000,
    end: int = 1_700_100_000,
    removed_at: int | None = None,
) -> dict:
    return {
        "raw_id": raw_id,
        "name": name,
        "start_epoch": start,
        "end_epoch": end,
        "removed_at": removed_at,
    }


def test_normalize_name_handles_unicode_width_and_spaces() -> None:
    assert normalize_name("  Ａ  B\u3000c ") == "a b c"
    assert normalize_name("") == ""
    assert normalize_name(" 多个   空格 ") == "多个 空格"


def test_base_raw_id_is_deterministic_and_short() -> None:
    first = base_raw_id("starrail", "测试活动")
    second = base_raw_id("starrail", " 测试活动 ")
    assert first == second
    assert first.startswith("a")
    assert len(first) == 17
    assert int(first[1:], 16) >= 0
    assert first != base_raw_id("genshin", "测试活动")


def test_content_hash_is_stable_and_tracks_changes() -> None:
    base = make_item("活动", description="描述")
    assert content_hash(base) == content_hash(make_item("活动", description="描述"))
    assert content_hash(base) != content_hash(make_item("活动", description="描述2"))
    assert content_hash(base) != content_hash(make_item("活动", start=1_700_000_001))
    assert content_hash(make_item("活动", kind=None)) == content_hash(
        make_item("活动", kind=""),
    )


def test_name_similarity_bounds() -> None:
    assert name_similarity("abc", "abc") == 1.0
    assert name_similarity("", "abc") == 0.0
    assert 0.9 <= name_similarity("abcdefghij", "abcdefghijx") < 1.0


def test_assign_raw_ids_reuses_id_for_same_fingerprint() -> None:
    existing = [make_existing("a0123456789abcdef", "活动A")]
    pairs, warnings = assign_raw_ids("starrail", [make_item("活动A")], existing)
    assert warnings == []
    assert len(pairs) == 1
    assert pairs[0][0] == "a0123456789abcdef"


def test_assign_raw_ids_gives_duplicate_names_suffixes() -> None:
    items = [
        make_item("同名活动", start=1_700_000_100),
        make_item("同名活动", start=1_700_000_000),
    ]
    pairs, warnings = assign_raw_ids("starrail", items, [])
    assert warnings == []
    base = base_raw_id("starrail", "同名活动")
    # Sorted by start: the earlier item gets the base id; pairs keep input order.
    assert pairs[0][0] == f"{base}-2"
    assert pairs[1][0] == base


def test_assign_raw_ids_never_reuses_reserved_id_of_other_activity() -> None:
    base = base_raw_id("starrail", "活动")
    existing = [make_existing(base, "活动", start=1_000, end=2_000)]
    pairs, _ = assign_raw_ids(
        "starrail",
        [make_item("活动", start=3_000, end=4_000)],
        existing,
    )
    assert len(pairs) == 1
    assert pairs[0][0] == f"{base}-2"


def test_is_rename_requires_equal_times_and_similar_names() -> None:
    old = make_existing("a1", "abcdefghij")
    assert is_rename(old, make_item("abcdefghijx"))
    assert not is_rename(old, make_item("abcdefghijx", start=1_700_000_001))
    assert not is_rename(old, make_item("完全不同的大标题"))


def test_assign_raw_ids_is_idempotent_across_runs() -> None:
    items = [make_item("活动一"), make_item("活动二")]
    first_pairs, _ = assign_raw_ids("starrail", items, [])
    existing = [
        make_existing(raw_id, item.name, start=item.start_epoch, end=item.end_epoch)
        for raw_id, item in first_pairs
    ]
    second_pairs, _ = assign_raw_ids("starrail", items, existing)
    assert [raw for raw, _ in second_pairs] == [raw for raw, _ in first_pairs]
