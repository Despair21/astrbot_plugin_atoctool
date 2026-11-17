"""Cross-source activity merging and primary-source selection."""

from __future__ import annotations

from typing import Any

from .diff import recompute_statuses
from .ids import name_similarity

MERGE_RATIO = 0.85
MERGE_WINDOW_SECONDS = 24 * 3600
STATUS_ORDER = {
    "ending_soon": 0,
    "active": 1,
    "upcoming": 2,
    "ended": 3,
    "removed": 4,
}


def _priority(priorities: dict[str, int], activity: dict[str, Any]) -> int:
    return int(priorities.get(str(activity.get("source_id") or ""), 100))


def primary_source(
    group: list[dict[str, Any]],
    priorities: dict[str, int],
) -> dict[str, Any]:
    """Return the primary record of a merged group (design §10.5)."""
    return min(
        group,
        key=lambda activity: (
            _priority(priorities, activity),
            int(activity.get("first_seen_at") or 0),
            str(activity.get("source_id") or ""),
        ),
    )


def _cluster(
    entries: list[dict[str, Any]],
    priorities: dict[str, int],
) -> list[list[dict[str, Any]]]:
    """Greedily cluster similar activities across different sources."""
    parent = list(range(len(entries)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left != root_right:
            parent[root_left] = root_right

    candidates: list[tuple[float, int, int, int, str, str, int, int]] = []
    for left in range(len(entries)):
        for right in range(left + 1, len(entries)):
            a = entries[left]
            b = entries[right]
            if a.get("source_id") == b.get("source_id"):
                continue
            start_diff = abs(
                int(a.get("start_epoch") or 0) - int(b.get("start_epoch") or 0),
            )
            if start_diff > MERGE_WINDOW_SECONDS:
                continue
            ratio = name_similarity(
                str(a.get("name") or ""),
                str(b.get("name") or ""),
            )
            if ratio < MERGE_RATIO:
                continue
            candidates.append(
                (
                    -ratio,
                    start_diff,
                    _priority(priorities, a),
                    _priority(priorities, b),
                    str(a.get("source_id") or ""),
                    str(b.get("source_id") or ""),
                    left,
                    right,
                ),
            )
    candidates.sort(key=lambda item: item[:6])
    for _ratio, _diff, _pa, _pb, _sa, _sb, left, right in candidates:
        union(left, right)

    groups: dict[int, list[dict[str, Any]]] = {}
    for index, entry in enumerate(entries):
        groups.setdefault(find(index), []).append(entry)
    return list(groups.values())


def merge_activities(
    activities: list[dict[str, Any]],
    priorities: dict[str, int],
    now: int,
    remind_days: list[int] | tuple[int, ...],
) -> list[dict[str, Any]]:
    """Merge activities per game and return display-ready DTO dictionaries.

    Args:
        activities: All persisted activities (including tombstones).
        priorities: ``source_id -> priority`` used for primary selection.
        now: Current epoch seconds (statuses are recomputed).
        remind_days: Reminder lead times used to derive ``ending_soon``.

    Returns:
        One row per merged group (primary fields plus ``merged_sources``), plus
        one row per removed activity, sorted for stable rendering.
    """
    statuses = recompute_statuses(activities, now, remind_days)
    by_game: dict[str, list[dict[str, Any]]] = {}
    for activity in statuses:
        by_game.setdefault(str(activity.get("game_id") or ""), []).append(activity)

    output: list[dict[str, Any]] = []
    for entries in by_game.values():
        active = [item for item in entries if item.get("status") != "removed"]
        removed = [item for item in entries if item.get("status") == "removed"]
        for group in _cluster(active, priorities):
            primary = primary_source(group, priorities)
            merged = [
                key
                for _priority, key in sorted(
                    (
                        _priority(priorities, item),
                        str(item.get("key") or ""),
                    )
                    for item in group
                )
            ]
            dto = dict(primary)
            dto["is_primary"] = True
            dto["merged_sources"] = merged
            output.append(dto)
        for item in removed:
            dto = dict(item)
            dto["is_primary"] = True
            dto["merged_sources"] = [str(item.get("key") or "")]
            output.append(dto)

    output.sort(
        key=lambda activity: (
            STATUS_ORDER.get(str(activity.get("status") or ""), 9),
            int(activity.get("end_epoch") or 0),
            str(activity.get("key") or ""),
        ),
    )
    return output
