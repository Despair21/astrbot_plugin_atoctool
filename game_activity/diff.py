"""Snapshot diffing, tombstones and derived statuses."""

from __future__ import annotations

from typing import Any

from .ids import assign_raw_ids, content_hash, is_rename, name_similarity
from .models import (
    SCHEMA_VERSION,
    Activity,
    NormalizedActivity,
    SourceConfig,
)


def derive_status(
    activity: dict[str, Any],
    now: int,
    remind_days: list[int] | tuple[int, ...],
) -> str:
    """Derive the display status of an activity for ``now`` (design §10.3)."""
    if activity.get("removed_at"):
        return "removed"
    end_epoch = int(activity.get("end_epoch") or 0)
    start_epoch = int(activity.get("start_epoch") or 0)
    if now > end_epoch:
        return "ended"
    if now < start_epoch:
        return "upcoming"
    window_days = min(remind_days) if remind_days else 1
    if end_epoch - now <= window_days * 86400:
        return "ending_soon"
    return "active"


def recompute_statuses(
    activities: list[dict[str, Any]],
    now: int,
    remind_days: list[int] | tuple[int, ...],
) -> list[dict[str, Any]]:
    """Return copies of ``activities`` with statuses recomputed at ``now``."""
    result: list[dict[str, Any]] = []
    for activity in activities:
        item = dict(activity)
        item["status"] = derive_status(item, now, remind_days)
        result.append(item)
    return result


def _as_normalized(activity: dict[str, Any]) -> NormalizedActivity:
    return NormalizedActivity(
        name=str(activity.get("name") or ""),
        description=str(activity.get("description") or ""),
        kind=activity.get("kind") if isinstance(activity.get("kind"), str) else None,
        start_epoch=int(activity.get("start_epoch") or 0),
        end_epoch=int(activity.get("end_epoch") or 0),
        start_iso=str(activity.get("start_iso") or ""),
        end_iso=str(activity.get("end_iso") or ""),
        cover_url=str(activity.get("cover_url") or ""),
    )


def _new_activity(
    cfg: SourceConfig,
    raw_id: str,
    item: NormalizedActivity,
    now: int,
) -> dict[str, Any]:
    return {
        "key": f"{cfg.source_id}#{raw_id}",
        "raw_id": raw_id,
        "source_id": cfg.source_id,
        "game_id": cfg.game_id,
        "name": item.name,
        "description": item.description,
        "kind": item.kind,
        "start_iso": item.start_iso,
        "end_iso": item.end_iso,
        "start_epoch": int(item.start_epoch),
        "end_epoch": int(item.end_epoch),
        "cover_url": item.cover_url,
        "content_hash": content_hash(item),
        "status": "active",
        "first_seen_at": now,
        "last_seen_at": now,
        "removed_at": None,
    }


def _rename_pairs(
    added: list[tuple[int, dict[str, Any]]],
    unmatched_old: list[dict[str, Any]],
) -> list[tuple[int, dict[str, Any]]]:
    """Greedily pair renamed new activities with unmatched old records."""
    candidates: list[tuple[float, str, str, int, dict[str, Any]]] = []
    for index, new_activity in added:
        normalized = _as_normalized(new_activity)
        for old in unmatched_old:
            if not is_rename(old, normalized):
                continue
            similarity = name_similarity(
                str(old.get("name") or ""),
                new_activity["name"],
            )
            candidates.append(
                (
                    -similarity,
                    str(old.get("raw_id") or ""),
                    str(new_activity.get("key") or ""),
                    index,
                    old,
                ),
            )
    candidates.sort(key=lambda entry: entry[:3])
    used_old: set[str] = set()
    used_new: set[int] = set()
    pairs: list[tuple[int, dict[str, Any]]] = []
    for _neg_similarity, raw_id, _key, index, old in candidates:
        if raw_id in used_old or index in used_new:
            continue
        used_old.add(raw_id)
        used_new.add(index)
        pairs.append((index, old))
    return pairs


def apply_source_items(
    old_snapshot: dict[str, Any] | None,
    cfg: SourceConfig,
    items: list[NormalizedActivity],
    *,
    now: int,
    retention_days: int,
    remind_days: list[int] | tuple[int, ...],
    invalid_items: int = 0,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Merge one source's parsed items into the game snapshot.

    Args:
        old_snapshot: Existing snapshot (``{}`` when absent).
        cfg: Source being synced.
        items: Parsed activities of this round.
        now: Current epoch seconds.
        retention_days: How long removed tombstones are kept.
        remind_days: Reminder lead times used to derive ``ending_soon``.
        invalid_items: Count of raw entries skipped by the adapter.

    Returns:
        ``(snapshot, stats)`` where stats holds event counts; when a non-empty
        snapshot receives an empty upstream list, removals are skipped and
        ``stats["skipped_empty"]`` is ``1`` (defensive, design §14.3 #5).
    """
    snapshot = dict(old_snapshot or {})
    raw_activities = snapshot.get("activities")
    old_activities: list[dict[str, Any]] = []
    if isinstance(raw_activities, list):
        for raw in raw_activities:
            if isinstance(raw, dict):
                old_activities.append(Activity.from_dict(raw).to_dict())

    others = [item for item in old_activities if item.get("source_id") != cfg.source_id]
    source_old = [
        item for item in old_activities if item.get("source_id") == cfg.source_id
    ]
    stats = {
        "added": 0,
        "changed": 0,
        "renamed": 0,
        "removed": 0,
        "invalid_items": int(invalid_items),
        "skipped_empty": 0,
    }
    if not items and source_old:
        return snapshot, stats | {"skipped_empty": 1}

    pairs, _warnings = assign_raw_ids(cfg.source_id, items, source_old)
    old_by_raw = {
        str(activity.get("raw_id")): activity
        for activity in source_old
        if activity.get("raw_id")
    }
    matched_old: set[str] = set()
    produced: list[dict[str, Any]] = [dict(activity) for activity in others]
    added: list[tuple[int, dict[str, Any]]] = []

    for raw_id, item in pairs:
        old = old_by_raw.get(raw_id)
        if old is not None and raw_id not in matched_old:
            matched_old.add(raw_id)
            updated = _new_activity(cfg, raw_id, item, now)
            updated["first_seen_at"] = int(old.get("first_seen_at") or now)
            updated["last_seen_at"] = now
            updated["removed_at"] = None
            if updated["content_hash"] != old.get("content_hash"):
                stats["changed"] += 1
            produced.append(updated)
        else:
            added.append((len(added), _new_activity(cfg, raw_id, item, now)))

    unmatched_old = [
        activity
        for activity in source_old
        if str(activity.get("raw_id")) not in matched_old
        and not activity.get("removed_at")
    ]
    renamed = _rename_pairs(added, unmatched_old)
    renamed_indexes = {index for index, _old in renamed}
    for index, old in renamed:
        new_activity = added[index][1]
        new_activity["raw_id"] = str(old.get("raw_id"))
        new_activity["key"] = f"{cfg.source_id}#{new_activity['raw_id']}"
        new_activity["first_seen_at"] = int(old.get("first_seen_at") or now)
        new_activity["last_seen_at"] = now
        new_activity["removed_at"] = None
        produced.append(new_activity)
        matched_old.add(str(old.get("raw_id")))
        stats["renamed"] += 1

    for index, new_activity in added:
        if index in renamed_indexes:
            continue
        produced.append(new_activity)
        stats["added"] += 1

    for old in source_old:
        raw_id = str(old.get("raw_id") or "")
        if raw_id in matched_old:
            continue
        if old.get("removed_at"):
            removed_at = int(old.get("removed_at") or 0)
            if now - removed_at <= retention_days * 86400:
                tombstone = dict(old)
                tombstone["status"] = "removed"
                produced.append(tombstone)
            continue
        tombstone = dict(old)
        tombstone["removed_at"] = now
        tombstone["last_seen_at"] = now
        tombstone["status"] = "removed"
        produced.append(tombstone)
        stats["removed"] += 1

    for activity in produced:
        activity["status"] = derive_status(activity, now, remind_days)
        activity.pop("is_primary", None)
        activity.pop("merged_sources", None)

    produced.sort(
        key=lambda activity: (
            int(activity.get("start_epoch") or 0),
            str(activity.get("key") or ""),
        )
    )
    snapshot["schema_version"] = SCHEMA_VERSION
    snapshot["game_id"] = cfg.game_id
    snapshot["game_name"] = cfg.game_name
    snapshot["last_sync_at"] = now
    snapshot["activities"] = produced
    return snapshot, stats
