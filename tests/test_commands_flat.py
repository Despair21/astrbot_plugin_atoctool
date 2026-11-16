"""Static guards for the flattened single-token command surface."""

from __future__ import annotations

import re
from pathlib import Path

from game_activity.commands import HELP_TEXT

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = TESTS_DIR.parent
MAIN_PY = PLUGIN_ROOT / "main.py"

COMMAND_ALIASES = {
    "活动": "activity",
    "活动列表": "activity_list",
    "活动今日": "activity_today",
    "活动详情": "activity_detail",
    "活动同步": "activity_sync",
    "活动状态": "activity_status",
    "活动订阅": "activity_subscribe",
    "活动取消订阅": "activity_unsubscribe",
}

OLD_SPACED_COMMANDS = [
    "/活动 列表",
    "/活动 今日",
    "/活动 详情",
    "/活动 同步",
    "/活动 状态",
    "/活动 订阅",
    "/活动 取消订阅",
]


def read_main() -> str:
    """Return the plugin entry point source."""
    return MAIN_PY.read_text(encoding="utf-8")


def test_no_command_group_and_flat_decorators() -> None:
    source = read_main()
    assert "command_group" not in source
    for name, alias in COMMAND_ALIASES.items():
        decorator = f'@filter.command("{name}", alias={{"{alias}"}})'
        assert decorator in source, f"missing flat decorator: {decorator}"


def test_help_text_lists_only_single_token_commands() -> None:
    for name in COMMAND_ALIASES:
        assert f"/{name}" in HELP_TEXT
    assert "<游戏>" in HELP_TEXT
    assert not re.search(r"/活动 [\u4e00-\u9fff]", HELP_TEXT)
    for old in OLD_SPACED_COMMANDS:
        assert old not in HELP_TEXT, f"old spaced command leaked: {old}"


def test_admin_decorator_precedes_sync_command() -> None:
    source = read_main()
    admin_index = source.index("PermissionType.ADMIN")
    sync_index = source.index('@filter.command("活动同步"')
    assert admin_index < sync_index
    admin_block = (
        "    @filter.permission_type(filter.PermissionType.ADMIN)\n"
        '    @filter.command("活动同步", alias={"activity_sync"})'
    )
    assert admin_block in source
