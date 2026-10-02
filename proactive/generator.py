"""主动消息生成。

这里决定「说什么」。核心是把三样东西喂给模型：**真实上下文**、**长期记忆**、
以及**为什么现在开口**。再加上一段强约束的风格指令，让输出像群友随口说的话，
而不是客服话术或模板。

生成后还有两道清洗：去前缀/去引号，以及与最近发过的内容做相似度查重——
重复说话比不说话更伤观感。
"""

from __future__ import annotations

from .context import ContextBuilder, looks_like_question
from .textutil import collapse, normalize, similarity, strip_markers, truncate

TRIGGER_LABELS = {
    "idle": "空闲唤醒",
    "followup": "话题追问",
    "random": "随机关怀",
    "schedule": "定时问候",
    "instant": "即时搭话",
    "manual": "手动触发",
    "preview": "预览",
}

# 不同触发方式下，机器人「为什么开口」的场景描述，直接进系统提示
TRIGGER_SCENES = {
    "instant": "群里刚有人说了一句话，你正跟着大家聊，想自然地接一句。",
    "followup": "你之前抛了个话题没人接，现在想自己把话头续下去。",
    "schedule": "到了一个固定的时间点，你按惯例跟大家打个招呼。",
    "random": "没什么特别的事，就是突然想到这个群里的人了。",
    "manual": "管理员让你说句话。",
    "preview": "这是一次演练，你只是在练着说一句话。",
}

# 相似度超过这个阈值就认定「和之前说过的太像」，重新生成一次
DUPLICATE_THRESHOLD = 0.82

SYSTEM_TEMPLATE = """你是「{bot_name}」，长期活跃在这个群里的一个真人风格群友。

{scene}

必须遵守：
1. 只说一句话。自然、口语化，是真人随口说出来的那种，不要书面语。
2. 可以参考下面的聊天记录来承接话题，但不要生硬复述别人的原话。
3. 不要连珠炮式提问，不要客服腔，不要出现「有什么可以帮您」「请问还有什么需要」这类话。
4. 不要提及自己是机器人、AI、助手或模型。
5. 不要重复你以前已经说过的话。
6. 只输出这句话本身：不要加引号、不要加署名前缀、不要作任何解释或说明。

说话风格参考：{style}"""

USER_TEMPLATE = """当前时间：{time_hint}

这次开口的原因：{reason}
{note_block}
最近群聊记录：
{transcript}

可参考的长期记忆（关于这个群里的人和事）：
{memories}

{last_sent_block}请直接输出你要说的那一句话。"""


class Generator:
    def __init__(self, config, store, memory, llm, context_builder: ContextBuilder | None = None, logger=None):
        self.config = config
        self.store = store
        self.memory = memory
        self.llm = llm
        self.context = context_builder or ContextBuilder(config, store)
        self.logger = logger
        self.bot_name = "微光"

    # ==================================================================
    #  对外入口
    # ==================================================================
    async def generate(
        self,
        session: dict,
        trigger: str,
        reason: str,
        note: str = "",
        *,
        preview: bool = False,
    ) -> dict:
        """生成一条主动消息。

        Returns:
            ``{"ok": bool, "content": str, "reason": str, "error": str, "trigger": str}``
        """
        umo = session.get("umo") or ""
        result = {"ok": False, "content": "", "reason": reason, "error": "", "trigger": trigger}

        transcript = await self.context.transcript(umo)
        memories = await self._memory_block(umo, transcript)
        last_sent = await self._last_sent_block(umo)

        prompt = USER_TEMPLATE.format(
            time_hint=ContextBuilder.time_hint(),
            reason=f"{TRIGGER_LABELS.get(trigger, trigger)}{('：' + reason) if reason else ''}",
            note_block=f"额外要求：{note}\n" if note else "",
            transcript=transcript or "（这个群最近没有可用的聊天记录）",
            memories=memories,
            last_sent_block=last_sent,
        )
        scene = TRIGGER_SCENES.get(trigger, "现在群里安静了一会儿，你打算主动开口说一句话。")
        system_prompt = SYSTEM_TEMPLATE.format(
            bot_name=self.bot_name, style=self.config.style, scene=scene
        )

        text = await self.llm.chat(prompt=prompt, system_prompt=system_prompt, umo=umo)
        if not text:
            result["error"] = self.llm.last_error or "模型没有返回内容"
            return result

        cleaned = self.clean(text)
        if not cleaned:
            result["error"] = "模型输出清洗后为空"
            return result

        if await self._too_similar(umo, cleaned):
            self._info(f"生成内容与历史重复，重试一次（{umo}）")
            retry_prompt = prompt + "\n\n注意：你上一句说的和以前太像了，换一个完全不同的角度和说法。"
            retry = await self.llm.chat(prompt=retry_prompt, system_prompt=system_prompt, umo=umo)
            retry_clean = self.clean(retry or "")
            if retry_clean and not await self._too_similar(umo, retry_clean):
                cleaned = retry_clean
            # 重试仍重复就用原结果，总比不发好

        result["ok"] = True
        result["content"] = cleaned
        return result

    def clean(self, text: str) -> str:
        """去前缀、去引号、压空白，并按配置截断长度。"""
        cleaned = strip_markers(text or "")
        cleaned = collapse(cleaned)
        if not cleaned:
            return ""
        # 模型偶尔会一次给多行，只取第一句有意义的话
        first_line = cleaned.split("\n")[0].strip()
        if first_line:
            cleaned = first_line
        cleaned = truncate(cleaned, self.config.max_chars)
        # 纯标点或纯表情的产物没有意义
        if not normalize(cleaned):
            return ""
        return cleaned

    # ==================================================================
    #  辅助
    # ==================================================================
    async def _memory_block(self, umo: str, transcript: str) -> str:
        if not self.config.memory_enable or self.config.max_inject <= 0:
            return "（本次未启用记忆）"
        query = transcript[-400:] if transcript else ""
        memories = await self.memory.retrieve(umo, query=query)
        if not memories:
            return "（暂无相关记忆）"
        return "\n".join(f"- {item.get('content', '')}" for item in memories)

    async def _last_sent_block(self, umo: str) -> str:
        recent = await self.store.recent_sent_contents(umo, limit=5)
        if not recent:
            return ""
        lines = "\n".join(f"- {item}" for item in recent)
        return f"你之前主动说过的话（不要重复或换汤不换药）：\n{lines}\n\n"

    async def _too_similar(self, umo: str, text: str) -> bool:
        recent = await self.store.recent_sent_contents(umo, limit=8)
        return any(similarity(text, old) >= DUPLICATE_THRESHOLD for old in recent if old)

    def _info(self, message: str) -> None:
        if self.logger is not None:
            try:
                self.logger.info(f"[微光] {message}")
            except Exception:
                pass


__all__ = ["Generator", "TRIGGER_LABELS", "looks_like_question"]
