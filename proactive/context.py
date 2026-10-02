"""会话上下文拼装。

生成主动消息时，「联系上下文」靠的就是这里——把最近若干条真实群聊
整理成一段带时间戳的对话记录喂给模型。模型因此知道大家在聊什么、
聊到哪一步了，而不是凭空打招呼。
"""

from __future__ import annotations

import datetime as _dt
import re

from .textutil import format_clock

# 判断机器人上一句是不是在提问——「话题追问」触发方式依赖它。
QUESTION_TAIL_RE = re.compile(r"[?？]\s*$")
QUESTION_WORDS = (
    "吗",
    "呢",
    "么",
    "如何",
    "怎么",
    "怎样",
    "有没有",
    "是不是",
    "能不能",
    "要不要",
    "谁",
    "哪里",
    "哪儿",
    "什么时候",
    "几点",
    "多少",
)


def looks_like_question(text: str) -> bool:
    """粗略判断一句话是不是在向别人发问。

    宁可漏判也不要误判——误判会让机器人在群里连着追问，观感很差。
    """
    content = (text or "").strip()
    if not content:
        return False
    if QUESTION_TAIL_RE.search(content):
        return True
    if len(content) > 60:
        return False
    return any(word in content for word in QUESTION_WORDS)


class ContextBuilder:
    """从存储里取消息并渲染成提示词片段。"""

    def __init__(self, config, store):
        self.config = config
        self.store = store

    async def rows(self, umo: str, limit: int | None = None) -> list[dict]:
        limit = limit or self.config.context_messages
        cutoff = 0.0
        max_age = self.config.context_max_age_minutes
        if max_age > 0:
            cutoff = _dt.datetime.now().timestamp() - max_age * 60
        return await self.store.recent_messages(umo, limit=limit, since_ts=cutoff)

    async def transcript(self, umo: str, limit: int | None = None) -> str:
        """渲染成「[14:32] 张三: 内容」形式的对话记录。无内容返回空串。"""
        messages = await self.rows(umo, limit=limit)
        return self.render(messages)

    @staticmethod
    def render(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            role = message.get("role") or "user"
            content = (message.get("content") or "").strip()
            if not content:
                continue
            clock = format_clock(message.get("ts"))
            if role == "bot":
                who = "我（机器人）"
            else:
                who = (message.get("sender_name") or "").strip() or "群友"
            lines.append(f"[{clock}] {who}: {content}")
        return "\n".join(lines)

    async def last_human_message(self, umo: str) -> dict | None:
        messages = await self.rows(umo, limit=30)
        for message in reversed(messages):
            if (message.get("role") or "") == "user":
                return message
        return None

    async def last_bot_message(self, umo: str) -> dict | None:
        messages = await self.rows(umo, limit=30)
        for message in reversed(messages):
            if (message.get("role") or "") == "bot":
                return message
        return None

    @staticmethod
    def time_hint(moment: _dt.datetime | None = None) -> str:
        """给模型一个时间锚点，让它别在半夜说「早上好」。"""
        moment = moment or _dt.datetime.now()
        hour = moment.hour
        if hour < 5:
            period = "凌晨"
        elif hour < 9:
            period = "清晨"
        elif hour < 12:
            period = "上午"
        elif hour < 14:
            period = "中午"
        elif hour < 18:
            period = "下午"
        elif hour < 22:
            period = "晚上"
        else:
            period = "深夜"
        weekday = "一二三四五六日"[moment.weekday()]
        return f"{moment.strftime('%Y-%m-%d %H:%M')}（{period}，周{weekday}）"
