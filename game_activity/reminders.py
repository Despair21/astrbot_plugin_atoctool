"""End-before reminder scheduling and per-target delivery state machine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Awaitable, Callable

from .diff import derive_status
from .models import GAME_NAMES, SCHEMA_VERSION, load_zone

SENDERS = Callable[[str, str], Awaitable[bool]]
MAX_TEXT_CHARS = 1800
MAX_LINES = 10
MAX_JOBS = 5000
TERMINAL_RETENTION_SECONDS = 30 * 86400
MAX_ATTEMPTS = 3
QQ_OFFICIAL_PREFIXES = ("qq_official:", "qq_official_webhook:")
TERMINAL_STATUSES = {"sent", "skipped", "failed"}


def parse_remind_time(value: Any) -> tuple[int, int]:
    """Parse ``HH:MM`` and fall back to 10:00 on invalid input."""
    if isinstance(value, str) and ":" in value:
        hour_text, minute_text = value.split(":", 1)
        try:
            hour = int(hour_text)
            minute = int(minute_text)
        except ValueError:
            return (10, 0)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return (hour, minute)
    return (10, 0)


def parse_remind_days(value: Any) -> list[int]:
    """Parse the ``remind_days_before`` config into sorted unique days."""
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        return []
    days: set[int] = set()
    for raw in raw_items:
        try:
            day = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 365:
            days.add(day)
    return sorted(days)


def compute_fire_at(
    end_iso: str,
    threshold_days: int,
    remind_time: str,
    end_epoch: int,
    tz: tzinfo | None = None,
) -> int:
    """Compute the reminder fire time for one activity and threshold.

    Args:
        end_iso: Offset-aware ISO 8601 activity end.
        threshold_days: Days before the end date.
        remind_time: ``HH:MM`` in the activity's timezone.
        end_epoch: Activity end epoch, used to avoid firing after the end.
        tz: Source timezone; defaults to the offset encoded in ``end_iso``.

    Returns:
        Epoch seconds, clamped to ``end_epoch - 60``.
    """
    parsed = datetime.fromisoformat(str(end_iso or ""))
    zone = tz or parsed.tzinfo
    if zone is None:
        raise ValueError("missing_timezone")
    hour, minute = parse_remind_time(remind_time)
    end_local = parsed.astimezone(zone)
    target_date = (end_local - timedelta(days=threshold_days)).date()
    fire_local = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        hour,
        minute,
        tzinfo=zone,
    )
    # Resolve a nonexistent local time (DST spring-forward) by moving forward.
    for _attempt in range(3):
        round_trip = fire_local.astimezone(timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) == fire_local.replace(tzinfo=None):
            break
        fire_local = fire_local + timedelta(hours=1)
    fire_at = int(fire_local.timestamp())
    if fire_at >= end_epoch:
        fire_at = max(0, int(end_epoch) - 60)
    return fire_at


def derive_job_status(job: dict[str, Any]) -> str:
    """Derive the job status from its per-target states (design §11.4)."""
    targets = job.get("targets") or {}
    statuses = [str(target.get("status") or "pending") for target in targets.values()]
    if any(status == "pending" for status in statuses):
        return "pending"
    if any(status == "sent" for status in statuses):
        return "sent"
    if any(status == "failed" for status in statuses):
        return "failed"
    return "skipped"


class ReminderEngine:
    """Persist and advance reminder jobs across ticks and restarts."""

    def __init__(self, store, logger) -> None:
        self.store = store
        self.logger = logger

    # -- state ------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        state = self.store.load_state(
            "reminders",
            {"schema_version": SCHEMA_VERSION, "items": {}},
        )
        if not isinstance(state.get("items"), dict):
            state["items"] = {}
        state["schema_version"] = SCHEMA_VERSION
        return state

    def _save(self, state: dict[str, Any], now: int) -> None:
        self._prune(state, now)
        try:
            self.store.save_state("reminders", state)
        except OSError as exc:
            self.logger.warning("Failed to persist reminders: %s", exc)

    def _prune(self, state: dict[str, Any], now: int) -> None:
        items: dict[str, Any] = state["items"]
        for key, job in list(items.items()):
            if job.get("status") in TERMINAL_STATUSES:
                fire_at = int(job.get("fire_at_epoch") or 0)
                if fire_at and now - fire_at > TERMINAL_RETENTION_SECONDS:
                    items.pop(key, None)
        if len(items) > MAX_JOBS:
            ordered = sorted(
                items.items(),
                key=lambda pair: int(pair[1].get("fire_at_epoch") or 0),
            )
            for key, job in ordered:
                if len(items) <= MAX_JOBS:
                    break
                if job.get("status") in TERMINAL_STATUSES:
                    items.pop(key, None)

    # -- tick -------------------------------------------------------------

    async def tick(
        self,
        *,
        now: int,
        activities: list[dict[str, Any]],
        subscriptions: dict[str, list[str]],
        config: dict[str, Any],
        send_message: SENDERS,
        timezones: dict[str, str] | None = None,
    ) -> dict[str, int]:
        """Advance one reminder tick.

        Args:
            now: Current epoch seconds (injected for deterministic tests).
            activities: All persisted activities across games.
            subscriptions: ``game_id -> [umo]`` from the subscription store.
            config: Reminder settings (``remind_days`` is already parsed).
            send_message: Async callable ``(umo, text) -> bool``.
            timezones: Optional ``source_id -> IANA timezone`` mapping.

        Returns:
            Counters describing what this tick did.
        """
        stats = {"created": 0, "sent": 0, "skipped": 0, "failed": 0, "pending": 0}
        if not config.get("enabled", True):
            return stats
        state = self._load()
        items: dict[str, Any] = state["items"]
        remind_days = list(config.get("remind_days") or [1])
        remind_time = str(config.get("remind_time") or "10:00")
        grace_seconds = max(0, int(config.get("max_grace_hours", 48))) * 3600
        targets_override = [
            str(umo) for umo in (config.get("target_umos") or []) if str(umo).strip()
        ]
        by_key = {
            str(activity.get("key") or ""): activity for activity in activities
        }

        stats["created"] += self._ensure_jobs(
            items,
            activities,
            subscriptions,
            targets_override,
            remind_days,
            remind_time,
            now,
            timezones or {},
        )
        self._close_finished_activities(items, by_key, now, remind_days, stats)
        await self._deliver_due(
            items,
            by_key,
            now,
            grace_seconds,
            int(config.get("max_sends_per_tick", 5)),
            send_message,
            stats,
        )
        for job in items.values():
            job["status"] = derive_job_status(job)
            self._refresh_job_summary(job, now)
        stats["pending"] = sum(
            1 for job in items.values() if job.get("status") == "pending"
        )
        self._save(state, now)
        return stats

    def _ensure_jobs(
        self,
        items: dict[str, Any],
        activities: list[dict[str, Any]],
        subscriptions: dict[str, list[str]],
        targets_override: list[str],
        remind_days: list[int],
        remind_time: str,
        now: int,
        timezones: dict[str, str],
    ) -> int:
        created = 0
        for activity in activities:
            activity_key = str(activity.get("key") or "")
            if not activity_key:
                continue
            if derive_status(activity, now, remind_days) in {"removed", "ended"}:
                continue
            source_id = str(activity.get("source_id") or "")
            try:
                source_zone = load_zone(
                    timezones.get(source_id) or "Asia/Shanghai",
                )
            except ValueError:
                self.logger.warning(
                    "Skipping reminder job %s: unknown timezone for %s",
                    activity_key,
                    source_id,
                )
                continue
            targets = self._targets_for(
                activity,
                subscriptions,
                targets_override,
            )
            for threshold in remind_days:
                key = f"{activity_key}#{threshold}"
                existing = items.get(key)
                if existing is not None:
                    if existing.get("status") == "sent":
                        continue
                    for umo in targets:
                        existing["targets"].setdefault(
                            umo,
                            self._new_target(),
                        )
                    continue
                try:
                    fire_at = compute_fire_at(
                        str(activity.get("end_iso") or ""),
                        threshold,
                        remind_time,
                        int(activity.get("end_epoch") or 0),
                        tz=source_zone,
                    )
                except ValueError as exc:
                    self.logger.warning(
                        "Skipping reminder job %s: %s",
                        key,
                        exc,
                    )
                    continue
                job = {
                    "key": key,
                    "game_id": str(activity.get("game_id") or ""),
                    "source_id": str(activity.get("source_id") or ""),
                    "raw_id": str(activity.get("raw_id") or ""),
                    "activity_key": activity_key,
                    "threshold_days": threshold,
                    "fire_at_epoch": fire_at,
                    "status": "pending",
                    "attempts": 0,
                    "last_error": "",
                    "sent_at": None,
                    "targets": {
                        umo: self._new_target() for umo in targets
                    },
                }
                if not targets:
                    job["status"] = "skipped"
                    job["last_error"] = "no_targets"
                items[key] = job
                created += 1
        return created

    @staticmethod
    def _new_target() -> dict[str, Any]:
        return {
            "status": "pending",
            "attempts": 0,
            "last_error": "",
            "next_attempt_at": 0,
            "sent_at": None,
        }

    @staticmethod
    def _targets_for(
        activity: dict[str, Any],
        subscriptions: dict[str, list[str]],
        targets_override: list[str],
    ) -> list[str]:
        game_id = str(activity.get("game_id") or "")
        combined = list(subscriptions.get(game_id) or []) + targets_override
        result: list[str] = []
        for umo in combined:
            text = str(umo).strip()
            if text and text not in result:
                result.append(text)
        return result

    def _close_finished_activities(
        self,
        items: dict[str, Any],
        by_key: dict[str, dict[str, Any]],
        now: int,
        remind_days: list[int],
        stats: dict[str, int],
    ) -> None:
        for job in items.values():
            if job.get("status") == "sent":
                continue
            activity = by_key.get(str(job.get("activity_key") or ""))
            finished = activity is None
            if activity is not None:
                finished = derive_status(activity, now, remind_days) in {
                    "removed",
                    "ended",
                }
            if not finished:
                continue
            for target in job.get("targets", {}).values():
                if target.get("status") == "pending":
                    target["status"] = "skipped"
                    target["last_error"] = "activity_ended"
                    stats["skipped"] += 1
            job["status"] = derive_job_status(job)

    async def _deliver_due(
        self,
        items: dict[str, Any],
        by_key: dict[str, dict[str, Any]],
        now: int,
        grace_seconds: int,
        max_sends: int,
        send_message: SENDERS,
        stats: dict[str, int],
    ) -> None:
        due: dict[str, list[dict[str, Any]]] = {}
        for job in items.values():
            if job.get("status") != "pending":
                continue
            fire_at = int(job.get("fire_at_epoch") or 0)
            if fire_at > now:
                continue
            if now - fire_at > grace_seconds:
                for target in job.get("targets", {}).values():
                    if target.get("status") == "pending":
                        target["status"] = "skipped"
                        target["last_error"] = "grace_expired"
                        stats["skipped"] += 1
                job["status"] = derive_job_status(job)
                continue
            for umo, target in job.get("targets", {}).items():
                if target.get("status") != "pending":
                    continue
                if int(target.get("next_attempt_at") or 0) > now:
                    continue
                due.setdefault(umo, []).append(job)

        sends_used = 0
        for umo in sorted(due):
            jobs = due[umo]
            if umo.startswith(QQ_OFFICIAL_PREFIXES):
                for job in jobs:
                    target = job["targets"].get(umo)
                    if target and target.get("status") == "pending":
                        target["status"] = "skipped"
                        target["last_error"] = "platform_unsupported"
                        stats["skipped"] += 1
                continue
            if sends_used >= max_sends:
                break
            sends_used += 1
            text = self._format_batch(jobs, by_key)
            try:
                delivered = await send_message(umo, text)
            except ValueError as exc:
                self._apply_failure(jobs, umo, now, "invalid_session", permanent=True)
                stats["failed"] += 1
                self.logger.warning("Reminder target invalid: %s", type(exc).__name__)
            except Exception as exc:  # noqa: BLE001 - classify all sender failures
                self._apply_failure(jobs, umo, now, str(exc)[:300], permanent=False)
                stats["failed"] += 1
            else:
                if delivered:
                    for job in jobs:
                        target = job["targets"].get(umo)
                        if target is not None:
                            target["status"] = "sent"
                            target["sent_at"] = now
                            target["next_attempt_at"] = 0
                            target["last_error"] = ""
                            stats["sent"] += 1
                else:
                    self._apply_failure(
                        jobs,
                        umo,
                        now,
                        "platform_not_found",
                        permanent=True,
                    )
                    stats["failed"] += 1

    @staticmethod
    def _apply_failure(
        jobs: list[dict[str, Any]],
        umo: str,
        now: int,
        error: str,
        *,
        permanent: bool,
    ) -> None:
        for job in jobs:
            target = job["targets"].get(umo)
            if target is None or target.get("status") != "pending":
                continue
            target["attempts"] = int(target.get("attempts") or 0) + 1
            target["last_error"] = error
            if permanent:
                target["status"] = "failed"
                target["next_attempt_at"] = 0
            elif target["attempts"] >= MAX_ATTEMPTS:
                target["status"] = "failed"
                target["last_error"] = f"transient_exhausted:{error}"
                target["next_attempt_at"] = 0
            else:
                backoff = min(60 * (2 ** target["attempts"]), 900)
                target["next_attempt_at"] = now + backoff

    @staticmethod
    def _refresh_job_summary(job: dict[str, Any], now: int) -> None:
        targets = job.get("targets") or {}
        attempts = [
            int(target.get("attempts") or 0) for target in targets.values()
        ]
        job["attempts"] = max(attempts, default=0)
        errors = [
            str(target.get("last_error") or "")
            for target in targets.values()
            if target.get("last_error")
        ]
        if errors:
            job["last_error"] = errors[-1]
        if job.get("status") == "sent" and not job.get("sent_at"):
            job["sent_at"] = now

    @staticmethod
    def _format_batch(
        jobs: list[dict[str, Any]],
        by_key: dict[str, dict[str, Any]],
    ) -> str:
        seen: dict[str, int] = {}
        lines: list[str] = []
        for job in jobs:
            activity = by_key.get(str(job.get("activity_key") or ""))
            if activity is None:
                continue
            activity_key = str(job.get("activity_key") or "")
            threshold = int(job.get("threshold_days") or 0)
            if activity_key in seen and seen[activity_key] <= threshold:
                continue
            seen[activity_key] = threshold
            game_name = GAME_NAMES.get(
                str(activity.get("game_id") or ""),
                str(activity.get("game_id") or ""),
            )
            end_text = str(activity.get("end_iso") or "").replace("T", " ")[:16]
            lines.append(
                f"⏰ {game_name} · {activity.get('name') or ''} "
                f"将于 {end_text} 结束（提前 {threshold} 天提醒）",
            )
        if not lines:
            return ""
        hidden = len(lines) - MAX_LINES
        body = "\n".join(lines[:MAX_LINES])
        if hidden > 0:
            body += f"\n另有 {hidden} 条提醒未显示，请使用 /活动 列表 查看。"
        text = "活动即将结束提醒：\n" + body
        return text[:MAX_TEXT_CHARS]
