"""微光 · 主动关怀 —— 插件主入口。

职责边界：

- 监听群消息，按隔离规则决定「记不记」，然后落库并更新会话状态
- 借 ``after_message_sent`` 钩子把机器人自己的回复也记进上下文，
  这样「话题追问」才知道机器人刚问过什么
- 借 ``on_llm_request`` 钩子把主动消息补进对话上下文，让后续聊天接得上
- 暴露 ``/主动`` 与 ``/记忆`` 两条指令
- 启动 / 关闭调度器与 WebUI 面板

本文件只做「编排」，具体逻辑都在 ``proactive/`` 与 ``web/`` 里。
"""

from __future__ import annotations

import asyncio
import os
import random
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .proactive.config import Config
from .proactive.context import ContextBuilder, looks_like_question
from .proactive.filters import IgnoreRule
from .proactive.generator import TRIGGER_LABELS, Generator
from .proactive.llm import LLMClient
from .proactive.memory import MemoryManager
from .proactive.sanitize import scrub_request, strip_system_reminders
from .proactive.scheduler import Scheduler
from .proactive.store import Store
from .proactive.textutil import format_ts, humanize_delta, now_ts

PLUGIN_NAME = "astrbot_plugin_proactive_care"

# ---- 消息链构造：不同 AstrBot 版本导出的位置不完全一致，逐级降级 ----
MessageChain: Any = None
Plain: Any = None
try:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.api.event import MessageChain  # type: ignore
except Exception:
    try:
        from astrbot.api.message_components import MessageChain  # type: ignore
    except Exception:
        MessageChain = None
try:  # pragma: no cover
    from astrbot.api.message_components import Plain  # type: ignore
except Exception:
    Plain = None


class ProactiveCarePlugin(Star):
    """微光插件。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        # 保留 AstrBot 传进来的原始配置对象，指令里改设置时要写回它才能持久化
        self._raw_config = config
        self.cfg = Config(config or {})
        self.rules = IgnoreRule(self.cfg)

        self.store = Store(self._resolve_data_dir())
        self.llm = LLMClient(context, self.cfg, logger)
        self.context_builder = ContextBuilder(self.cfg, self.store)
        self.memory = MemoryManager(self.cfg, self.store, self.llm, self.context_builder, logger)
        self.generator = Generator(self.cfg, self.store, self.memory, self.llm, self.context_builder, logger)
        self.scheduler = Scheduler(self.cfg, self.store, self.memory, self.generator, self._send, logger)

        self.web = None
        # 正在等待延迟发声的会话，避免一条消息触发多个并发任务
        self._instant_pending: set[str] = set()
        # 持有即时搭话任务的强引用（见 _record_incoming 里的说明）
        self._instant_tasks: set[asyncio.Task] = set()
        self._register_pages(context)

        for warning in self.cfg.warnings:
            logger.warning(f"[微光] 配置提醒：{warning}")

    # ==================================================================
    #  生命周期
    # ==================================================================
    async def initialize(self) -> None:
        await self._boot()

    @filter.on_astrbot_loaded()
    async def on_loaded(self) -> None:
        await self._boot()

    async def _boot(self) -> None:
        """启动调度器。重复调用是安全的（Scheduler.start 自带幂等）。"""
        if self.scheduler.running:
            return
        try:
            await self.scheduler.start()
        except Exception as exc:
            logger.warning(f"[微光] 调度器启动失败: {exc}")

    async def terminate(self) -> None:
        try:
            await self.scheduler.stop()
        except Exception:
            pass
        try:
            self.store.close()
        except Exception:
            pass
        logger.info("[微光] 插件已卸载")

    def _resolve_data_dir(self) -> str:
        """数据一律放在 AstrBot 的 data 目录下，插件更新/重装不会丢。"""
        candidates = (
            ("astrbot.core.utils.astrbot_path", "get_astrbot_plugin_data_path", PLUGIN_NAME),
            (
                "astrbot.core.utils.astrbot_path",
                "get_astrbot_data_path",
                os.path.join("plugin_data", PLUGIN_NAME),
            ),
        )
        for module_path, func_name, suffix in candidates:
            try:
                module = __import__(module_path, fromlist=[func_name])
                base = getattr(module, func_name)()
            except Exception:
                continue
            if base:
                return os.path.join(str(base), suffix)
        # 兜底：假设插件位于 <AstrBot>/data/plugins/<plugin>/
        fallback = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        return os.path.join(fallback, "data", "plugin_data", PLUGIN_NAME)

    # ==================================================================
    #  WebUI 面板
    # ==================================================================
    def _register_pages(self, context: Context) -> None:
        """注册面板接口。低版本 AstrBot 没有这个能力，静默降级即可。"""
        try:
            from .web.api import WebController
            from .web.service import PanelService

            service = PanelService(self)
            self.web = WebController(context, service, logger)
            self.web.register_routes()
        except Exception as exc:
            logger.warning(f"[微光] 配置面板注册失败（不影响主动消息功能）: {exc}")
            self.web = None

    # ==================================================================
    #  消息接入
    # ==================================================================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """所有进来的消息都先经过这里，决定记不记。

        注意：这里**不 yield 任何东西**，也不调用 stop_event，
        所以不会影响其它插件（包括匿名树洞）的正常工作。
        """
        try:
            await self._record_incoming(event)
        except Exception as exc:
            logger.warning(f"[微光] 记录消息时异常: {exc}")

    async def _record_incoming(self, event: AstrMessageEvent) -> None:
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return

        target = self._target_of(event)
        if target is None:
            return
        scope, target_id = target

        text = (getattr(event, "message_str", "") or "").strip()
        if not text:
            return

        ignore, reason = self.rules.judge(
            text=text,
            sender_id=self._safe(event.get_sender_id),
            sender_name=self._safe(event.get_sender_name),
            self_id=self._safe(event.get_self_id),
        )
        if ignore:
            logger.debug(f"[微光] 跳过一条消息（{reason}）")
            return

        # 机器人自己发的消息**不进"群友发言"这条账**。
        #
        # 这类消息正常会被上面的忽略规则挡掉，但不能只依赖配置：一旦
        # ignore_self_sent 被关掉，机器人自己的话就会被当成真人发言写进
        # last_human_ts，而「即时搭话」正是以"刚有真人说话"为前提的——
        # 结果就是自己说一句、再触发一次搭话，形成单口相声闭环。
        # 另外，把机器人自己的话记成 "user" 也会污染记忆抽取的语料。
        if self._is_self_sent(event):
            logger.debug("[微光] 机器人自己的消息，不计入群友发言")
            return

        await self.store.ensure_session(umo, scope, target_id, enabled=True)
        session = await self.store.get_session(umo) or {}

        await self.store.add_message(
            umo,
            "user",
            text,
            sender_id=self._safe(event.get_sender_id),
            sender_name=self._safe(event.get_sender_name),
        )

        # 有人说话了 -> 结束「等待回应」状态，沉默计数清零
        if int(session.get("awaiting_reply") or 0):
            await self.store.mark_latest_replied(umo)
        await self.store.update_session(
            umo,
            last_human_ts=now_ts(),
            awaiting_reply=0,
            pending_question=0,
            unanswered_streak=0,
        )
        # 重新读一次：上面刚写过 last_human_ts，而 session 是写之前取的快照。
        # 后面的即时搭话闸门要用到它，不刷新就会拿旧值判断。
        session = await self.store.get_session(umo) or session

        await self.store.prune_messages(umo, keep=200)
        await self.memory.maybe_extract(umo, session)

        # 即时搭话：群友一发言就掷一次骰子，命中就隔几秒接一句。
        #
        # 这些情况**一律不搭话**，否则会出「自言自语」：机器人自己发的消息
        # 若也被当成"群友发言"，就会一环扣一环地自己跟自己聊下去。
        if not self.cfg.instant_enable:
            return
        # ① 机器人自己发的消息：绝不触发。
        #    必须放在最前面——忽略规则在更后面才判，那之前任务就已经排上了。
        if self._is_self_sent(event):
            logger.debug("[微光] 机器人自己的消息，不触发即时搭话")
            return
        # ② @机器人 / 唤醒消息：交给正常管线回，否则一次 @ 会得到两种语气
        if self._was_addressed(event):
            logger.debug("[微光] 这条是 @机器人/唤醒消息，交给正常管线，不即时搭话")
            return
        # ③ 机器人刚说过话（正常回复或上一次主动消息）
        if self._bot_replied_recently(session):
            logger.debug("[微光] 机器人刚说过话，本次不即时搭话")
            return
        # ④ 最近根本没有真人说过话：没人在场就不该发言，
        #    否则延时任务会变成"隔一阵自动冒一句"的定时自言自语。
        if not self._human_spoke_recently(session):
            logger.debug("[微光] 最近没有真人发言，不即时搭话")
            return

        if self.cfg.enabled and umo not in self._instant_pending:
            self._instant_pending.add(umo)
            # 必须握住强引用：事件循环只保留 task 的弱引用，
            # 没人引用时可能在 await 中途被 GC 掉，任务静默消失且不报错，
            # 而 finally 里的 _instant_pending.discard 永远不会执行，
            # 该会话就被永久卡住（表现为"有时候就不搭话了"）。
            task = asyncio.create_task(self._instant_later(umo), name=f"proactive-instant-{umo}")
            self._instant_tasks.add(task)
            task.add_done_callback(self._instant_tasks.discard)

    def _is_self_sent(self, event: AstrMessageEvent) -> bool:
        """这条消息是不是机器人自己发出来的。

        即时搭话最怕把自己发的消息也当成"群友发言"——那会形成闭环：
        自己说一句 → 触发一次搭话 → 再说一句……群里就成了机器人的单口相声。
        """
        self_id = self._safe(event.get_self_id)
        sender_id = self._safe(event.get_sender_id)
        return bool(self_id) and self_id == sender_id

    def _human_spoke_recently(self, session: dict) -> bool:
        """最近是否真的有真人说过话。

        即时搭话的前提是「刚有人在聊」。若距最后一条真人消息已经超过
        ``max(60, min_human_gap_minutes*60)`` 秒，说明群里早没人了，
        这时再开口不属于"搭话"，只是定时自言自语。

        这个判断是纯时间比较，不依赖"当前这条消息是谁发的"——机器人自己的
        消息在 :meth:`_record_incoming` 里已被排除，不会刷新 ``last_human_ts``。
        """
        last_human = float((session or {}).get("last_human_ts") or 0)
        if not last_human:
            return False
        limit = max(60.0, float(self.cfg.min_human_gap_minutes) * 60.0)
        return (now_ts() - last_human) <= limit

    def _bot_replied_recently(self, session: dict) -> bool:
        """机器人是否刚回过话。

        正常回复由 ``after_message_sent`` 写 ``last_bot_ts``；
        主动消息由 ``scheduler.fire`` 写同一个字段。
        距现在不足 ``instant_reply_guard_seconds`` 就认为"刚说过"，
        此时插话必然造成重复回应。
        """
        guard = float(getattr(self.cfg, "instant_reply_guard_seconds", 0) or 0)
        if guard <= 0:
            return False
        last_bot = float((session or {}).get("last_bot_ts") or 0)
        if not last_bot:
            return False
        return (now_ts() - last_bot) < guard

    def _was_addressed(self, event: AstrMessageEvent) -> bool:
        """这条消息是不是在跟机器人说话（@机器人 / 唤醒词）。

        AstrBot 的 ``AstrMessageEvent.is_wake_up()`` 就是干这个的
        （内部读 ``is_wake`` 标记）。不同版本可能只有属性没有方法，
        因此两种形态都试；都取不到时保守返回 False——
        不能因为接口缺失就永远不搭话。
        """
        for name in ("is_wake_up", "is_at_or_wake", "is_wake"):
            value = getattr(event, name, None)
            if value is None:
                continue
            if callable(value):
                try:
                    if value():
                        return True
                except Exception:
                    pass
                continue
            if bool(value):
                return True
        return False

    async def _instant_later(self, umo: str) -> None:
        """延迟一小会儿再判定，秒回太像机器人了。"""
        try:
            low, high = self.cfg.instant_delay
            await asyncio.sleep(random.uniform(low, high))
            session = await self.store.get_session(umo)
            if session is None or not int(session.get("enabled") or 0):
                return
            # 延迟期间群里可能又有人说话了，拿最新的会话状态来判定
            await self.scheduler.try_instant(session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[微光] 即时搭话判定异常: {exc}")
        finally:
            self._instant_pending.discard(umo)

    @filter.after_message_sent()
    async def on_message_sent(self, event: AstrMessageEvent):
        """机器人自己的回复也要进上下文，否则「话题追问」无从判断。"""
        try:
            umo = getattr(event, "unified_msg_origin", "") or ""
            if not umo or self._target_of(event) is None:
                return
            text = self._extract_sent_text(event)
            if not text:
                return
            # 机器人回复里可能混入系统注入的元信息（群名/时间/上下文块）。
            # 一旦存进历史，下一轮会被当成"上文"喂回模型，越滚越长且人格漂移，
            # 所以入库前就清掉——这是切断循环最关键的一步。
            raw_len = len(text)
            text = strip_system_reminders(text).strip()
            if not text:
                return
            if len(text) != raw_len:
                logger.info(f"[微光] 已从回复中剔除 {raw_len - len(text)} 字符系统注入内容")
            await self.store.add_message(umo, "bot", text, sender_name="微光")
            updates: dict[str, Any] = {"last_bot_ts": now_ts()}
            if looks_like_question(text):
                updates["pending_question"] = 1
                updates["pending_question_ts"] = now_ts()
            await self.store.update_session(umo, **updates)
        except Exception as exc:
            logger.debug(f"[微光] 记录机器人回复时异常: {exc}")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: Any):
        """把最近的主动消息补进上下文，让群友回复时机器人「记得」自己说过什么。

        为什么需要这一步：主动消息是 AstrBot 直接发出去的，不会进入它自己的
        对话历史。不补的话，用户回一句「什么？」机器人会一脸茫然。

        同时做一件事：**清掉注入内容里残留的 ``<system_reminder>`` 块**。
        AstrBot 内置会把「群名 + 当前时间」和「你上一条回复之后的群聊上下文」
        作为 system_reminder 注入进请求；这些是**给模型看的元信息**，
        但模型有时会把它们当成对话内容照抄出来，于是回复正文里带上：
            <system_reminder>Group name: xxx
            Current datetime: 2026-10-03 22:39 (CST), Weekday: Saturday</system_reminder>
        更糟的是这些内容会被写进对话历史，下一轮又被当成"上文"喂回去，
        越滚越长、人格也跟着漂。这里在注入前把残留清掉，切断这个循环。
        """
        # —— 先清理：这一步与 inject_into_context 无关，任何情况下都该做
        try:
            n = scrub_request(req)
            if n:
                logger.info(f"[微光] 已清理 {n} 处残留的 system_reminder/上下文块")
        except Exception as exc:
            logger.debug(f"[微光] 清理 system_reminder 失败（已忽略）: {exc}")

        if not self.cfg.inject_into_context:
            return
        try:
            umo = getattr(event, "unified_msg_origin", "") or ""
            if not umo:
                return
            history = await self.store.list_history(umo, limit=1)
            if not history:
                return
            latest = history[0]
            if not latest.get("sent"):
                return
            if now_ts() - float(latest.get("ts") or 0) > 3 * 3600:
                return
            text = str(latest.get("content") or "").strip()
            if not text:
                return

            contexts = getattr(req, "contexts", None)
            if not isinstance(contexts, list):
                return
            if any(self._context_has(item, text) for item in contexts):
                return

            entry = self._context_entry(contexts, text)
            if entry is not None:
                contexts.append(entry)
        except Exception as exc:
            logger.debug(f"[微光] 注入上下文失败（已忽略）: {exc}")

    # ==================================================================
    #  指令
    # ==================================================================
    @filter.command("主动")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_proactive(self, event: AstrMessageEvent, arg: str = ""):
        """主动消息控制台：/主动 [状态|开启|关闭|立即|预览|暂停|恢复]"""
        action = (arg or "").strip().lower()
        umo = getattr(event, "unified_msg_origin", "") or ""

        if action in ("", "状态", "status"):
            yield event.plain_result(await self._status_text(umo))
            return

        if action in ("开启", "on"):
            await self._set_enabled(True)
            await self._boot()
            yield event.plain_result("微光已开启。群聊安静下来后它会主动开口，具体规则可在面板里调整。")
            return

        if action in ("关闭", "off"):
            await self._set_enabled(False)
            yield event.plain_result("微光已关闭，不会再主动发消息。")
            return

        if action in ("暂停", "pause"):
            await self.store.ensure_session(umo, *self._scope_of(event), enabled=True)
            await self.store.update_session(umo, paused=1, paused_reason="手动暂停")
            yield event.plain_result("当前会话已暂停主动消息。用 `/主动 恢复` 重新开启。")
            return

        if action in ("恢复", "resume"):
            await self.store.ensure_session(umo, *self._scope_of(event), enabled=True)
            await self.store.update_session(
                umo, paused=0, paused_reason="", unanswered_streak=0, awaiting_reply=0
            )
            yield event.plain_result("当前会话已恢复。")
            return

        if action in ("立即", "now", "触发"):
            session = await self.store.get_session(umo)
            if session is None:
                session = await self.store.ensure_session(umo, *self._scope_of(event), enabled=True)
            outcome = await self.scheduler.fire(
                session, "manual", "管理员手动触发", ignore_guards=True
            )
            if outcome.get("sent"):
                yield event.plain_result(f"已发送：{outcome['content']}")
            elif outcome.get("preview"):
                yield event.plain_result(f"演练模式，未真正发送。生成内容：{outcome['content']}")
            else:
                yield event.plain_result(f"生成或发送失败：{outcome.get('error') or '未知原因'}")
            return

        if action in ("预览", "preview"):
            session = await self.store.get_session(umo)
            if session is None:
                session = await self.store.ensure_session(umo, *self._scope_of(event), enabled=True)
            outcome = await self.generator.generate(session, "preview", "管理员预览")
            if outcome.get("ok"):
                yield event.plain_result(f"预览（未发送）：\n{outcome['content']}")
            else:
                yield event.plain_result(f"生成失败：{outcome.get('error') or '未知原因'}")
            return

        if action in ("概率", "prob") or action.startswith("概率") or action.startswith("prob"):
            value = action.replace("概率", "").replace("prob", "").strip()
            if not value:
                yield event.plain_result(
                    f"当前即时搭话：{'开启' if self.cfg.instant_enable else '关闭'}，"
                    f"概率 {self.cfg.instant_probability_percent}%。\n"
                    "用法：`/主动 概率 30`（0~100，0 等于关闭）"
                )
                return
            try:
                percent = int(float(value))
            except ValueError:
                yield event.plain_result("概率要填 0~100 的数字，例如 `/主动 概率 30`。")
                return
            if not 0 <= percent <= 100:
                yield event.plain_result("概率要在 0~100 之间。")
                return
            await self._set_instant_probability(percent)
            yield event.plain_result(
                f"即时搭话概率已设为 {percent}%"
                + ("（概率低于 1% 基本不会触发）" if percent < 1 else "")
                + ("。群里会比较热闹，注意别太吵。" if percent >= 60 else "。")
            )
            return

        yield event.plain_result(
            "用法：/主动 [状态|开启|关闭|立即|预览|暂停|恢复|概率]\n"
            "「立即」会真的发出去，「预览」只生成不发送，「概率 30」调节即时搭话的触发概率。"
        )

    @filter.command("记忆")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_memory(self, event: AstrMessageEvent, arg: str = ""):
        """长期记忆管理：/记忆 [列表|搜索 关键词|添加 内容|删除 ID|清空|整理]"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        parts = (arg or "").strip().split(maxsplit=1)
        action = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if action in ("", "列表", "list"):
            rows = await self.store.list_memories(umo=umo, limit=15)
            if not rows:
                yield event.plain_result("这个会话还没有任何记忆。多聊几句，或发送 `/记忆 整理` 立刻抽取一次。")
                return
            lines = [f"共 {len(rows)} 条（最多显示 15 条）："]
            for row in rows:
                lines.append(
                    f"#{row['id']} [{row.get('kind') or 'fact'}] {row.get('content')} "
                    f"（重要度 {row.get('importance')}）"
                )
            yield event.plain_result("\n".join(lines))
            return

        if action in ("搜索", "search"):
            if not rest:
                yield event.plain_result("用法：/记忆 搜索 关键词")
                return
            rows = await self.store.list_memories(umo=umo, limit=15, query=rest)
            if not rows:
                yield event.plain_result(f"没有找到包含「{rest}」的记忆。")
                return
            lines = [f"匹配到 {len(rows)} 条："]
            lines += [f"#{row['id']} {row.get('content')}" for row in rows]
            yield event.plain_result("\n".join(lines))
            return

        if action in ("添加", "add"):
            if not rest:
                yield event.plain_result("用法：/记忆 添加 要记住的内容")
                return
            from .proactive.textutil import normalize

            memory_id = await self.store.upsert_memory(umo, rest, normalize(rest), importance=0.8)
            yield event.plain_result(f"已记住（#{memory_id}）：{rest}")
            return

        if action in ("删除", "del", "delete"):
            if not rest.isdigit():
                yield event.plain_result("用法：/记忆 删除 记忆ID")
                return
            removed = await self.store.delete_memory(int(rest))
            yield event.plain_result("已删除。" if removed else "没有找到这条记忆。")
            return

        if action in ("清空", "clear"):
            count = await self.store.clear_memories(umo)
            yield event.plain_result(f"已清空 {count} 条记忆。")
            return

        if action in ("整理", "抽取", "extract"):
            session = await self.store.get_session(umo)
            count = await self.memory.extract(umo, session=session)
            if count:
                yield event.plain_result(f"整理完成，新增或更新了 {count} 条记忆。")
            else:
                yield event.plain_result("这次没有抽取到新的记忆（可能是记录太少，或者模型没返回有效内容）。")
            return

        yield event.plain_result(
            "用法：/记忆 [列表|搜索 关键词|添加 内容|删除 ID|清空|整理]\n"
            "「整理」会立刻调用模型抽取一次记忆。"
        )

    # ==================================================================
    #  发送
    # ==================================================================
    async def _send(self, umo: str, text: str) -> bool:
        chain = self._build_chain(text)
        try:
            result = await self.context.send_message(umo, chain)
            return bool(result)
        except Exception as exc:
            logger.warning(f"[微光] 发送消息失败（{umo}）: {exc}")
            return False

    def _build_chain(self, text: str):
        if MessageChain is not None:
            for build in (
                lambda: MessageChain().message(text),
                lambda: MessageChain([Plain(text)]) if Plain is not None else None,
            ):
                try:
                    chain = build()
                except Exception:
                    continue
                if chain is not None:
                    return chain
        if Plain is not None:
            return [Plain(text)]
        return text

    # ==================================================================
    #  辅助
    # ==================================================================
    async def _status_text(self, umo: str) -> str:
        session = await self.store.get_session(umo) or {}
        overview = await self.store.overview()
        status = self.scheduler.status()
        lines = [
            "微光 · 主动关怀",
            f"总开关：{'已开启' if self.cfg.enabled else '已关闭'}"
            + ("（演练模式）" if self.cfg.dry_run else ""),
            f"调度器：{'运行中' if status['running'] else '未运行'}"
            f"（每 {status['tick_seconds']} 秒轮询一次，共轮询 {status['tick_count']} 次）",
            f"已跟踪会话：{overview['sessions']} 个，其中生效 {overview['active']} 个、暂停 {overview['paused']} 个",
            f"主动消息：今日 {overview['sent_today']} 条 / 累计 {overview['sent_total']} 条，"
            f"被回应率 {round(overview['answer_rate'] * 100)}%",
            f"记忆库：{overview['memories']} 条",
            f"即时搭话：{'开启' if self.cfg.instant_enable else '关闭'}，"
            f"概率 {int(self.cfg.instant_probability_for(session) * 100)}%"
            + (
                f"（本群单独设置，全局 {self.cfg.instant_probability_percent}%）"
                if session and int(self.cfg.instant_probability_for(session) * 100)
                != self.cfg.instant_probability_percent
                else ""
            ),
            "",
        ]
        if session:
            eta = await self.scheduler.estimate_next(session)
            last_human = float(session.get("last_human_ts") or 0)
            lines += [
                f"本会话：{'已暂停（' + (session.get('paused_reason') or '') + '）' if session.get('paused') else '正常'}",
                f"最后一条真人发言：{format_ts(last_human)}"
                + (f"（{humanize_delta(now_ts() - last_human)}前）" if last_human else ""),
                f"下次可能开口：{format_ts(eta['eta'])}（{eta['why']}）" if eta.get("eta") else f"下次可能开口：暂无（{eta.get('why')}）",
                f"本会话累计发送：{session.get('total_sent') or 0} 条，连续无回应 {session.get('unanswered_streak') or 0} 次",
            ]
        else:
            lines.append("本会话尚未被跟踪——先让人在这里说句话，或者发送 `/主动 立即` 手动创建。")
        if not self.llm.list_providers():
            lines.append("")
            lines.append("提示：当前没有检测到任何对话模型，生成会失败，请先到「服务提供商」页配置。")
        return "\n".join(lines)

    async def _set_enabled(self, value: bool) -> None:
        """改总开关并落盘。写回的是 AstrBot 的配置对象，重载插件后依然生效。"""
        if self._raw_config is None:
            self._raw_config = self.cfg.raw
        try:
            section = self._raw_config.setdefault("basic", {})
            section["enabled"] = bool(value)
        except Exception as exc:
            logger.warning(f"[微光] 写入配置失败: {exc}")
        await self._persist_config()

    async def _set_instant_probability(self, percent: int) -> None:
        """改即时搭话概率并落盘。"""
        if self._raw_config is None:
            self._raw_config = self.cfg.raw
        try:
            section = self._raw_config.setdefault("trigger", {})
            section["instant_probability"] = int(percent)
            if int(percent) > 0:
                section["instant_enable"] = True
        except Exception as exc:
            logger.warning(f"[微光] 写入概率失败: {exc}")
        await self._persist_config()

    async def _persist_config(self) -> None:
        """把配置写回 AstrBot，并热更新内存中的副本。"""
        saver = getattr(self._raw_config, "save_config", None)
        if callable(saver):
            try:
                saver()
            except Exception as exc:
                logger.warning(f"[微光] 保存配置失败: {exc}")
        self.reload_runtime_config(self._raw_config)

    def reload_runtime_config(self, raw: dict | None = None) -> None:
        """面板改完配置后调用，让新规则立刻生效，不用重载插件。"""
        self.cfg = Config(raw if raw is not None else self.cfg.raw)
        self.rules = IgnoreRule(self.cfg)
        for component in (self.llm, self.context_builder, self.memory, self.generator, self.scheduler):
            try:
                component.config = self.cfg
            except Exception:
                pass
        self.scheduler.generator = self.generator
        self.scheduler.memory = self.memory

    def _target_of(self, event: AstrMessageEvent) -> tuple[str, str] | None:
        """返回 (scope, target_id)；不在生效范围则返回 None。"""
        group_id = self._safe(event.get_group_id)
        if group_id:
            return ("group", group_id) if self.cfg.group_allowed(group_id) else None
        user_id = self._safe(event.get_sender_id)
        if user_id and self.cfg.private_allowed(user_id):
            return "private", user_id
        return None

    def _scope_of(self, event: AstrMessageEvent) -> tuple[str, str]:
        group_id = self._safe(event.get_group_id)
        if group_id:
            return "group", group_id
        return "private", self._safe(event.get_sender_id)

    @staticmethod
    def _safe(getter) -> str:
        try:
            value = getter()
        except Exception:
            return ""
        return str(value) if value is not None else ""

    def _extract_sent_text(self, event: AstrMessageEvent) -> str:
        """从发送结果里取出纯文本。取不到就返回空串，不做猜测。"""
        result = None
        for getter in ("get_result",):
            func = getattr(event, getter, None)
            if callable(func):
                try:
                    result = func()
                except Exception:
                    result = None
                if result is not None:
                    break
        if result is None:
            return ""
        for attribute in ("get_plain_text", "message_str"):
            value = getattr(result, attribute, None)
            if callable(value):
                try:
                    text = value()
                except Exception:
                    continue
                if text:
                    return str(text).strip()
            elif isinstance(value, str) and value:
                return value.strip()
        chain = getattr(result, "chain", None)
        if isinstance(chain, list):
            chunks = []
            for component in chain:
                text = getattr(component, "text", None)
                if isinstance(text, str):
                    chunks.append(text)
            return "".join(chunks).strip()
        return ""

    def _context_entry(self, contexts: list, text: str) -> dict | None:
        """按现有 contexts 的形状，造一条 assistant 消息，尽量跟随宿主格式。"""
        sample = next((item for item in contexts if isinstance(item, dict) and item.get("role")), None)
        if sample is None:
            return {"role": "assistant", "content": text}
        content = sample.get("content")
        if isinstance(content, str):
            return {"role": "assistant", "content": text}
        if isinstance(content, list):
            block = next((item for item in content if isinstance(item, dict)), None)
            if block is not None:
                new_block = {**block, "text": text}
                return {"role": "assistant", "content": [new_block]}
            return {"role": "assistant", "content": [{"type": "text", "text": text}]}
        return {"role": "assistant", "content": text}

    @staticmethod
    def _context_has(item: Any, text: str) -> bool:
        if not isinstance(item, dict):
            return False
        content = item.get("content")
        haystack = ""
        if isinstance(content, str):
            haystack = content
        elif isinstance(content, list):
            haystack = " ".join(
                str(part.get("text") or "") for part in content if isinstance(part, dict)
            )
        probe = text[:20]
        return bool(probe) and probe in haystack


__all__ = ["ProactiveCarePlugin", "PLUGIN_NAME", "TRIGGER_LABELS"]
