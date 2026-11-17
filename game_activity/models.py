"""Domain models and constants shared by the game activity plugin modules.

This module is intentionally free of any AstrBot import so the domain logic can
be unit tested without a running AstrBot instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION = 1

GAME_IDS: tuple[str, ...] = (
    "genshin",
    "starrail",
    "zenless",
    "arknights",
    "endfield",
    "wuthering_waves",
)

GAME_NAMES: dict[str, str] = {
    "genshin": "原神",
    "starrail": "崩坏：星穹铁道",
    "zenless": "绝区零",
    "arknights": "明日方舟",
    "endfield": "明日方舟：终末地",
    "wuthering_waves": "鸣潮",
}

DEFAULT_SOURCE_URLS: dict[str, str] = {
    "genshin": "https://starrailassistant.top/api/v1/activity/ys.json",
    "starrail": "https://starrailassistant.top/api/v1/activity/sr.json",
    "zenless": "https://starrailassistant.top/api/v1/activity/zzz.json",
    "arknights": "https://starrailassistant.top/api/v1/activity/ak.json",
    "endfield": "https://starrailassistant.top/api/v1/activity/end.json",
    "wuthering_waves": "https://starrailassistant.top/api/v1/activity/ww.json",
}

STATUSES: tuple[str, ...] = (
    "upcoming",
    "active",
    "ending_soon",
    "ended",
    "removed",
)

SOURCE_STATUSES: tuple[str, ...] = ("ok", "stale", "error", "disabled")

SYNC_STATUSES: tuple[str, ...] = ("idle", "running", "error")

REMINDER_STATUSES: tuple[str, ...] = ("pending", "sent", "skipped", "failed")

DEFAULT_REMIND_DAYS: tuple[int, ...] = (1,)

GAME_ID_PATTERN = r"^[a-z0-9_]{1,32}$"

_FIXED_OFFSET_HOURS = {
    "asia/shanghai": 8,
    "asia/chongqing": 8,
    "prc": 8,
    "etc/gmt-8": 8,
}


def load_zone(name: str) -> tzinfo:
    """Load a timezone, falling back to a fixed offset when tzdata is absent.

    Args:
        name: IANA timezone name.

    Returns:
        A tzinfo instance.

    Raises:
        ValueError: When the zone is unknown and has no safe fixed-offset alias.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        offset = _FIXED_OFFSET_HOURS.get(str(name or "").strip().lower())
        if offset is not None:
            return timezone(timedelta(hours=offset))
        raise ValueError(f"unknown_timezone:{name}") from None


DTO_FIELDS: tuple[str, ...] = (
    "key",
    "raw_id",
    "source_id",
    "game_id",
    "name",
    "description",
    "kind",
    "start_iso",
    "end_iso",
    "start_epoch",
    "end_epoch",
    "cover_url",
    "content_hash",
    "status",
    "is_primary",
    "merged_sources",
    "first_seen_at",
    "last_seen_at",
    "removed_at",
)


@dataclass(slots=True)
class NormalizedActivity:
    """One upstream activity after adapter normalization."""

    name: str
    description: str
    kind: str | None
    start_epoch: int
    end_epoch: int
    start_iso: str
    end_iso: str
    cover_url: str = ""


@dataclass(slots=True)
class SourceConfig:
    """One configured activity source."""

    source_id: str
    game_id: str
    game_name: str
    url: str
    adapter: str
    priority: int = 100
    timezone: str = "Asia/Shanghai"
    enabled: bool = True
    mapping: dict[str, str] = field(default_factory=dict)
    disabled_reason: str = ""

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation."""
        return {
            "source_id": self.source_id,
            "game_id": self.game_id,
            "game_name": self.game_name,
            "url": self.url,
            "adapter": self.adapter,
            "priority": self.priority,
            "timezone": self.timezone,
            "enabled": self.enabled,
            "mapping": dict(self.mapping),
            "disabled_reason": self.disabled_reason,
        }


@dataclass(slots=True)
class Activity:
    """One persisted activity record."""

    key: str
    raw_id: str
    source_id: str
    game_id: str
    name: str
    description: str
    kind: str | None
    start_iso: str
    end_iso: str
    start_epoch: int
    end_epoch: int
    cover_url: str
    content_hash: str
    status: str
    first_seen_at: int
    last_seen_at: int
    removed_at: int | None = None

    def to_dict(self) -> dict:
        """Return the persisted representation (derived fields excluded)."""
        return {
            "key": self.key,
            "raw_id": self.raw_id,
            "source_id": self.source_id,
            "game_id": self.game_id,
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "start_iso": self.start_iso,
            "end_iso": self.end_iso,
            "start_epoch": self.start_epoch,
            "end_epoch": self.end_epoch,
            "cover_url": self.cover_url,
            "content_hash": self.content_hash,
            "status": self.status,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "removed_at": self.removed_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Activity:
        """Build an activity from persisted data, tolerating missing fields."""
        return cls(
            key=str(data.get("key") or ""),
            raw_id=str(data.get("raw_id") or ""),
            source_id=str(data.get("source_id") or ""),
            game_id=str(data.get("game_id") or ""),
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            kind=data.get("kind") if isinstance(data.get("kind"), str) else None,
            start_iso=str(data.get("start_iso") or ""),
            end_iso=str(data.get("end_iso") or ""),
            start_epoch=int(data.get("start_epoch") or 0),
            end_epoch=int(data.get("end_epoch") or 0),
            cover_url=str(data.get("cover_url") or ""),
            content_hash=str(data.get("content_hash") or ""),
            status=str(data.get("status") or "active"),
            first_seen_at=int(data.get("first_seen_at") or 0),
            last_seen_at=int(data.get("last_seen_at") or 0),
            removed_at=(
                int(data["removed_at"])
                if isinstance(data.get("removed_at"), (int, float))
                else None
            ),
        )

    def to_dto(
        self,
        *,
        is_primary: bool = True,
        merged_sources: list[str] | None = None,
    ) -> dict:
        """Return the API DTO with contract field order."""
        data = self.to_dict()
        data["is_primary"] = is_primary
        data["merged_sources"] = list(merged_sources) if merged_sources else [self.key]
        return {name: data[name] for name in DTO_FIELDS}
