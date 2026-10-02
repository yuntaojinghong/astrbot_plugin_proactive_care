"""消息准入与匿名隔离。

这是本插件与「匿名树洞」类插件和平共处的关键一层。

背景：匿名树洞通常的实现方式是——用户私聊机器人投稿，机器人再把内容
转发到群里（发送者因此是机器人自己，常见格式为「【昵称】正文」或直接
用一个昵称池里的名字）。

微光在这里做三道隔离：

1. **只跟踪群聊**。私聊内容从入口就不读取、不落库，匿名投稿的原始私聊
   微光根本接触不到，因此不存在反匿名化的可能。
2. **忽略机器人自己发出的消息**。转发帖的 sender 就是机器人自己，
   被这一条规则天然挡掉，不会被当成「群友在聊天」。
3. **昵称池 / 前缀正则白名单式过滤**。作为兜底，即使用户的树洞用了
   别的转发方式，也能靠配置把这类消息排除在记忆与生成之外。

另外，命中忽略规则的消息会返回明确的原因串，面板与日志都能看出
「为什么这条没被记录」，排查起来不用猜。
"""

from __future__ import annotations

import re
from typing import Any

# 内置转述格式：「【昵称】正文」。昵称限 1~12 字，避免把正文里的书名号误伤。
BUILTIN_TRANSCRIPT_RE = re.compile(r"^\s*[【\[](?P<nick>[^】\]]{1,12})[】\]]")

# 忽略原因（对外暴露，面板会直接展示这些字面量）
REASON_SELF = "机器人自己发出"
REASON_NICKNAME = "命中匿名昵称池"
REASON_PATTERN = "命中自定义忽略正则"
REASON_BUILTIN = "识别为转述格式"
REASON_COMMAND = "指令消息"


class IgnoreRule:
    """把配置里的忽略规则编译好，避免每条消息都重新编译正则。"""

    def __init__(self, config: Any):
        self.ignore_self = bool(getattr(config, "ignore_self_sent", True))
        self.ignore_builtin = bool(getattr(config, "ignore_builtin", True))
        self.ignore_commands = bool(getattr(config, "ignore_commands", True))
        self.nicknames = {name.strip() for name in getattr(config, "ignore_nicknames", []) if name.strip()}
        self.raw_patterns: list[str] = list(getattr(config, "ignore_patterns", []) or [])
        self.patterns: list[re.Pattern] = []
        for pattern in self.raw_patterns:
            try:
                self.patterns.append(re.compile(pattern))
            except re.error:
                # 编译失败的正则已在 Config._validate 里提示过，这里静默跳过
                continue

    def judge(self, *, text: str, sender_id: str, sender_name: str, self_id: str) -> tuple[bool, str]:
        """判断一条群消息是否应当被忽略。

        Returns:
            ``(是否忽略, 原因)``。原因在此为人类可读文案，便于面板展示。
        """
        content = (text or "").strip()

        if self.ignore_self and self_id and str(sender_id) == str(self_id):
            return True, REASON_SELF

        if self.ignore_commands and content.startswith("/"):
            return True, REASON_COMMAND

        if self.nicknames:
            name = (sender_name or "").strip()
            if name and name in self.nicknames:
                return True, REASON_NICKNAME

        if content:
            for pattern in self.patterns:
                if pattern.search(content):
                    return True, REASON_PATTERN
            if self.ignore_builtin:
                matched = BUILTIN_TRANSCRIPT_RE.match(content)
                if matched and self._looks_like_transcript(matched.group("nick")):
                    return True, REASON_BUILTIN

        return False, ""

    def _looks_like_transcript(self, nick: str) -> bool:
        """判断「【xxx】」更像匿名昵称还是正文里的书名号。

        配置过昵称池时以昵称池为准；否则只排除明显是正文的情况
        （含标点、过长、或是「公告/通知」之类常见标题词）。
        """
        cleaned = nick.strip()
        if not cleaned or len(cleaned) > 12:
            return False
        if self.nicknames:
            return cleaned in self.nicknames
        if re.search(r"[，。！？,\.!\?、；;：:\s]", cleaned):
            return False
        if cleaned in {"公告", "通知", "群规", "置顶", "活动", "提醒"}:
            return False
        return True
