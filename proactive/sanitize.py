"""清理注入内容里残留的元信息块。

为什么需要：AstrBot 内置会把「群名 + 当前时间」以及「你上一条回复之后的群聊
上下文」以 ``<system_reminder>`` 形式注入到 LLM 请求里。这些是给模型看的元信息，
但模型有时会把它们当成对话正文照抄出来，于是用户看到机器人回复里带着：

    <system_reminder>Group name: 大实验树洞✨
    Current datetime: 2026-10-03 22:39 (CST), Weekday: Saturday</system_reminder>

而这段回复又会被写进对话历史，下一轮当作"上文"再次喂给模型 —— 越滚越长，
人格也跟着漂移。

本模块提供纯函数：把这类块从任意文本里剥掉。不做任何 IO，便于单测。
"""

from __future__ import annotations

import re

#: 完整的 <system_reminder>...</system_reminder> 块（跨行、非贪婪）
_REMINDER_BLOCK = re.compile(
    r"<system_reminder>.*?</system_reminder>", re.DOTALL | re.IGNORECASE
)
#: 只有开标签没有闭标签的情况（被截断时）
_REMINDER_OPEN = re.compile(r"<system_reminder>.*\Z", re.DOTALL | re.IGNORECASE)
#: 只有闭标签的残留
_REMINDER_CLOSE = re.compile(r"</system_reminder>", re.IGNORECASE)

#: 群聊上下文块的裸标记（偶尔会脱离 reminder 单独出现）
_RE_CONTEXT_BLOCK = re.compile(
    r"---\s*Begin CONTEXT---.*?---\s*End CONTEXT\s*---", re.DOTALL | re.IGNORECASE
)
_RE_CONTEXT_HEAD = re.compile(
    r"You are in a group chat\.?\s*Belows are group chat context after your last reply:?",
    re.IGNORECASE,
)


def strip_system_reminders(text: str) -> str:
    """把 system_reminder 与群聊上下文块从文本里剥掉，并整理空白。

    只做「删元信息」这一件事，不改动其余正文；因此可以安全地作用在
    用户可见回复、对话历史条目、注入片段上。
    """
    if not text or not isinstance(text, str):
        return text or ""
    low = text.lower()
    # 注意要把**孤立的闭标签**也算进来：截断或上游只留了尾巴时，
    # 只有 </system_reminder> 而没有开标签，早退就会漏掉它。
    if ("system_reminder" not in low
            and "begin context" not in low
            and "group chat context after your last reply" not in low):
        return text

    out = _REMINDER_BLOCK.sub("", text)
    out = _RE_CONTEXT_BLOCK.sub("", out)
    out = _RE_CONTEXT_HEAD.sub("", out)
    out = _REMINDER_OPEN.sub("", out)
    out = _REMINDER_CLOSE.sub("", out)

    # 收尾：去掉因此产生的多余空行（连续 3+ 换行压成 2 个）
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def scrub_request(req) -> int:
    """就地清理一个 LLM 请求对象里的注入片段。返回清理的片段数。

    覆盖两个位置：
      · ``extra_user_content_parts``：AstrBot 内置注入 reminder 的地方
      · ``contexts``：对话历史条目（模型抄出来的内容会沉淀在这里）
    """
    if req is None:
        return 0
    removed = 0

    parts = getattr(req, "extra_user_content_parts", None)
    if isinstance(parts, list):
        kept = []
        for part in parts:
            text = getattr(part, "text", None)
            if isinstance(text, str) and text.strip():
                cleaned = strip_system_reminders(text)
                if not cleaned.strip():
                    removed += 1
                    continue
                if cleaned != text:
                    try:
                        part.text = cleaned
                    except Exception:
                        removed += 1
                        continue
            kept.append(part)
        if len(kept) != len(parts):
            try:
                parts[:] = kept
            except Exception:
                pass

    contexts = getattr(req, "contexts", None)
    if isinstance(contexts, list):
        for item in contexts:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, str) and content:
                cleaned = strip_system_reminders(content)
                if cleaned != content:
                    item["content"] = cleaned
                    removed += 1

    return removed
