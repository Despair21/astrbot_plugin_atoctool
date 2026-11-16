"""AstrBot plugin entry point for the game activity calendar."""

from __future__ import annotations

from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_game_activity"


class GameActivityPlugin(Star):
    """Sync game activity calendars and remind users before activities end."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = dict(config or {})

    async def initialize(self) -> None:
        """Called when the plugin is activated."""

    async def terminate(self) -> None:
        """Called when the plugin is disabled or reloaded."""
