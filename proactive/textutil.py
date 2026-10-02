"""文本、时间与相似度小工具。

刻意不引入第三方库：去重与相似度用标准库 ``difflib`` 足够，
且它的行为是确定的，便于写单测。
"""

from __future__ import annotations

import datetime as _dt
import difflib
import re

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[\s,，。.!！?？~～、;；:：\"'“”‘’()（）\[\]【】<>《》\-—_*#]+")
_MARKER_RE = re.compile(
    r"^\s*(?:微光|机器人|bot|assistant|助手|系统)\s*[:：]\s*",
    re.IGNORECASE,
)
_QUOTE_PAIRS = (("\"", "\""), ("'", "'"), ("“", "”"), ("「", "」"), ("『", "』"), ("《", "》"))


def normalize(text: str) -> str:
    """归一化：去空白、去标点、转小写。用于记忆去重与消息查重。"""
    if not text:
        return ""
    return _PUNCT_RE.sub("", _WS_RE.sub(" ", str(text))).lower()


def similarity(left: str, right: str) -> float:
    """0~1 的相似度。两边都归一化后再比较，避免被标点干扰。"""
    a, b = normalize(left), normalize(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def keyword_overlap(query: str, text: str) -> float:
    """按「二元字组」估算重叠度。中文没有空格分词，二元组是便宜且有效的近似。"""
    q, t = normalize(query), normalize(text)
    if len(q) < 2 or len(t) < 2:
        return 0.0
    grams_q = {q[i : i + 2] for i in range(len(q) - 1)}
    grams_t = {t[i : i + 2] for i in range(len(t) - 1)}
    if not grams_q or not grams_t:
        return 0.0
    return len(grams_q & grams_t) / len(grams_q)


def collapse(text: str) -> str:
    """压掉多余空白与换行，把多行输出拉成适合聊天窗口的一行。"""
    return _WS_RE.sub(" ", str(text or "")).strip()


def strip_markers(text: str) -> str:
    """去掉模型有时会自作主张加上的「微光：」「机器人：」之类前缀。"""
    out = collapse(text)
    for _ in range(3):
        stripped = _MARKER_RE.sub("", out)
        if stripped == out:
            break
        out = stripped.strip()
    for left, right in _QUOTE_PAIRS:
        if len(out) >= 2 and out.startswith(left) and out.endswith(right):
            out = out[1:-1].strip()
    return out.strip()


def truncate(text: str, limit: int) -> str:
    """按长度截断。limit <= 0 表示不截断。"""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def now_ts() -> float:
    return _dt.datetime.now().timestamp()


def day_key(ts: float | None = None) -> str:
    moment = _dt.datetime.fromtimestamp(ts) if ts else _dt.datetime.now()
    return moment.strftime("%Y-%m-%d")


def day_start_ts(ts: float | None = None) -> float:
    moment = _dt.datetime.fromtimestamp(ts) if ts else _dt.datetime.now()
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def format_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def format_clock(ts: float | None) -> str:
    if not ts:
        return "-"
    return _dt.datetime.fromtimestamp(ts).strftime("%H:%M")


def in_quiet_hours(moment: _dt.datetime, start: tuple[int, int], end: tuple[int, int]) -> bool:
    """判断是否处于静默时段。支持跨零点（如 23:30 → 08:00）。

    起止相同视为全天静默——这是用户显式表达「别说话」的方式。
    """
    minutes = moment.hour * 60 + moment.minute
    begin = start[0] * 60 + start[1]
    finish = end[0] * 60 + end[1]
    if begin == finish:
        return True
    if begin < finish:
        return begin <= minutes < finish
    return minutes >= begin or minutes < finish


def humanize_delta(seconds: float) -> str:
    """把秒差渲染成「3 小时 12 分」这样的短文案。"""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时" if hours else f"{days} 天"
