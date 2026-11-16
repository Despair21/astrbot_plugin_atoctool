"""Upstream payload adapters and source configuration normalization."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .fetch import FetchError, validate_source_url
from .ids import normalize_name
from .models import (
    DEFAULT_SOURCE_URLS,
    GAME_IDS,
    GAME_NAMES,
    NormalizedActivity,
    SourceConfig,
)

_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d")
_FIXED_OFFSET_HOURS = {
    "asia/shanghai": 8,
    "asia/chongqing": 8,
    "prc": 8,
    "etc/gmt-8": 8,
}
_MAP_KEYS = (
    "map_activities",
    "map_name",
    "map_description",
    "map_start",
    "map_end",
    "map_cover",
    "map_kind",
    "map_version",
    "map_version_name",
)
_SOURCE_ID_RE = re.compile(r"[^a-z0-9_]+")
_ASCII_ID_RE = re.compile(r"[^a-z0-9]+")


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


def parse_upstream_time(value: Any, tz: tzinfo) -> tuple[int, str]:
    """Parse an upstream time string into ``(epoch, offset_iso)``.

    Args:
        value: Upstream timestamp; ISO 8601 (with ``T`` or space), ``Z`` suffix
            or a date-only value are accepted.
        tz: Timezone applied when the value has no offset.

    Raises:
        ValueError: When the value cannot be parsed.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid_time")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in _TIME_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        raise ValueError("invalid_time")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return int(parsed.timestamp()), parsed.isoformat()


def get_by_path(obj: Any, path: str, default: Any = None) -> Any:
    """Read a dotted path from nested objects (no array indexes)."""
    current = obj
    for part in path.split(".") if path else []:
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


class Adapter:
    """Base class for upstream payload adapters."""

    name = ""

    def validate(self, cfg: SourceConfig) -> list[str]:
        """Return configuration errors for one source (empty list = valid)."""
        return []

    def count_raw(self, payload: dict[str, Any], cfg: SourceConfig) -> int:
        """Return the number of raw activity entries in a payload."""
        activities = payload.get("activities")
        return len(activities) if isinstance(activities, list) else 0

    def parse(
        self,
        payload: dict[str, Any],
        cfg: SourceConfig,
    ) -> list[NormalizedActivity]:
        """Convert a payload into normalized activities."""
        raise NotImplementedError

    def version_info(self, payload: dict[str, Any], cfg: SourceConfig) -> tuple[str, str]:
        """Return upstream version fields for the snapshot source record."""
        version = payload.get("version")
        version_name = payload.get("versionName")
        return (
            str(version) if isinstance(version, (str, int, float)) else "",
            str(version_name) if isinstance(version_name, str) else "",
        )


class StarrailAssistantV1Adapter(Adapter):
    """Adapter for the shared starrailassistant.top activity envelope."""

    name = "starrail_assistant_v1"

    def validate(self, cfg: SourceConfig) -> list[str]:
        errors: list[str] = []
        if not cfg.url:
            errors.append("missing_url")
        return errors

    def parse(
        self,
        payload: dict[str, Any],
        cfg: SourceConfig,
    ) -> list[NormalizedActivity]:
        tz = load_zone(cfg.timezone)
        activities = payload.get("activities")
        if not isinstance(activities, list):
            raise ValueError("invalid_envelope")
        items: list[NormalizedActivity] = []
        for raw in activities:
            if not isinstance(raw, dict):
                continue
            name = raw.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            try:
                start_epoch, start_iso = parse_upstream_time(raw.get("startTime"), tz)
                end_epoch, end_iso = parse_upstream_time(raw.get("endTime"), tz)
            except ValueError:
                continue
            description = raw.get("description")
            cover = raw.get("cover")
            kind = raw.get("kind")
            items.append(
                NormalizedActivity(
                    name=name.strip(),
                    description=description.strip() if isinstance(description, str) else "",
                    kind=(kind.strip() or None) if isinstance(kind, str) else None,
                    start_epoch=start_epoch,
                    end_epoch=end_epoch,
                    start_iso=start_iso,
                    end_iso=end_iso,
                    cover_url=cover.strip() if isinstance(cover, str) else "",
                ),
            )
        return items


class GenericJsonAdapter(Adapter):
    """Adapter for arbitrary JSON feeds configured through dotted paths."""

    name = "generic_json"

    _REQUIRED = ("map_activities", "map_name", "map_start", "map_end")

    def validate(self, cfg: SourceConfig) -> list[str]:
        errors = [f"missing_{key}" for key in self._REQUIRED if not cfg.mapping.get(key)]
        if not cfg.url:
            errors.append("missing_url")
        return errors

    def count_raw(self, payload: dict[str, Any], cfg: SourceConfig) -> int:
        path = cfg.mapping.get("map_activities") or "activities"
        activities = get_by_path(payload, path)
        return len(activities) if isinstance(activities, list) else 0

    def version_info(self, payload: dict[str, Any], cfg: SourceConfig) -> tuple[str, str]:
        version_path = cfg.mapping.get("map_version") or "version"
        version_name_path = cfg.mapping.get("map_version_name") or "versionName"
        version = get_by_path(payload, version_path, "")
        version_name = get_by_path(payload, version_name_path, "")
        return (
            str(version) if isinstance(version, (str, int, float)) else "",
            str(version_name) if isinstance(version_name, str) else "",
        )

    def parse(
        self,
        payload: dict[str, Any],
        cfg: SourceConfig,
    ) -> list[NormalizedActivity]:
        missing = [key for key in self._REQUIRED if not cfg.mapping.get(key)]
        if missing:
            raise ValueError(f"missing_mapping:{','.join(missing)}")
        tz = load_zone(cfg.timezone)
        activities = get_by_path(payload, cfg.mapping["map_activities"])
        if not isinstance(activities, list):
            raise ValueError("invalid_envelope")
        items: list[NormalizedActivity] = []
        for raw in activities:
            if not isinstance(raw, dict):
                continue
            name = get_by_path(raw, cfg.mapping["map_name"])
            if not isinstance(name, str) or not name.strip():
                continue
            try:
                start_epoch, start_iso = parse_upstream_time(
                    get_by_path(raw, cfg.mapping["map_start"]),
                    tz,
                )
                end_epoch, end_iso = parse_upstream_time(
                    get_by_path(raw, cfg.mapping["map_end"]),
                    tz,
                )
            except ValueError:
                continue
            description = self._optional(raw, cfg, "map_description")
            cover = self._optional(raw, cfg, "map_cover")
            kind = self._optional(raw, cfg, "map_kind")
            items.append(
                NormalizedActivity(
                    name=name.strip(),
                    description=description,
                    kind=kind or None,
                    start_epoch=start_epoch,
                    end_epoch=end_epoch,
                    start_iso=start_iso,
                    end_iso=end_iso,
                    cover_url=cover,
                ),
            )
        return items

    @staticmethod
    def _optional(raw: dict[str, Any], cfg: SourceConfig, key: str) -> str:
        path = cfg.mapping.get(key)
        if not path:
            return ""
        value = get_by_path(raw, path)
        return value.strip() if isinstance(value, str) else ""


ADAPTERS: dict[str, Adapter] = {
    "starrail_assistant_v1": StarrailAssistantV1Adapter(),
    "generic_json": GenericJsonAdapter(),
}


def _default_sources() -> list[SourceConfig]:
    return [
        SourceConfig(
            source_id=game_id,
            game_id=game_id,
            game_name=GAME_NAMES[game_id],
            url=DEFAULT_SOURCE_URLS[game_id],
            adapter="starrail_assistant_v1",
            priority=100,
            timezone="Asia/Shanghai",
            enabled=True,
        )
        for game_id in GAME_IDS
    ]


def _clamp_priority(value: Any) -> int:
    try:
        priority = int(value)
    except (TypeError, ValueError):
        return 100
    return max(0, min(999, priority))


def _sanitize_source_id(value: str) -> str:
    cleaned = _SOURCE_ID_RE.sub("_", value.strip().lower()).strip("_")
    return cleaned[:32]


def _sanitize_game_id(value: str) -> str:
    cleaned = _ASCII_ID_RE.sub("_", normalize_name(value)).strip("_")
    return cleaned[:32]


def _derive_source_id(game_name: str) -> str:
    digest = hashlib.sha1(normalize_name(game_name).encode("utf-8")).hexdigest()
    return f"src{digest[:6]}"


def _build_generic_config(entry: dict[str, Any]) -> SourceConfig | None:
    raw_source_id = str(entry.get("source_id") or "").strip().lower()
    game_name = str(entry.get("game_name") or "").strip()
    source_id = _sanitize_source_id(raw_source_id) if raw_source_id else ""
    if not source_id:
        source_id = _derive_source_id(game_name) if game_name else ""
    if not source_id:
        return None
    game_id = _sanitize_game_id(game_name) or source_id
    mapping = {
        key: str(entry.get(key) or "").strip()
        for key in _MAP_KEYS
    }
    return SourceConfig(
        source_id=source_id,
        game_id=game_id,
        game_name=game_name or source_id,
        url=str(entry.get("url") or "").strip(),
        adapter="generic_json",
        priority=_clamp_priority(entry.get("priority", 100)),
        timezone=str(entry.get("timezone") or "Asia/Shanghai").strip(),
        enabled=bool(entry.get("enabled", True)),
        mapping=mapping,
    )


def _build_preset_config(entry: dict[str, Any]) -> SourceConfig | None:
    game_id = str(entry.get("game") or "").strip()
    if game_id not in GAME_IDS:
        return None
    return SourceConfig(
        source_id=game_id,
        game_id=game_id,
        game_name=GAME_NAMES[game_id],
        url=str(entry.get("url") or "").strip() or DEFAULT_SOURCE_URLS[game_id],
        adapter="starrail_assistant_v1",
        priority=_clamp_priority(entry.get("priority", 100)),
        timezone=str(entry.get("timezone") or "Asia/Shanghai").strip(),
        enabled=bool(entry.get("enabled", True)),
    )


def normalize_sources(
    raw: Any,
    *,
    allow_insecure_http: bool = False,
) -> tuple[list[SourceConfig], list[str]]:
    """Normalize the ``sources`` configuration into source configs.

    Args:
        raw: Raw ``sources`` config value. A missing, non-list or empty value
            enables the six built-in sources.
        allow_insecure_http: Whether plain HTTP sources are allowed.

    Returns:
        ``(sources, warnings)``; invalid entries are skipped or disabled with a
        recorded reason so one bad source never breaks the plugin.
    """
    warnings: list[str] = []
    if not isinstance(raw, list) or not raw:
        return _default_sources(), warnings

    sources: list[SourceConfig] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            warnings.append(f"source[{index}]:not_object")
            continue
        adapter_name = str(entry.get("adapter") or "starrail_assistant_v1").strip()
        if adapter_name == "starrail_assistant_v1":
            cfg = _build_preset_config(entry)
            if cfg is None:
                warnings.append(f"source[{index}]:unknown_game")
                continue
        elif adapter_name == "generic_json":
            cfg = _build_generic_config(entry)
            if cfg is None:
                warnings.append(f"source[{index}]:missing_source_id")
                continue
        else:
            warnings.append(f"source[{index}]:unknown_adapter:{adapter_name}")
            continue
        if cfg.source_id in seen:
            warnings.append(f"source[{index}]:duplicate_source:{cfg.source_id}")
            continue
        seen.add(cfg.source_id)

        try:
            load_zone(cfg.timezone)
        except ValueError:
            cfg.enabled = False
            cfg.disabled_reason = "unknown_timezone"
            warnings.append(f"source[{index}]:unknown_timezone:{cfg.timezone}")
        if not cfg.url:
            cfg.enabled = False
            cfg.disabled_reason = "missing_url"
        else:
            try:
                validate_source_url(cfg.url, allow_insecure_http)
            except FetchError as exc:
                cfg.enabled = False
                cfg.disabled_reason = exc.message
                warnings.append(f"source[{index}]:{exc.message}")
        adapter = ADAPTERS.get(cfg.adapter)
        if adapter is None:
            cfg.enabled = False
            cfg.disabled_reason = "unknown_adapter"
            warnings.append(f"source[{index}]:unknown_adapter:{cfg.adapter}")
            continue
        errors = adapter.validate(cfg)
        if errors:
            cfg.enabled = False
            cfg.disabled_reason = errors[0]
            warnings.append(f"source[{index}]:{errors[0]}")
        sources.append(cfg)
    return sources, warnings
