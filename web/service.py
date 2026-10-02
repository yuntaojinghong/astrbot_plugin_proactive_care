"""面板业务逻辑。

把插件内部状态整理成前端直接可渲染的形状，并处理面板发来的写操作。
刻意与 HTTP 层解耦：``web/api.py`` 只管路由与请求解析，这里只管业务，
两边都能单独测试。
"""

from __future__ import annotations

import json
from typing import Any

from ..proactive.textutil import format_ts, humanize_delta, normalize, now_ts


class PanelService:
    def __init__(self, plugin: Any):
        self.plugin = plugin

    # ==================================================================
    #  属性快捷方式
    # ==================================================================
    @property
    def cfg(self):
        return self.plugin.cfg

    @property
    def store(self):
        return self.plugin.store

    @property
    def scheduler(self):
        return self.plugin.scheduler

    @property
    def memory(self):
        return self.plugin.memory

    # ==================================================================
    #  初始化
    # ==================================================================
    async def bootstrap(self) -> dict:
        return {
            "overview": await self.overview(),
            "config": self.cfg.as_dict(),
            "warnings": list(self.cfg.warnings),
            "providers": self.plugin.llm.list_providers(),
            "scheduler": self.scheduler.status(),
            "limits": await self._limits(),
        }

    async def overview(self) -> dict:
        data = await self.store.overview()
        data.update(
            {
                "enabled": self.cfg.enabled,
                "dry_run": self.cfg.dry_run,
                "scheduler": self.scheduler.status(),
                "now": now_ts(),
            }
        )
        return data

    async def _limits(self) -> dict:
        """把散落在配置里的限制集中展示，方便前端做输入约束。"""
        low, high = self.cfg.random_range
        return {
            "max_per_day": self.cfg.max_per_day,
            "cooldown_minutes": self.cfg.cooldown_minutes,
            "min_human_gap_minutes": self.cfg.min_human_gap_minutes,
            "reply_window_minutes": self.cfg.reply_window_minutes,
            "unanswered_pause": self.cfg.unanswered_pause,
            "idle_minutes": self.cfg.idle_minutes,
            "followup_minutes": self.cfg.followup_minutes,
            "random_range": [low, high],
            "quiet": {
                "start": self.cfg.quiet_range[0],
                "end": self.cfg.quiet_range[1],
            },
            "group_whitelist": self.cfg.group_whitelist,
            "tick_seconds": self.cfg.tick_seconds,
        }

    # ==================================================================
    #  配置
    # ==================================================================
    async def update_config(self, payload: dict) -> dict:
        """合并式更新：只覆盖前端传来的那几个键，同级的其它键保持不动。"""
        if not isinstance(payload, dict) or not payload:
            raise ValueError("配置内容格式不正确")

        plugin = self.plugin
        raw = plugin._raw_config if plugin._raw_config is not None else {}
        # 深合并，避免「只改 trigger.idle_minutes」把整个 trigger 分组冲掉
        merged_raw = _deep_merge_dict(dict(raw), payload)
        try:
            raw.clear()
            raw.update(merged_raw)
        except Exception:
            raw = merged_raw
            plugin._raw_config = raw

        plugin.reload_runtime_config(raw)

        saver = getattr(raw, "save_config", None)
        if callable(saver):
            try:
                saver()
            except Exception:
                pass
        return {"config": self.cfg.as_dict(), "warnings": list(plugin.cfg.warnings)}

    # ==================================================================
    #  会话
    # ==================================================================
    async def list_sessions(self) -> list[dict]:
        sessions = await self.store.list_sessions()
        out = []
        for session in sessions:
            out.append(await self._decorate(session))
        out.sort(key=lambda item: (not item["enabled"], item["scope"], -(item["last_human_ts"] or 0)))
        return out

    async def get_session(self, umo: str) -> dict:
        session = await self.store.get_session(umo)
        if session is None:
            raise ValueError("该会话不存在")
        return await self._decorate(session)

    async def _decorate(self, session: dict) -> dict:
        umo = session.get("umo") or ""
        override = _load_json(session.get("override_json"))
        eta = await self.scheduler.estimate_next(session)
        sent_today = await self.store.count_sent_today(umo)
        memories = await self.store.list_memories(umo=umo, limit=1000)
        last_human = float(session.get("last_human_ts") or 0)
        last_proactive = float(session.get("last_proactive_ts") or 0)
        return {
            "umo": umo,
            "scope": session.get("scope") or "group",
            "target_id": str(session.get("target_id") or ""),
            "name": override.get("name") or "",
            "enabled": bool(session.get("enabled")),
            "paused": bool(session.get("paused")),
            "paused_reason": session.get("paused_reason") or "",
            "in_scope": self.scheduler._scope_allowed(session),
            "last_human_ts": last_human,
            "last_human_text": format_ts(last_human),
            "quiet_for": humanize_delta(now_ts() - last_human) if last_human else "",
            "last_proactive_ts": last_proactive,
            "last_proactive_text": format_ts(last_proactive),
            "total_sent": int(session.get("total_sent") or 0),
            "sent_today": sent_today,
            "unanswered_streak": int(session.get("unanswered_streak") or 0),
            "awaiting_reply": bool(session.get("awaiting_reply")),
            "pending_question": bool(session.get("pending_question")),
            "memory_count": len(memories),
            "override": override,
            "eta": eta.get("eta"),
            "eta_text": format_ts(eta.get("eta")),
            "eta_why": eta.get("why") or "",
        }

    async def update_session(self, umo: str, payload: dict) -> dict:
        if not umo:
            raise ValueError("缺少会话标识")
        session = await self.store.get_session(umo)
        if session is None:
            raise ValueError("该会话不存在")

        fields: dict[str, Any] = {}
        for key in ("enabled", "paused"):
            if key in payload:
                fields[key] = 1 if _truthy(payload[key]) else 0
        if "paused" in fields and not fields["paused"]:
            fields["paused_reason"] = ""
            fields["unanswered_streak"] = 0
            fields["awaiting_reply"] = 0
        if fields.get("paused"):
            fields["paused_reason"] = str(payload.get("paused_reason") or "面板手动暂停")

        override = _load_json(session.get("override_json"))
        if "name" in payload:
            override["name"] = str(payload.get("name") or "")[:40]
        fields["override_json"] = json.dumps(override, ensure_ascii=False)

        await self.store.update_session(umo, **fields)
        return await self.get_session(umo)

    async def delete_session(self, umo: str) -> dict:
        await self.store.delete_session(umo, purge=True)
        return {"deleted": True, "umo": umo}

    async def reset_session(self, umo: str) -> dict:
        """清空某个会话的上下文与发送历史，但保留记忆与开关状态。"""
        await self.store.clear_messages(umo)
        await self.store.clear_history(umo)
        await self.store.update_session(
            umo,
            awaiting_reply=0,
            pending_question=0,
            unanswered_streak=0,
            last_proactive_ts=0,
            paused=0,
            paused_reason="",
        )
        return await self.get_session(umo)

    # ==================================================================
    #  记忆
    # ==================================================================
    async def list_memories(self, umo: str = "", query: str = "", limit: int = 300) -> list[dict]:
        rows = await self.store.list_memories(umo=umo or None, limit=limit, query=query)
        for row in rows:
            row["created_text"] = format_ts(row.get("created_ts"))
            row["hit_text"] = format_ts(row.get("last_hit_ts")) if row.get("last_hit_ts") else "从未"
        return rows

    async def add_memory(self, umo: str, content: str, kind: str = "fact", importance: float = 0.7) -> dict:
        content = (content or "").strip()
        if not umo:
            raise ValueError("缺少会话标识")
        if len(content) < 2:
            raise ValueError("记忆内容太短")
        memory_id = await self.store.upsert_memory(
            umo, content, normalize(content), kind=kind or "fact", importance=_clamp(importance)
        )
        return {"id": memory_id, "content": content}

    async def update_memory(self, memory_id: int, payload: dict) -> dict:
        content = payload.get("content")
        importance = payload.get("importance")
        changed = await self.store.update_memory(
            int(memory_id),
            content=str(content).strip() if content is not None else None,
            importance=_clamp(importance) if importance is not None else None,
        )
        if not changed:
            raise ValueError("没有可更新的内容")
        return {"updated": changed}

    async def delete_memory(self, memory_id: int) -> dict:
        return {"deleted": await self.store.delete_memory(int(memory_id))}

    async def clear_memories(self, umo: str) -> dict:
        return {"deleted": await self.store.clear_memories(umo)}

    async def extract_memories(self, umo: str) -> dict:
        session = await self.store.get_session(umo)
        if session is None:
            raise ValueError("该会话不存在")
        count = await self.memory.extract(umo, session=session)
        return {"extracted": count, "error": self.plugin.llm.last_error}

    async def memory_stats(self, umo: str = "") -> dict:
        if umo:
            return await self.memory.stats(umo)
        rows = await self.store.list_memories(limit=5000)
        return {"total": len(rows), "kinds": {}, "avg_importance": 0.0}

    # ==================================================================
    #  发送历史
    # ==================================================================
    async def list_history(self, umo: str = "", limit: int = 100) -> list[dict]:
        rows = await self.store.list_history(umo=umo or None, limit=limit)
        for row in rows:
            row["time_text"] = format_ts(row.get("ts"))
        return rows

    async def clear_history(self, umo: str = "") -> dict:
        return {"deleted": await self.store.clear_history(umo or None)}

    # ==================================================================
    #  定时规则
    # ==================================================================
    async def list_schedules(self, umo: str = "") -> list[dict]:
        rows = await self.store.list_schedules(umo=umo or None)
        for row in rows:
            row["weekday_list"] = [
                part.strip() for part in str(row.get("weekdays") or "").split(",") if part.strip()
            ]
        return rows

    async def add_schedule(self, payload: dict) -> dict:
        umo = str(payload.get("umo") or "").strip()
        at_time = str(payload.get("at_time") or "").strip()
        if not umo:
            raise ValueError("请先选择要应用到的会话")
        if not _valid_hhmm(at_time):
            raise ValueError("时间格式应为 HH:MM，例如 08:30")
        weekdays = _clean_weekdays(payload.get("weekdays"))
        schedule_id = await self.store.add_schedule(
            umo,
            at_time=at_time,
            name=str(payload.get("name") or "")[:40],
            weekdays=weekdays,
            brief=str(payload.get("brief") or "")[:200],
            enabled=_truthy(payload.get("enabled", True)),
        )
        return {"id": schedule_id}

    async def update_schedule(self, schedule_id: int, payload: dict) -> dict:
        fields: dict[str, Any] = {}
        if "at_time" in payload:
            at_time = str(payload.get("at_time") or "").strip()
            if not _valid_hhmm(at_time):
                raise ValueError("时间格式应为 HH:MM")
            fields["at_time"] = at_time
        if "name" in payload:
            fields["name"] = str(payload.get("name") or "")[:40]
        if "brief" in payload:
            fields["brief"] = str(payload.get("brief") or "")[:200]
        if "weekdays" in payload:
            fields["weekdays"] = _clean_weekdays(payload.get("weekdays"))
        if "enabled" in payload:
            fields["enabled"] = 1 if _truthy(payload["enabled"]) else 0
        changed = await self.store.update_schedule(int(schedule_id), **fields)
        if not changed:
            raise ValueError("没有可更新的字段")
        return {"updated": changed}

    async def delete_schedule(self, schedule_id: int) -> dict:
        return {"deleted": await self.store.delete_schedule(int(schedule_id))}

    # ==================================================================
    #  手动操作
    # ==================================================================
    async def preview(self, umo: str) -> dict:
        """只生成不发送，用于调参时看效果。"""
        session = await self.store.get_session(umo)
        if session is None:
            raise ValueError("该会话不存在")
        outcome = await self.plugin.generator.generate(session, "preview", "面板预览")
        return outcome

    async def trigger(self, umo: str) -> dict:
        session = await self.store.get_session(umo)
        if session is None:
            raise ValueError("该会话不存在")
        return await self.scheduler.fire(session, "manual", "面板手动触发", ignore_guards=True)

    async def run_tick(self) -> dict:
        """立刻跑一次调度判定，便于调试闸门是否按预期工作。"""
        actions = await self.scheduler.tick()
        return {"actions": actions, "count": len(actions)}

    # ==================================================================
    #  诊断
    # ==================================================================
    async def diagnostics(self) -> dict:
        providers = self.plugin.llm.list_providers()
        checks = [
            _check("插件总开关", self.cfg.enabled, "打开后才会主动发消息", True),
            _check(
                "生效范围",
                bool(self.cfg.group_whitelist) or self.cfg.enable_private,
                "尚未选择任何生效的群或私聊，插件不会在任何地方发言",
                True,
            ),
            _check("对话模型", bool(providers), "没有检测到可用的对话模型，生成必然失败", True),
            _check("调度器", self.scheduler.running, "调度器未运行，不会自动触发", True),
            _check(
                "记忆功能",
                self.cfg.memory_enable,
                "关闭状态下不会抽取长期记忆",
                False,
            ),
            _check(
                "防骚扰保险",
                self.cfg.unanswered_pause > 0,
                "连续无回应会自动暂停，建议保持开启",
                False,
            ),
        ]
        return {
            "checks": checks,
            "providers": providers,
            "config": self.cfg.as_dict(),
            "warnings": list(self.cfg.warnings),
            "scheduler": self.scheduler.status(),
            "data_file": getattr(self.store, "path", ""),
        }


# ======================================================================
#  辅助
# ======================================================================
def _check(name: str, ok: bool, detail: str, blocking: bool) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail, "blocking": blocking}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是", "开")
    return bool(value)


def _clamp(value: Any, default: float = 0.7) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number > 1.0:
        number = number / 10.0
    return round(min(1.0, max(0.0, number)), 3)


def _load_json(raw: Any) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        loaded = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _deep_merge_dict(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge_dict(out[key], value)
        else:
            out[key] = value
    return out


def _valid_hhmm(value: str) -> bool:
    parts = value.split(":")
    if len(parts) != 2:
        return False
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


def _clean_weekdays(value: Any) -> str:
    if value is None:
        return "1,2,3,4,5,6,7"
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        items = [str(part).strip() for part in value]
    else:
        return "1,2,3,4,5,6,7"
    days = sorted({int(item) for item in items if item.isdigit() and 1 <= int(item) <= 7})
    return ",".join(str(day) for day in days) or "1,2,3,4,5,6,7"
