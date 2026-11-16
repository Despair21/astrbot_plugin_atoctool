"""Pure text rendering and argument helpers for the /活动 command group."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import GAME_NAMES, STATUSES

MAX_TEXT_CHARS = 1800
STATUS_ORDER = {"ending_soon": 0, "active": 1, "upcoming": 2, "ended": 3, "removed": 4}
STATUS_ICONS = {
    "ending_soon": "⏰",
    "active": "▶",
    "upcoming": "🕒",
    "ended": "✅",
    "removed": "🗑",
}
STATUS_LABELS = {
    "ending_soon": "即将结束",
    "active": "进行中",
    "upcoming": "即将开始",
    "ended": "已结束",
    "removed": "已下架",
}
SOURCE_STATUS_LABELS = {
    "ok": "正常",
    "stale": "过期",
    "error": "错误",
    "disabled": "已禁用",
}


def truncate_text(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    """Truncate text to the platform-safe budget."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)] + "\n…（内容过长已截断）"


def short_text(text: Any, limit: int = 80) -> str:
    """Shorten one display field."""
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def format_iso_short(value: Any) -> str:
    """Render an offset ISO timestamp as ``MM-DD HH:MM``."""
    text = str(value or "")
    if len(text) >= 16:
        return text[5:16].replace("T", " ")
    return text or "—"


def format_epoch(value: Any) -> str:
    """Render Unix seconds as local ``MM-DD HH:MM`` (empty for 0)."""
    try:
        epoch = int(value)
    except (TypeError, ValueError):
        return "—"
    if epoch <= 0:
        return "—"
    try:
        return datetime.fromtimestamp(epoch).strftime("%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "—"


def _sort_key(activity: dict[str, Any]) -> tuple[int, int, str]:
    return (
        STATUS_ORDER.get(str(activity.get("status") or ""), 9),
        int(activity.get("end_epoch") or 0),
        str(activity.get("key") or ""),
    )


def _line(activity: dict[str, Any]) -> str:
    icon = STATUS_ICONS.get(str(activity.get("status") or ""), "•")
    label = STATUS_LABELS.get(str(activity.get("status") or ""), "")
    return (
        f"{icon} {short_text(activity.get('name'))} | "
        f"{format_iso_short(activity.get('end_iso'))} 结束 · {label}"
    )


def _grouped_lines(activities: list[dict[str, Any]], max_lines: int) -> list[str]:
    """Group activities by game, ordered by the earliest ending game."""
    by_game: dict[str, list[dict[str, Any]]] = {}
    for activity in activities:
        by_game.setdefault(str(activity.get("game_id") or ""), []).append(activity)
    game_order = sorted(
        by_game,
        key=lambda game: min(int(item.get("end_epoch") or 0) for item in by_game[game]),
    )
    lines: list[str] = []
    for game in game_order:
        if len(lines) >= max_lines:
            break
        lines.append(f"【{GAME_NAMES.get(game, game)}】")
        for activity in sorted(by_game[game], key=_sort_key):
            if len(lines) >= max_lines:
                break
            lines.append(_line(activity))
    return lines


def render_list(activities: list[dict[str, Any]], max_lines: int = 20) -> str:
    """Render the activity list grouped by game."""
    rows = [item for item in activities if item.get("status") != "removed"]
    rows.sort(key=_sort_key)
    if not rows:
        return "暂无活动数据。管理员可发送 /活动 同步 手动同步。"
    lines = _grouped_lines(rows, max_lines)
    if len(rows) > max_lines:
        lines.append(
            f"…共 {len(rows)} 条，完整列表请使用 /活动 列表 或 Dashboard 查看。"
        )
    return truncate_text("活动列表：\n" + "\n".join(lines))


def render_today(activities: list[dict[str, Any]], now: int) -> str:
    """Render activities that are running today or start within 24 hours."""
    rows = [
        item
        for item in activities
        if item.get("status") in {"active", "ending_soon"}
        or (
            item.get("status") == "upcoming"
            and int(item.get("start_epoch") or 0) - now <= 86400
        )
    ]
    rows.sort(key=_sort_key)
    if not rows:
        return "今天没有进行中的活动，也没有 24 小时内开始的活动。"
    lines = _grouped_lines(rows, 20)
    if len(rows) > 20:
        lines.append(f"…共 {len(rows)} 条。")
    return truncate_text("今日活动：\n" + "\n".join(lines))


def render_detail(activity: dict[str, Any] | None) -> str | None:
    """Render one activity detail block, or None when absent."""
    if not activity:
        return None
    game_id = str(activity.get("game_id") or "")
    lines = [
        f"【{GAME_NAMES.get(game_id, game_id)}】{short_text(activity.get('name'), 120)}",
        f"状态：{STATUS_LABELS.get(str(activity.get('status') or ''), activity.get('status'))}",
        f"时间：{format_iso_short(activity.get('start_iso'))} 至 "
        f"{format_iso_short(activity.get('end_iso'))}",
        f"来源：{activity.get('source_id') or '—'}",
    ]
    merged = activity.get("merged_sources")
    if isinstance(merged, list) and len(merged) > 1:
        lines.append(f"合并来源数：{len(merged)}")
    if activity.get("kind"):
        lines.append(f"类型：{activity['kind']}")
    description = str(activity.get("description") or "").strip()
    if description:
        lines.append(f"描述：{short_text(description, 300)}")
    lines.append(f"活动ID：{activity.get('key') or '—'}")
    return truncate_text("\n".join(lines))


def render_status(overview: dict[str, Any], sources: list[dict[str, Any]]) -> str:
    """Render sync and source health information."""
    health = overview.get("source_health") or {}
    sync_labels = {"idle": "空闲", "running": "同步中", "error": "同步失败"}
    lines = [
        "同步状态：" + sync_labels.get(str(overview.get("sync_status") or ""), "未知"),
        f"最后同步：{format_epoch(overview.get('last_sync_at'))}",
        "数据源健康："
        f"{health.get('ok', 0)} 正常 / {health.get('stale', 0)} 过期 / "
        f"{health.get('error', 0)} 错误 / {health.get('disabled', 0)} 禁用",
    ]
    if overview.get("sync_error"):
        lines.append(f"最近错误：{short_text(overview['sync_error'], 200)}")
    for source in sources:
        status = str(source.get("status") or "")
        line = (
            f"- {source.get('source_id')}：{SOURCE_STATUS_LABELS.get(status, status)}"
        )
        failures = int(source.get("consecutive_failures") or 0)
        if failures:
            line += f"，连续失败 {failures} 次"
        if source.get("last_error"):
            line += f"，错误：{short_text(source['last_error'], 120)}"
        lines.append(line)
    return truncate_text("\n".join(lines))


def render_sync_result(result: dict[str, Any]) -> str:
    """Render the outcome of a manual background sync."""
    return (
        "已触发同步："
        f"共 {result.get('sources', 0)} 个数据源，"
        f"成功 {result.get('succeeded', 0)} 个，失败 {result.get('failed', 0)} 个。"
        "可发送 /活动 状态 查看结果。"
    )


def render_subscriptions(
    subscriptions: dict[str, list[str]],
    umo: str,
) -> str:
    """Render the games the current session subscribed to."""
    games = [game for game, umos in subscriptions.items() if umo in umos]
    if not games:
        return "当前会话尚未订阅任何游戏。发送 /活动 订阅 <游戏> 开启提醒。"
    names = [GAME_NAMES.get(game, game) for game in games]
    return "当前会话订阅：" + "、".join(names)


def resolve_game(arg: str, known_games: set[str]) -> str | None:
    """Resolve a game argument by id or Chinese name.

    Raises:
        ValueError: When the argument does not match a known game.
    """
    value = str(arg or "").strip()
    if not value:
        return None
    if value in known_games:
        return value
    for game_id, name in GAME_NAMES.items():
        if value == name and (game_id in known_games or not known_games):
            return game_id
    lowered = value.casefold()
    for game_id in known_games:
        if game_id.casefold() == lowered:
            return game_id
    raise ValueError(f"unknown_game:{value}")


def render_subscription_change(game_id: str, subscribed: bool) -> str:
    """Render the result of a subscribe/unsubscribe action."""
    name = GAME_NAMES.get(game_id, game_id)
    if subscribed:
        return f"已订阅 {name} 的活动结束提醒。"
    return f"已取消订阅 {name}。"


HELP_TEXT = (
    "游戏活动日历指令：\n"
    "/活动 列表 [游戏] — 查看活动\n"
    "/活动 今日 [游戏] — 今日与 24 小时内开始的活动\n"
    "/活动 详情 <名称> — 查看活动详情\n"
    "/活动 状态 — 同步状态与数据源健康\n"
    "/活动 同步 [游戏] — 手动同步（管理员）\n"
    "/活动 订阅 [游戏] — 订阅/查看结束提醒\n"
    "/活动 取消订阅 [游戏] — 取消提醒订阅"
)


def is_known_status(value: str) -> bool:
    """Return whether a status value is part of the supported enum."""
    return value in STATUSES
