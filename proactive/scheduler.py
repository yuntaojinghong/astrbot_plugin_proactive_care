"""调度循环与触发判定。

一个 asyncio 后台任务，每 ``tick_seconds`` 秒走一遍所有会话，按固定优先级
挑选触发方式：

    话题追问 > 定时问候 > 空闲唤醒 > 随机关怀

另有一种**不进轮询**的触发方式「即时搭话」（:meth:`try_instant`）：群友一发
消息就按概率判定，由消息事件直接驱动，延迟几秒后发声。

判定一律**从便宜到昂贵**：先过静默时段、每日上限、冷却、最小间隔这些
零成本闸门，全部通过才去调用模型。所以轮询本身不烧任何 token。

「连续没人回应就自动闭嘴」也在这里结算：发出主动消息后进入 ``awaiting_reply``
状态，超过回应窗口仍无真人发言，就累加一次沉默计数，达到阈值直接暂停该群。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import random
from typing import Any, Awaitable, Callable

from .config import parse_hhmm
from .context import looks_like_question
from .generator import TRIGGER_LABELS
from .textutil import day_key, humanize_delta, in_quiet_hours, now_ts

# 定时规则错过太久就不补发了（免得晚上把早上的「早安」补出来）
SCHEDULE_TOLERANCE_MINUTES = 120

Sender = Callable[[str, str], Awaitable[bool]]


class Scheduler:
    def __init__(
        self,
        config: Any,
        store: Any,
        memory: Any,
        generator: Any,
        sender: Sender,
        logger: Any = None,
    ):
        self.config = config
        self.store = store
        self.memory = memory
        self.generator = generator
        self.sender = sender
        self.logger = logger

        self.bot_name = "微光"
        self.bot_id = ""
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._random_plan: dict[str, list[float]] = {}
        self.tick_count = 0
        self.error_count = 0
        self.last_tick_ts = 0.0

    # ==================================================================
    #  生命周期
    # ==================================================================
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="proactive-care-scheduler")
        self._info(f"调度器已启动，轮询间隔 {self.config.tick_seconds} 秒")

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        self._info("调度器已停止")

    async def _loop(self) -> None:
        # 启动后稍等一会，避免和 AstrBot 自身的初始化抢资源
        await asyncio.sleep(5)
        while True:
            stop = self._stop
            if stop is not None and stop.is_set():
                break
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - 兜底，绝不让调度任务死掉
                self.error_count += 1
                self._warn(f"调度轮询异常（已跳过本次）: {exc}")
            timeout = max(5, int(self.config.tick_seconds))
            try:
                if stop is not None:
                    await asyncio.wait_for(stop.wait(), timeout=timeout)
                else:
                    await asyncio.sleep(timeout)
            except asyncio.TimeoutError:
                continue

    # ==================================================================
    #  单次轮询
    # ==================================================================
    async def tick(self) -> list[dict]:
        """走一遍所有会话，返回本次实际产生的动作列表。"""
        self.tick_count += 1
        actions: list[dict] = []
        if not self.config.enabled:
            self.last_tick_ts = now_ts()
            return actions

        moment = _dt.datetime.now()
        try:
            sessions = await self.store.list_sessions()
        except Exception as exc:
            self._warn(f"读取会话列表失败: {exc}")
            return actions

        for session in sessions:
            try:
                action = await self._process(session, moment)
            except Exception as exc:
                self.error_count += 1
                self._warn(f"处理会话 {session.get('umo')} 时异常: {exc}")
                continue
            if action:
                actions.append(action)

        self.last_tick_ts = now_ts()
        return actions

    async def _process(self, session: dict, moment: _dt.datetime) -> dict | None:
        umo = session.get("umo") or ""
        if not umo or not int(session.get("enabled") or 0):
            return None

        session = await self._settle_reply_window(session)
        if int(session.get("paused") or 0):
            return None

        if not self._scope_allowed(session):
            return None

        if in_quiet_hours(moment, *self.config.quiet_range):
            return None

        current = now_ts()
        sent_today = await self.store.count_sent_today(umo)
        if sent_today >= self.config.max_per_day:
            return None

        last_proactive = float(session.get("last_proactive_ts") or 0)
        cooldown = self.config.cooldown_minutes * 60
        if last_proactive and current - last_proactive < cooldown:
            return None

        picked = await self._pick_trigger(session, current, moment)
        if not picked:
            return None

        trigger, reason, note = picked
        return await self.fire(session, trigger, reason, note)

    # ==================================================================
    #  发送失败：计数并在连续失败后自动停掉该群
    # ==================================================================
    async def _register_send_failure(self, session: dict, umo: str, error: str) -> None:
        """记录一次发送失败；连续失败到上限就自动暂停该会话。

        为什么必须这样：发送失败时**不会**写 ``last_proactive_ts``，
        于是冷却时间永远是 0 —— 调度器每 30 秒就会再试一次。
        用户退群/群被解散后，日志里就会看到插件「一直重试」，永不停止。

        这里改成：连续失败 ``max_send_failures`` 次后把会话暂停，
        并在原因里写清楚，面板与 ``/主动 状态`` 都能看到。
        """
        limit = max(1, int(getattr(self.config, "max_send_failures", 3) or 3))
        count = int(session.get("send_failures") or 0) + 1
        updates: dict[str, Any] = {
            "send_failures": count,
            "last_send_error": str(error or "")[:200],
        }
        if count >= limit:
            updates.update({
                "paused": 1,
                "paused_reason": (
                    f"连续 {count} 次发送失败，已自动停用"
                    f"（最后错误：{str(error or '')[:80]}）"
                    "。修好后可在面板或「/主动 恢复」重新开启。"
                ),
            })
            self._warn(
                f"{umo} 连续 {count} 次发送失败，已自动停用该会话（{error}）"
            )
        else:
            self._warn(f"{umo} 发送失败第 {count}/{limit} 次：{error}")
        try:
            await self.store.update_session(umo, **updates)
        except Exception as exc:
            self._warn(f"记录发送失败时异常（已忽略）: {exc}")

    # ==================================================================
    #  回应窗口结算
    # ==================================================================
    async def _settle_reply_window(self, session: dict) -> dict:
        if not int(session.get("awaiting_reply") or 0):
            return session

        umo = session.get("umo") or ""
        elapsed = now_ts() - float(session.get("last_proactive_ts") or 0)
        if elapsed <= self.config.reply_window_minutes * 60:
            return session

        streak = int(session.get("unanswered_streak") or 0) + 1
        fields: dict[str, Any] = {"unanswered_streak": streak, "awaiting_reply": 0}
        if streak >= self.config.unanswered_pause:
            fields["paused"] = 1
            fields["paused_reason"] = f"连续 {streak} 次主动消息没有人回应，已自动暂停"
            self._info(f"会话 {umo} 连续 {streak} 次无回应，自动暂停")
        await self.store.update_session(umo, **fields)
        return {**session, **fields}

    # ==================================================================
    #  触发判定
    # ==================================================================
    async def _pick_trigger(
        self, session: dict, current: float, moment: _dt.datetime
    ) -> tuple[str, str, str] | None:
        umo = session.get("umo") or ""
        last_human = float(session.get("last_human_ts") or 0)
        gap_secs = self.config.min_human_gap_minutes * 60
        quiet_enough = (not last_human) or (current - last_human) >= gap_secs

        # 1) 话题追问——优先级最高，因为它是唯一「有明确未完成对话」的场景
        if self.config.followup_enable and int(session.get("pending_question") or 0):
            asked_at = float(session.get("pending_question_ts") or 0)
            waited = current - asked_at
            if asked_at and waited >= self.config.followup_minutes * 60 and quiet_enough:
                return (
                    "followup",
                    f"你提问后已经 {humanize_delta(waited)} 没人接话",
                    "可以自己把话题续下去，或者换一个更轻松的方向，但不要重复问同一个问题",
                )

        # 2) 定时问候
        if self.config.schedule_enable and quiet_enough:
            due = await self._due_schedule(umo, moment)
            if due is not None:
                label = due.get("name") or due.get("at_time") or "定时规则"
                return ("schedule", f"到达定时规则「{label}」", str(due.get("brief") or ""))

        # 3) 空闲唤醒
        if self.config.idle_enable and last_human and quiet_enough:
            idle_for = current - last_human
            if idle_for >= self.config.idle_minutes * 60:
                return (
                    "idle",
                    f"群聊已经安静了 {humanize_delta(idle_for)}（阈值 {self.config.idle_minutes} 分钟）",
                    "顺着大家刚才聊的内容自然接一句，别显得突兀",
                )

        # 4) 随机关怀——按当天预先排好、可复现的随机时刻表执行
        if self.config.random_enable and quiet_enough:
            plan = self.random_targets(umo, moment)
            done = await self.store.count_trigger_today(umo, "random")
            if done < len(plan) and current >= plan[done]:
                return (
                    "random",
                    f"每日随机关怀第 {done + 1}/{len(plan)} 次",
                    "就是突然想到大家了，分享点轻松的东西或者随口聊一句",
                )

        return None

    async def _due_schedule(self, umo: str, moment: _dt.datetime) -> dict | None:
        try:
            schedules = await self.store.list_schedules(umo)
        except Exception:
            return None
        today = day_key()
        weekday = str(moment.isoweekday())
        for item in schedules:
            if not int(item.get("enabled") or 0):
                continue
            if item.get("last_run_date") == today:
                continue
            weekdays = {part.strip() for part in str(item.get("weekdays") or "").split(",") if part.strip()}
            if weekdays and weekday not in weekdays:
                continue
            hour, minute = parse_hhmm(item.get("at_time"), (-1, -1))
            if hour < 0:
                continue
            target = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
            delta_minutes = (moment - target).total_seconds() / 60
            if delta_minutes < 0:
                continue  # 还没到点
            if delta_minutes > SCHEDULE_TOLERANCE_MINUTES:
                # 错过了太久，标记为已执行，避免补发一个不合时宜的问候
                await self.store.mark_schedule_run(int(item.get("id") or 0), today)
                self._info(f"定时规则「{item.get('name') or item.get('at_time')}」已错过，跳过")
                continue
            await self.store.mark_schedule_run(int(item.get("id") or 0), today)
            return item
        return None

    def random_targets(self, umo: str, moment: _dt.datetime | None = None) -> list[float]:
        """当天随机关怀的时间点（时间戳列表）。

        用 ``umo + 日期`` 做随机种子，所以同一天重启插件得到的排期完全一致，
        不会因为重启就把当天次数重置掉。
        """
        moment = moment or _dt.datetime.now()
        key = f"{umo}|{moment.strftime('%Y-%m-%d')}"
        cached = self._random_plan.get(key)
        if cached is not None:
            return cached

        low, high = self.config.random_range
        if high <= 0:
            self._random_plan = {key: []}
            return []

        (quiet_start_h, quiet_start_m), (quiet_end_h, quiet_end_m) = self.config.quiet_range
        window_begin = quiet_end_h * 60 + quiet_end_m       # 静默结束 = 可发言起点
        window_end = quiet_start_h * 60 + quiet_start_m     # 静默开始 = 可发言终点
        if window_end <= window_begin:
            self._random_plan = {key: []}
            return []

        rng = random.Random(key)
        count = rng.randint(low, high)
        minutes = sorted(rng.uniform(window_begin, window_end) for _ in range(count))
        midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        stamps = [midnight + value * 60 for value in minutes]

        # 只保留当天的排期，避免字典无限增长
        self._random_plan = {key: stamps}
        return stamps

    # ==================================================================
    #  即时搭话（群友一发言就按概率接一句）
    # ==================================================================
    async def try_instant(self, session: dict, moment: _dt.datetime | None = None) -> dict | None:
        """收到群友消息后立刻判定要不要接一句。

        与轮询触发的区别：

        - 不看「距最后一条真人消息的最短间隔」——刚有人说话才轮到它，
          那条闸门本来就是用来防止在热闹时插话的，对它不适用
        - 用独立的**秒级**冷却，而不是全局的分钟级冷却，否则 120 分钟冷却
          会让这个功能形同虚设
        - 总开关、生效范围、静默时段、每日上限这些仍然照常生效
        """
        if not self.config.enabled or not self.config.instant_enable:
            return None

        umo = session.get("umo") or ""
        if not umo or not int(session.get("enabled") or 0):
            return None
        if int(session.get("paused") or 0):
            return None
        if not self._scope_allowed(session):
            return None

        moment = moment or _dt.datetime.now()
        if in_quiet_hours(moment, *self.config.quiet_range):
            return None

        if await self.store.count_sent_today(umo) >= self.config.max_per_day:
            return None

        last_proactive = float(session.get("last_proactive_ts") or 0)
        if last_proactive and now_ts() - last_proactive < self.config.instant_cooldown_seconds:
            return None

        probability = self.config.instant_probability_for(session)
        if probability <= 0:
            return None
        if random.random() >= probability:
            self._info(f"会话 {umo} 即时搭话未命中概率（{probability:.0%}）")
            return None

        return await self.fire(
            session,
            "instant",
            f"群友刚发言，按 {probability:.0%} 的概率搭话",
            "顺着刚才那句话自然接一句，像群里一个真人那样搭腔；"
            "不要复述对方的话，不要每句都提问，没话可接就换个轻松的方向",
        )

    # ==================================================================
    #  执行发送
    # ==================================================================
    async def fire(
        self,
        session: dict,
        trigger: str,
        reason: str,
        note: str = "",
        *,
        ignore_guards: bool = False,
    ) -> dict:
        """生成并发送一条主动消息，同时落库、更新会话状态。"""
        umo = session.get("umo") or ""
        record: dict[str, Any] = {
            "umo": umo,
            "trigger": trigger,
            "trigger_label": TRIGGER_LABELS.get(trigger, trigger),
            "reason": reason,
            "content": "",
            "sent": False,
            "error": "",
        }

        outcome = await self.generator.generate(session, trigger, reason, note)
        if not outcome.get("ok"):
            record["error"] = outcome.get("error") or "生成失败"
            await self.store.add_history(umo, trigger, f"{reason}（生成失败）", record["error"], sent=False)
            return record

        text = outcome["content"]
        record["content"] = text

        dry = self.config.dry_run and not ignore_guards
        if dry:
            await self.store.add_history(umo, trigger, f"{reason}（演练模式，未发送）", text, sent=False)
            record["preview"] = True
            return record

        sent = False
        try:
            sent = bool(await self.sender(umo, text))
        except Exception as exc:
            record["error"] = f"发送失败: {exc}"
            self._warn(f"发送到 {umo} 失败: {exc}")

        record["sent"] = sent
        await self.store.add_history(umo, trigger, reason, text, sent=sent)
        if not sent:
            record["error"] = record["error"] or "发送未成功"
            await self._register_send_failure(session, umo, record["error"])
            return record

        # 发送成功：清掉失败计数与上次的错误说明
        if session.get("send_failures") or session.get("last_send_error"):
            await self.store.update_session(
                umo, send_failures=0, last_send_error=""
            )

        updates: dict[str, Any] = {
            "last_proactive_ts": now_ts(),
            "last_bot_ts": now_ts(),
            "awaiting_reply": 1,
            "pending_question": 0,
        }
        if looks_like_question(text):
            # 自己抛了问题，就允许「话题追问」在没人接话时再补一句
            updates["pending_question"] = 1
            updates["pending_question_ts"] = now_ts()
        await self.store.update_session(umo, **updates)
        await self.store.bump_session(umo, "total_sent", 1)

        # 写进上下文，这样群友回复时机器人记得自己刚说过什么
        if self.config.inject_into_context:
            try:
                await self.store.add_message(
                    umo, "bot", text, sender_id=self.bot_id, sender_name=self.bot_name
                )
                await self.store.prune_messages(umo, keep=200)
            except Exception as exc:
                self._warn(f"回写上下文失败: {exc}")

        self._info(f"已向 {umo} 发送主动消息（{record['trigger_label']}）: {text}")
        return record

    # ==================================================================
    #  对外查询（供面板倒计时用）
    # ==================================================================
    async def estimate_next(self, session: dict, moment: _dt.datetime | None = None) -> dict:
        """估算该会话下一次可能开口的时间。仅供展示，不参与实际判定。"""
        moment = moment or _dt.datetime.now()
        umo = session.get("umo") or ""
        current = moment.timestamp()
        if not int(session.get("enabled") or 0):
            return {"eta": None, "why": "已关闭"}
        if int(session.get("paused") or 0):
            return {"eta": None, "why": session.get("paused_reason") or "已暂停"}
        if not self.config.enabled:
            return {"eta": None, "why": "总开关未打开"}
        if not self._scope_allowed(session):
            return {"eta": None, "why": "不在生效范围内"}
        if in_quiet_hours(moment, *self.config.quiet_range):
            # 静默时段结束后才可能说话
            _, (begin_h, begin_m) = self.config.quiet_range
            target = moment.replace(hour=begin_h, minute=begin_m, second=0, microsecond=0)
            if target.timestamp() <= current:
                target += _dt.timedelta(days=1)
            return {"eta": target.timestamp(), "why": "静默时段中"}

        candidates: list[tuple[float, str]] = []

        if self.config.followup_enable and int(session.get("pending_question") or 0):
            asked_at = float(session.get("pending_question_ts") or 0)
            if asked_at:
                candidates.append((asked_at + self.config.followup_minutes * 60, "话题追问"))

        if self.config.idle_enable:
            last_human = float(session.get("last_human_ts") or 0)
            if last_human:
                candidates.append((last_human + self.config.idle_minutes * 60, "空闲唤醒"))

        if self.config.schedule_enable:
            try:
                for item in await self.store.list_schedules(umo):
                    hour, minute = parse_hhmm(item.get("at_time"), (-1, -1))
                    if hour < 0 or not int(item.get("enabled") or 0):
                        continue
                    target = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if target.timestamp() <= current or item.get("last_run_date") == day_key():
                        target += _dt.timedelta(days=1)
                    candidates.append((target.timestamp(), f"定时问候「{item.get('name') or item.get('at_time')}」"))
            except Exception:
                pass

        if self.config.random_enable:
            plan = self.random_targets(umo, moment)
            done = await self.store.count_trigger_today(umo, "random")
            if done < len(plan):
                candidates.append((plan[done], "随机关怀"))

        if not candidates:
            if self.config.instant_enable:
                percent = int(self.config.instant_probability_for(session) * 100)
                return {"eta": None, "why": f"群友发言时有 {percent}% 概率搭话"}
            return {"eta": None, "why": "当前没有可用的触发方式"}

        # 冷却与每日上限会把时间往后推，这里做一个近似补偿
        last_proactive = float(session.get("last_proactive_ts") or 0)
        floor = last_proactive + self.config.cooldown_minutes * 60
        eta, why = min(candidates, key=lambda item: item[0])
        eta = max(eta, floor, current)
        return {"eta": eta, "why": why}

    # ==================================================================
    #  辅助
    # ==================================================================
    def _scope_allowed(self, session: dict) -> bool:
        scope = session.get("scope") or "group"
        target = str(session.get("target_id") or "")
        if scope == "private":
            return self.config.private_allowed(target)
        return self.config.group_allowed(target)

    def status(self) -> dict:
        return {
            "running": self.running,
            "tick_count": self.tick_count,
            "error_count": self.error_count,
            "last_tick_ts": self.last_tick_ts,
            "tick_seconds": self.config.tick_seconds,
            "dry_run": self.config.dry_run,
        }

    def _info(self, message: str) -> None:
        if self.logger is not None:
            try:
                self.logger.info(f"[微光] {message}")
            except Exception:
                pass

    def _warn(self, message: str) -> None:
        if self.logger is not None:
            try:
                self.logger.warning(f"[微光] {message}")
            except Exception:
                pass
