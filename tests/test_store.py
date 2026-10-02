"""SQLite 存储层测试。所有异步方法用 asyncio.run 驱动，避免额外依赖 pytest-asyncio。"""

from __future__ import annotations

import asyncio

from proactive.textutil import day_start_ts, now_ts

UMO = "aiocqhttp:GroupMessage:1001"


def run(coro):
    return asyncio.run(coro)


def test_ensure_session_is_idempotent(store):
    async def scenario():
        first = await store.ensure_session(UMO, "group", "1001")
        second = await store.ensure_session(UMO, "group", "1001")
        assert first["umo"] == second["umo"]
        assert first["scope"] == "group"
        assert len(await store.list_sessions()) == 1

    run(scenario())


def test_update_session_only_touches_whitelisted_fields(store):
    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        # 夹带一个想混进 SET 子句的键，必须被白名单挡掉
        await store.update_session(UMO, last_human_ts=123.0, **{"total_sent = 999, enabled": 1})
        session = await store.get_session(UMO)
        assert session["last_human_ts"] == 123.0
        assert session["total_sent"] == 0
        assert session["enabled"] == 1
        assert session["umo"] == UMO

    run(scenario())


def test_update_session_with_no_valid_field_is_a_noop(store):
    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        assert await store.update_session(UMO, 完全不合法的字段=1) == 0

    run(scenario())


def test_bump_session_rejects_non_numeric_field(store):
    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        # 文本列不能让 SQL 去做减法
        try:
            await store.bump_session(UMO, "paused_reason", 1)
            raise AssertionError("不该允许对文本列自增")
        except ValueError:
            pass
        # 表里根本没有的列更不行
        try:
            await store.bump_session(UMO, "不存在的列", 1)
            raise AssertionError("不该允许对未知列自增")
        except ValueError:
            pass
        # 数值列正常工作
        await store.bump_session(UMO, "total_sent", 3)
        session = await store.get_session(UMO)
        assert session["total_sent"] == 3

    run(scenario())


def test_messages_are_returned_in_chronological_order(store):
    async def scenario():
        base = now_ts()
        for index in range(5):
            await store.add_message(UMO, "user", f"消息{index}", ts=base + index)
        rows = await store.recent_messages(UMO, limit=3)
        assert [row["content"] for row in rows] == ["消息2", "消息3", "消息4"]

    run(scenario())


def test_messages_can_be_filtered_by_time(store):
    async def scenario():
        await store.add_message(UMO, "user", "旧消息", ts=now_ts() - 10000)
        await store.add_message(UMO, "user", "新消息", ts=now_ts())
        rows = await store.recent_messages(UMO, limit=10, since_ts=now_ts() - 60)
        assert [row["content"] for row in rows] == ["新消息"]
        assert await store.count_messages(UMO, since_ts=now_ts() - 60) == 1

    run(scenario())


def test_prune_messages_keeps_recent(store):
    async def scenario():
        base = now_ts()
        for index in range(40):
            await store.add_message(UMO, "user", f"m{index}", ts=base + index)
        await store.prune_messages(UMO, keep=10)
        assert await store.count_messages(UMO) == 10

    run(scenario())


def test_memory_dedupes_by_normalized_content(store):
    """同一件事说两遍不应该变成两条记忆，而是保留较高的重要度。"""
    async def scenario():
        first = await store.upsert_memory(UMO, "小李下周要去上海出差", "小李下周要去上海出差", importance=0.4)
        second = await store.upsert_memory(
            UMO, "小李下周要去上海出差！", "小李下周要去上海出差", importance=0.9
        )
        assert first == second
        rows = await store.list_memories(umo=UMO)
        assert len(rows) == 1
        assert rows[0]["importance"] == 0.9

    run(scenario())


def test_memory_search_and_update_and_delete(store):
    async def scenario():
        memory_id = await store.upsert_memory(UMO, "群里在筹备十一聚会", "群里在筹备十一聚会")
        await store.upsert_memory(UMO, "小王喜欢喝美式", "小王喜欢喝美式")

        found = await store.list_memories(umo=UMO, query="聚会")
        assert len(found) == 1

        await store.update_memory(memory_id, content="群里在筹备十一聚会", importance=0.95)
        rows = await store.list_memories(umo=UMO, query="聚会")
        assert rows[0]["importance"] == 0.95

        assert await store.delete_memory(memory_id) == 1
        assert len(await store.list_memories(umo=UMO)) == 1

        assert await store.clear_memories(UMO) == 1
        assert await store.list_memories(umo=UMO) == []

    run(scenario())


def test_touch_memories_increments_hits(store):
    async def scenario():
        memory_id = await store.upsert_memory(UMO, "记住这条", "记住这条")
        await store.touch_memories([memory_id])
        await store.touch_memories([memory_id])
        rows = await store.list_memories(umo=UMO)
        assert rows[0]["hits"] == 2
        assert rows[0]["last_hit_ts"] > 0

    run(scenario())


def test_history_counters(store):
    async def scenario():
        await store.add_history(UMO, "idle", "静默太久", "在忙吗", sent=True)
        await store.add_history(UMO, "random", "随机", "随口一句", sent=True)
        await store.add_history(UMO, "preview", "预览", "只生成", sent=False)

        assert await store.count_sent_today(UMO) == 2
        assert await store.count_trigger_today(UMO, "idle") == 1
        assert await store.count_trigger_today(UMO, "random") == 1
        # 未发送（预览/失败）的不算进「说出去过的话」
        assert len(await store.recent_sent_contents(UMO)) == 2

    run(scenario())


def test_mark_latest_replied_only_marks_once(store):
    """每次有人回应只结算一条，不会把历史里所有未回应的一次性刷成已回应。"""
    async def scenario():
        await store.add_history(UMO, "idle", "静默太久", "在忙吗", sent=True)
        assert await store.mark_latest_replied(UMO) == 1

        rows = await store.list_history(UMO)
        assert [row["answered"] for row in rows] == [1]

        # 已经全部结算过，再调用不应重复标记
        assert await store.mark_latest_replied(UMO) == 0

    run(scenario())


def test_history_since_midnight(store):
    async def scenario():
        await store.add_history(UMO, "idle", "昨天", "旧消息", sent=True, ts=day_start_ts() - 3600)
        await store.add_history(UMO, "idle", "今天", "新消息", sent=True, ts=now_ts())
        assert await store.count_sent_today(UMO) == 1

    run(scenario())


def test_schedule_lifecycle(store):
    async def scenario():
        schedule_id = await store.add_schedule(UMO, "08:30", name="早安", weekdays="1,2,3,4,5")
        rows = await store.list_schedules(UMO)
        assert len(rows) == 1
        assert rows[0]["at_time"] == "08:30"
        assert rows[0]["enabled"] == 1

        await store.update_schedule(schedule_id, at_time="09:00", enabled=False)
        rows = await store.list_schedules(UMO)
        assert rows[0]["at_time"] == "09:00"
        assert rows[0]["enabled"] == 0

        await store.mark_schedule_run(schedule_id, "2026-01-01")
        rows = await store.list_schedules(UMO)
        assert rows[0]["last_run_date"] == "2026-01-01"

        assert await store.delete_schedule(schedule_id) == 1
        assert await store.list_schedules(UMO) == []

    run(scenario())


def test_overview_aggregates(store):
    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.ensure_session("aiocqhttp:GroupMessage:1002", "group", "1002")
        await store.update_session("aiocqhttp:GroupMessage:1002", paused=1, paused_reason="测试")

        await store.add_history(UMO, "idle", "静默", "在忙吗", sent=True)
        await store.upsert_memory(UMO, "一条记忆", "一条记忆")
        await store.add_message(UMO, "user", "一句话")

        data = await store.overview()
        assert data["sessions"] == 2
        assert data["active"] == 1
        assert data["paused"] == 1
        assert data["sent_total"] == 1
        assert data["memories"] == 1
        assert data["messages"] == 1
        assert data["answer_rate"] == 0.0

    run(scenario())


def test_delete_session_purges_related_rows(store):
    async def scenario():
        await store.ensure_session(UMO, "group", "1001")
        await store.add_message(UMO, "user", "内容")
        await store.upsert_memory(UMO, "记忆内容", "记忆内容")
        await store.add_history(UMO, "idle", "原因", "文案", sent=True)
        await store.add_schedule(UMO, "08:00")

        await store.delete_session(UMO)
        assert await store.get_session(UMO) is None
        assert await store.count_messages(UMO) == 0
        assert await store.list_memories(umo=UMO) == []
        assert await store.list_history(UMO) == []
        assert await store.list_schedules(UMO) == []

    run(scenario())


def test_data_survives_reopen(tmp_path):
    """关掉再打开，数据还在——这是换 SQLite 的核心目的。"""
    from proactive.store import Store

    first = Store(str(tmp_path))
    run(first.ensure_session(UMO, "group", "1001"))
    run(first.upsert_memory(UMO, "持久化的一条记忆", "持久化的一条记忆"))
    run(first.add_history(UMO, "idle", "原因", "文案", sent=True))
    first.close()

    second = Store(str(tmp_path))
    assert run(second.get_session(UMO)) is not None
    assert len(run(second.list_memories(umo=UMO))) == 1
    assert len(run(second.list_history(UMO))) == 1
    second.close()
