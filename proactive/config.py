"""配置读取。

默认值不在这里手写一遍，而是直接从 ``_conf_schema.json`` 里递归抽取
``default`` 字段——这样 schema 与代码永远不会各自漂移。
若 schema 缺失（例如脱离 AstrBot 的单元测试环境），退化为空默认值。
"""

from __future__ import annotations

import copy
import json
import os
import re
from typing import Any

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_conf_schema.json")

# 兜底默认值：仅当 _conf_schema.json 读不到时才使用
_FALLBACK: dict[str, Any] = {
    "basic": {"enabled": False, "group_whitelist": [], "enable_private": False, "private_whitelist": []},
    "isolation": {
        "ignore_self_sent": True,
        "ignore_nicknames": [],
        "ignore_patterns": [],
        "ignore_builtin": True,
        "ignore_commands": True,
    },
    "context": {"context_messages": 20, "context_max_age_minutes": 720, "inject_into_context": True},
    "trigger": {
        "idle_enable": True,
        "idle_minutes": 180,
        "followup_enable": True,
        "followup_minutes": 30,
        "random_enable": True,
        "random_min_per_day": 1,
        "random_max_per_day": 2,
        "schedule_enable": False,
        "instant_enable": False,
        "instant_probability": 25,
        "instant_delay_min": 2,
        "instant_delay_max": 8,
        "instant_cooldown_seconds": 300,
    },
    "guard": {
        "quiet_start": "23:30",
        "quiet_end": "08:00",
        "max_per_day": 3,
        "cooldown_minutes": 120,
        "min_human_gap_minutes": 20,
        "reply_window_minutes": 15,
        "unanswered_pause": 3,
    },
    "memory": {"enable": True, "extract_every": 30, "max_inject": 6, "decay_days": 30, "auto_cleanup": False},
    "llm": {"provider_id": "", "style": "", "max_chars": 80, "temperature": 0.9},
    "advanced": {"tick_seconds": 30, "dry_run": False, "log_level": "info"},
}


def _walk_defaults(node: Any) -> Any:
    """递归把 schema 节点折叠成「同结构的默认值」。"""
    if not isinstance(node, dict):
        return None
    if node.get("type") == "object":
        items = node.get("items") or {}
        return {key: _walk_defaults(sub) for key, sub in items.items()}
    return copy.deepcopy(node.get("default"))


def schema_defaults(path: str = SCHEMA_PATH) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            schema = json.load(fh)
    except Exception:
        return copy.deepcopy(_FALLBACK)
    if not isinstance(schema, dict):
        return copy.deepcopy(_FALLBACK)
    built = {key: _walk_defaults(node) for key, node in schema.items()}
    # schema 里新增了分组但代码还没跟上时也不至于 KeyError
    for key, value in _FALLBACK.items():
        built.setdefault(key, copy.deepcopy(value))
    return built


def deep_merge(base: dict, override: dict) -> dict:
    """把 override 合并进 base 的副本，dict 逐层下潜，其余类型直接覆盖。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(value: Any, fallback: tuple[int, int]) -> tuple[int, int]:
    """把 ``HH:MM`` 解析成 (时, 分)。非法输入回退到 fallback。"""
    if isinstance(value, str):
        matched = _TIME_RE.match(value.strip())
        if matched:
            return int(matched.group(1)), int(matched.group(2))
    return fallback


def _to_int(value: Any, default: int, low: int | None = None, high: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if low is not None:
        number = max(low, number)
    if high is not None:
        number = min(high, number)
    return number


def _to_float(value: Any, default: float, low: float | None = None, high: float | None = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if low is not None:
        number = max(low, number)
    if high is not None:
        number = min(high, number)
    return number


def _to_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = re.split(r"[,，\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        return []
    return [part.strip() for part in parts if part and part.strip()]


class Config:
    """对合并后的配置做只读封装，并提供带类型与范围约束的取值接口。"""

    def __init__(self, raw: dict | None = None):
        self.raw = deep_merge(schema_defaults(), raw or {})
        self.warnings: list[str] = []
        self._validate()

    # ---------- 原始取值 ----------
    def section(self, name: str) -> dict:
        value = self.raw.get(name)
        return value if isinstance(value, dict) else {}

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if node is not None else default

    # ---------- 基础 ----------
    @property
    def enabled(self) -> bool:
        return bool(self.section("basic").get("enabled"))

    @property
    def dry_run(self) -> bool:
        return bool(self.section("advanced").get("dry_run"))

    @property
    def enable_private(self) -> bool:
        return bool(self.section("basic").get("enable_private"))

    @property
    def group_whitelist(self) -> list[str]:
        return _to_str_list(self.section("basic").get("group_whitelist"))

    @property
    def private_whitelist(self) -> list[str]:
        return _to_str_list(self.section("basic").get("private_whitelist"))

    def group_allowed(self, group_id: str) -> bool:
        allowed = self.group_whitelist
        if not allowed:
            return False
        if "all" in [item.lower() for item in allowed]:
            return True
        return str(group_id) in allowed

    def private_allowed(self, user_id: str) -> bool:
        if not self.enable_private:
            return False
        allowed = self.private_whitelist
        if not allowed:
            return True
        return str(user_id) in allowed

    # ---------- 隔离 ----------
    @property
    def ignore_self_sent(self) -> bool:
        return bool(self.section("isolation").get("ignore_self_sent"))

    @property
    def ignore_builtin(self) -> bool:
        return bool(self.section("isolation").get("ignore_builtin"))

    @property
    def ignore_commands(self) -> bool:
        return bool(self.section("isolation").get("ignore_commands"))

    @property
    def ignore_nicknames(self) -> list[str]:
        return _to_str_list(self.section("isolation").get("ignore_nicknames"))

    @property
    def ignore_patterns(self) -> list[str]:
        return _to_str_list(self.section("isolation").get("ignore_patterns"))

    # ---------- 上下文 ----------
    @property
    def context_messages(self) -> int:
        return _to_int(self.section("context").get("context_messages"), 20, 2, 200)

    @property
    def context_max_age_minutes(self) -> int:
        return _to_int(self.section("context").get("context_max_age_minutes"), 720, 1)

    @property
    def inject_into_context(self) -> bool:
        return bool(self.section("context").get("inject_into_context"))

    # ---------- 触发 ----------
    @property
    def idle_enable(self) -> bool:
        return bool(self.section("trigger").get("idle_enable"))

    @property
    def idle_minutes(self) -> int:
        return _to_int(self.section("trigger").get("idle_minutes"), 180, 1)

    @property
    def followup_enable(self) -> bool:
        return bool(self.section("trigger").get("followup_enable"))

    @property
    def followup_minutes(self) -> int:
        return _to_int(self.section("trigger").get("followup_minutes"), 30, 1)

    @property
    def random_enable(self) -> bool:
        return bool(self.section("trigger").get("random_enable"))

    @property
    def random_range(self) -> tuple[int, int]:
        low = _to_int(self.section("trigger").get("random_min_per_day"), 1, 0, 20)
        high = _to_int(self.section("trigger").get("random_max_per_day"), 2, 0, 20)
        if low > high:
            low, high = high, low
        return low, high

    @property
    def schedule_enable(self) -> bool:
        return bool(self.section("trigger").get("schedule_enable"))

    # ---------- 即时搭话 ----------
    @property
    def instant_enable(self) -> bool:
        return bool(self.section("trigger").get("instant_enable"))

    @property
    def instant_probability(self) -> float:
        """每条群友消息的回复概率，0.0 ~ 1.0。"""
        return _to_int(self.section("trigger").get("instant_probability"), 25, 0, 100) / 100.0

    @property
    def instant_probability_percent(self) -> int:
        return _to_int(self.section("trigger").get("instant_probability"), 25, 0, 100)

    @property
    def instant_delay(self) -> tuple[float, float]:
        """回复前的随机延迟区间（秒）。"""
        low = _to_int(self.section("trigger").get("instant_delay_min"), 2, 0, 600)
        high = _to_int(self.section("trigger").get("instant_delay_max"), 8, 0, 600)
        if high < low:
            low, high = high, low
        return float(low), float(high)

    @property
    def instant_cooldown_seconds(self) -> int:
        return _to_int(self.section("trigger").get("instant_cooldown_seconds"), 300, 0, 86400)

    def instant_probability_for(self, session: dict | None) -> float:
        """会话级概率优先，没设才用全局值。"""
        override = (session or {}).get("override_json")
        if isinstance(override, str):
            try:
                override = json.loads(override or "{}")
            except (TypeError, ValueError):
                override = {}
        if isinstance(override, dict):
            raw = override.get("instant_probability")
            if raw is not None and str(raw).strip() != "":
                try:
                    value = int(float(raw))
                except (TypeError, ValueError):
                    return self.instant_probability
                return max(0, min(100, value)) / 100.0
        return self.instant_probability

    # ---------- 防骚扰 ----------
    @property
    def quiet_range(self) -> tuple[tuple[int, int], tuple[int, int]]:
        guard = self.section("guard")
        start = parse_hhmm(guard.get("quiet_start"), (23, 30))
        end = parse_hhmm(guard.get("quiet_end"), (8, 0))
        return start, end

    @property
    def max_per_day(self) -> int:
        return _to_int(self.section("guard").get("max_per_day"), 3, 1, 100)

    @property
    def cooldown_minutes(self) -> int:
        return _to_int(self.section("guard").get("cooldown_minutes"), 120, 0)

    @property
    def min_human_gap_minutes(self) -> int:
        return _to_int(self.section("guard").get("min_human_gap_minutes"), 20, 0)

    @property
    def reply_window_minutes(self) -> int:
        return _to_int(self.section("guard").get("reply_window_minutes"), 15, 1)

    @property
    def unanswered_pause(self) -> int:
        return _to_int(self.section("guard").get("unanswered_pause"), 3, 1)

    # ---------- 记忆 ----------
    @property
    def memory_enable(self) -> bool:
        return bool(self.section("memory").get("enable"))

    @property
    def extract_every(self) -> int:
        return _to_int(self.section("memory").get("extract_every"), 30, 1)

    @property
    def max_inject(self) -> int:
        return _to_int(self.section("memory").get("max_inject"), 6, 0, 50)

    @property
    def decay_days(self) -> int:
        return _to_int(self.section("memory").get("decay_days"), 30, 0)

    @property
    def auto_cleanup(self) -> bool:
        return bool(self.section("memory").get("auto_cleanup"))

    # ---------- 模型 ----------
    @property
    def provider_id(self) -> str:
        return str(self.section("llm").get("provider_id") or "").strip()

    @property
    def style(self) -> str:
        value = str(self.section("llm").get("style") or "").strip()
        return value or _FALLBACK["llm"]["style"]

    @property
    def max_chars(self) -> int:
        return _to_int(self.section("llm").get("max_chars"), 80, 5, 2000)

    @property
    def temperature(self) -> float:
        return _to_float(self.section("llm").get("temperature"), 0.9, 0.0, 2.0)

    # ---------- 高级 ----------
    @property
    def tick_seconds(self) -> int:
        return _to_int(self.section("advanced").get("tick_seconds"), 30, 10, 600)

    # ---------- 校验 ----------
    def _validate(self) -> None:
        if self.enabled and not self.group_whitelist and not self.enable_private:
            self.warnings.append(
                "总开关已打开，但「生效群号列表」为空且未开启私聊，插件不会在任何地方发消息。"
                "请至少填写一个群号，或填 all 表示全部群。"
            )
        start, end = self.quiet_range
        if start == end:
            self.warnings.append("静默时段的开始与结束时间相同，将被视为全天静默，不会有任何主动消息。")
        low, high = self.random_range
        if self.random_enable and high == 0:
            self.warnings.append("随机关怀的每日最多次数为 0，该触发方式实际不会生效。")
        if low > high:
            self.warnings.append("随机关怀的最少次数大于最多次数，已自动交换。")
        idle = self.idle_minutes
        gap = self.min_human_gap_minutes
        if self.idle_enable and idle <= gap:
            self.warnings.append(
                f"空闲唤醒阈值（{idle} 分钟）不大于「距最后一条真人消息的最短间隔」（{gap} 分钟），"
                "空闲唤醒很难有机会触发。"
            )
        if self.instant_enable and self.instant_probability_percent == 0:
            self.warnings.append("即时搭话已启用但概率为 0%，实际不会触发。")
        if self.instant_enable and self.instant_probability_percent == 100:
            self.warnings.append("即时搭话概率为 100%，每条消息都会回，群里可能会很吵。")
        low_delay, high_delay = self.instant_delay
        if self.instant_enable and high_delay > 120:
            self.warnings.append("即时搭话的最长延迟超过 120 秒，回复可能明显滞后于对话。")
        for pattern in self.ignore_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                self.warnings.append(f"忽略正则 `{pattern}` 无法编译，已跳过：{exc}")

    def as_dict(self) -> dict:
        return copy.deepcopy(self.raw)
