"""调度器：随机排期、四层闸门、回应结算与发送。"""

from __future__ import annotations

import asyncio
import datetime as _dt

from proactive.config import Config
from proactive.scheduler import Scheduler
from proactive.textutil import now_ts

import proactive.scheduler as scheduler_module

UMO = "aiocqhttp:GroupMessage:1001"


def run(coro):
    return asyncio.run(coro)


class FakeGenerator:
    def __init__(self, text="大家最近都还好吗", fail=False):
        self.text = text
        self.fail = fail
        self.calls: list[tuple] = []

    async def generate(self, session, trigger, reason, note="", preview=False):
        self.calls.append((trigger, reason, note))
        if self.fail:
            return {"ok": False, "content": "", "error": "模型不可用", "reason": reason, "trigger": trigger}
        return {"ok": True, "content": self.text, "error": "", "reason": reason, "trigger": trigger}


def quiet_off(monkeypatch):
    """把静默时段判定关掉，让其它闸门可以被单独测试。"""
    monkeypatch.setattr(scheduler_module, "in_quiet_hours", lambda *args, **kwargs: False)


def make_scheduler(store, sent, generator=None, **raw):
    base = {
        "basic": {"enabled": True, "group_whitelist": ["all"]},
        "guard": {"cooldown_minutes": 0, "max_per_day": 5, "min_human_gap_minutes": 20},
    }
    base.update(raw)
    cfg = Config(base)
    generator = generator or FakeGenerator()

    async def sender(umo, text):
        sent.append((umo, text))
        return True

    return Scheduler(cfg, store, None, generator, sender, None)


# ======================================================================
#  随机排期
# ======================================================================
def test_random_targets_are_deterministic_and_within_window(store):
    sent: list = []
    scheduler = make_scheduler(store, sent, trigger={"random_min_per_day": 2, "random_max_per_day": 2})
    moment = _dt.datetime(2026, 3, 1, 12, 0)

    first = scheduler.random_targets(UMO, moment)
    second = scheduler.random_targets(UMO, moment)
    assert first == second, "同一天同样会话的排期必须可复现，否则重启会重置当天次数"
    assert len(first) == 2
    assert first == sorted(first)

    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    for stamp in first:
        offset = (stamp - midnight) / 60
        assert 8 * 60 <= offset <= 23 * 60 + 30, offset


def test_random_targets_respects_count_range(store):
    sent: list = []
    scheduler = make_scheduler(store, sent, trigger={"random_min_per_day": 1, "random_max_per_day": 3})
    moment = _dt.datetime(2026, 3, 1, 12, 0)
    counts = {len(scheduler.random_targets(f"{UMO}:{day}", moment)) for day in range(1, 30)}
    assert counts <= {1, 2, 3}
    assert len(counts) > 1, "不同日期应当产生不同的次数"


def test_random_targets_empty_when_disabled(store):
    sent: list = []
    scheduler = make_scheduler(store, sent, trigger={"random_max_per_day": 0})
    assert scheduler.random_targets(UMO, _dt.datetime(2026, 3, 1, 12, 0)) == []


# ======================================================================
#  闸门
# ======================================================================
def test_disabled_master_switch_does_nothing(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(UMO, last_human_ts=now_ts() - 86400)
        scheduler = make_scheduler(store, sent, basic={"enabled": False, "group_whitelist": ["all"]})
        return await scheduler.tick()

    assert run(scenario()) == []
    assert sent == []


def test_idle_trigger_fires_after_threshold(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(UMO, last_human_ts=now_ts() - 4 * 3600)
        scheduler = make_scheduler(store, sent)
        return await scheduler.tick()

    actions = run(scenario())
    assert len(actions) == 1
    assert actions[0]["trigger"] == "idle"
    assert actions[0]["sent"] is True
    assert sent and sent[0][0] == UMO


def test_no_trigger_when_group_is_active(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(UMO, last_human_ts=now_ts() - 60)
        scheduler = make_scheduler(store, sent)
        return await scheduler.tick()

    assert run(scenario()) == []
    assert sent == []


def test_min_human_gap_blocks_interrupting(store, monkeypatch):
    """群友刚说完话不久，即使满足空闲阈值也不该插话。"""
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        # 空闲阈值设为 30 分钟，但最后一条真人消息只有 10 分钟前
        await store.update_session(UMO, last_human_ts=now_ts() - 10 * 60)
        scheduler = make_scheduler(
            store, sent, trigger={"idle_minutes": 30}, guard={"min_human_gap_minutes": 20, "cooldown_minutes": 0, "max_per_day": 5}
        )
        return await scheduler.tick()

    assert run(scenario()) == []


def test_quiet_hours_block_everything(store, monkeypatch):
    sent: list = []
    monkeypatch.setattr(scheduler_module, "in_quiet_hours", lambda *args, **kwargs: True)

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(UMO, last_human_ts=now_ts() - 86400)
        scheduler = make_scheduler(store, sent)
        return await scheduler.tick()

    assert run(scenario()) == []
    assert sent == []


def test_daily_cap_blocks_after_limit(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(UMO, last_human_ts=now_ts() - 86400)
        for _ in range(2):
            await store.add_history(UMO, "idle", "已发过", "文案", sent=True)
        scheduler = make_scheduler(store, sent, guard={"cooldown_minutes": 0, "max_per_day": 2, "min_human_gap_minutes": 0})
        return await scheduler.tick()

    assert run(scenario()) == []
    assert sent == []


def test_cooldown_blocks_rapid_repeat(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(
            UMO, last_human_ts=now_ts() - 86400, last_proactive_ts=now_ts() - 5 * 60
        )
        scheduler = make_scheduler(
            store, sent, guard={"cooldown_minutes": 120, "max_per_day": 5, "min_human_gap_minutes": 0}
        )
        return await scheduler.tick()

    assert run(scenario()) == []


def test_out_of_scope_session_is_skipped(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(UMO, last_human_ts=now_ts() - 86400)
        scheduler = make_scheduler(store, sent, basic={"enabled": True, "group_whitelist": ["2002"]})
        return await scheduler.tick()

    assert run(scenario()) == []


def test_paused_session_is_skipped(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(UMO, last_human_ts=now_ts() - 86400, paused=1, paused_reason="测试")
        scheduler = make_scheduler(store, sent)
        return await scheduler.tick()

    assert run(scenario()) == []


def test_followup_takes_priority_over_idle(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(
            UMO,
            last_human_ts=now_ts() - 4 * 3600,
            pending_question=1,
            pending_question_ts=now_ts() - 2 * 3600,
        )
        scheduler = make_scheduler(store, sent)
        return await scheduler.tick()

    actions = run(scenario())
    assert len(actions) == 1
    assert actions[0]["trigger"] == "followup"


def test_followup_waits_for_its_own_threshold(store, monkeypatch):
    """刚问完 5 分钟就追问会很烦，应当还没到点。"""
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(
            UMO,
            last_human_ts=now_ts() - 4 * 3600,
            pending_question=1,
            pending_question_ts=now_ts() - 5 * 60,
        )
        scheduler = make_scheduler(store, sent, trigger={"followup_minutes": 30, "idle_enable": False})
        return await scheduler.tick()

    assert run(scenario()) == []


def test_schedule_trigger_fires_once_per_day(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(UMO, last_human_ts=now_ts() - 86400)
        now = _dt.datetime.now()
        await store.add_schedule(UMO, now.strftime("%H:%M"), name="测试问候")
        scheduler = make_scheduler(
            store,
            sent,
            trigger={"idle_enable": False, "random_enable": False, "schedule_enable": True},
            guard={"cooldown_minutes": 0, "max_per_day": 5, "min_human_gap_minutes": 0},
        )
        first = await scheduler.tick()
        # 清掉冷却影响，再跑一次应当因为「今天已执行」而被跳过
        await store.update_session(UMO, last_proactive_ts=0)
        second = await scheduler.tick()
        return first, second

    first, second = run(scenario())
    assert [item["trigger"] for item in first] == ["schedule"]
    assert second == []


def test_expired_schedule_is_marked_without_firing(store, monkeypatch):
    """错过太久的定时问候不该补发。"""
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(UMO, last_human_ts=now_ts() - 86400)
        now = _dt.datetime.now()
        missed = (now - _dt.timedelta(hours=6)).strftime("%H:%M")
        await store.add_schedule(UMO, missed, name="早上的问候")
        scheduler = make_scheduler(
            store,
            sent,
            trigger={"idle_enable": False, "random_enable": False, "schedule_enable": True},
            guard={"cooldown_minutes": 0, "max_per_day": 5, "min_human_gap_minutes": 0},
        )
        actions = await scheduler.tick()
        rows = await store.list_schedules(UMO)
        return actions, rows

    actions, rows = run(scenario())
    assert actions == []
    assert rows[0]["last_run_date"] != ""


# ======================================================================
#  回应结算
# ======================================================================
def test_unanswered_streak_grows_and_pauses(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        scheduler = make_scheduler(store, sent, guard={"unanswered_pause": 2, "reply_window_minutes": 15, "cooldown_minutes": 0, "max_per_day": 9, "min_human_gap_minutes": 0})
        await store.update_session(
            UMO,
            awaiting_reply=1,
            last_proactive_ts=now_ts() - 60 * 60,
            last_human_ts=now_ts() - 7200,
        )
        first = await store.get_session(UMO)
        settled = await scheduler._settle_reply_window(first)
        assert settled["unanswered_streak"] == 1
        assert settled["paused"] == 0

        await store.update_session(
            UMO,
            awaiting_reply=1,
            last_proactive_ts=now_ts() - 60 * 60,
            unanswered_streak=1,
        )
        second = await store.get_session(UMO)
        settled = await scheduler._settle_reply_window(second)
        assert settled["unanswered_streak"] == 2
        assert settled["paused"] == 1
        assert "没有人回应" in settled["paused_reason"]
        return settled

    run(scenario())


def test_reply_window_not_settled_too_early(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        scheduler = make_scheduler(store, sent)
        await store.update_session(UMO, awaiting_reply=1, last_proactive_ts=now_ts() - 60)
        session = await store.get_session(UMO)
        settled = await scheduler._settle_reply_window(session)
        assert settled["awaiting_reply"] == 1
        assert settled["unanswered_streak"] == 0

    run(scenario())


# ======================================================================
#  发送
# ======================================================================
def test_fire_sends_and_updates_state(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        scheduler = make_scheduler(store, sent, generator=FakeGenerator("大家好呀，好久没聊了"))
        session = await store.get_session(UMO)
        outcome = await scheduler.fire(session, "idle", "静默太久")
        after = await store.get_session(UMO)
        messages = await store.recent_messages(UMO, limit=5)
        return outcome, after, messages

    outcome, after, messages = run(scenario())
    assert outcome["sent"] is True
    assert sent[0] == (UMO, "大家好呀，好久没聊了")
    assert after["awaiting_reply"] == 1
    assert after["last_proactive_ts"] > 0
    assert after["total_sent"] == 1
    assert messages[-1]["role"] == "bot"
    assert messages[-1]["content"] == "大家好呀，好久没聊了"


def test_fire_marks_pending_question_when_text_asks(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        scheduler = make_scheduler(store, sent, generator=FakeGenerator("大家最近还好吗？"))
        session = await store.get_session(UMO)
        await scheduler.fire(session, "idle", "静默太久")
        return await store.get_session(UMO)

    after = run(scenario())
    assert after["pending_question"] == 1
    assert after["pending_question_ts"] > 0


def test_dry_run_generates_but_does_not_send(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        scheduler = make_scheduler(store, sent, advanced={"dry_run": True})
        session = await store.get_session(UMO)
        outcome = await scheduler.fire(session, "idle", "静默太久")
        history = await store.list_history(UMO)
        return outcome, history

    outcome, history = run(scenario())
    assert outcome["preview"] is True
    assert outcome["sent"] is False
    assert sent == []
    assert history[0]["sent"] == 0
    assert "演练" in history[0]["reason"]


def test_manual_trigger_ignores_dry_run(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        scheduler = make_scheduler(store, sent, advanced={"dry_run": True})
        session = await store.get_session(UMO)
        return await scheduler.fire(session, "manual", "管理员手动触发", ignore_guards=True)

    outcome = run(scenario())
    assert outcome["sent"] is True
    assert len(sent) == 1


def test_failed_generation_is_recorded(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        scheduler = make_scheduler(store, sent, generator=FakeGenerator(fail=True))
        session = await store.get_session(UMO)
        outcome = await scheduler.fire(session, "idle", "静默太久")
        history = await store.list_history(UMO)
        return outcome, history

    outcome, history = run(scenario())
    assert outcome["sent"] is False
    assert outcome["error"]
    assert sent == []
    assert history[0]["sent"] == 0
    assert "失败" in history[0]["reason"]


def test_send_failure_does_not_mark_awaiting(store, monkeypatch):
    quiet_off(monkeypatch)

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        cfg = Config({"basic": {"enabled": True, "group_whitelist": ["all"]}})

        async def failing_sender(umo, text):
            return False

        scheduler = Scheduler(cfg, store, None, FakeGenerator(), failing_sender, None)
        session = await store.get_session(UMO)
        await scheduler.fire(session, "idle", "静默太久")
        return await store.get_session(UMO)

    after = run(scenario())
    assert after["awaiting_reply"] == 0
    assert after["total_sent"] == 0


# ======================================================================
#  预估
# ======================================================================
def test_estimate_next_reports_reason(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(UMO, last_human_ts=now_ts() - 3600)
        scheduler = make_scheduler(store, sent)
        session = await store.get_session(UMO)
        return await scheduler.estimate_next(session)

    eta = run(scenario())
    assert eta["eta"] is not None
    assert "空闲唤醒" in eta["why"] or "随机关怀" in eta["why"]


def test_estimate_next_explains_disabled(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(UMO, enabled=0)
        scheduler = make_scheduler(store, sent)
        session = await store.get_session(UMO)
        return await scheduler.estimate_next(session)

    eta = run(scenario())
    assert eta["eta"] is None
    assert eta["why"] == "已关闭"


def test_status_snapshot(store):
    sent: list = []
    scheduler = make_scheduler(store, sent)
    status = scheduler.status()
    assert status["running"] is False
    assert status["tick_seconds"] == 30
    assert status["error_count"] == 0


# ======================================================================
#  即时搭话（群友发言后按概率接一句）
# ======================================================================
def _instant_config(**overrides):
    trigger = {"instant_enable": True, "instant_probability": 100, "instant_cooldown_seconds": 0}
    trigger.update(overrides)
    return trigger


def test_instant_fires_when_probability_hits(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []
    generator = FakeGenerator("我也想去")

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        scheduler = make_scheduler(store, sent, generator, trigger=_instant_config())
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    record = run(scenario())
    assert record is not None and record["sent"] is True
    assert record["trigger"] == "instant"
    assert sent == [(UMO, "我也想去")]
    assert generator.calls[0][0] == "instant"


def test_instant_respects_probability_zero(store, monkeypatch):
    """概率 0 时哪怕别的条件都满足也不能发。"""
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        scheduler = make_scheduler(store, sent, trigger=_instant_config(instant_probability=0))
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    assert run(scenario()) is None
    assert sent == []


def test_instant_roll_is_probabilistic(store, monkeypatch):
    """概率 50% 时，随机值落在两侧的结果必须相反。"""
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario(roll):
        await store.ensure_session(UMO, "group", "1001")
        scheduler = make_scheduler(store, sent, trigger=_instant_config(instant_probability=50))
        monkeypatch.setattr(scheduler_module.random, "random", lambda: roll)
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    assert run(scenario(0.10)) is not None, "0.10 < 0.5 应当命中"
    assert run(scenario(0.90)) is None, "0.90 > 0.5 应当不命中"


def test_instant_skips_min_human_gap(store, monkeypatch):
    """刚有人说话也要能接——这条闸门本来就是防热闹时插话的，对它不适用。"""
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        # 最后一條真人消息就在刚刚，min_human_gap 会把轮询触发全部挡掉
        await store.update_session(UMO, last_human_ts=now_ts())
        scheduler = make_scheduler(
            store, sent, trigger=_instant_config(), guard={"min_human_gap_minutes": 20}
        )
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    assert run(scenario()) is not None
    assert sent


def test_instant_uses_own_seconds_cooldown(store, monkeypatch):
    """即时搭话用秒级冷却，不受全局分钟级冷却影响；但冷却内不能再发。"""
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(UMO, last_proactive_ts=now_ts() - 30)
        scheduler = make_scheduler(
            store,
            sent,
            trigger=_instant_config(instant_cooldown_seconds=300),
            guard={"cooldown_minutes": 120},
        )
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    assert run(scenario()) is None, "30 秒前刚说过，仍在 300 秒冷却内"
    assert sent == []


def test_instant_still_respects_daily_cap_and_quiet(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        scheduler = make_scheduler(
            store, sent, trigger=_instant_config(), guard={"max_per_day": 1}
        )
        await store.add_history(UMO, "idle", "之前发过一条", "大家好", sent=True)
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    assert run(scenario()) is None
    assert sent == []


def test_instant_respects_quiet_hours(store, monkeypatch):
    sent: list = []

    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        scheduler = make_scheduler(store, sent, trigger=_instant_config())
        monkeypatch.setattr(scheduler_module, "in_quiet_hours", lambda *args, **kwargs: True)
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    assert run(scenario()) is None
    assert sent == []


def test_instant_disabled_switch_or_master(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario(trigger, basic):
        await store.ensure_session(UMO, "group", "1001")
        scheduler = make_scheduler(store, sent, trigger=trigger, basic=basic)
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    assert run(scenario(_instant_config(instant_enable=False), {"enabled": True, "group_whitelist": ["all"]})) is None
    assert run(scenario(_instant_config(), {"enabled": False, "group_whitelist": ["all"]})) is None
    assert sent == []


def test_instant_skips_paused_or_out_of_scope(store, monkeypatch):
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario(**fields):
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(UMO, **fields)
        scheduler = make_scheduler(store, sent, trigger=_instant_config())
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    assert run(scenario(paused=1)) is None
    assert run(scenario(enabled=0)) is None
    assert sent == []


def test_instant_session_override_probability(store, monkeypatch):
    """单个群可以覆盖全局概率。"""
    quiet_off(monkeypatch)
    sent: list = []

    async def scenario(override):
        await store.ensure_session(UMO, "group", "1001")
        await store.update_session(UMO, override_json=override)
        scheduler = make_scheduler(store, sent, trigger=_instant_config(instant_probability=100))
        monkeypatch.setattr(scheduler_module.random, "random", lambda: 0.5)
        session = await store.get_session(UMO)
        return await scheduler.try_instant(session)

    import json

    assert run(scenario(json.dumps({"instant_probability": 0}))) is None, "本群设为 0% 应不触发"
    assert run(scenario(json.dumps({"instant_probability": 80}))) is not None, "本群设为 80% 应命中 0.5"
    assert sent
