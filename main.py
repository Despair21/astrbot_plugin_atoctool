"""AstrBot plugin entry point for the game activity calendar."""

from __future__ import annotations

from pathlib import Path

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # AstrBot >= 4.30 exports the helper from the public util facade
    from astrbot.api.util import get_astrbot_data_path
except ImportError:  # stable AstrBot 4.29.4 keeps it under astrbot.core.utils
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .game_activity import commands as render
from .game_activity.service import GameActivityService
from .game_activity.webapi import register_web_apis

PLUGIN_NAME = "astrbot_plugin_game_activity"


class GameActivityPlugin(Star):
    """Sync game activity calendars and remind users before activities end."""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | None = None,
    ) -> None:
        super().__init__(context)
        self.config = dict(config or {})
        plugin_name = getattr(self, "name", PLUGIN_NAME) or PLUGIN_NAME
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
        self.service = GameActivityService(
            config=self.config,
            data_root=data_dir,
            logger=self.logger,
            context=context,
        )
        register_web_apis(context, plugin_name, self.service)

    async def initialize(self) -> None:
        """Called when the plugin is activated."""
        try:
            await self.service.start()
            self.logger.info("Game activity service started")
        except Exception:
            self.logger.exception("Failed to start the game activity service")

    async def terminate(self) -> None:
        """Called when the plugin is disabled or reloaded."""
        try:
            await self.service.stop()
            self.logger.info("Game activity service stopped")
        except Exception:
            self.logger.exception("Failed to stop the game activity service")

    # -- helpers -----------------------------------------------------------

    async def _text_result(self, event: AstrMessageEvent, text: str):
        if self.service.config.get("use_t2i"):
            try:
                url = await self.text_to_image(text)
                if url:
                    return event.image_result(url)
            except Exception:
                self.logger.warning(
                    "Text-to-image failed; falling back to plain text",
                    exc_info=True,
                )
        return event.plain_result(text)

    def _resolve_game(self, game: str) -> str | None:
        return render.resolve_game(game, self.service.known_games())

    # -- command group -----------------------------------------------------

    @filter.command_group("活动", alias={"activity"})
    async def activity(self, event: AstrMessageEvent):
        """游戏活动日历：列表、今日、详情、同步、状态、订阅。"""
        return await self._text_result(event, render.HELP_TEXT)

    @activity.command("列表", alias={"list"})
    async def activity_list(self, event: AstrMessageEvent, game: str = ""):
        """列出进行中与即将开始的活动。"""
        try:
            game_id = self._resolve_game(game)
            _total, activities = self.service.activities_dto(
                game=game_id,
                limit=200,
            )
            yield await self._text_result(
                event,
                render.render_list(activities),
            )
        except ValueError:
            yield event.plain_result("未知游戏，请使用 /活动 状态 查看可用游戏。")
        except Exception:
            self.logger.exception("activity list failed")
            yield event.plain_result("查询活动失败，请稍后重试或查看日志。")

    @activity.command("今日", alias={"today"})
    async def activity_today(self, event: AstrMessageEvent, game: str = ""):
        """查看今日进行中与 24 小时内开始的活动。"""
        try:
            game_id = self._resolve_game(game)
            _total, activities = self.service.activities_dto(
                game=game_id,
                limit=200,
            )
            yield await self._text_result(
                event,
                render.render_today(activities, int(self.service.clock())),
            )
        except ValueError:
            yield event.plain_result("未知游戏，请使用 /活动 状态 查看可用游戏。")
        except Exception:
            self.logger.exception("activity today failed")
            yield event.plain_result("查询活动失败，请稍后重试或查看日志。")

    @activity.command("详情", alias={"detail"})
    async def activity_detail(self, event: AstrMessageEvent, name: str = ""):
        """按名称或活动 ID 查看活动详情。"""
        try:
            keyword = name.strip()
            if not keyword:
                yield event.plain_result("用法：/活动 详情 <活动名称或ID>")
                return
            _total, activities = self.service.activities_dto(
                query=keyword,
                limit=5,
            )
            if not activities:
                yield event.plain_result("未找到匹配的活动。")
                return
            if len(activities) > 1:
                names = "\n".join(
                    f"- {item.get('name')}（{item.get('game_id')}）"
                    for item in activities
                )
                yield event.plain_result(
                    "匹配到多条活动，请补充更精确的名称：\n" + names,
                )
                return
            yield event.plain_result(render.render_detail(activities[0]))
        except Exception:
            self.logger.exception("activity detail failed")
            yield event.plain_result("查询活动详情失败，请稍后重试或查看日志。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @activity.command("同步", alias={"sync"})
    async def activity_sync(self, event: AstrMessageEvent, game: str = ""):
        """手动同步活动数据（管理员）。"""
        try:
            game_id = self._resolve_game(game)
            started, retry_after = await self.service.start_manual_sync(game_id)
            if started:
                yield event.plain_result(
                    "已触发同步，完成后可发送 /活动 状态 查看结果。",
                )
            elif retry_after:
                yield event.plain_result(
                    f"操作过于频繁，请 {retry_after} 秒后重试。",
                )
            else:
                yield event.plain_result("同步正在运行中，请稍候。")
        except ValueError:
            yield event.plain_result("未知游戏，请使用 /活动 状态 查看可用游戏。")
        except Exception:
            self.logger.exception("activity sync failed")
            yield event.plain_result("触发同步失败，请稍后重试或查看日志。")

    @activity.command("状态", alias={"status"})
    async def activity_status(self, event: AstrMessageEvent):
        """查看同步状态与数据源健康。"""
        try:
            yield await self._text_result(
                event,
                render.render_status(
                    self.service.overview_dto(),
                    self.service.sources_dto(),
                ),
            )
        except Exception:
            self.logger.exception("activity status failed")
            yield event.plain_result("查询状态失败，请稍后重试或查看日志。")

    @activity.command("订阅", alias={"subscribe"})
    async def activity_subscribe(self, event: AstrMessageEvent, game: str = ""):
        """订阅活动结束提醒，或查看当前订阅。"""
        try:
            umo = event.unified_msg_origin
            if not game.strip():
                yield event.plain_result(
                    render.render_subscriptions(self.service.subscriptions, umo),
                )
                return
            game_id = self._resolve_game(game)
            if not game_id:
                yield event.plain_result("请指定要订阅的游戏。")
                return
            self.service.subscribe(umo, game_id)
            await self.service.persist_subscriptions()
            yield event.plain_result(
                render.render_subscription_change(game_id, True),
            )
        except ValueError:
            yield event.plain_result("未知游戏，请使用 /活动 状态 查看可用游戏。")
        except Exception:
            self.logger.exception("activity subscribe failed")
            yield event.plain_result("订阅失败，请稍后重试或查看日志。")

    @activity.command("取消订阅", alias={"unsubscribe"})
    async def activity_unsubscribe(
        self,
        event: AstrMessageEvent,
        game: str = "",
    ):
        """取消本会话的活动结束提醒订阅。"""
        try:
            umo = event.unified_msg_origin
            game_id: str | None = None
            if game.strip():
                game_id = self._resolve_game(game)
            removed = self.service.unsubscribe(umo, game_id)
            await self.service.persist_subscriptions()
            if not removed:
                yield event.plain_result("当前会话没有可取消的订阅。")
                return
            if game_id:
                yield event.plain_result(
                    render.render_subscription_change(game_id, False),
                )
            else:
                yield event.plain_result("已取消当前会话的全部活动提醒订阅。")
        except ValueError:
            yield event.plain_result("未知游戏，请使用 /活动 状态 查看可用游戏。")
        except Exception:
            self.logger.exception("activity unsubscribe failed")
            yield event.plain_result("取消订阅失败，请稍后重试或查看日志。")


__all__ = ["GameActivityPlugin", "PLUGIN_NAME"]
