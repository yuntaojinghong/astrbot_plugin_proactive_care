"""插件集成测试：加载、消息记录、匿名隔离、上下文注入、指令。

这一组测试用的是完整插件实例（带 AstrBot 桩），所以能覆盖模块之间的接线问题。
"""

from __future__ import annotations

import asyncio
import types

from astrbot.api.event import AstrMessageEvent

UMO = "aiocqhttp:GroupMessage:1001"


def run(coro):
    return asyncio.run(coro)


async def collect(agen):
    """把异步生成器（指令 handler）的产出收集成列表。"""
    return [item async for item in agen]


def make_event(**kwargs):
    kwargs.setdefault("umo", UMO)
    kwargs.setdefault("group_id", "1001")
    kwargs.setdefault("self_id", "777")
    return AstrMessageEvent(**kwargs)


# ======================================================================
#  加载
# ======================================================================
def test_plugin_loads_and_wires_everything(plugin):
    instance, context = plugin
    assert instance.store is not None
    assert instance.scheduler is not None
    assert instance.memory is not None
    assert instance.generator is not None
    assert instance.cfg.group_allowed("1001") is True


def test_web_routes_are_registered_with_plugin_prefix(plugin):
    _, context = plugin
    assert len(context.routes) >= 20
    for route, _handler, methods, _desc in context.routes:
        assert route.startswith("/astrbot_plugin_proactive_care/")
        assert methods


def test_group_not_in_whitelist_is_ignored(plugin):
    instance, _ = plugin
    raw = instance.cfg.as_dict()
    raw["basic"]["group_whitelist"] = ["2002"]
    instance.reload_runtime_config(raw)

    async def scenario():
        await instance.on_message(make_event(text="随便说点什么内容"))
        return await instance.store.count_messages(UMO)

    assert run(scenario()) == 0


# ======================================================================
#  消息记录
# ======================================================================
def test_normal_group_message_is_recorded(plugin):
    instance, _ = plugin

    async def scenario():
        await instance.on_message(make_event(text="今天的进度同步到文档了", sender_id="123", sender_name="小李"))
        session = await instance.store.get_session(UMO)
        messages = await instance.store.recent_messages(UMO)
        return session, messages

    session, messages = run(scenario())
    assert session is not None
    assert session["scope"] == "group"
    assert session["target_id"] == "1001"
    assert session["last_human_ts"] > 0
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["sender_name"] == "小李"


def test_human_message_clears_awaiting_and_streak(plugin):
    instance, _ = plugin

    async def scenario():
        await instance.store.ensure_session(UMO, "group", "1001")
        await instance.store.update_session(
            UMO, awaiting_reply=1, unanswered_streak=2, last_proactive_ts=1.0, pending_question=1
        )
        await instance.store.add_history(UMO, "idle", "静默", "在忙吗", sent=True)
        await instance.on_message(make_event(text="在的，刚看到", sender_id="123", sender_name="小李"))
        session = await instance.store.get_session(UMO)
        history = await instance.store.list_history(UMO)
        return session, history

    session, history = run(scenario())
    assert session["awaiting_reply"] == 0
    assert session["unanswered_streak"] == 0
    assert session["pending_question"] == 0
    assert history[0]["answered"] == 1


# ======================================================================
#  匿名树洞隔离（重点）
# ======================================================================
def test_robot_forwarded_message_does_not_reset_idle_timer(plugin):
    """树洞帖是机器人自己转发的，绝不能算成「群友在聊天」。

    如果这里判断错了，树洞每发一条帖就会把空闲计时器清零，
    微光的空闲唤醒将永远触发不了——这正是最典型的插件冲突。
    """
    instance, _ = plugin

    async def scenario():
        await instance.on_message(make_event(text="【番茄】今天心情不太好", sender_id="777", sender_name="微光"))
        return await instance.store.get_session(UMO), await instance.store.count_messages(UMO)

    session, count = run(scenario())
    assert session is None, "机器人自己转发的消息不该创建会话"
    assert count == 0


def test_nickname_pool_messages_are_skipped(plugin):
    instance, _ = plugin
    raw = instance.cfg.as_dict()
    raw["isolation"]["ignore_nicknames"] = ["番茄", "苹果", "橘子"]
    instance.reload_runtime_config(raw)

    async def scenario():
        await instance.on_message(make_event(text="今天不太开心", sender_id="9001", sender_name="苹果"))
        await instance.on_message(make_event(text="正常聊一句内容", sender_id="123", sender_name="小李"))
        return await instance.store.count_messages(UMO)

    assert run(scenario()) == 1


def test_anonymous_posts_never_reach_memory(plugin):
    """转述内容不能进记忆库——这是反匿名化的最后一道防线。"""
    instance, _ = plugin
    raw = instance.cfg.as_dict()
    raw["isolation"]["ignore_nicknames"] = ["番茄"]
    instance.reload_runtime_config(raw)

    async def scenario():
        await instance.store.ensure_session(UMO, "group", "1001")
        await instance.on_message(make_event(text="【番茄】我把老板骂了一顿", sender_id="777", sender_name="微光"))
        await instance.on_message(make_event(text="有人骂了老板", sender_id="9001", sender_name="番茄"))
        return await instance.store.count_messages(UMO)

    assert run(scenario()) == 0


def test_isolated_recording_leaves_other_plugins_alone(plugin):
    """记录逻辑不应拦截事件传播，否则会影响匿名树洞等其它插件。"""
    instance, _ = plugin

    async def scenario():
        event = make_event(text="普通群聊内容", sender_id="123", sender_name="小李")
        await instance.on_message(event)
        return event.stopped

    assert run(scenario()) is False


# ======================================================================
#  机器人自己的回复
# ======================================================================
def test_after_message_sent_records_bot_reply_and_question(plugin):
    instance, _ = plugin

    async def scenario():
        await instance.store.ensure_session(UMO, "group", "1001")
        result = types.SimpleNamespace(get_plain_text=lambda: "大家这周末有空吗？")
        await instance.on_message_sent(make_event(result=result))
        session = await instance.store.get_session(UMO)
        messages = await instance.store.recent_messages(UMO)
        return session, messages

    session, messages = run(scenario())
    assert messages[-1]["role"] == "bot"
    assert messages[-1]["content"] == "大家这周末有空吗？"
    assert session["pending_question"] == 1
    assert session["pending_question_ts"] > 0


def test_after_message_sent_extracts_text_from_chain(plugin):
    instance, _ = plugin

    async def scenario():
        await instance.store.ensure_session(UMO, "group", "1001")
        result = types.SimpleNamespace(chain=[types.SimpleNamespace(text="从消息链里取出来的内容")])
        await instance.on_message_sent(make_event(result=result))
        return await instance.store.recent_messages(UMO)

    messages = run(scenario())
    assert messages[-1]["content"] == "从消息链里取出来的内容"


def test_after_message_sent_ignores_empty_result(plugin):
    instance, _ = plugin

    async def scenario():
        await instance.store.ensure_session(UMO, "group", "1001")
        await instance.on_message_sent(make_event(result=None))
        return await instance.store.count_messages(UMO)

    assert run(scenario()) == 0


# ======================================================================
#  上下文注入
# ======================================================================
def test_proactive_message_is_injected_into_llm_context(plugin):
    """主动消息补进上下文，群友回一句「什么？」机器人才不会一脸茫然。"""
    instance, _ = plugin

    async def scenario():
        await instance.store.ensure_session(UMO, "group", "1001")
        await instance.store.add_history(UMO, "idle", "静默太久", "大家晚上好呀", sent=True)
        req = types.SimpleNamespace(contexts=[{"role": "user", "content": [{"type": "text", "text": "什么？"}]}])
        await instance.on_llm_request(make_event(), req)
        return req.contexts

    contexts = run(scenario())
    assert len(contexts) == 2
    assert contexts[-1]["role"] == "assistant"
    assert "大家晚上好呀" in contexts[-1]["content"][0]["text"]


def test_injection_is_not_repeated(plugin):
    instance, _ = plugin

    async def scenario():
        await instance.store.ensure_session(UMO, "group", "1001")
        await instance.store.add_history(UMO, "idle", "静默太久", "大家晚上好呀", sent=True)
        req = types.SimpleNamespace(
            contexts=[
                {"role": "user", "content": [{"type": "text", "text": "什么？"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "大家晚上好呀"}]},
            ]
        )
        await instance.on_llm_request(make_event(), req)
        return req.contexts

    assert len(run(scenario())) == 2


def test_injection_skipped_when_stale(plugin):
    instance, _ = plugin

    async def scenario():
        await instance.store.ensure_session(UMO, "group", "1001")
        await instance.store.add_history(UMO, "idle", "静默太久", "很久以前说过的话", sent=True, ts=1.0)
        req = types.SimpleNamespace(contexts=[])
        await instance.on_llm_request(make_event(), req)
        return req.contexts

    assert run(scenario()) == []


def test_injection_can_be_disabled(plugin):
    instance, _ = plugin
    raw = instance.cfg.as_dict()
    raw["context"]["inject_into_context"] = False
    instance.reload_runtime_config(raw)

    async def scenario():
        await instance.store.ensure_session(UMO, "group", "1001")
        await instance.store.add_history(UMO, "idle", "静默", "说过的话", sent=True)
        req = types.SimpleNamespace(contexts=[])
        await instance.on_llm_request(make_event(), req)
        return req.contexts

    assert run(scenario()) == []


def test_injection_never_breaks_on_odd_contexts(plugin):
    """宿主格式变化时应当静默跳过，而不是把请求搞崩。"""
    instance, _ = plugin

    async def scenario():
        await instance.store.ensure_session(UMO, "group", "1001")
        await instance.store.add_history(UMO, "idle", "静默", "说过的话", sent=True)
        req = types.SimpleNamespace(contexts="这不是列表")
        await instance.on_llm_request(make_event(), req)
        return req.contexts

    assert run(scenario()) == "这不是列表"


# ======================================================================
#  指令
# ======================================================================
def test_status_command_reports_state(plugin):
    instance, _ = plugin

    async def scenario():
        await instance.store.ensure_session(UMO, "group", "1001")
        await instance.store.update_session(UMO, last_human_ts=100.0)
        return [item.text for item in await collect(instance.cmd_proactive(make_event(), "状态"))]

    output = "\n".join(run(scenario()))
    assert "微光 · 主动关怀" in output
    assert "总开关" in output
    assert "调度器" in output
    assert "下次可能开口" in output


def test_toggle_commands_persist(plugin):
    instance, _ = plugin

    async def scenario():
        await collect(instance.cmd_proactive(make_event(), "关闭"))
        return instance.cfg.enabled

    assert run(scenario()) is False
    assert instance._raw_config.get("basic", {}).get("enabled") is False


def test_probability_command_persists_and_enables(plugin):
    """`/主动 概率 40` 要写回 AstrBot 的配置对象，并顺手把即时搭话打开。"""
    instance, _ = plugin

    async def scenario():
        shown = [item.text for item in await collect(instance.cmd_proactive(make_event(), "概率"))]
        applied = [item.text for item in await collect(instance.cmd_proactive(make_event(), "概率 40"))]
        return shown, applied

    shown, applied = run(scenario())
    assert "概率" in shown[0]
    assert "40%" in applied[0]
    assert instance.cfg.instant_probability_percent == 40
    assert instance.cfg.instant_enable is True, "设了概率就应当顺带开启"
    assert instance._raw_config.get("trigger", {}).get("instant_probability") == 40


def test_probability_command_rejects_out_of_range(plugin):
    instance, _ = plugin

    async def scenario():
        return [
            [item.text for item in await collect(instance.cmd_proactive(make_event(), "概率 200"))],
            [item.text for item in await collect(instance.cmd_proactive(make_event(), "概率 abc"))],
        ]

    too_big, not_a_number = run(scenario())
    assert "0~100" in too_big[0]
    assert "0~100" in not_a_number[0]
    assert instance.cfg.instant_probability_percent != 200


def test_pause_and_resume(plugin):
    instance, _ = plugin

    async def scenario():
        await collect(instance.cmd_proactive(make_event(), "暂停"))
        paused = await instance.store.get_session(UMO)
        await collect(instance.cmd_proactive(make_event(), "恢复"))
        resumed = await instance.store.get_session(UMO)
        return paused, resumed

    paused, resumed = run(scenario())
    assert paused["paused"] == 1
    assert resumed["paused"] == 0


def test_memory_commands(plugin):
    instance, _ = plugin

    async def scenario():
        await instance.store.ensure_session(UMO, "group", "1001")
        added = [item.text for item in await collect(instance.cmd_memory(make_event(), "添加 小王喜欢喝美式"))]
        listed = [item.text for item in await collect(instance.cmd_memory(make_event(), "列表"))]
        searched = [item.text for item in await collect(instance.cmd_memory(make_event(), "搜索 美式"))]
        await collect(instance.cmd_memory(make_event(), "清空"))
        cleared = [item.text for item in await collect(instance.cmd_memory(make_event(), "列表"))]
        return added, listed, searched, cleared

    added, listed, searched, cleared = run(scenario())
    assert "已记住" in added[0]
    assert "小王喜欢喝美式" in listed[0]
    assert "小王喜欢喝美式" in searched[0]
    assert "还没有任何记忆" in cleared[0]


def test_panel_sets_session_instant_probability(plugin):
    """面板给单个群设概率，留空则回到跟随全局。"""
    instance, _ = plugin
    from astrbot_plugin_proactive_care.web.service import PanelService

    service = PanelService(instance)

    async def scenario():
        await instance.store.ensure_session(UMO, "group", "1001")
        await service.update_session(UMO, {"instant_probability": 65})
        custom = await service.get_session(UMO)
        await service.update_session(UMO, {"instant_probability": None})
        cleared = await service.get_session(UMO)
        return custom, cleared

    custom, cleared = run(scenario())
    assert abs(custom["instant_probability"] - 65) < 1e-6
    assert custom["instant_probability"] != custom["instant_probability_global"]
    assert abs(cleared["instant_probability"] - cleared["instant_probability_global"]) < 1e-6


def test_panel_rejects_bad_probability(plugin):
    instance, _ = plugin
    from astrbot_plugin_proactive_care.web.service import PanelService

    service = PanelService(instance)

    async def scenario():
        await instance.store.ensure_session(UMO, "group", "1001")
        errors = []
        for bad in (200, -5, "不是数字"):
            try:
                await service.update_session(UMO, {"instant_probability": bad})
            except ValueError as exc:
                errors.append(str(exc))
        return errors

    assert len(run(scenario())) == 3


def test_unknown_subcommand_prints_usage(plugin):
    instance, _ = plugin

    async def scenario():
        return [item.text for item in await collect(instance.cmd_proactive(make_event(), "乱输入"))]

    assert "用法" in run(scenario())[0]


# ======================================================================
#  发送
# ======================================================================
def test_send_builds_message_chain(plugin):
    instance, context = plugin

    async def scenario():
        return await instance._send(UMO, "测试一句话")

    assert run(scenario()) is True
    assert context.sent[0][0] == UMO
    assert "测试一句话" in str(context.sent[0][1])


def test_preview_does_not_send(plugin):
    instance, context = plugin
    instance.llm.chat = _fake_chat("这是预览生成的内容")

    async def scenario():
        await instance.store.ensure_session(UMO, "group", "1001")
        session = await instance.store.get_session(UMO)
        return await instance.generator.generate(session, "preview", "预览")

    outcome = run(scenario())
    assert outcome["ok"] is True
    assert context.sent == []  # 预览绝不发送


def _fake_chat(reply):
    async def chat(prompt="", system_prompt="", umo=None, temperature=None):
        return reply

    return chat


# ======================================================================
#  生命周期
# ======================================================================
def test_scheduler_start_stop(plugin):
    instance, _ = plugin

    async def scenario():
        await instance._boot()
        running = instance.scheduler.running
        await instance.scheduler.stop()
        return running, instance.scheduler.running

    running, stopped = run(scenario())
    assert running is True
    assert stopped is False
