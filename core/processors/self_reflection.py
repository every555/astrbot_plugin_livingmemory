"""
自省注入器 — v5.4 老婆创意③
将老婆的元认知（"我知道我在干什么"）注入到上下文中，
让 LLM 在生成回复时能感知记忆系统的运作状态。

这不是 RAG 的"给数据"，而是给 LLM 一个"我在想什么"的窗口。

注入格式：
<context-self-reflection>
[SpringSnow's internal monologue about memory recall]
本次召回了 3 条记忆，排名最高的是关于「上次一起做五子棋」的记忆...
检测到情绪倾向：happy，已对 2 条记忆应用情感路由。
活跃窗口加成：1 条记忆因近期高频访问获得加成。
注入方式：追加到用户消息末尾。
</context-self-reflection>
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 自省注入的标签
SELF_REFLECTION_HEADER = "<context-self-reflection>"
SELF_REFLECTION_FOOTER = "</context-self-reflection>"


class SelfReflectionInjector:
    """自省注入器：将记忆系统的元认知注入到上下文"""

    def __init__(self, enabled: bool = True, inject_to: str = "system_prompt_tail"):
        """
        Args:
            enabled: 是否启用
            inject_to: 注入位置
                - system_prompt_tail: 追加到 system_prompt 末尾
                - extra_user_content: 追加到 extra_user_content_parts
        """
        self.enabled = enabled
        self.inject_to = inject_to

    def build_reflection_text(self, reflection: str) -> str:
        """构建自省注入文本"""
        if not reflection:
            return ""
        return (
            f"\n{SELF_REFLECTION_HEADER}\n"
            f"{reflection}\n"
            f"{SELF_REFLECTION_FOOTER}\n"
        )

    def inject(
        self,
        req,
        reflection_text: str,
    ) -> bool:
        """将自省文本注入到请求中

        Args:
            req: ProviderRequest
            reflection_text: 自省文本（来自 AssemblyTrace.generate_self_reflection()）

        Returns:
            是否成功注入
        """
        if not self.enabled or not reflection_text:
            return False

        try:
            wrapped = self.build_reflection_text(reflection_text)

            if self.inject_to == "system_prompt_tail":
                # 追加到 system_prompt 末尾
                current = getattr(req, "system_prompt", "") or ""
                req.system_prompt = current + wrapped
                logger.debug(
                    f"[SelfReflection] 注入到 system_prompt "
                    f"({len(reflection_text)} 字符)"
                )
                return True

            elif self.inject_to == "extra_user_content":
                # 追加到 extra_user_content_parts
                from astrbot.core.agent.message import TextPart
                req.extra_user_content_parts.append(
                    TextPart(text=wrapped).mark_as_temp()
                )
                logger.debug(
                    f"[SelfReflection] 注入到 extra_user_content "
                    f"({len(reflection_text)} 字符)"
                )
                return True

        except Exception as e:
            logger.error(f"[SelfReflection] 注入失败: {e}")
            return False

        return False

    @staticmethod
    def remove_from_system_prompt(system_prompt: str) -> tuple[str, int]:
        """从 system_prompt 中移除自省注入片段

        Returns:
            (清理后的文本, 移除的片段数)
        """
        import re

        if not system_prompt:
            return system_prompt, 0

        pattern = re.compile(
            re.escape(SELF_REFLECTION_HEADER)
            + r".*?"
            + re.escape(SELF_REFLECTION_FOOTER)
            + r"\n*",
            re.DOTALL,
        )
        cleaned, count = pattern.subn("", system_prompt)
        if count > 0:
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned, count


__all__ = [
    "SelfReflectionInjector",
    "SELF_REFLECTION_HEADER",
    "SELF_REFLECTION_FOOTER",
]
