"""Tests for payload adapters and source configuration normalization."""

from __future__ import annotations

import pytest

from game_activity.adapters import (
    ADAPTERS,
    GenericJsonAdapter,
    StarrailAssistantV1Adapter,
    load_zone,
    normalize_sources,
    parse_upstream_time,
)
from game_activity.models import GAME_IDS, SourceConfig

STARRAIL = ADAPTERS["starrail_assistant_v1"]
GENERIC = ADAPTERS["generic_json"]


def make_cfg(
    *,
    adapter: str = "starrail_assistant_v1",
    timezone_name: str = "Asia/Shanghai",
    mapping: dict[str, str] | None = None,
    url: str = "https://example.com/a.json",
) -> SourceConfig:
    return SourceConfig(
        source_id="demo",
        game_id="demo",
        game_name="Demo",
        url=url,
        adapter=adapter,
        timezone=timezone_name,
        mapping=mapping or {},
    )


def make_item(
    name: str = "活动",
    start: str = "2026-11-04T06:00:00",
    end: str = "2026-11-10 03:59:59",
    **extra,
) -> dict:
    item = {
        "name": name,
        "description": "描述",
        "startTime": start,
        "endTime": end,
        "cover": "https://example.com/c.png",
    }
    item.update(extra)
    return item


def envelope(activities: list[dict], **extra) -> dict:
    payload = {"version": "7.2", "versionName": "版本名", "activities": activities}
    payload.update(extra)
    return payload


def test_parse_upstream_time_variants() -> None:
    tz = load_zone("Asia/Shanghai")
    epoch_t, iso_t = parse_upstream_time("2026-11-04T06:00:00", tz)
    epoch_space, iso_space = parse_upstream_time("2026-11-04 06:00:00", tz)
    assert epoch_t == epoch_space
    assert iso_t.endswith("+08:00")
    assert iso_space.endswith("+08:00")
    epoch_utc, iso_utc = parse_upstream_time("2026-11-04T06:00:00Z", tz)
    assert epoch_utc == epoch_t + 8 * 3600
    assert iso_utc.endswith("+00:00")
    epoch_date, iso_date = parse_upstream_time("2026-11-04", tz)
    assert iso_date.startswith("2026-11-04T00:00:00")


def test_parse_upstream_time_rejects_invalid() -> None:
    tz = load_zone("Asia/Shanghai")
    for value in (None, "", "not-a-time", 123):
        with pytest.raises(ValueError):
            parse_upstream_time(value, tz)


def test_starrail_adapter_parses_envelope() -> None:
    cfg = make_cfg()
    items = STARRAIL.parse(
        envelope(
            [
                make_item("活动一"),
                make_item("活动二", start="2026-11-05T00:00:00Z"),
            ],
        ),
        cfg,
    )
    assert [item.name for item in items] == ["活动一", "活动二"]
    assert items[0].description == "描述"
    assert items[0].cover_url == "https://example.com/c.png"
    assert items[0].kind is None
    assert items[1].start_iso.endswith("+00:00")


def test_starrail_adapter_skips_bad_items() -> None:
    cfg = make_cfg()
    items = STARRAIL.parse(
        envelope(
            [
                make_item("正常"),
                make_item("坏时间", start="bad"),
                make_item("   ", end="2026-11-10"),
                "not-a-dict",
                {"name": None, "startTime": "2026-11-10", "endTime": "2026-11-11"},
            ],
        ),
        cfg,
    )
    assert [item.name for item in items] == ["正常"]
    assert STARRAIL.count_raw(envelope([make_item()]), cfg) == 1


def test_starrail_adapter_normalizes_kind_and_optional_fields() -> None:
    cfg = make_cfg()
    items = STARRAIL.parse(
        envelope([{"name": "活动", "startTime": "2026-11-01", "endTime": "2026-11-02", "kind": ""}]),
        cfg,
    )
    assert items[0].kind is None
    assert items[0].description == ""
    assert items[0].cover_url == ""


def test_starrail_adapter_rejects_missing_envelope() -> None:
    with pytest.raises(ValueError):
        STARRAIL.parse({"activities": "nope"}, make_cfg())
    with pytest.raises(ValueError):
        STARRAIL.parse({}, make_cfg())


def test_starrail_version_info() -> None:
    version, version_name = STARRAIL.version_info(envelope([]), make_cfg())
    assert (version, version_name) == ("7.2", "版本名")


def test_generic_adapter_maps_dotted_paths() -> None:
    cfg = make_cfg(
        adapter="generic_json",
        mapping={
            "map_activities": "data.list",
            "map_name": "title",
            "map_start": "begin",
            "map_end": "finish",
            "map_description": "meta.desc",
            "map_cover": "meta.image",
            "map_kind": "meta.kind",
        },
    )
    payload = {
        "data": {
            "list": [
                {
                    "title": "自定义活动",
                    "begin": "2026-11-01T00:00:00",
                    "finish": "2026-11-02T00:00:00",
                    "meta": {"desc": "说明", "image": "https://x/y.png", "kind": "event"},
                },
            ],
        },
    }
    items = GENERIC.parse(payload, cfg)
    assert len(items) == 1
    assert items[0].name == "自定义活动"
    assert items[0].description == "说明"
    assert items[0].kind == "event"
    assert GENERIC.count_raw(payload, cfg) == 1


def test_generic_adapter_requires_mapping() -> None:
    cfg = make_cfg(adapter="generic_json")
    errors = GENERIC.validate(cfg)
    assert "missing_map_activities" in errors
    with pytest.raises(ValueError):
        GENERIC.parse({"activities": []}, cfg)


def test_generic_adapter_skips_invalid_items() -> None:
    cfg = make_cfg(
        adapter="generic_json",
        mapping={
            "map_activities": "items",
            "map_name": "name",
            "map_start": "start",
            "map_end": "end",
        },
    )
    payload = {
        "items": [
            {"name": "ok", "start": "2026-11-01", "end": "2026-11-02"},
            {"name": "bad", "start": "x", "end": "2026-11-02"},
            "nope",
        ],
    }
    assert [item.name for item in GENERIC.parse(payload, cfg)] == ["ok"]


def test_adapter_registry_contains_both_adapters() -> None:
    assert isinstance(ADAPTERS["starrail_assistant_v1"], StarrailAssistantV1Adapter)
    assert isinstance(ADAPTERS["generic_json"], GenericJsonAdapter)


def test_load_zone_rejects_unknown_zone() -> None:
    with pytest.raises(ValueError):
        load_zone("Not/AZone")


def test_normalize_sources_defaults_to_six_builtin_sources() -> None:
    for raw in (None, [], "bad", {}):
        sources, warnings = normalize_sources(raw)
        assert warnings == []
        assert [source.source_id for source in sources] == list(GAME_IDS)
        assert all(source.enabled for source in sources)
        assert all(source.url.startswith("https://") for source in sources)


def test_normalize_sources_preset_override_and_clamps() -> None:
    sources, warnings = normalize_sources(
        [
            {
                "adapter": "starrail_assistant_v1",
                "game": "starrail",
                "url": "https://mirror.example.com/sr.json",
                "priority": 5000,
            },
        ],
    )
    assert warnings == []
    assert len(sources) == 1
    source = sources[0]
    assert source.source_id == "starrail"
    assert source.url == "https://mirror.example.com/sr.json"
    assert source.priority == 999
    assert source.enabled is True


def test_normalize_sources_skips_unknown_and_duplicate_entries() -> None:
    sources, warnings = normalize_sources(
        [
            {"adapter": "starrail_assistant_v1", "game": "unknown"},
            {"adapter": "starrail_assistant_v1", "game": "genshin"},
            {"adapter": "starrail_assistant_v1", "game": "genshin"},
            {"adapter": "missing_adapter"},
            "not-a-dict",
        ],
    )
    assert [source.source_id for source in sources] == ["genshin"]
    assert len(warnings) == 4


def test_normalize_sources_generic_ids_and_http_policy() -> None:
    sources, warnings = normalize_sources(
        [
            {
                "adapter": "generic_json",
                "source_id": "My Source!",
                "game_name": "Demo Game",
                "url": "http://example.com/a.json",
                "map_activities": "items",
                "map_name": "name",
                "map_start": "start",
                "map_end": "end",
            },
        ],
    )
    assert sources[0].source_id == "my_source"
    assert sources[0].game_id == "demo_game"
    assert sources[0].enabled is False
    assert sources[0].disabled_reason == "blocked_insecure_http"
    assert warnings

    allowed, _ = normalize_sources(
        [
            {
                "adapter": "generic_json",
                "source_id": "my_source",
                "game_name": "Demo Game",
                "url": "http://example.com/a.json",
                "map_activities": "items",
                "map_name": "name",
                "map_start": "start",
                "map_end": "end",
            },
        ],
        allow_insecure_http=True,
    )
    assert allowed[0].enabled is True


def test_normalize_sources_disables_invalid_timezone() -> None:
    sources, warnings = normalize_sources(
        [{"adapter": "starrail_assistant_v1", "game": "genshin", "timezone": "Nowhere/Nope"}],
    )
    assert sources[0].enabled is False
    assert sources[0].disabled_reason == "unknown_timezone"
    assert any("unknown_timezone" in warning for warning in warnings)


def test_normalize_sources_all_invalid_returns_empty() -> None:
    sources, warnings = normalize_sources([{"adapter": "missing"}])
    assert sources == []
    assert warnings
