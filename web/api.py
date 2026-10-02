"""面板的 HTTP 路由层。

只做三件事：解析请求、调用 :class:`PanelService`、包装响应。
业务逻辑一律不写在这里，方便单独测试 service。

约定（与 AstrBot 插件 Pages 机制一致）：

- 注册路由必须带插件名前缀 ``/astrbot_plugin_proactive_care/...``
- 前端 bridge 调用时**不带**前缀，例如 ``bridge.apiGet("sessions")``
"""

from __future__ import annotations

from typing import Any, Callable

PLUGIN_NAME = "astrbot_plugin_proactive_care"

# astrbot.api.web 在 4.24+ 提供；老版本回退到 quart，实在没有就整体降级
_WEB_IMPORT_ERROR = ""
try:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.api.web import error_response, json_response, request  # type: ignore
except Exception as exc:
    _WEB_IMPORT_ERROR = str(exc)
    json_response = None  # type: ignore
    error_response = None  # type: ignore
    request = None  # type: ignore
    try:
        from quart import jsonify  # type: ignore
        from quart import request as _quart_request  # type: ignore

        def json_response(payload: Any):  # type: ignore
            return jsonify(payload)

        def error_response(message: str, status_code: int = 400):  # type: ignore
            return jsonify({"status": "error", "message": message}), status_code

        request = _quart_request  # type: ignore
    except Exception:
        pass


class WebController:
    def __init__(self, context: Any, service: Any, logger: Any = None):
        self.context = context
        self.service = service
        self.logger = logger
        self.registered = False

    # ==================================================================
    #  注册
    # ==================================================================
    def register_routes(self) -> bool:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            self._warn(
                "当前 AstrBot 版本不支持插件 Pages（缺少 register_web_api），"
                "配置面板不可用，请升级到 4.24.2 以上"
            )
            return False
        if request is None or json_response is None:
            self._warn(f"Web 请求模块不可用，跳过面板注册: {_WEB_IMPORT_ERROR}")
            return False

        routes: list[tuple[str, Callable, list[str], str]] = [
            ("/bootstrap", self.api_bootstrap, ["GET"], "面板初始化数据"),
            ("/overview", self.api_overview, ["GET"], "运行概览"),
            ("/diagnostics", self.api_diagnostics, ["GET"], "自检诊断"),
            ("/config", self.api_update_config, ["POST"], "保存配置"),
            ("/sessions", self.api_sessions, ["GET"], "会话列表"),
            ("/session", self.api_session, ["GET"], "单个会话详情"),
            ("/session", self.api_update_session, ["POST"], "更新会话设置"),
            ("/session/delete", self.api_delete_session, ["POST"], "删除会话"),
            ("/session/reset", self.api_reset_session, ["POST"], "清空会话上下文"),
            ("/memories", self.api_memories, ["GET"], "记忆列表"),
            ("/memory/add", self.api_memory_add, ["POST"], "手动添加记忆"),
            ("/memory/update", self.api_memory_update, ["POST"], "修改记忆"),
            ("/memory/delete", self.api_memory_delete, ["POST"], "删除记忆"),
            ("/memory/clear", self.api_memory_clear, ["POST"], "清空记忆"),
            ("/memory/extract", self.api_memory_extract, ["POST"], "立即抽取记忆"),
            ("/history", self.api_history, ["GET"], "主动消息历史"),
            ("/history/clear", self.api_history_clear, ["POST"], "清空历史"),
            ("/schedules", self.api_schedules, ["GET"], "定时规则列表"),
            ("/schedule/add", self.api_schedule_add, ["POST"], "新增定时规则"),
            ("/schedule/update", self.api_schedule_update, ["POST"], "修改定时规则"),
            ("/schedule/delete", self.api_schedule_delete, ["POST"], "删除定时规则"),
            ("/preview", self.api_preview, ["POST"], "预览生成（不发送）"),
            ("/trigger", self.api_trigger, ["POST"], "手动立即触发"),
            ("/tick", self.api_tick, ["POST"], "立刻跑一次调度判定"),
        ]

        ok = 0
        for path, handler, methods, desc in routes:
            try:
                register(f"/{PLUGIN_NAME}{path}", self._guard(handler), methods, desc)
                ok += 1
            except Exception as exc:
                self._error(f"注册路由 {path} 失败: {exc}")

        self.registered = ok > 0
        if self.registered:
            self._info(f"配置面板已注册 {ok} 个接口")
        return self.registered

    def _guard(self, handler: Callable) -> Callable:
        """统一异常：ValueError 视为用户输入问题返回 400，其余返回 500。"""

        async def wrapped(*args, **kwargs):
            try:
                return await handler(*args, **kwargs)
            except ValueError as exc:
                return _err(str(exc), 400)
            except Exception as exc:  # pragma: no cover - 兜底
                self._error(f"面板接口异常: {exc}")
                return _err(f"服务器内部错误: {exc}", 500)

        wrapped.__name__ = getattr(handler, "__name__", "handler")
        return wrapped

    # ==================================================================
    #  读接口
    # ==================================================================
    async def api_bootstrap(self):
        return _ok(await self.service.bootstrap())

    async def api_overview(self):
        return _ok(await self.service.overview())

    async def api_diagnostics(self):
        return _ok(await self.service.diagnostics())

    async def api_sessions(self):
        return _ok({"sessions": await self.service.list_sessions()})

    async def api_session(self):
        return _ok(await self.service.get_session(_query("umo")))

    async def api_memories(self):
        return _ok(
            {
                "memories": await self.service.list_memories(
                    umo=_query("umo"), query=_query("q"), limit=_query_int("limit", 300)
                )
            }
        )

    async def api_history(self):
        return _ok(
            {
                "history": await self.service.list_history(
                    umo=_query("umo"), limit=_query_int("limit", 100)
                )
            }
        )

    async def api_schedules(self):
        return _ok({"schedules": await self.service.list_schedules(umo=_query("umo"))})

    # ==================================================================
    #  写接口
    # ==================================================================
    async def api_update_config(self):
        return _ok(await self.service.update_config(await _body()))

    async def api_update_session(self):
        payload = await _body()
        return _ok(await self.service.update_session(str(payload.get("umo") or ""), payload))

    async def api_delete_session(self):
        payload = await _body()
        return _ok(await self.service.delete_session(str(payload.get("umo") or "")))

    async def api_reset_session(self):
        payload = await _body()
        return _ok(await self.service.reset_session(str(payload.get("umo") or "")))

    async def api_memory_add(self):
        payload = await _body()
        return _ok(
            await self.service.add_memory(
                str(payload.get("umo") or ""),
                str(payload.get("content") or ""),
                str(payload.get("kind") or "fact"),
                payload.get("importance", 0.7),
            )
        )

    async def api_memory_update(self):
        payload = await _body()
        memory_id = payload.get("id")
        if memory_id is None:
            raise ValueError("缺少记忆 ID")
        return _ok(await self.service.update_memory(int(memory_id), payload))

    async def api_memory_delete(self):
        payload = await _body()
        memory_id = payload.get("id")
        if memory_id is None:
            raise ValueError("缺少记忆 ID")
        return _ok(await self.service.delete_memory(int(memory_id)))

    async def api_memory_clear(self):
        payload = await _body()
        umo = str(payload.get("umo") or "")
        if not umo:
            raise ValueError("缺少会话标识")
        return _ok(await self.service.clear_memories(umo))

    async def api_memory_extract(self):
        payload = await _body()
        return _ok(await self.service.extract_memories(str(payload.get("umo") or "")))

    async def api_history_clear(self):
        payload = await _body()
        return _ok(await self.service.clear_history(str(payload.get("umo") or "")))

    async def api_schedule_add(self):
        return _ok(await self.service.add_schedule(await _body()))

    async def api_schedule_update(self):
        payload = await _body()
        schedule_id = payload.get("id")
        if schedule_id is None:
            raise ValueError("缺少规则 ID")
        return _ok(await self.service.update_schedule(int(schedule_id), payload))

    async def api_schedule_delete(self):
        payload = await _body()
        schedule_id = payload.get("id")
        if schedule_id is None:
            raise ValueError("缺少规则 ID")
        return _ok(await self.service.delete_schedule(int(schedule_id)))

    async def api_preview(self):
        payload = await _body()
        return _ok(await self.service.preview(str(payload.get("umo") or "")))

    async def api_trigger(self):
        payload = await _body()
        return _ok(await self.service.trigger(str(payload.get("umo") or "")))

    async def api_tick(self):
        return _ok(await self.service.run_tick())

    # ==================================================================
    #  日志
    # ==================================================================
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

    def _error(self, message: str) -> None:
        if self.logger is not None:
            try:
                self.logger.error(f"[微光] {message}")
            except Exception:
                pass


# ======================================================================
#  请求 / 响应辅助
# ======================================================================
def _ok(data: Any):
    if json_response is None:
        raise RuntimeError("Web 响应模块不可用")
    return json_response(data)


def _err(message: str, status_code: int = 400):
    if error_response is None:
        raise RuntimeError("Web 响应模块不可用")
    return error_response(message, status_code=status_code)


def _query(key: str, default: str = "") -> str:
    try:
        value = request.query.get(key, default)
    except Exception:
        return default
    return str(value) if value is not None else default


def _query_int(key: str, default: int) -> int:
    try:
        return int(request.query.get(key, default, type=int))
    except Exception:
        try:
            return int(_query(key, str(default)))
        except (TypeError, ValueError):
            return default


async def _body() -> dict:
    try:
        payload = await request.json(default={})
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = ["WebController", "PLUGIN_NAME"]
