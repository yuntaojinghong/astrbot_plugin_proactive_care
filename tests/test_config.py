"""配置、文本工具与消息隔离规则的测试。"""

from __future__ import annotations

import datetime as _dt

import pytest

from proactive.config import Config
from proactive.filters import IgnoreRule
from proactive.textutil import (
    humanize_delta,
    in_quiet_hours,
    normalize,
    similarity,
    strip_markers,
    truncate,
)


# ======================================================================
#  配置
# ======================================================================
def test_defaults_come_from_schema():
    """默认值直接从 _conf_schema.json 抽取，保证 schema 与代码不漂移。"""
    cfg = Config({})
    assert cfg.enabled is False
    assert cfg.group_whitelist == []
    assert cfg.tick_seconds == 30
    assert cfg.idle_minutes == 180
    assert cfg.max_per_day == 3
    assert cfg.quiet_range == ((23, 30), (8, 0))
    assert cfg.ignore_self_sent is True


def test_partial_override_keeps_sibling_keys():
    """只改一个子项时，同级的其它默认值不能被冲掉。"""
    cfg = Config({"trigger": {"idle_minutes": 45}})
    assert cfg.idle_minutes == 45
    assert cfg.followup_minutes == 30  # 仍然来自 schema 默认值
    assert cfg.random_range == (1, 2)


def test_unknown_keys_do_not_break():
    cfg = Config({"basic": {"enabled": True}, "不存在的分组": {"x": 1}})
    assert cfg.enabled is True


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("08:05", (8, 5)),
        ("23:59", (23, 59)),
        ("0:00", (0, 0)),
        ("25:00", (23, 30)),  # 非法 -> 回退默认
        ("8点", (23, 30)),
        ("", (23, 30)),
        (None, (23, 30)),
    ],
)
def test_quiet_range_parsing(raw, expected):
    cfg = Config({"guard": {"quiet_start": raw}})
    assert cfg.quiet_range[0] == expected


def test_group_allowed_semantics():
    assert Config({}).group_allowed("1001") is False  # 空名单 = 不生效
    assert Config({"basic": {"group_whitelist": ["1001"]}}).group_allowed("1001") is True
    assert Config({"basic": {"group_whitelist": ["1001"]}}).group_allowed("1002") is False
    assert Config({"basic": {"group_whitelist": ["all"]}}).group_allowed("9999") is True


def test_private_requires_explicit_enable():
    cfg = Config({"basic": {"private_whitelist": ["42"]}})
    assert cfg.private_allowed("42") is False  # 没打开私聊开关
    cfg2 = Config({"basic": {"enable_private": True, "private_whitelist": ["42"]}})
    assert cfg2.private_allowed("42") is True
    assert cfg2.private_allowed("43") is False


def test_string_list_is_split():
    cfg = Config({"basic": {"group_whitelist": "1001, 1002 1003"}})
    assert cfg.group_whitelist == ["1001", "1002", "1003"]


def test_random_range_swaps_when_inverted():
    cfg = Config({"trigger": {"random_min_per_day": 5, "random_max_per_day": 2}})
    assert cfg.random_range == (2, 5)


def test_validation_warns_about_empty_scope():
    cfg = Config({"basic": {"enabled": True}})
    assert any("生效群号列表" in item for item in cfg.warnings)


def test_validation_warns_about_all_day_quiet():
    cfg = Config({"guard": {"quiet_start": "08:00", "quiet_end": "08:00"}})
    assert any("全天静默" in item for item in cfg.warnings)


def test_validation_reports_bad_regex():
    cfg = Config({"isolation": {"ignore_patterns": ["[unclosed"]}})
    assert any("无法编译" in item for item in cfg.warnings)


# ======================================================================
#  文本工具
# ======================================================================
def test_normalize_strips_noise():
    assert normalize(" 你  好，世界！ ") == "你好世界"
    assert normalize("ABC-def") == "abcdef"
    assert normalize("") == ""


def test_similarity_detects_near_duplicates():
    assert similarity("大家晚上好啊", "大家晚上好呀") > 0.8
    assert similarity("今天天气不错", "我们来聊聊项目进度") < 0.3
    assert similarity("", "") == 1.0
    assert similarity("有内容", "") == 0.0


def test_strip_markers_removes_prefix_and_quotes():
    assert strip_markers("微光：晚上好呀") == "晚上好呀"
    assert strip_markers("机器人: 「你好」") == "你好"
    assert strip_markers('"普通一句话"') == "普通一句话"
    assert strip_markers("  多  余   空格 ") == "多 余 空格"


def test_truncate_keeps_limit():
    assert len(truncate("一二三四五六七八九十", 5)) == 5
    assert truncate("短", 5) == "短"
    assert truncate("一二三四五", 0) == "一二三四五"


@pytest.mark.parametrize(
    "hour,expected",
    [
        (23, True), (0, True), (7, True), (8, False), (12, False), (23, True),
    ],
)
def test_in_quiet_hours_wraps_midnight(hour, expected):
    moment = _dt.datetime(2026, 1, 1, hour, 45)
    assert in_quiet_hours(moment, (23, 30), (8, 0)) is expected


def test_in_quiet_hours_same_start_end_means_silent_all_day():
    moment = _dt.datetime(2026, 1, 1, 12, 0)
    assert in_quiet_hours(moment, (8, 0), (8, 0)) is True


def test_humanize_delta():
    assert humanize_delta(30) == "30 秒"
    assert humanize_delta(120) == "2 分钟"
    assert humanize_delta(7200) == "2 小时"
    assert humanize_delta(90000) == "1 天 1 小时"


# ======================================================================
#  隔离规则（匿名树洞兼容）
# ======================================================================
def test_robot_own_message_is_ignored():
    """匿名树洞是靠机器人账号转发到群里的，这条规则把它们整体挡在外面。"""
    rules = IgnoreRule(Config({}))
    ignored, reason = rules.judge(
        text="【番茄】今天心情不太好", sender_id="777", sender_name="微光", self_id="777"
    )
    assert ignored is True
    assert "机器人" in reason


def test_anonymous_nickname_pool_is_ignored():
    rules = IgnoreRule(Config({"isolation": {"ignore_nicknames": ["番茄", "苹果"]}}))
    ignored, reason = rules.judge(
        text="今天心情不太好", sender_id="123", sender_name="番茄", self_id="777"
    )
    assert ignored is True
    assert "昵称池" in reason


def test_custom_pattern_is_ignored():
    rules = IgnoreRule(Config({"isolation": {"ignore_patterns": [r"^\[匿名\]"]}}))
    ignored, _ = rules.judge(text="[匿名] 说点什么", sender_id="1", sender_name="A", self_id="777")
    assert ignored is True


def test_builtin_transcript_format_is_ignored():
    rules = IgnoreRule(Config({}))
    ignored, reason = rules.judge(
        text="【橘子】老板今天又发火了", sender_id="1", sender_name="橘子", self_id="777"
    )
    assert ignored is True
    assert "转述" in reason


def test_builtin_does_not_misfire_on_announcements():
    """「【公告】」这类正文标题不应被误判成匿名转述。"""
    rules = IgnoreRule(Config({}))
    ignored, _ = rules.judge(
        text="【公告】本周六维护，请注意", sender_id="1", sender_name="管理员", self_id="777"
    )
    assert ignored is False


def test_command_message_is_ignored():
    rules = IgnoreRule(Config({}))
    ignored, reason = rules.judge(text="/签到", sender_id="1", sender_name="A", self_id="777")
    assert ignored is True
    assert "指令" in reason


def test_normal_group_message_passes():
    rules = IgnoreRule(Config({}))
    ignored, reason = rules.judge(
        text="今天的进度我已经同步到文档里了", sender_id="123", sender_name="小李", self_id="777"
    )
    assert ignored is False
    assert reason == ""


def test_ignore_switches_can_be_turned_off():
    rules = IgnoreRule(Config({"isolation": {"ignore_self_sent": False, "ignore_commands": False}}))
    assert rules.judge(text="hi", sender_id="777", sender_name="微光", self_id="777")[0] is False
    assert rules.judge(text="/help", sender_id="1", sender_name="A", self_id="777")[0] is False


def test_bad_regex_is_skipped_without_crashing():
    rules = IgnoreRule(Config({"isolation": {"ignore_patterns": ["[bad", r"^广告"]}}))
    assert rules.judge(text="广告来了", sender_id="1", sender_name="A", self_id="7")[0] is True
