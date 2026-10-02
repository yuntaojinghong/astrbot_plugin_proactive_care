"""即时搭话：群友一发消息就按概率接一句。

这一路与轮询触发共用大部分闸门，但有三处刻意不同，测试要盯住：

1. 不看「距最后一条真人消息的最短间隔」——它就是要在大家聊天时接话
2. 用独立的**秒级**冷却，而不是全局分钟级冷却（否则 120 分钟冷却会让它形同虚设）
3. 由消息事件直接驱动，不进轮询
"""

from __future__ import annotations

import asyncio
import json

import proactive.scheduler as scheduler_module
from proactive.config import Config
from proactive.scheduler import Scheduler
from proactive.textutil import now_ts

UMO = "aiocqhttp:GroupMessage:1001"


def run(coro):
    return asyncio.run(coro)


class FakeGenerator:
    def __init__(self, text="我也想去"):
        self.text = text
        self.calls: list[tuple] = []

    async def generate(self, session, trigger, reason, note="", preview=False):
        self.calls.append((trigger, reason, note))
        return {"ok": True, "content": self.text, "error": "", "reason": reason, "trigger": trigger}


def make_scheduler(store, sent, generator=None, **raw):
    base = {
        "basic": {"enabled": True, "group_whitelist": ["all"]},
        "guard": {"cooldown_minutes": 0, "max_per_day": 5, "min_human_gap_minutes": 20},
    }
    base.update(raw)
    cfg = Config(base)

    async def sender(umo, text):
        sent.append((umo, text))
        return True

    return Scheduler(cfg, store, None, generator or FakeGenerator(), sender, None)


def instant_trigger(**overrides):
    trigger = {"instant_enable": True, "instant_probability": 100, "instant_cooldown_seconds": 0}
    trigger.update(overrides)
    return trigger


def quiet_off(monkeypatch):
    monkeypatch.setattr(scheduler_module, "in_quiet_hours", lambda *args, **kwargs: False)


# ======================================================================
#  基本行为
# ======================================================================
def test_instant_fires_when_probability_hits(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []
    generator = FakeGenerator()

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        scheduler = make_scheduler(store, sent, generator, trigger=instant_trigger())
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    record = run(scenario())
    assert record is not None
    assert record["trigger"] == "instant"
    assert record["sent"] is True
    assert sent == [(UMO, "我也想去")]
    assert generator.calls[0][0] == "instant"


def test_instant_roll_is_probabilistic(store, monkeypatch):
    """概率 50% 时，落在两侧的随机值必须给出相反结果。"""
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario(roll):
        await store.ensure_session(UMO, "group", "1001")
        scheduler = make_scheduler(store, sent, trigger=instant_trigger(instant_probability=50))
        monkeypatch.setattr(scheduler_module.random, "random", lambda: roll)
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    assert run(scenario(0.10)) is not None, "0.10 小于 0.5，应当命中"
    assert run(scenario(0.90)) is None, "0.90 大于 0.5，应当不命中"


def test_instant_probability_zero_never_fires(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        scheduler = make_scheduler(store, sent, trigger=instant_trigger(instant_probability=0))
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    assert run(scenario()) is None
    assert sent == []


# ======================================================================
#  闸门
# ======================================================================
def test_instant_ignores_min_human_gap(store, monkeypatch):
    """刚有人说话也要能接：那条闸门本来是防热闹时插话的，对它不适用。"""
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(UMO, last_human_ts=now_ts())
        scheduler = make_scheduler(
            store, sent, trigger=instant_trigger(), guard={"min_human_gap_minutes": 20}
        )
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    assert run(scenario()) is not None
    assert sent


def test_instant_uses_seconds_cooldown_not_global_minutes(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(UMO, last_proactive_ts=now_ts() - 30)
        scheduler = make_scheduler(
            store,
            sent,
            trigger=instant_trigger(instant_cooldown_seconds=300),
            guard={"cooldown_minutes": 120},
        )
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    assert run(scenario()) is None, "30 秒前刚说过，仍在 300 秒冷却内"
    assert sent == []


def test_instant_passes_when_cooldown_expired(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(UMO, last_proactive_ts=now_ts() - 600)
        scheduler = make_scheduler(
            store,
            sent,
            trigger=instant_trigger(instant_cooldown_seconds=300),
            guard={"cooldown_minutes": 120},
        )
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    assert run(scenario()) is not None, "超过 300 秒冷却就该放行，尽管全局冷却是 120 分钟"
    assert sent


def test_instant_respects_daily_cap(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        scheduler = make_scheduler(store, sent, trigger=instant_trigger(), guard={"max_per_day": 1})
        await store.add_history(UMO, "idle", "之前发过一条", "大家好", sent=True)
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    assert run(scenario()) is None
    assert sent == []


def test_instant_respects_quiet_hours(store, monkeypatch):
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        scheduler = make_scheduler(store, sent, trigger=instant_trigger())
        monkeypatch.setattr(scheduler_module, "in_quiet_hours", lambda *args, **kwargs: True)
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    assert run(scenario()) is None
    assert sent == []


def test_instant_needs_both_switches(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario(trigger, basic):
        await store.ensure_session(UMO, "group", "1001")
        scheduler = make_scheduler(store, sent, trigger=trigger, basic=basic)
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    enabled = {"enabled": True, "group_whitelist": ["all"]}
    assert run(scenario(instant_trigger(instant_enable=False), enabled)) is None
    assert run(scenario(instant_trigger(), {"enabled": False, "group_whitelist": ["all"]})) is None
    assert sent == []


def test_instant_skips_paused_disabled_and_out_of_scope(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario(**fields):
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(UMO, **fields)
        scheduler = make_scheduler(store, sent, trigger=instant_trigger())
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    assert run(scenario(paused=1)) is None
    assert run(scenario(enabled=0)) is None
    assert sent == []

    # 不在白名单里的群同样不生效
    async def out_of_scope():
        await store.ensure_session(UMO, "group", "1001")
        scheduler = make_scheduler(
            store, sent, trigger=instant_trigger(), basic={"enabled": True, "group_whitelist": ["9999"]}
        )
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    assert run(out_of_scope()) is None
    assert sent == []


# ======================================================================
#  每个群单独的概率
# ======================================================================
def test_instant_session_override_probability(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario(override):
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(UMO, override_json=override)
        scheduler = make_scheduler(store, sent, trigger=instant_trigger(instant_probability=100))
        monkeypatch.setattr(scheduler_module.random, "random", lambda: 0.5)
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    assert run(scenario(json.dumps({"instant_probability": 0}))) is None, "本群设为 0% 不该触发"
    assert run(scenario(json.dumps({"instant_probability": 80}))) is not None, "本群 80% 应命中 0.5"
    assert sent


def test_instant_override_falls_back_when_invalid(store, monkeypatch):
    """覆盖值写坏了不能把功能搞挂，退回全局概率。"""
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario(override):
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(UMO, override_json=override)
        scheduler = make_scheduler(store, sent, trigger=instant_trigger(instant_probability=100))
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    assert run(scenario(json.dumps({"instant_probability": "不是数字"}))) is not None
    assert run(scenario("{坏掉的 json")) is not None
    assert len(sent) == 2


# ======================================================================
#  配置
# ======================================================================
def test_instant_config_defaults_and_clamping(cfg):
    config = cfg({"trigger": {"instant_probability": 999, "instant_delay_min": 30, "instant_delay_max": 5}})
    assert config.instant_probability_percent == 100, "概率被夹到 100"
    assert config.instant_probability == 1.0
    low, high = config.instant_delay
    assert (low, high) == (5.0, 30.0), "延迟上下限写反了要自动交换"


def test_instant_config_probability_for_session(cfg):
    config = cfg({"trigger": {"instant_probability": 20}})
    assert abs(config.instant_probability_for({}) - 0.2) < 1e-9
    session = {"override_json": json.dumps({"instant_probability": 70})}
    assert abs(config.instant_probability_for(session) - 0.7) < 1e-9
    assert abs(config.instant_probability_for({"override_json": "{坏 json"}) - 0.2) < 1e-9


def test_instant_warns_on_extreme_probability(cfg):
    assert any("0%" in item for item in cfg({"trigger": {"instant_enable": True, "instant_probability": 0}}).warnings)
    assert any(
        "100%" in item for item in cfg({"trigger": {"instant_enable": True, "instant_probability": 100}}).warnings
    )
