"""微光的资源占用：实测数据 + 三道自我保护。

用户问「轮询会不会把 2 核 2G 的服务器跑死」。实测结论：

  · 一轮 tick（30 秒一次）在 20 个会话下约 7ms、零 token —— **轮询本身可忽略**
  · 每条群消息的存储开销约 1ms
  · 真正重的是**记忆抽取**：一次把最多 200 条消息渲染成提示词
    （实测约 1 万字符 / 8 千多 token），且发生在消息处理路径里、要等模型返回

而 AstrBot 的 EventBus 给每个事件起独立任务、**没有并发上限**，
所以「模型慢 + 消息密」时任务会堆积。因此加了三道保护：

  1. 同一群不并发抽取（并发去重放在 extract 里，覆盖面板/指令等所有入口）
  2. extract_min_interval：两次抽取之间的最短间隔
  3. extract_max_messages：单次抽取读取的消息条数上限（直接决定提示词大小）
"""

from __future__ import annotations

import asyncio

from proactive.config import Config
from proactive.context import ContextBuilder
from proactive.memory import MemoryManager

UMO = "aiocqhttp:GroupMessage:1001"
SAMPLE = "今天中午吃什么好呢 我想吃面 你们呢 要不一起点外卖吧 我请客"


class RecLLM:
    """记录每次送进模型的 prompt 长度，并可模拟慢模型。"""

    def __init__(self, delay: float = 0.0):
        self.calls: list[int] = []
        self.delay = delay
        self.last_error = ""

    async def chat(self, prompt="", system_prompt="", umo=None, temperature=None):
        self.calls.append(len(prompt))
        if self.delay:
            await asyncio.sleep(self.delay)
        return "[]"


def build(store, **memory_over):
    cfg = Config({"memory": memory_over} if memory_over else {})
    llm = RecLLM()
    mem = MemoryManager(cfg, store, llm, ContextBuilder(cfg, store), None)
    return cfg, mem, llm


async def fill(store, n: int = 200):
    await store.ensure_session(UMO, "group", "1001", enabled=True)
    for i in range(n):
        await store.add_message(UMO, "user", f"{SAMPLE}（第{i}条）",
                                sender_id="999", sender_name="群友")


def run(coro):
    return asyncio.run(coro)


# ======================================================================
#  默认值
# ======================================================================
def test_extract_defaults_are_bounded(cfg):
    c = cfg({})
    assert c.extract_every == 30
    assert c.extract_min_interval == 120, "默认应限制抽取频率，否则密集消息会连续触发长调用"
    assert c.extract_max_messages == 200
    # 越界要夹住
    assert cfg({"memory": {"extract_min_interval": -5}}).extract_min_interval == 0
    assert cfg({"memory": {"extract_min_interval": 999999}}).extract_min_interval == 86400
    assert cfg({"memory": {"extract_max_messages": 99999}}).extract_max_messages == 500
    assert cfg({"memory": {"extract_max_messages": 1}}).extract_max_messages == 5


# ======================================================================
#  保护一：同一群不并发抽取
# ======================================================================
def test_concurrent_extract_is_deduplicated(store):
    """并发触发只能产生一次模型调用（否则小内存机器会被叠加的长调用拖垮）。"""
    async def scenario():
        await fill(store)
        cfg, mem, llm = build(store, extract_min_interval=0)
        mem.llm.delay = 0.3       # 模拟慢模型，制造重叠窗口
        await asyncio.gather(*[mem.extract(UMO) for _ in range(5)])
        return len(llm.calls)

    assert run(scenario()) == 1


def test_concurrent_maybe_extract_is_deduplicated(store):
    async def scenario():
        await fill(store)
        cfg, mem, llm = build(store, extract_min_interval=0)
        mem.llm.delay = 0.3
        await asyncio.gather(*[mem.maybe_extract(UMO) for _ in range(5)])
        return len(llm.calls)

    assert run(scenario()) == 1


# ======================================================================
#  保护二：最短间隔
# ======================================================================
def test_min_interval_blocks_repeat(store):
    async def scenario():
        await fill(store)
        cfg, mem, llm = build(store, extract_min_interval=600, extract_every=10)
        await mem.maybe_extract(UMO)
        first = len(llm.calls)
        await fill(store, 60)              # 再攒够条数
        await mem.maybe_extract(UMO)       # 但间隔没到
        blocked = len(llm.calls)
        await store.update_session(UMO, last_extract_ts=0)   # 模拟间隔已过
        await mem.maybe_extract(UMO)
        after = len(llm.calls)
        return first, blocked, after

    first, blocked, after = run(scenario())
    assert first == 1, f"首次应抽取: {first}"
    assert blocked == first, f"间隔内不该重复抽取: {blocked}"
    assert after > blocked, f"间隔过后应能再抽取: {after}"


# ======================================================================
#  保护三：提示词大小随上限收敛
# ======================================================================
def test_max_messages_shrinks_prompt(store):
    async def measure(limit: int) -> int:
        await fill(store, 260)
        cfg, mem, llm = build(store, extract_max_messages=limit,
                              extract_min_interval=0, extract_every=1)
        await store.update_session(UMO, last_extract_ts=0)
        await mem.extract(UMO)
        return llm.calls[0] if llm.calls else 0

    # 同一个 store 复用，先清一下消息，避免相互影响
    sizes = {}
    for limit in (200, 50):
        run(store.clear_messages(UMO))
        sizes[limit] = run(measure(limit))

    assert sizes[200] > 0 and sizes[50] > 0, sizes
    assert sizes[50] < sizes[200] * 0.6, (
        f"上限没起作用: 200->{sizes[200]} 字符, 50->{sizes[50]} 字符"
    )


def test_extract_disabled_is_free(store):
    """关掉记忆抽取后，消息路径上不再有任何模型调用。"""
    async def scenario():
        await fill(store)
        cfg, mem, llm = build(store, enable=False)
        await mem.maybe_extract(UMO)
        return len(llm.calls)

    assert run(scenario()) == 0
