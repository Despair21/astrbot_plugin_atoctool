"""Dashboard Web API registration and input validation."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .models import GAME_ID_PATTERN, SCHEMA_VERSION, STATUSES

_GAME_ID_RE = re.compile(GAME_ID_PATTERN)

# Injected by ``register_web_apis`` so this module stays importable (and unit
# testable) without booting the whole AstrBot core.
logger: Any = None
request: Any = None
json_response: Any = None
error_response: Any = None


def _error(marker: str, message: str, status_code: int, **extra: Any):
    data = {"schema_version": SCHEMA_VERSION}
    data.update(extra)
    return error_response(f"{marker}: {message}", status_code=status_code, data=data)


def _unauthorized():
    return _error("unauthorized", "未登录或会话已失效", 401)


def _internal_error(exc: Exception):
    if logger is not None:
        logger.exception("Game activity Web API failed")
    return _error("internal_error", type(exc).__name__, 500)


def parse_limit(value: Any, default: int, low: int, high: int) -> int | None:
    """Parse a numeric query parameter; return None when it is not an integer."""
    if value is None or value == "":
        return default
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return max(low, min(high, number))


def parse_game(value: Any) -> str | None:
    """Validate an optional game id; return None when absent, raise ValueError."""
    if value is None or value == "":
        return None
    game = str(value).strip()
    if not _GAME_ID_RE.match(game):
        raise ValueError("game")
    return game


def register_web_apis(
    context,
    plugin_name: str,
    service,
    *,
    web: tuple[Any, Any, Any, Any] | None = None,
) -> None:
    """Register the seven plugin dashboard endpoints on the AstrBot context.

    Args:
        context: AstrBot plugin context exposing ``register_web_api``.
        plugin_name: Plugin package name used as the route prefix.
        service: ``GameActivityService`` instance.
        web: Optional ``(logger, error_response, json_response, request)``
            injection used by tests; production resolves the AstrBot facades.
    """
    global logger, request, json_response, error_response
    if web is not None:
        logger, error_response, json_response, request = web
    else:
        from astrbot.api import logger as astrbot_logger
        from astrbot.api.web import error_response as astrbot_error_response
        from astrbot.api.web import json_response as astrbot_json_response
        from astrbot.api.web import request as astrbot_request

        logger = astrbot_logger
        error_response = astrbot_error_response
        json_response = astrbot_json_response
        request = astrbot_request
    base = f"/{plugin_name}"

    async def health_handler():
        if not request.username:
            return _unauthorized()
        try:
            return json_response(service.health_dto())
        except Exception as exc:  # noqa: BLE001 - never leak internals
            return _internal_error(exc)

    async def overview_handler():
        if not request.username:
            return _unauthorized()
        try:
            return json_response(service.overview_dto())
        except Exception as exc:  # noqa: BLE001
            return _internal_error(exc)

    async def games_handler():
        if not request.username:
            return _unauthorized()
        try:
            return json_response(
                {
                    "schema_version": SCHEMA_VERSION,
                    "games": service.games_dto(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            return _internal_error(exc)

    async def sources_handler():
        if not request.username:
            return _unauthorized()
        try:
            return json_response(
                {
                    "schema_version": SCHEMA_VERSION,
                    "sources": service.sources_dto(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            return _internal_error(exc)

    async def activities_handler():
        if not request.username:
            return _unauthorized()
        try:
            game = parse_game(request.query.get("game"))
            known = {source.game_id for source in service.sources}
            if game and known and game not in known:
                return _error("unknown_game", game, 404)
            status = request.query.get("status")
            if status is not None and status not in STATUSES:
                return _error("invalid_parameter", "status", 400)
            limit = parse_limit(request.query.get("limit"), 50, 1, 200)
            if limit is None:
                return _error("invalid_parameter", "limit", 400)
            query = str(request.query.get("q") or "").strip()[:64]
            total, activities = service.activities_dto(
                game=game,
                status=status,
                query=query,
                limit=limit,
            )
            return json_response(
                {
                    "schema_version": SCHEMA_VERSION,
                    "total": total,
                    "activities": activities,
                },
            )
        except ValueError as exc:
            return _error("invalid_parameter", str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            return _internal_error(exc)

    async def reminders_handler():
        if not request.username:
            return _unauthorized()
        try:
            limit = parse_limit(request.query.get("limit"), 50, 1, 100)
            if limit is None:
                return _error("invalid_parameter", "limit", 400)
            return json_response(service.reminders_dto(limit))
        except Exception as exc:  # noqa: BLE001
            return _internal_error(exc)

    async def sync_handler():
        if not request.username:
            return _unauthorized()
        try:
            body = await request.json(default={})
            if not isinstance(body, dict):
                return _error("invalid_parameter", "body", 400)
            game = parse_game(body.get("game"))
            known = {source.game_id for source in service.sources}
            if game and known and game not in known:
                return _error("unknown_game", game, 404)
            started, retry_after = await service.start_manual_sync(game)
            if started:
                return json_response(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "started": True,
                        "game": game,
                        "requested_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            if retry_after:
                return _error(
                    "sync_rate_limited",
                    f"retry_after_seconds={retry_after}",
                    429,
                    retry_after_seconds=retry_after,
                )
            return _error("sync_already_running", f"game={game or 'all'}", 409)
        except ValueError as exc:
            return _error("invalid_parameter", str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            return _internal_error(exc)

    context.register_web_api(
        f"{base}/health",
        health_handler,
        ["GET"],
        "游戏活动插件健康检查",
    )
    context.register_web_api(
        f"{base}/overview",
        overview_handler,
        ["GET"],
        "游戏活动插件总览",
    )
    context.register_web_api(
        f"{base}/games",
        games_handler,
        ["GET"],
        "游戏活动插件游戏列表",
    )
    context.register_web_api(
        f"{base}/activities",
        activities_handler,
        ["GET"],
        "游戏活动列表",
    )
    context.register_web_api(
        f"{base}/sources",
        sources_handler,
        ["GET"],
        "活动数据源状态",
    )
    context.register_web_api(
        f"{base}/sync",
        sync_handler,
        ["POST"],
        "手动触发活动同步",
    )
    context.register_web_api(
        f"{base}/reminders",
        reminders_handler,
        ["GET"],
        "提醒任务状态",
    )
