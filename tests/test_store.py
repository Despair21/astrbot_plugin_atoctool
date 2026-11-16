"""Tests for atomic persistence and corrupt-file recovery."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import pytest

from game_activity.store import Store

LOGGER = logging.getLogger("test_store")


def make_store(data_root: Path) -> Store:
    return Store(data_root / "plugin_data", LOGGER)


def test_save_snapshot_roundtrip_and_no_tmp_left(data_root: Path) -> None:
    store = make_store(data_root)
    store.save_snapshot("starrail", {"schema_version": 1, "activities": []})
    assert store.load_snapshot("starrail")["schema_version"] == 1
    assert list((store.snapshots_dir).glob("*.tmp")) == []


def test_missing_snapshot_returns_empty(data_root: Path) -> None:
    assert make_store(data_root).load_snapshot("genshin") == {}


def test_invalid_game_id_is_rejected(data_root: Path) -> None:
    store = make_store(data_root)
    for bad in ("../evil", "UPPER", "", "a" * 33, "a/b"):
        with pytest.raises(ValueError):
            store.load_snapshot(bad)


def test_corrupt_json_is_quarantined(data_root: Path) -> None:
    store = make_store(data_root)
    store.ensure_dirs()
    path = store.snapshots_dir / "starrail.json"
    path.write_text("{not json", encoding="utf-8")
    assert store.load_snapshot("starrail") == {}
    assert not path.exists()
    assert list(store.snapshots_dir.glob("starrail.json.corrupt-*"))


def test_non_dict_root_is_quarantined(data_root: Path) -> None:
    store = make_store(data_root)
    store.ensure_dirs()
    (store.state_dir / "http_cache.json").write_text("[1, 2]", encoding="utf-8")
    assert store.load_state("http_cache") == {}
    assert list(store.state_dir.glob("http_cache.json.corrupt-*"))


def test_concurrent_writes_never_corrupt(data_root: Path) -> None:
    store = make_store(data_root)
    payloads = [{"schema_version": 1, "writer": index} for index in range(8)]

    async def run() -> None:
        await asyncio.gather(
            *(
                asyncio.to_thread(store.save_snapshot, "starrail", data)
                for data in payloads
            ),
        )

    asyncio.run(run())
    loaded = store.load_snapshot("starrail")
    assert loaded in payloads
    assert list(store.snapshots_dir.glob("*.tmp")) == []


def test_cleanup_removes_only_stale_tmp_files(data_root: Path) -> None:
    store = make_store(data_root)
    store.ensure_dirs()
    stale = store.snapshots_dir / "starrail.json.stale.tmp"
    fresh = store.snapshots_dir / "starrail.json.fresh.tmp"
    stale.write_text("x", encoding="utf-8")
    fresh.write_text("x", encoding="utf-8")
    os.utime(stale, (1, 1))
    assert store.cleanup_temp_files(max_age_seconds=3600) == 1
    assert not stale.exists()
    assert fresh.exists()


def test_write_failure_propagates_and_cleans_tmp(
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(data_root)

    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.save_state("reminders", {"schema_version": 1})
    assert list(store.state_dir.glob("*.tmp")) == []


def test_load_state_uses_default_copy(data_root: Path) -> None:
    store = make_store(data_root)
    default = {"schema_version": 1, "items": {}}
    loaded = store.load_state("reminders", default)
    loaded["items"]["x"] = 1
    assert default == {"schema_version": 1, "items": {}}


def test_state_name_is_validated(data_root: Path) -> None:
    store = make_store(data_root)
    with pytest.raises(ValueError):
        store.load_state("Bad-Name")
    with pytest.raises(ValueError):
        store.load_state("../etc/passwd")


def test_probe_writable(data_root: Path) -> None:
    store = make_store(data_root)
    assert store.probe_writable() is True
    assert not list(store.data_root.glob("*.probe"))


def test_snapshot_json_is_sorted_and_utf8(data_root: Path) -> None:
    store = make_store(data_root)
    store.save_snapshot("starrail", {"b": 1, "a": "活动"})
    raw = (store.snapshots_dir / "starrail.json").read_text(encoding="utf-8")
    assert raw.index('"a"') < raw.index('"b"')
    assert json.loads(raw) == {"a": "活动", "b": 1}
