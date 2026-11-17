"""Orchestration of configuration, sync, queries and reminder delivery."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from .adapters import ADAPTERS, normalize_sources
from .diff import apply_source_items, derive_status
from .fetch import FetchError, ValidatedResolver, fetch_with_retry
from .merge import merge_activities
from .models import (
    DTO_FIELDS,
    GAME_NAMES,
    SCHEMA_VERSION,
    SourceConfig,
    load_zone,
)
from .reminders import ReminderEngine, parse_remind_days
from .store import Store

SYNC_INTERVAL_MIN = 5
SYNC_INTERVAL_MAX = 1440
SEND_LIMIT_MAX = 20
MANUAL_RATE_LIMIT_SECONDS = 30


def _as_bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _as_text(value: Any, default: str) -> str:
    text = str(value).strip() if isinstance(value, (str, int)) else ""
    return text or default


def normalize_config(raw: Any) -> dict[str, Any]:
    """Normalize the plugin configuration with defensive range clamping."""
    config = dict(raw) if isinstance(raw, dict) else {}
    general = config.get("general") if isinstance(config.get("general"), dict) else {}
    reminder = (
        config.get("reminder") if isinstance(config.get("reminder"), dict) else {}
    )
    view = config.get("view") if isinstance(config.get("view"), dict) else {}
    advanced = (
        config.get("advanced") if isinstance(config.get("advanced"), dict) else {}
    )
    remind_days = parse_remind_days(reminder.get("remind_days_before", "3,1"))
    if not remind_days:
        remind_days = [1]
    targets = reminder.get("target_umos")
    target_umos = [
        str(umo).strip() for umo in (targets if isinstance(targets, list) else [])
    ]
    return {
        "enabled": _as_bool(general.get("enabled"), True),
        "sync_interval_minutes": _as_int(
            general.get("sync_interval_minutes"),
            30,
            SYNC_INTERVAL_MIN,
            SYNC_INTERVAL_MAX,
        ),
        "request_timeout_seconds": _as_int(
            general.get("request_timeout_seconds"),
            20,
            5,
            60,
        ),
        "max_response_kb": _as_int(general.get("max_response_kb"), 4096, 256, 16384),
        "use_t2i": _as_bool(general.get("use_t2i"), False),
        "remind_enabled": _as_bool(reminder.get("enabled"), True),
        "remind_days": remind_days,
        "remind_time": _as_text(reminder.get("remind_time"), "10:00"),
        "max_grace_hours": _as_int(reminder.get("max_grace_hours"), 48, 0, 168),
        "target_umos": [umo for umo in target_umos if umo],
        "max_sends_per_tick": _as_int(reminder.get("max_sends_per_tick"), 5, 1, 20),
        "default_game": _as_text(view.get("default_game"), ""),
        "poll_seconds": _as_int(view.get("poll_seconds"), 30, 10, 120),
        "show_covers": _as_bool(view.get("show_covers"), True),
        "show_timezone": _as_bool(view.get("show_timezone"), False),
        "retention_days": _as_int(advanced.get("retention_days"), 7, 1, 90),
        "allow_insecure_http": _as_bool(advanced.get("allow_insecure_http"), False),
        "http_user_agent": _as_text(
            advanced.get("http_user_agent"),
            "Mozilla/5.0 (compatible; astrbot-plugin-game-activity/0.1.0)",
        ),
    }


class GameActivityService:
    """Single service instance owning state, sync and delivery for the plugin."""

    def __init__(
        self,
        *,
        config: Any,
        data_root: Path,
        logger,
        context: Any = None,
        clock=time.time,
    ) -> None:
        self.logger = logger
        self.context = context
        self.clock = clock
        self.store = Store(data_root, logger)
        self.config = normalize_config(config)
        self.sources, self.config_warnings = normalize_sources(
            (dict(config) if isinstance(config, dict) else {}).get("sources"),
            allow_insecure_http=self.config["allow_insecure_http"],
        )
        self.reminder_engine = ReminderEngine(self.store, logger)
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.source_states: dict[str, dict[str, Any]] = {}
        self.subscriptions: dict[str, list[str]] = {}
        self.sync_status = "idle"
        self.last_sync_at = 0
        self.sync_error = ""
        self.started = False
        self._data_dir_writable = True
        self._started_at = 0
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._sync_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._sync_task: asyncio.Task | None = None
        self._transient_tasks: set[asyncio.Task] = set()
        self._last_manual_monotonic = 0.0
        self._session: aiohttp.ClientSession | None = None
        self._resolver: ValidatedResolver | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Load persisted state, create the HTTP session and start ticks."""
        if self.started:
            return
        self.started = True
        self._stop.clear()
        self.store.ensure_dirs()
        self._data_dir_writable = self.store.probe_writable()
        self.store.cleanup_temp_files()
        for warning in self.config_warnings:
            self.logger.warning("Source config: %s", warning)
        for game_id in {source.game_id for source in self.sources}:
            self.snapshots[game_id] = self.store.load_snapshot(game_id)
        cache_state = self.store.load_state(
            "http_cache",
            {"schema_version": SCHEMA_VERSION, "sources": {}},
        )
        stored_sources = cache_state.get("sources")
        if isinstance(stored_sources, dict):
            self.source_states = {
                str(key): value
                for key, value in stored_sources.items()
                if isinstance(value, dict)
            }
        subscription_state = self.store.load_state(
            "subscriptions",
            {"schema_version": SCHEMA_VERSION, "by_game": {}},
        )
        by_game = subscription_state.get("by_game")
        if isinstance(by_game, dict):
            self.subscriptions = {
                str(game): [str(umo) for umo in umos if isinstance(umo, str)]
                for game, umos in by_game.items()
                if isinstance(umos, list)
            }
        self._resolver = ValidatedResolver()
        timeout = aiohttp.ClientTimeout(total=self.config["request_timeout_seconds"])
        connector = aiohttp.TCPConnector(limit=4, resolver=self._resolver)
        self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        self._started_at = int(self.clock())
        self._tasks = [
            asyncio.create_task(self._sync_loop()),
            asyncio.create_task(self._reminder_loop()),
        ]
        self._tasks.append(asyncio.create_task(self._initial_sync()))

    async def _initial_sync(self) -> None:
        await asyncio.sleep(0)
        await self.sync(manual=False)

    async def stop(self) -> None:
        """Cancel background work and close the HTTP session."""
        if not self.started:
            return
        self.started = False
        self._stop.set()
        tasks = list(self._tasks) + list(self._transient_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._transient_tasks.clear()
        self._sync_task = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._resolver = None
        await self._save_http_cache()
        await self._save_subscriptions()

    async def _sync_loop(self) -> None:
        interval = self.config["sync_interval_minutes"] * 60
        try:
            while not self._stop.is_set():
                await self._wait(interval)
                if self._stop.is_set():
                    break
                if not self.config["enabled"]:
                    continue
                try:
                    await self.sync(manual=False)
                except Exception:  # noqa: BLE001 - keep the loop alive
                    self.logger.exception("Scheduled sync failed")
        except asyncio.CancelledError:
            raise

    async def _reminder_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self.run_reminder_tick()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - keep the loop alive
                    self.logger.exception("Reminder tick failed")
                await self._wait(60)
        except asyncio.CancelledError:
            raise

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # -- sync --------------------------------------------------------------

    def _enabled_sources(self, game: str | None = None) -> list[SourceConfig]:
        return [
            source
            for source in self.sources
            if source.enabled and (game is None or source.game_id == game)
        ]

    async def sync(
        self,
        game: str | None = None,
        *,
        manual: bool = False,
    ) -> dict[str, Any]:
        """Sync one game (or all games) and persist changed snapshots."""
        async with self._sync_lock:
            self.sync_status = "running"
            self.sync_error = ""
            targets = self._enabled_sources(game)
            succeeded = 0
            failed = 0
            if self._session is None:
                self.sync_status = "error"
                self.sync_error = "service_not_started"
                return {"sources": 0, "succeeded": 0, "failed": 0}
            for source in targets:
                try:
                    state = await self._sync_source(source, force=manual)
                except Exception:  # noqa: BLE001 - isolate one bad source
                    self.logger.exception("Source sync crashed: %s", source.source_id)
                    failed += 1
                    continue
                if state.get("status") == "ok":
                    succeeded += 1
                else:
                    failed += 1
                    if not self.sync_error:
                        self.sync_error = str(state.get("last_error") or "source_error")
            self.last_sync_at = int(self.clock())
            self.sync_status = "error" if targets and failed == len(targets) else "idle"
            await self._save_http_cache()
            return {
                "sources": len(targets),
                "succeeded": succeeded,
                "failed": failed,
            }

    async def _sync_source(
        self, source: SourceConfig, *, force: bool
    ) -> dict[str, Any]:
        now = int(self.clock())
        state = self.source_states.setdefault(
            source.source_id,
            {
                "status": "stale",
                "last_checked": 0,
                "last_success": 0,
                "http_status": 0,
                "consecutive_failures": 0,
                "last_error": "",
                "next_retry_at": 0,
                "activity_count": 0,
                "etag": "",
                "last_modified": "",
                "version": "",
                "version_name": "",
            },
        )
        state["game_id"] = source.game_id
        state["url"] = source.url
        state["adapter"] = source.adapter
        state["priority"] = source.priority
        state["enabled"] = source.enabled
        state["timezone"] = source.timezone
        if not force and int(state.get("next_retry_at") or 0) > now:
            state["last_checked"] = int(state.get("last_checked") or 0)
            return state

        cache = {
            "etag": str(state.get("etag") or ""),
            "last_modified": str(state.get("last_modified") or ""),
        }
        try:
            result = await fetch_with_retry(
                self._session,
                source,
                cache,
                advanced=self.config,
                resolver=self._resolver,
            )
        except FetchError as exc:
            failures = int(state.get("consecutive_failures") or 0) + 1
            state.update(
                status="error" if exc.kind != "blocked" else "error",
                last_checked=now,
                consecutive_failures=failures,
                last_error=exc.message[:300],
                http_status=int(exc.http_status or 0),
            )
            if exc.kind == "transient":
                state["next_retry_at"] = now + min(60 * (2**failures), 900)
            else:
                state["next_retry_at"] = 0
            self.logger.warning(
                "Source %s failed (%s): %s",
                source.source_id,
                exc.kind,
                exc.message,
            )
            return state

        state["last_checked"] = now
        state["http_status"] = result.http_status
        state["next_retry_at"] = 0
        if result.not_modified:
            state.update(
                status="ok",
                last_success=now,
                consecutive_failures=0,
                last_error="",
            )
            return state
        payload = result.payload or {}
        adapter = ADAPTERS.get(source.adapter)
        if adapter is None:
            state.update(status="error", last_error="unknown_adapter")
            return state
        try:
            items = adapter.parse(payload, source)
        except ValueError as exc:
            state.update(
                status="error",
                consecutive_failures=int(state.get("consecutive_failures") or 0) + 1,
                last_error=str(exc)[:300],
            )
            return state
        raw_count = adapter.count_raw(payload, source)
        invalid = max(0, raw_count - len(items))
        version, version_name = adapter.version_info(payload, source)
        snapshot = self.snapshots.get(source.game_id) or {}
        old_activities = [
            item
            for item in snapshot.get("activities", [])
            if isinstance(item, dict) and item.get("source_id") == source.source_id
        ]
        new_snapshot, stats = apply_source_items(
            snapshot,
            source,
            items,
            now=now,
            retention_days=self.config["retention_days"],
            remind_days=self.config["remind_days"],
            invalid_items=invalid,
        )
        if stats.get("skipped_empty"):
            state.update(
                status="stale",
                last_success=int(state.get("last_success") or 0),
                consecutive_failures=0,
                last_error="empty_upstream",
                etag=result.etag or state.get("etag", ""),
                last_modified=result.last_modified or state.get("last_modified", ""),
                activity_count=len(old_activities),
                version=version,
                version_name=version_name,
            )
            self.logger.warning(
                "Source %s returned no activities; keeping previous snapshot",
                source.source_id,
            )
            return state
        self.snapshots[source.game_id] = new_snapshot
        try:
            await asyncio.to_thread(
                self.store.save_snapshot,
                source.game_id,
                new_snapshot,
            )
        except OSError as exc:
            self._data_dir_writable = False
            self.logger.warning(
                "Failed to persist snapshot for %s: %s",
                source.game_id,
                exc,
            )
        state.update(
            status="ok",
            last_success=now,
            consecutive_failures=0,
            last_error="",
            etag=result.etag or state.get("etag", ""),
            last_modified=result.last_modified or state.get("last_modified", ""),
            activity_count=sum(
                1
                for item in new_snapshot.get("activities", [])
                if item.get("source_id") == source.source_id
                and not item.get("removed_at")
            ),
            version=version,
            version_name=version_name,
        )
        self.logger.info(
            "Source %s synced: added=%s changed=%s renamed=%s removed=%s invalid=%s",
            source.source_id,
            stats["added"],
            stats["changed"],
            stats["renamed"],
            stats["removed"],
            stats["invalid_items"],
        )
        return state

    async def start_manual_sync(
        self,
        game: str | None = None,
    ) -> tuple[bool, int]:
        """Trigger a background manual sync, enforcing single-flight and rate limit.

        Returns:
            ``(started, retry_after_seconds)``.
        """
        if self.sync_status == "running":
            return (False, 0)
        if self._sync_task is not None and not self._sync_task.done():
            return (False, 0)
        now = time.monotonic()
        if now - self._last_manual_monotonic < MANUAL_RATE_LIMIT_SECONDS:
            retry_after = int(
                MANUAL_RATE_LIMIT_SECONDS - (now - self._last_manual_monotonic),
            )
            return (False, max(1, retry_after))
        self._last_manual_monotonic = now
        self.sync_status = "running"
        task = asyncio.create_task(self.sync(game=game, manual=True))
        self._sync_task = task
        self._transient_tasks.add(task)
        task.add_done_callback(self._transient_tasks.discard)
        return (True, 0)

    async def _save_http_cache(self) -> None:
        async with self._state_lock:
            try:
                await asyncio.to_thread(
                    self.store.save_state,
                    "http_cache",
                    {
                        "schema_version": SCHEMA_VERSION,
                        "sources": self.source_states,
                    },
                )
            except OSError as exc:
                self._data_dir_writable = False
                self.logger.warning("Failed to persist http cache: %s", exc)

    async def _save_subscriptions(self) -> None:
        async with self._state_lock:
            try:
                await asyncio.to_thread(
                    self.store.save_state,
                    "subscriptions",
                    {
                        "schema_version": SCHEMA_VERSION,
                        "by_game": self.subscriptions,
                    },
                )
            except OSError as exc:
                self._data_dir_writable = False
                self.logger.warning("Failed to persist subscriptions: %s", exc)

    # -- reminders ---------------------------------------------------------

    async def run_reminder_tick(self) -> dict[str, int]:
        """Run one reminder tick using the configured delivery callback."""
        now = int(self.clock())
        timezones = {source.source_id: source.timezone for source in self.sources}
        config = {
            "enabled": self.config["remind_enabled"],
            "remind_days": self.config["remind_days"],
            "remind_time": self.config["remind_time"],
            "max_grace_hours": self.config["max_grace_hours"],
            "max_sends_per_tick": self.config["max_sends_per_tick"],
            "target_umos": self.config["target_umos"],
        }
        stats = await self.reminder_engine.tick(
            now=now,
            activities=self.all_activities(),
            subscriptions=self.subscriptions,
            config=config,
            send_message=self._send_message,
            timezones=timezones,
        )
        if any(stats.values()):
            self.logger.info(
                "Reminders: created=%s sent=%s skipped=%s failed=%s pending=%s",
                stats["created"],
                stats["sent"],
                stats["skipped"],
                stats["failed"],
                stats["pending"],
            )
        return stats

    async def _send_message(self, umo: str, text: str) -> bool:
        if self.context is None:
            return False
        if ":GroupMessage:" in umo:
            platform = self.context.get_platform_inst(umo.split(":", 1)[0])
            if platform is not None and platform.meta().name in {
                "qq_official",
                "qq_official_webhook",
            }:
                self.logger.warning(
                    "QQ official group proactive send to %s depends on AstrBot's "
                    "in-process group scene cache; a cold group may be silently "
                    "skipped by the core. Warm the group with any group message or "
                    "/活动订阅 first; 'sent' is not a platform delivery receipt.",
                    umo,
                )
        from astrbot.api.event import MessageChain

        return await self.context.send_message(umo, MessageChain().message(text))

    def all_activities(self) -> list[dict[str, Any]]:
        """Return every persisted activity across games."""
        result: list[dict[str, Any]] = []
        for snapshot in self.snapshots.values():
            activities = snapshot.get("activities")
            if isinstance(activities, list):
                result.extend(item for item in activities if isinstance(item, dict))
        return result

    def _priorities(self) -> dict[str, int]:
        return {source.source_id: source.priority for source in self.sources}

    def known_games(self) -> set[str]:
        """Return the game ids this service can answer for."""
        return {source.game_id for source in self.sources} | set(self.snapshots)

    def merged_activities(self) -> list[dict[str, Any]]:
        """Return merged DTO dictionaries for display and API responses."""
        return merge_activities(
            self.all_activities(),
            self._priorities(),
            int(self.clock()),
            self.config["remind_days"],
        )

    # -- queries -----------------------------------------------------------

    def activities_dto(
        self,
        *,
        game: str | None = None,
        status: str | None = None,
        query: str = "",
        limit: int = 50,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Filter merged activities for the API."""
        merged = self.merged_activities()
        needle = query.strip().casefold()
        filtered = []
        for activity in merged:
            if game and activity.get("game_id") != game:
                continue
            if status and activity.get("status") != status:
                continue
            if needle:
                haystack = (
                    f"{activity.get('name') or ''} {activity.get('description') or ''}"
                ).casefold()
                if needle not in haystack:
                    continue
            filtered.append(activity)
        total = len(filtered)
        sliced = filtered[: max(1, min(200, limit))]
        return total, [
            {field: activity.get(field) for field in DTO_FIELDS} for activity in sliced
        ]

    def games_dto(self) -> list[dict[str, Any]]:
        """Return per-game summary rows."""
        now = int(self.clock())
        game_ids = list(
            dict.fromkeys(
                [source.game_id for source in self.sources]
                + list(self.snapshots.keys()),
            ),
        )
        result = []
        for game_id in game_ids:
            activities = [
                item
                for item in self.all_activities()
                if item.get("game_id") == game_id and not item.get("removed_at")
            ]
            active = sum(
                1
                for item in activities
                if derive_status(item, now, self.config["remind_days"])
                in {"active", "ending_soon"}
            )
            snapshot = self.snapshots.get(game_id) or {}
            result.append(
                {
                    "game_id": game_id,
                    "name": GAME_NAMES.get(game_id)
                    or str(snapshot.get("game_name") or game_id),
                    "enabled_sources": sum(
                        1
                        for source in self.sources
                        if source.game_id == game_id and source.enabled
                    ),
                    "activity_count": len(activities),
                    "active_count": active,
                    "last_sync_at": int(snapshot.get("last_sync_at") or 0),
                },
            )
        return result

    def sources_dto(self) -> list[dict[str, Any]]:
        """Return configured source rows merged with their runtime state."""
        rows = []
        for source in self.sources:
            state = self.source_states.get(source.source_id, {})
            rows.append(
                {
                    "source_id": source.source_id,
                    "game_id": source.game_id,
                    "url": source.url,
                    "adapter": source.adapter,
                    "priority": source.priority,
                    "enabled": source.enabled,
                    "status": "disabled"
                    if not source.enabled
                    else str(
                        state.get("status") or "stale",
                    ),
                    "last_checked": int(state.get("last_checked") or 0),
                    "last_success": int(state.get("last_success") or 0),
                    "http_status": int(state.get("http_status") or 0),
                    "etag_present": bool(state.get("etag")),
                    "last_modified_present": bool(state.get("last_modified")),
                    "consecutive_failures": int(
                        state.get("consecutive_failures") or 0,
                    ),
                    "last_error": str(state.get("last_error") or "")[:300],
                    "activity_count": int(state.get("activity_count") or 0),
                    "next_retry_at": int(state.get("next_retry_at") or 0),
                },
            )
        return rows

    def overview_dto(self) -> dict[str, Any]:
        """Return the dashboard overview payload."""
        now = int(self.clock())
        activities = self.all_activities()
        statuses = [
            derive_status(item, now, self.config["remind_days"]) for item in activities
        ]
        counts = {
            "activities": sum(1 for status in statuses if status != "removed"),
            "active": statuses.count("active"),
            "ending_soon": statuses.count("ending_soon"),
            "upcoming": statuses.count("upcoming"),
            "ended": statuses.count("ended"),
            "removed": statuses.count("removed"),
            "games": len(self.games_dto()),
            "sources": sum(1 for source in self.sources if source.enabled),
            "reminders_pending": self._pending_reminders(),
        }
        source_health = {
            "ok": 0,
            "stale": 0,
            "error": 0,
            "disabled": 0,
            "total": len(self.sources),
        }
        for source in self.sources:
            if not source.enabled:
                source_health["disabled"] += 1
                continue
            status = str(
                self.source_states.get(source.source_id, {}).get("status") or "stale",
            )
            if status in {"ok", "stale", "error"}:
                source_health[status] += 1
            else:
                source_health["stale"] += 1
        removed_24h = sum(
            1
            for item in activities
            if item.get("removed_at")
            and now - int(item.get("removed_at") or 0) <= 86400
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "counts": counts,
            "sync_status": self.sync_status,
            "last_sync_at": self.last_sync_at,
            "sync_error": self.sync_error or None,
            "source_health": source_health,
            "removed_24h": removed_24h,
            "next_reminder": self._next_reminder(),
            "poll_seconds": self.config["poll_seconds"],
            "show_timezone": self.config["show_timezone"],
        }

    def _pending_reminders(self) -> int:
        state = self.store.load_state("reminders", {"items": {}})
        items = state.get("items")
        if not isinstance(items, dict):
            return 0
        return sum(1 for job in items.values() if job.get("status") == "pending")

    def _next_reminder(self) -> dict[str, Any] | None:
        state = self.store.load_state("reminders", {"items": {}})
        items = state.get("items")
        if not isinstance(items, dict):
            return None
        pending = [
            job
            for job in items.values()
            if job.get("status") == "pending" and job.get("fire_at_epoch")
        ]
        if not pending:
            return None
        job = min(pending, key=lambda entry: int(entry.get("fire_at_epoch") or 0))
        activity = next(
            (
                item
                for item in self.all_activities()
                if item.get("key") == job.get("activity_key")
            ),
            {},
        )
        return {
            "activity_name": str(activity.get("name") or ""),
            "game_id": str(job.get("game_id") or ""),
            "fire_at_iso": self._epoch_to_iso(
                int(job.get("fire_at_epoch") or 0),
                str(activity.get("source_id") or ""),
            ),
            "threshold_days": int(job.get("threshold_days") or 0),
        }

    def reminders_dto(self, limit: int = 50) -> dict[str, Any]:
        """Return reminder jobs for the dashboard."""
        state = self.store.load_state("reminders", {"items": {}})
        items = state.get("items")
        jobs = []
        if isinstance(items, dict):
            activity_names = {
                str(item.get("key") or ""): str(item.get("name") or "")
                for item in self.all_activities()
            }
            ordered = sorted(
                items.values(),
                key=lambda job: int(job.get("fire_at_epoch") or 0),
            )
            for job in ordered[: max(1, min(100, limit))]:
                jobs.append(
                    {
                        "key": str(job.get("key") or ""),
                        "game_id": str(job.get("game_id") or ""),
                        "source_id": str(job.get("source_id") or ""),
                        "raw_id": str(job.get("raw_id") or ""),
                        "activity_name": activity_names.get(
                            str(job.get("activity_key") or ""),
                            "",
                        ),
                        "threshold_days": int(job.get("threshold_days") or 0),
                        "fire_at_iso": self._epoch_to_iso(
                            int(job.get("fire_at_epoch") or 0),
                            str(job.get("source_id") or ""),
                        ),
                        "status": str(job.get("status") or "pending"),
                        "attempts": int(job.get("attempts") or 0),
                        "sent_at": job.get("sent_at"),
                        "last_error": str(job.get("last_error") or ""),
                    },
                )
        subscription_count = len(
            {umo for umos in self.subscriptions.values() for umo in umos},
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "jobs": jobs,
            "subscriptions": {"count": subscription_count},
        }

    def health_dto(self) -> dict[str, Any]:
        """Return plugin health information."""
        enabled = [source for source in self.sources if source.enabled]
        degraded = not self._data_dir_writable
        if enabled:
            all_error = all(
                self.source_states.get(source.source_id, {}).get("status") == "error"
                for source in enabled
            )
            if all_error and self.last_sync_at:
                degraded = True
            interval = self.config["sync_interval_minutes"] * 60
            if self.last_sync_at and self.clock() - self.last_sync_at > interval * 3:
                degraded = True
        if self.sync_status == "error":
            degraded = True
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "degraded" if degraded else "ok",
            "version": "0.1.0",
            "uptime_seconds": max(0, int(self.clock()) - self._started_at)
            if self._started_at
            else 0,
            "data_dir_writable": self._data_dir_writable,
            "sync_status": self.sync_status,
            "last_sync_at": self.last_sync_at,
        }

    # -- subscriptions -----------------------------------------------------

    def subscribe(self, umo: str, game_id: str) -> None:
        """Subscribe one session to reminders for a game."""
        subscribers = self.subscriptions.setdefault(game_id, [])
        if umo not in subscribers:
            subscribers.append(umo)

    def unsubscribe(self, umo: str, game_id: str | None = None) -> int:
        """Remove a session subscription; return the number of removed entries."""
        removed = 0
        for game, umos in self.subscriptions.items():
            if game_id and game != game_id:
                continue
            while umo in umos:
                umos.remove(umo)
                removed += 1
        self.subscriptions = {
            game: umos for game, umos in self.subscriptions.items() if umos
        }
        return removed

    async def persist_subscriptions(self) -> None:
        """Persist the current subscription map."""
        await self._save_subscriptions()

    def _epoch_to_iso(self, epoch: int, source_id: str) -> str:
        if epoch <= 0:
            return ""
        zone = timezone.utc
        for source in self.sources:
            if source.source_id == source_id:
                try:
                    zone = load_zone(source.timezone)
                except ValueError:
                    zone = timezone.utc
                break
        return datetime.fromtimestamp(epoch, zone).isoformat()
