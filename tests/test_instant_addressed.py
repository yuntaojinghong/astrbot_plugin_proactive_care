"""@机器人 时不该再补一句「即时搭话」。

背景（用户实际遇到的现象）：@机器人 之后，正常管线先回了一条（机器人自己的人设），
2~8 秒后即时搭话又补了一条——而即时搭话用的是 generator 里那套「群友人设」
提示词（"你是长期活跃在这个群里的一个真人风格群友"），口吻完全不同。
于是用户看到的是「一个 @ 换来两种语气」，像是机器人突然换了个人说话。

根因：``_record_incoming`` 对所有消息一律掷骰子，不区分这条消息是不是在跟机器人说话。

这里盯死三件事：
1. 唤醒消息（@机器人）不触发即时搭话
2. 机器人刚回过话的静默窗口内不触发
3. 机器人没刚回过话的普通群聊照旧能触发（别把功能修死）
"""

from __future__ import annotations

import asyncio

import pytest

from proactive.textutil import now_ts

UMO = "aiocqhttp:GroupMessage:1001"

INSTANT_ON = {
    "basic": {"enabled": True, "group_whitelist": ["all"]},
    "guard": {"cooldown_minutes": 0, "max_per_day": 50, "min_human_gap_minutes": 0},
    "trigger": {
        "instant_enable": True,
        "instant_probability": 100,
        "instant_delay_min": 0,
        "instant_delay_max": 0,
        "instant_cooldown_seconds": 0,
    },
}


@pytest.fixture()
def pe(tmp_path, monkeypatch):
    """带可用 provider 的插件实例，并解除静默时段。

    注意必须用**包路径**导入 scheduler：插件自己是这么加载的，
    用 ``import proactive.scheduler`` 会拿到另一个模块对象，补丁打不上去。
    """
    import importlib
    import sys
    import types

    path_mod = sys.modules["astrbot.core.utils.astrbot_path"]
    monkeypatch.setattr(path_mod, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    monkeypatch.setattr(path_mod, "get_astrbot_data_path", lambda: str(tmp_path))

    sched = importlib.import_module("astrbot_plugin_proactive_care.proactive.scheduler")
    monkeypatch.setattr(sched, "in_quiet_hours", lambda *a, **k: False)

    from astrbot.api.star import Context

    plugin_main = importlib.import_module("astrbot_plugin_proactive_care.main")
    # 延迟归零、概率必中，测试才跑得动
    monkeypatch.setattr(plugin_main.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(sched.random, "random", lambda: 0.0)

    class Provider:
        def __init__(self):
            self.calls = []

        async def text_chat(self, prompt="", system_prompt="", **kw):
            self.calls.append(system_prompt)
            return types.SimpleNamespace(completion_text="哈哈我也这么觉得")

    context = Context()
    provider = Provider()
    context.providers = [provider]

    plugin = plugin_main.ProactiveCarePlugin(context, INSTANT_ON)
    yield plugin, context, provider, plugin_main
    plugin.store.close()


def make_event(plugin_main, **kw):
    return plugin_main.AstrMessageEvent(
        umo=UMO, group_id="1001", sender_id="2001", sender_name="张三", self_id="9999", **kw
    )


async def settle(plugin):
    """等即时搭话的延迟任务跑完。

    兼容修复前后：修复前没有 ``_instant_tasks`` 这个强引用集合，
    就退回去按任务名扫当前事件循环里的任务。
    """
    pending = list(getattr(plugin, "_instant_tasks", ()) or ())
    if not pending:
        pending = [t for t in asyncio.all_tasks() if "proactive-instant" in (t.get_name() or "")]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)


def run(coro):
    return asyncio.run(coro)


def test_wake_message_does_not_trigger_instant(pe):
    """@机器人（唤醒）不该触发即时搭话——否则一次 @ 会得到两种语气。"""
    plugin, context, provider, plugin_main = pe

    async def scenario():
        await plugin.store.ensure_session(UMO, "group", "1001", enabled=True)
        event = make_event(plugin_main, text="@微光 你今天怎么这么安静", wake=True)
        assert event.is_wake_up() is True, "桩事件要能正确表达「唤醒」"
        await plugin.on_message(event)
        await settle(plugin)

    run(scenario())
    assert context.sent == [], f"唤醒消息不该再补一句，实际发了 {context.sent}"
    assert provider.calls == [], "唤醒消息不该调用即时搭话的生成器"


def test_recent_bot_reply_suppresses_instant(pe):
    """机器人刚用正常管线回过话时，即时搭话要让路。"""
    plugin, context, provider, plugin_main = pe

    async def scenario():
        await plugin.store.ensure_session(UMO, "group", "1001", enabled=True)
        # 模拟正常管线刚刚回复完（after_message_sent 会写 last_bot_ts）
        await plugin.store.update_session(UMO, last_bot_ts=now_ts())
        event = make_event(plugin_main, text="那你刚才干嘛去了", wake=False)
        await plugin.on_message(event)
        await settle(plugin)

    run(scenario())
    assert context.sent == [], f"刚回过话还被搭腔了：{context.sent}"
    assert provider.calls == []


def test_plain_chatter_still_triggers_instant(pe):
    """普通群聊（没人跟机器人说话、它也没刚回过话）照旧能搭一句。"""
    plugin, context, provider, plugin_main = pe

    async def scenario():
        await plugin.store.ensure_session(UMO, "group", "1001", enabled=True)
        event = make_event(plugin_main, text="今天中午吃啥好呢", wake=False)
        await plugin.on_message(event)
        await settle(plugin)

    run(scenario())
    assert len(context.sent) == 1, f"普通群聊应该能即时搭话，实际 {context.sent}"
    assert len(provider.calls) >= 1


def test_guard_window_is_configurable(cfg):
    """静默窗口可配置，0 表示关掉这道保护。"""
    config = cfg({"trigger": {"instant_reply_guard_seconds": 120}})
    assert config.instant_reply_guard_seconds == 120
    assert cfg({"trigger": {"instant_reply_guard_seconds": 0}}).instant_reply_guard_seconds == 0
    # 默认 90 秒，越界要夹住
    assert cfg({}).instant_reply_guard_seconds == 90
    assert cfg({"trigger": {"instant_reply_guard_seconds": 99999}}).instant_reply_guard_seconds == 3600
