"""两个真实事故的回归：

一、自言自语
群里只有机器人自己在说话，它却每隔一两分钟自动冒一句，形成单口相声。
根因：即时搭话没有排除「机器人自己发的消息」，也没要求「最近有真人说过话」。
      自己发的消息 → 又触发一次搭话 → 再说一句，闭环成立。

二、把 Python 对象当聊天内容发出去
群里出现了一条 `LLMResponse(role='assistant', result_chain=MessageChain(chain=[Pla…`。
根因：proactive/llm.py 在拿不到 completion_text 时退化成 `str(response)`，
      把响应对象的 repr 当成模型输出返回，随后被原样发进群。
"""

from __future__ import annotations

import asyncio
import types

import pytest

from proactive.llm import LLMClient
from proactive.textutil import now_ts

UMO = "aiocqhttp:GroupMessage:1001"

INSTANT_ON = {
    "basic": {"enabled": True, "group_whitelist": ["all"]},
    "guard": {"cooldown_minutes": 0, "max_per_day": 50, "min_human_gap_minutes": 20},
    "trigger": {
        "instant_enable": True,
        "instant_probability": 100,
        "instant_delay_min": 0,
        "instant_delay_max": 0,
        "instant_cooldown_seconds": 0,
    },
}

BOT_ID = "9999"
HUMAN_ID = "2001"


@pytest.fixture()
def pe(tmp_path, monkeypatch):
    import importlib
    import sys

    path_mod = sys.modules["astrbot.core.utils.astrbot_path"]
    monkeypatch.setattr(path_mod, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    monkeypatch.setattr(path_mod, "get_astrbot_data_path", lambda: str(tmp_path))

    sched = importlib.import_module("astrbot_plugin_proactive_care.proactive.scheduler")
    monkeypatch.setattr(sched, "in_quiet_hours", lambda *a, **k: False)

    from astrbot.api.star import Context

    plugin_main = importlib.import_module("astrbot_plugin_proactive_care.main")
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


def make_event(plugin_main, *, sender_id=HUMAN_ID, text="随便说点什么", **kw):
    return plugin_main.AstrMessageEvent(
        umo=UMO, group_id="1001", sender_id=sender_id, sender_name="张三",
        self_id=BOT_ID, text=text, **kw
    )


async def settle(plugin):
    pending = list(getattr(plugin, "_instant_tasks", ()) or ())
    if not pending:
        pending = [t for t in asyncio.all_tasks() if "proactive-instant" in (t.get_name() or "")]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)


def run(coro):
    return asyncio.run(coro)


# ======================================================================
#  一、自言自语
# ======================================================================
def test_own_message_never_triggers_instant(pe):
    """机器人自己发的消息不得触发即时搭话（否则形成闭环）。"""
    plugin, context, provider, plugin_main = pe

    async def scenario():
        await plugin.store.ensure_session(UMO, "group", "1001", enabled=True)
        await plugin.store.update_session(UMO, last_human_ts=now_ts())
        event = make_event(plugin_main, sender_id=BOT_ID, text="行吧，我自己吃")
        assert plugin._is_self_sent(event) is True
        await plugin.on_message(event)
        await settle(plugin)

    run(scenario())
    assert context.sent == [], f"机器人自己的消息触发了搭话：{context.sent}"
    assert provider.calls == []


def test_empty_group_does_not_trigger_instant(pe):
    """群里只剩机器人自己说话时，不该继续发言（否则就是定时自言自语）。

    真实场景：早上 8 点群里没人，机器人自己叨叨了半小时。
    这里模拟「机器人连发几条自己的消息」——这些消息既不能触发即时搭话，
    也不能把 last_human_ts 刷新成"刚刚有人说话"。
    """
    plugin, context, provider, plugin_main = pe

    async def scenario():
        await plugin.store.ensure_session(UMO, "group", "1001", enabled=True)
        # 三小时前有真人说过话，此后群里只有机器人
        await plugin.store.update_session(
            UMO, last_human_ts=now_ts() - 3 * 3600, last_bot_ts=now_ts() - 3 * 3600
        )
        for line in ("有人吗", "行吧我自己吃", "碗还没洗"):
            await plugin.on_message(make_event(plugin_main, sender_id=BOT_ID, text=line))
        await settle(plugin)
        # 机器人自己的话不该被算成真人发言
        after = await plugin.store.get_session(UMO)
        assert abs(float(after["last_human_ts"]) - (now_ts() - 3 * 3600)) < 5, (
            "机器人自己的消息刷新了 last_human_ts，"
            f"变成了 {now_ts() - float(after['last_human_ts']):.0f} 秒前"
        )

    run(scenario())
    assert context.sent == [], f"没人说话还自己发言：{context.sent}"
    assert provider.calls == []


def test_stale_human_activity_gate(pe):
    """纯时间判断：真人消息太旧时闸门应关闭，刚说过则放行。"""
    plugin, context, provider, plugin_main = pe
    assert plugin._human_spoke_recently({"last_human_ts": now_ts()}) is True
    assert plugin._human_spoke_recently({"last_human_ts": now_ts() - 3 * 3600}) is False
    assert plugin._human_spoke_recently({"last_human_ts": 0}) is False


def test_recent_human_activity_still_allows_instant(pe):
    """真人刚说完话时，即时搭话照旧要能工作（别把功能修死）。"""
    plugin, context, provider, plugin_main = pe

    async def scenario():
        await plugin.store.ensure_session(UMO, "group", "1001", enabled=True)
        # 注意：这里故意用机器人+真人混合的历史，last_bot_ts 保持很久以前，
        # 只让 last_human_ts 是"刚刚"
        await plugin.store.update_session(
            UMO, last_human_ts=now_ts(), last_bot_ts=now_ts() - 3600
        )
        event = make_event(plugin_main, text="今天中午吃啥好呢")
        await plugin.on_message(event)
        await settle(plugin)

    run(scenario())
    assert len(context.sent) == 1, f"真人刚发言时应能搭话，实际 {context.sent}"


# ======================================================================
#  二、响应对象被当成聊天内容
# ======================================================================
class _Chain:
    """模拟 MessageChain：有 get_plain_text()。"""

    def __init__(self, text=""):
        self.chain = [types.SimpleNamespace(text=text)]
        self._text = text

    def get_plain_text(self):
        return self._text


def test_llm_never_returns_object_repr():
    """拿不到文本时必须返回 None，绝不能把响应对象 str() 出去。"""
    resp = types.SimpleNamespace(
        role="assistant",
        completion_text=None,
        result_chain=None,
    )
    assert LLMClient._extract_text(resp) == "", "取不到文本应返回空串"

    # 真实的 LLMResponse repr 长这样，绝不能被当成模型输出
    class FakeLLMResponse:
        def __repr__(self):
            return (
                "LLMResponse(role='assistant', result_chain="
                "MessageChain(chain=[Plain(type=<Comp…"
            )

    got = LLMClient._extract_text(FakeLLMResponse())
    assert "LLMResponse(" not in got, f"把对象 repr 当成了输出：{got!r}"
    assert got == ""


def test_llm_extracts_text_from_result_chain():
    """completion_text 为空但有 result_chain 时，应从消息链里取文本。"""
    resp = types.SimpleNamespace(completion_text="", result_chain=_Chain("面煮好了也没人"))
    assert LLMClient._extract_text(resp) == "面煮好了也没人"


def test_llm_prefers_completion_text():
    resp = types.SimpleNamespace(completion_text="正文在这里", result_chain=_Chain("别的"))
    assert LLMClient._extract_text(resp) == "正文在这里"


def test_llm_extracts_from_raw_completion():
    resp = types.SimpleNamespace(completion_text=None, result_chain=None,
                                 raw_completion=_Chain("原始回复"))
    assert LLMClient._extract_text(resp) == "原始回复"


def test_llm_handles_plain_string_response():
    assert LLMClient._extract_text("直接是字符串") == "直接是字符串"


def test_chat_returns_none_when_nothing_extractable(cfg):
    """端到端：供应商回了个没法解析的对象，chat 必须返回 None 并留下错误。"""
    import asyncio as _asyncio

    class Provider:
        async def text_chat(self, **kw):
            return types.SimpleNamespace(completion_text=None, result_chain=None)

    class Ctx:
        def get_provider_by_id(self, provider_id=None):
            return Provider()

        async def get_using_provider_async(self, umo=None):
            return Provider()

        def get_all_providers(self):
            return [Provider()]

    client = LLMClient(Ctx(), cfg({}), None)
    out = _asyncio.run(client.chat(prompt="hi", umo=UMO))
    assert out is None, f"应返回 None，实际 {out!r}"
    assert "无法解析" in client.last_error, client.last_error
