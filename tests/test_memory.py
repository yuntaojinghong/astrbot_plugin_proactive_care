"""长期记忆：解析容错、打分排序与抽取流程。"""

from __future__ import annotations

import asyncio
import datetime as _dt

from proactive.config import Config
from proactive.context import ContextBuilder
from proactive.memory import MemoryManager, parse_items, _clamp
from proactive.textutil import now_ts

UMO = "aiocqhttp:GroupMessage:1001"


def run(coro):
    return asyncio.run(coro)


class FakeLLM:
    """只实现被用到的那两个成员。"""

    def __init__(self, reply=""):
        self.reply = reply
        self.last_error = ""
        self.calls = []

    async def chat(self, prompt="", system_prompt="", umo=None, temperature=None):
        self.calls.append(prompt)
        return self.reply


def make_manager(store, llm=None, **raw):
    cfg = Config(raw)
    llm = llm or FakeLLM()
    return MemoryManager(cfg, store, llm, ContextBuilder(cfg, store), None)


# ======================================================================
#  解析
# ======================================================================
def test_parse_clean_json():
    items = parse_items('[{"kind":"event","content":"小李下周三要去上海出差","importance":0.8}]')
    assert len(items) == 1
    assert items[0]["kind"] == "event"
    assert items[0]["content"] == "小李下周三要去上海出差"
    assert items[0]["importance"] == 0.8


def test_parse_tolerates_markdown_fence_and_prose():
    raw = """好的，我整理出来如下：

```json
[
  {"kind": "fact", "content": "群里在筹备十一聚会", "importance": 0.6}
]
```

希望有帮助！"""
    items = parse_items(raw)
    assert len(items) == 1
    assert "聚会" in items[0]["content"]


def test_parse_falls_back_to_lines_when_not_json():
    raw = "- 小王喜欢喝美式\n- 老张每周三请假\n\n以上。"
    items = parse_items(raw)
    assert len(items) == 2
    assert items[0]["kind"] == "fact"
    assert items[0]["importance"] == 0.5


def test_parse_maps_chinese_kind_and_scales_importance():
    items = parse_items('[{"kind":"喜好","content":"他不太能吃辣","importance":7}]')
    assert items[0]["kind"] == "preference"
    assert items[0]["importance"] == 0.7


def test_parse_drops_noise_and_duplicates():
    # 超过 120 字的条目会被丢弃（模型偶尔会把整段对话抄进来）
    too_long = "这是一条特别长的记忆内容" * 15
    raw = """[
      {"content": "好"},
      {"content": "群里在筹备十一聚会"},
      {"content": "群里在筹备十一聚会！"},
      {"content": "%s"},
      {"content": "暂无"}
    ]""" % too_long
    items = parse_items(raw)
    assert len(items) == 1
    assert items[0]["content"] == "群里在筹备十一聚会"


def test_parse_caps_batch_size():
    payload = "[" + ",".join('{"content":"记忆内容编号%d"}' % i for i in range(30)) + "]"
    assert len(parse_items(payload)) == 8


def test_parse_empty_input():
    assert parse_items("") == []
    assert parse_items("[]") == []
    # 模型回一句「没什么好记的」时，绝不能被当成一条记忆存进去
    assert parse_items("模型今天不想干活") == []
    assert parse_items("这段对话里没有值得长期记住的信息。") == []


def test_parse_line_fallback_only_for_list_like_text():
    assert parse_items("这是一段没有任何列表结构的普通说明文字") == []


def test_clamp_handles_bad_values():
    assert _clamp(0.5) == 0.5
    assert _clamp("abc") == 0.5
    assert _clamp(-1) == 0.0
    assert _clamp(99) == 1.0


# ======================================================================
#  打分
# ======================================================================
def test_score_prefers_recent_and_important(store):
    manager = make_manager(store)
    moment = _dt.datetime.now()
    fresh = {"content": "刚刚发生的事", "importance": 0.8, "created_ts": moment.timestamp(), "hits": 0}
    old = {"content": "很久以前的事", "importance": 0.8, "created_ts": moment.timestamp() - 86400 * 90, "hits": 0}
    weak = {"content": "刚刚发生的琐事", "importance": 0.1, "created_ts": moment.timestamp(), "hits": 0}

    assert manager.score(fresh, "", moment) > manager.score(old, "", moment)
    assert manager.score(fresh, "", moment) > manager.score(weak, "", moment)


def test_score_boosts_topic_overlap(store):
    manager = make_manager(store)
    moment = _dt.datetime.now()
    about_trip = {"content": "小李下周要去上海出差", "importance": 0.5, "created_ts": moment.timestamp(), "hits": 0}
    about_food = {"content": "楼下新开了一家面馆", "importance": 0.5, "created_ts": moment.timestamp(), "hits": 0}

    query = "上海出差的事情定下来了吗"
    assert manager.score(about_trip, query, moment) > manager.score(about_food, query, moment)


def test_decay_can_be_disabled(store):
    manager = make_manager(store, memory={"decay_days": 0})
    moment = _dt.datetime.now()
    old = {"content": "很久以前", "importance": 0.9, "created_ts": moment.timestamp() - 86400 * 3650, "hits": 0}
    new = {"content": "刚刚", "importance": 0.9, "created_ts": moment.timestamp(), "hits": 0}
    assert abs(manager.score(old, "", moment) - manager.score(new, "", moment)) < 1e-6


# ======================================================================
#  检索
# ======================================================================
def test_retrieve_respects_limit_and_records_hits(store):
    async def scenario():
        for index in range(10):
            await store.upsert_memory(
                UMO, f"记忆条目内容{index}", f"记忆条目内容{index}", importance=0.1 * index
            )
        manager = make_manager(store, memory={"max_inject": 3})
        picked = await manager.retrieve(UMO, query="记忆条目")
        assert len(picked) == 3

        rows = await store.list_memories(umo=UMO, limit=100)
        assert sum(row["hits"] for row in rows) == 3

    run(scenario())


def test_retrieve_returns_nothing_when_disabled(store):
    async def scenario():
        await store.upsert_memory(UMO, "内容足够长的一条", "内容足够长的一条")
        manager = make_manager(store, memory={"enable": False})
        assert await manager.retrieve(UMO) == []

    run(scenario())


def test_retrieve_returns_nothing_for_empty_library(store):
    async def scenario():
        manager = make_manager(store)
        assert await manager.retrieve(UMO) == []

    run(scenario())


# ======================================================================
#  抽取
# ======================================================================
def test_extract_stores_memories_and_advances_marker(store):
    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.add_message(UMO, "user", "小李说他下周三要去上海出差", ts=now_ts())
        await store.add_message(UMO, "user", "大家别忘了", ts=now_ts() + 1)

        llm = FakeLLM('[{"kind":"event","content":"小李下周三去上海出差","importance":0.8}]')
        manager = make_manager(store, llm)
        count = await manager.extract(UMO)

        assert count == 1
        rows = await store.list_memories(umo=UMO)
        assert rows[0]["content"] == "小李下周三去上海出差"

        session = await store.get_session(UMO)
        assert session["last_extract_ts"] > 0

        # 标记推进后，没有新消息就不该再抽
        assert await manager.maybe_extract(UMO, session) == 0

    run(scenario())


def test_maybe_extract_waits_for_threshold(store):
    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        for index in range(3):
            await store.add_message(UMO, "user", f"消息{index}", ts=now_ts() + index)
        llm = FakeLLM('[{"content":"一条够长的记忆内容"}]')
        manager = make_manager(store, llm, memory={"extract_every": 10})

        assert await manager.maybe_extract(UMO) == 0
        assert llm.calls == []  # 没到阈值就不该调用模型

    run(scenario())


def test_extract_handles_model_failure(store):
    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.add_message(UMO, "user", "随便说点什么内容", ts=now_ts())
        manager = make_manager(store, FakeLLM(""))
        assert await manager.extract(UMO) == 0
        assert await store.list_memories(umo=UMO) == []

    run(scenario())


def test_stats_summarises_library(store):
    async def scenario():
        await store.upsert_memory(UMO, "第一条记忆内容", "ns1", kind="fact", importance=0.4)
        await store.upsert_memory(UMO, "第二条记忆内容", "ns2", kind="event", importance=0.6)
        manager = make_manager(store)
        stats = await manager.stats(UMO)
        assert stats["total"] == 2
        assert stats["kinds"] == {"fact": 1, "event": 1}
        assert stats["avg_importance"] == 0.5

    run(scenario())
