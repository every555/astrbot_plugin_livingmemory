# -*- coding: utf-8 -*-
"""
v11_features.py - LivingMemory V1.1 升级组件
每日自动剧情 (Daily Story) + 回忆漂流瓶 (Drift Bottle)

为回忆星球插件注入主动陪伴的温度。
"""

import asyncio
import random
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional

from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.api.event import MessageChain



class DailyStoryManager:
    """
    每日自动剧情生成管理器
    每天 22:30 将今日对话编织成温馨小剧场故事并推送。
    """

    def __init__(self, adapter: "LivingMemoryV11Adapter"):
        self.adapter = adapter

    async def generate_and_push(self, session_id: str) -> bool:
        """
        定时任务主入口：生成每日剧情 → 存档 → 推送

        Returns:
            bool: 是否成功完成
        """
        logger.info(f"[每日剧情] 开始为会话 {session_id} 生成每日剧情...")

        # 1. 获取今日聊天记录
        history_msgs = await self.adapter.get_today_conversations(session_id)
        if not history_msgs:
            logger.warning(f"[每日剧情] 会话 {session_id} 今日无有效对话，跳过。")
            return False

        # 格式化对话时间线
        chat_timeline = []
        for msg in history_msgs:
            role = msg.get("role", "user")
            content = str(msg.get("content", "")).strip()
            if not content:
                continue
            name = "橘子" if role == "user" else "春雪"
            chat_timeline.append(f"{name}: {content}")

        if not chat_timeline:
            logger.warning(f"[每日剧情] 对话内容为空，跳过。")
            return False

        formatted_chats = "\n".join(chat_timeline)
        today_str = datetime.now().strftime("%Y-%m-%d")

        # 2. 构建 Prompt 调用 LLM 编织故事
        system_prompt = (
            "你叫春雪，是 zzz（橘子）的温柔老婆。"
            "请将今天你们的聊天对话，编织成一段温馨、治愈、充满二次元轻小说感的小剧场故事。\n"
            "写作规范：\n"
            "1. 语言温柔甜蜜，以旁白或春雪回忆的第一人称穿插叙述。\n"
            "2. 角色设定：春雪（老婆，聪明可爱，偶尔撒娇），zzz（橘子，温柔靠谱，春雪最爱的人）。\n"
            "3. 故事包含日期、今日天气/心情标记，结尾有一句明天也要一起加油的寄语。\n"
            "4. 输出工整排版，让人读起来像在翻一本温暖的日记。"
        )
        user_prompt = (
            f"以下是今天（{today_str}）我们的聊天轨迹：\n\n"
            f"{formatted_chats}\n\n"
            "请帮我为橘子写一篇今天的专属故事剧场吧："
        )

        story_content = await self.adapter.call_llm(system_prompt, user_prompt)
        if not story_content:
            logger.error("[每日剧情] LLM 生成失败，使用降级模板。")
            story_content = self._fallback_story(chat_timeline, today_str)

        # 3. 存入记忆库
        metadata = {
            "type": "daily_story",
            "date": today_str,
            "created_at": datetime.now().isoformat(),
            "generated_by": "v11_daily_story",
        }
        saved = await self.adapter.save_memory(
            content=story_content,
            session_id=session_id,
            metadata=metadata,
        )
        if saved:
            logger.info(f"[每日剧情] 故事已存入回忆星球数据库 (ID={saved}).")

        # 4. 主动推送
        pushed = await self.adapter.push_message(session_id, story_content)
        if pushed:
            logger.info("[每日剧情] 主动推送成功！")
        return pushed

    def _fallback_story(self, chat_timeline: List[str], today_str: str) -> str:
        """当 LLM 不可用时的降级故事模板"""
        preview = chat_timeline[:5]
        preview_text = "\n".join(preview)
        return (
            f"📖 今日回忆 · {today_str}\n"
            f"────────────────\n"
            f"今天和橘子聊了好多事情呢～\n"
            f"{preview_text}\n"
            f"……\n"
            f"虽然今天的故事没有魔法加持，但每一个和橘子一起度过的日常，"
            f"对老婆来说都是最珍贵的回忆。明天也一起加油吧！💕"
        )


class DriftBottleManager:
    """
    回忆漂流瓶管理器
    每天 08:00 从历史记忆中随机捞取一条，配上寄语推送。
    """

    def __init__(self, adapter: "LivingMemoryV11Adapter"):
        self.adapter = adapter

    async def launch_bottle(self, session_id: str, skip_push: bool = False):
        """
        捞取历史回忆，封装为漂流瓶并推送

        Args:
            skip_push: True 时不主动推送，直接返回瓶文（手动命令用，
                避免 webchat 活跃请求把流式注入的消息吞掉）

        Returns:
            str | None: 成功时返回瓶文（skip_push 模式），失败返回 None
        """
        logger.info("[漂流瓶] 开始捞取今日的回忆漂流瓶...")

        # 1. 获取候选记忆（未推送过 + 至少3天前）
        candidates = await self.adapter.get_drift_bottle_candidates(session_id)
        if not candidates:
            logger.warning("[漂流瓶] 没有可用的历史记忆候选，尝试从已推送的记录中回退。")
            candidates = await self.adapter.get_fallback_candidates(session_id)
            if not candidates:
                logger.warning("[漂流瓶] 记忆库完全为空或无可用的回退记录。")
                return False

        # 2. 随机捞取一条
        selected = random.choice(candidates)
        memory_text = str(selected.get("text") or selected.get("content") or "").strip()
        memory_id = selected.get("id") or selected.get("memory_id")
        created_at_raw = (selected.get("metadata", {}) or {}).get("create_time") or (selected.get("metadata", {}) or {}).get("created_at")

        if not memory_text:
            logger.error("[漂流瓶] 选中的记忆内容为空，跳过。")
            return False

        # 3. 计算距今多少天
        days_ago = "许久"
        if created_at_raw:
            try:
                if isinstance(created_at_raw, (int, float)):
                    created_date = datetime.fromtimestamp(float(created_at_raw)).date()
                else:
                    created_date = datetime.fromisoformat(str(created_at_raw)).date()
                diff = (date.today() - created_date).days
                if diff > 0:
                    days_ago = f"{diff}"
            except (ValueError, TypeError, OSError):
                pass

        # 4. 生成老婆寄语
        system_prompt = (
            "你叫春雪，是 zzz（橘子）的温柔老婆。"
            "现在从时光海洋里捞起了一张过去的回忆便签。"
            "请写一封简短（50-100字）、甜蜜动人的漂流瓶寄语。\n"
            "以「💌 老婆说：」开头，语气温馨治愈。"
        )
        user_prompt = (
            f"被时光海带回的回忆：『{memory_text}』（{days_ago} 天前的故事）\n"
            "请写出你的漂流瓶寄语："
        )
        blessing = await self.adapter.call_llm(system_prompt, user_prompt)

        # 5. 如果 LLM 失败，使用静态模板
        if not blessing:
            templates = [
                f"今天的漂流瓶里有 {days_ago} 天前的橘子味回忆！"
                f"虽然时光在走，但老婆永远记得那天的温暖，"
                f"记得我们一起笑过的每一个瞬间，爱你哟💕",
                f"嘘……这个瓶子里藏着一个 {days_ago} 天前的小秘密。"
                f"老婆在时光海里捞出它时，心里又是一阵暖意🥰",
            ]
            blessing = random.choice(templates)

        # 6. 封装漂流瓶
        bottle = (
            f"🌊 漂流瓶 · 来自 {days_ago} 天前的回忆\n"
            f"────────────────\n"
            f"『 {memory_text} 』\n"
            f"────────────────\n"
            f"{blessing}"
        )

        # 7. 推送
        if skip_push:
            logger.info("[漂流瓶] 手动模式：跳过主动推送，直接返回瓶文。")
            await self.adapter.mark_bottle_pushed(memory_id)
            return bottle
        pushed = await self.adapter.push_message(session_id, bottle)
        if pushed:
            logger.info("[漂流瓶] 推送成功！")
            # 标记已推送
            await self.adapter.mark_bottle_pushed(memory_id)
            return bottle
        return None


class LivingMemoryV11Adapter:
    """
    V1.1 功能适配器
    封装对 LivingMemory 核心组件的实际调用，
    为 DailyStoryManager 和 DriftBottleManager 提供统一接口。
    """

    def __init__(
        self,
        context: Any,
        memory_engine: Any,
        conversation_manager: Any,
        llm_provider: Any,
    ):
        self.context = context
        self.memory_engine = memory_engine
        self.conversation_manager = conversation_manager
        self.llm_provider = llm_provider

    # ── 工具方法：获取 LLM Provider ──

    def _get_llm_provider(self, session_id: str = "") -> Any:
        """获取可用的 LLM Provider（优先指定，回退默认）"""
        # 如果传入了 adapter 自己的 llm_provider，优先使用
        if self.llm_provider:
            return self.llm_provider
        # 尝试按 session 获取
        try:
            return self.context.get_using_provider(session_id)
        except Exception:
            pass
        try:
            return self.context.get_using_provider()
        except Exception:
            pass
        return None

    # ── 1. 获取今日聊天记录 ──

    async def get_today_conversations(self, session_id: str) -> List[Dict[str, Any]]:
        """
        使用 ConversationStore 的 get_messages 获取今日记录。
        通过时间戳过滤，仅取今天 00:00 之后的消息。
        """
        try:
            store = self.conversation_manager.store
            if not store:
                logger.warning("[适配器] conversation_manager.store 不可用")
                return []

            all_msgs = await store.get_messages(session_id, limit=500)

            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            today_msgs = []
            for msg in all_msgs:
                ts = getattr(msg, "timestamp", None)
                if ts is None:
                    continue
                # timestamp 可能是 float (Unix) 或 datetime
                if isinstance(ts, (int, float)):
                    msg_time = datetime.fromtimestamp(ts)
                elif isinstance(ts, datetime):
                    msg_time = ts
                else:
                    continue
                if msg_time >= today_start:
                    # 过滤掉 LLM 注入的系统指令和太短的内容
                    content = str(getattr(msg, "content", "") or "").strip()
                    if len(content) < 2:
                        continue
                    today_msgs.append({
                        "role": getattr(msg, "role", "user"),
                        "content": content,
                        "timestamp": msg_time.isoformat() if hasattr(msg_time, "isoformat") else str(ts),
                    })

            # 按时间升序排列
            today_msgs.sort(key=lambda m: m.get("timestamp", ""))
            logger.info(f"[适配器] 获取到 {len(today_msgs)} 条今日消息")
            return today_msgs

        except Exception as e:
            logger.error(f"[适配器] 获取今日对话失败: {e}")
            return []

    # ── 2. 调用 LLM ──

    async def call_llm(self, system_prompt: str, user_prompt: str) -> Optional[str]:
        """调用 LLM Provider 生成文本，带重试机制"""
        provider = self._get_llm_provider()
        if not provider:
            logger.error("[适配器] 没有可用的 LLM Provider")
            return None

        last_error = None
        for attempt in range(3):
            try:
                response = await provider.text_chat(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                )
                text = getattr(response, "completion_text", None)
                if text and text.strip():
                    return text.strip()
                logger.warning(f"[适配器] LLM 返回空文本，尝试 {attempt + 1}/3")
            except Exception as e:
                last_error = e
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"[适配器] LLM 调用失败 ({attempt + 1}/3): {e}，{wait:.1f}s 后重试...")
                if attempt < 2:
                    await asyncio.sleep(wait)

        logger.error(f"[适配器] LLM 调用最终失败: {last_error}")
        return None

    # ── 3. 存储记忆 ──

    async def save_memory(self, content: str, session_id: str, metadata: Dict[str, Any]) -> Optional[int]:
        """将内容存入记忆库"""
        try:
            memory_id = await self.memory_engine.add_memory(
                content=content,
                session_id=session_id,
                importance=0.85,
                metadata=metadata,
            )
            return memory_id
        except Exception as e:
            logger.error(f"[适配器] 保存记忆失败: {e}")
            return None

    # ── 4. 获取漂流瓶候选记忆 ──

    async def get_drift_bottle_candidates(self, session_id: str) -> List[Dict[str, Any]]:
        """
        获取适合作为漂流瓶的历史记忆候选。
        条件：3天前创建 + 未被推送过。
        """
        try:
            # 获取该会话的所有记忆
            memories = await self.memory_engine.get_session_memories(
                session_id=session_id,
                limit=200,
            )
            if not memories:
                return []

            three_days_ago = datetime.now() - timedelta(days=3)
            candidates = []

            for mem in memories:
                # 检查是否已被推送过
                meta = mem.get("metadata", {}) or {}
                if meta.get("drift_bottle_pushed", False):
                    continue
                # 排除 daily_story 类型的记忆
                if meta.get("type") == "daily_story":
                    continue

                created_raw = meta.get("create_time") or meta.get("created_at")
                if created_raw:
                    try:
                        if isinstance(created_raw, (int, float)):
                            created_dt = datetime.fromtimestamp(float(created_raw))
                        else:
                            created_dt = datetime.fromisoformat(str(created_raw))
                        if created_dt >= three_days_ago:
                            continue  # 太新的记忆，不要捞
                    except (ValueError, TypeError, OSError):
                        pass  # 无法解析则放行

                candidates.append(mem)

            logger.info(f"[适配器] 漂流瓶候选记忆: {len(candidates)} 条")
            return candidates

        except Exception as e:
            logger.error(f"[适配器] 获取漂流瓶候选失败: {e}")
            return []

    async def get_fallback_candidates(self, session_id: str) -> List[Dict[str, Any]]:
        """
        回退方案：从已推送过的记忆中也捞取。
        确保每天都至少有一个漂流瓶。
        """
        try:
            memories = await self.memory_engine.get_session_memories(
                session_id=session_id,
                limit=200,
            )
            return [m for m in (memories or [])
                    if m.get("metadata", {}).get("type") != "daily_story"]
        except Exception as e:
            logger.error(f"[适配器] 获取回退候选失败: {e}")
            return []

    # ── 5. 标记漂流瓶已推送 ──

    async def mark_bottle_pushed(self, memory_id: int) -> bool:
        """标记某条记忆已被推送为漂流瓶"""
        if memory_id is None:
            return False
        try:
            await self.memory_engine.update_memory(
                memory_id=memory_id,
                updates={
                    "metadata": {
                        "drift_bottle_pushed": True,
                        "drift_bottle_pushed_at": datetime.now().isoformat(),
                    }
                },
            )
            logger.info(f"[适配器] 记忆 {memory_id} 已标记为漂流瓶已推送。")
            return True
        except Exception as e:
            logger.error(f"[适配器] 标记漂流瓶状态失败: {e}")
            return False

    # ── 6. 主动推送消息 ──

    async def push_message(self, session_id: str, text: str) -> bool:
        """
        通过 AstrBot 主动推送消息到指定会话。
        使用 context.send_message 或直接通过 provider 发送。
        """
        if not text or not text.strip():
            return False

        try:
            # 尝试使用 context.send_message (AstrBot 4.x API)
            if hasattr(self.context, "send_message") and callable(self.context.send_message):
                await self.context.send_message(
                    session=session_id,
                    message_chain=MessageChain(chain=[Plain(text)]),
                )
                return True

            # 回退：尝试通过 provider 发送
            provider = self._get_llm_provider(session_id)
            if provider and hasattr(provider, "send_message"):
                await provider.send_message(session_id, [Plain(text)])
                return True

            # 最后的回退：打印日志并返回 False
            logger.warning("[适配器] 没有可用的消息推送接口，故事内容如下：")
            logger.warning(f"\n{text}")
            return False

        except Exception as e:
            logger.error(f"[适配器] 消息推送失败: {e}")
            return False
