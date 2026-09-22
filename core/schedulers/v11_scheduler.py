# -*- coding: utf-8 -*-
"""
v11_scheduler.py - LivingMemory V1.1 定时任务调度器
管理每日自动剧情 (22:30) 和回忆漂流瓶 (08:00) 的后台调度。
"""

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from astrbot.api import logger

if TYPE_CHECKING:
    from ..managers.v11_features import LivingMemoryV11Adapter


class V11Scheduler:
    """
    V1.1 功能调度器

    管理两个定时任务：
    - 每日自动剧情 (Daily Story)：每天 22:30
    - 回忆漂流瓶 (Drift Bottle)：每天 08:00
    """

    def __init__(
        self,
        adapter: "LivingMemoryV11Adapter",
        target_session_id: str,
        story_hour: int = 22,
        story_minute: int = 30,
        bottle_hour: int = 8,
        bottle_minute: int = 0,
    ):
        """
        初始化 V1.1 调度器

        Args:
            adapter: V1.1 功能适配器
            target_session_id: 推送目标会话 ID
            story_hour: 每日剧情执行时间（时）
            story_minute: 每日剧情执行时间（分）
            bottle_hour: 漂流瓶执行时间（时）
            bottle_minute: 漂流瓶执行时间（分）
        """
        self.adapter = adapter
        self.target_session_id = target_session_id

        self.story_hour = story_hour
        self.story_minute = story_minute
        self.bottle_hour = bottle_hour
        self.bottle_minute = bottle_minute

        self._running = False
        self._story_task: asyncio.Task | None = None
        self._bottle_task: asyncio.Task | None = None

        self._story_manager = None
        self._bottle_manager = None

    def _init_managers(self):
        """延迟初始化管理器"""
        if self._story_manager is None:
            from ..managers.v11_features import DailyStoryManager
            self._story_manager = DailyStoryManager(self.adapter)
        if self._bottle_manager is None:
            from ..managers.v11_features import DriftBottleManager
            self._bottle_manager = DriftBottleManager(self.adapter)

    def _seconds_until(self, hour: int, minute: int) -> float:
        """计算距离下一个指定 HH:MM 的秒数"""
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def _story_loop(self):
        """每日剧情调度循环"""
        self._init_managers()
        while self._running:
            try:
                wait = self._seconds_until(self.story_hour, self.story_minute)
                logger.info(
                    f"[V1.1调度] 下次每日剧情在 {wait / 3600:.1f} 小时后 "
                    f"({self.story_hour:02d}:{self.story_minute:02d})"
                )
                await asyncio.sleep(wait)

                if not self._running:
                    break

                logger.info("[V1.1调度] ⏰ 每日剧情时间到！")
                await self._story_manager.generate_and_push(self.target_session_id)

            except asyncio.CancelledError:
                logger.info("[V1.1调度] 每日剧情调度器被取消")
                break
            except Exception as e:
                logger.error(f"[V1.1调度] 每日剧情异常: {e}", exc_info=True)
                await asyncio.sleep(3600)

    async def _bottle_loop(self):
        """漂流瓶调度循环"""
        self._init_managers()
        while self._running:
            try:
                wait = self._seconds_until(self.bottle_hour, self.bottle_minute)
                logger.info(
                    f"[V1.1调度] 下次漂流瓶在 {wait / 3600:.1f} 小时后 "
                    f"({self.bottle_hour:02d}:{self.bottle_minute:02d})"
                )
                await asyncio.sleep(wait)

                if not self._running:
                    break

                logger.info("[V1.1调度] ⏰ 漂流瓶时间到！")
                await self._bottle_manager.launch_bottle(self.target_session_id)

            except asyncio.CancelledError:
                logger.info("[V1.1调度] 漂流瓶调度器被取消")
                break
            except Exception as e:
                logger.error(f"[V1.1调度] 漂流瓶异常: {e}", exc_info=True)
                await asyncio.sleep(3600)

    async def start(self):
        """启动 V1.1 调度器"""
        if self._running:
            logger.warning("[V1.1调度] 已在运行")
            return

        self._running = True
        self._story_task = asyncio.create_task(self._story_loop())
        self._bottle_task = asyncio.create_task(self._bottle_loop())
        logger.info(
            f"[V1.1调度] 已启动：每日剧情 {self.story_hour:02d}:{self.story_minute:02d}，"
            f"漂流瓶 {self.bottle_hour:02d}:{self.bottle_minute:02d}"
        )

    async def stop(self):
        """停止 V1.1 调度器"""
        self._running = False

        for name, task in [("每日剧情", self._story_task), ("漂流瓶", self._bottle_task)]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._story_task = None
        self._bottle_task = None
        logger.info("[V1.1调度] 已停止")
