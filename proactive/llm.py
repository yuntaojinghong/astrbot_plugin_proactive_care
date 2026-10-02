"""模型供应商解析与调用。

解析顺序刻意设计成「用户显式指定 → 当前会话默认 → 全局第一个可用」，
目标是**默认就用 AstrBot 里已经配好的模型**，装完插件不用额外配任何东西。

每一层失败都会把真实原因写进 ``last_error`` 与日志，这样面板上能直接看到
「为什么没生成」，而不是只给一个笼统的失败。
"""

from __future__ import annotations

import inspect
from typing import Any


class LLMClient:
    """对 AstrBot 的 Provider 做一层薄封装，统一异常与返回值形状。"""

    def __init__(self, context: Any, config: Any, logger: Any = None):
        self.context = context
        self.config = config
        self.logger = logger
        self.last_error: str = ""
        self._cached: Any = None

    # ==================================================================
    #  解析
    # ==================================================================
    async def resolve(self, umo: str | None = None) -> Any | None:
        ctx = self.context
        if ctx is None:
            self.last_error = "拿不到 AstrBot 上下文"
            return None

        tried: list[str] = []

        wanted = self.config.provider_id
        if wanted:
            provider = self._by_id(ctx, wanted, tried)
            if provider is not None:
                return provider

        provider = await self._for_session(ctx, umo, tried)
        if provider is not None:
            return provider

        provider = self._first(ctx, tried)
        if provider is not None:
            return provider

        detail = "；".join(tried) if tried else "没有任何可用的对话模型"
        self.last_error = f"未找到可用模型（{detail}）"
        self._warn(f"未找到可用的对话模型，请到「服务提供商」页添加一个。尝试记录：{tried}")
        return None

    def _by_id(self, ctx: Any, provider_id: str, tried: list[str]) -> Any | None:
        fetch = getattr(ctx, "get_provider_by_id", None)
        if not callable(fetch):
            return None
        for call in (
            lambda: fetch(provider_id=provider_id),
            lambda: fetch(provider_id),
        ):
            try:
                provider = call()
            except TypeError:
                continue
            except Exception as exc:  # pragma: no cover - 取决于 AstrBot 版本
                tried.append(f"指定 ID「{provider_id}」调用失败: {exc}")
                self._warn(f"按 ID 取模型失败（{provider_id}）: {exc}")
                return None
            if provider is None:
                tried.append(f"指定 ID「{provider_id}」不存在")
                return None
            return provider
        tried.append(f"指定 ID「{provider_id}」接口不兼容")
        return None

    async def _for_session(self, ctx: Any, umo: str | None, tried: list[str]) -> Any | None:
        fetch = getattr(ctx, "get_using_provider_async", None)
        if not callable(fetch):
            return None
        for call in (
            lambda: fetch(umo=umo),
            lambda: fetch(),
        ):
            try:
                provider = await call()
            except TypeError:
                continue
            except Exception as exc:
                tried.append(f"取会话默认模型失败: {exc}")
                return None
            if provider is None:
                tried.append("会话未设置默认模型")
                return None
            return provider
        return None

    def _first(self, ctx: Any, tried: list[str]) -> Any | None:
        for name in ("get_all_providers", "get_all_stt_providers"):
            fetch = getattr(ctx, name, None)
            if not callable(fetch):
                continue
            try:
                providers = fetch() or []
            except Exception as exc:
                tried.append(f"列举模型失败: {exc}")
                continue
            providers = list(providers)
            if providers:
                return providers[0]
        tried.append("全局没有配置任何模型")
        return None

    # ==================================================================
    #  调用
    # ==================================================================
    async def chat(
        self,
        prompt: str,
        system_prompt: str = "",
        umo: str | None = None,
        temperature: float | None = None,
    ) -> str | None:
        """发一次对话请求，返回纯文本。失败返回 None 并写 ``last_error``。"""
        provider = await self.resolve(umo)
        if provider is None:
            return None

        call = getattr(provider, "text_chat", None)
        if not callable(call):
            self.last_error = "该供应商不支持 text_chat 接口"
            self._warn(self.last_error)
            return None

        want_temp = self.config.temperature if temperature is None else temperature
        kwargs: dict[str, Any] = {"prompt": prompt}
        if system_prompt:
            kwargs["system_prompt"] = system_prompt
        if want_temp is not None and self._accepts(call, "temperature"):
            kwargs["temperature"] = want_temp

        try:
            response = await call(**kwargs)
        except TypeError:
            # 少数供应商不接受 temperature，退一步重试
            kwargs.pop("temperature", None)
            try:
                response = await call(**kwargs)
            except Exception as exc:
                self.last_error = f"模型调用失败: {exc}"
                self._warn(self.last_error)
                return None
        except Exception as exc:
            self.last_error = f"模型调用失败: {exc}"
            self._warn(self.last_error)
            return None

        text = getattr(response, "completion_text", None)
        if not text:
            text = str(response or "")
        text = text.strip()
        if not text:
            self.last_error = "模型返回了空内容"
            return None
        self.last_error = ""
        return text

    @staticmethod
    def _accepts(func: Any, keyword: str) -> bool:
        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):
            return False
        parameters = signature.parameters
        if keyword in parameters:
            return True
        return any(param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values())

    # ==================================================================
    #  列表（供面板下拉）
    # ==================================================================
    def list_providers(self) -> list[dict]:
        ctx = self.context
        fetch = getattr(ctx, "get_all_providers", None) if ctx is not None else None
        if not callable(fetch):
            return []
        try:
            providers = list(fetch() or [])
        except Exception:
            return []
        result = []
        for provider in providers:
            result.append(
                {
                    "id": provider_id(provider),
                    "label": provider_label(provider),
                }
            )
        return result

    def _warn(self, message: str) -> None:
        if self.logger is not None:
            try:
                self.logger.warning(f"[微光] {message}")
            except Exception:
                pass


def provider_id(provider: Any) -> str:
    """从不同版本的 Provider 对象里挖出 ID。"""
    for attribute in ("provider_id", "id"):
        value = getattr(provider, attribute, None)
        if isinstance(value, str) and value:
            return value
    meta = None
    try:
        meta = provider.meta()
    except Exception:
        meta = getattr(provider, "meta", None)
    if isinstance(meta, dict):
        return str(meta.get("id") or "")
    value = getattr(meta, "id", None)
    return value if isinstance(value, str) else ""


def provider_label(provider: Any) -> str:
    """给面板用的可读标签，尽力而为，取不到就退回类名。"""
    model = ""
    kind = ""
    try:
        meta = provider.meta()
    except Exception:
        meta = getattr(provider, "meta", None)
    if isinstance(meta, dict):
        model = str(meta.get("model") or "")
        kind = str(meta.get("type") or "")
    else:
        model = str(getattr(meta, "model", "") or "")
        kind = str(getattr(meta, "type", "") or "")
    pid = provider_id(provider)
    parts = [part for part in (kind, model or pid) if part]
    label = " · ".join(parts) if parts else type(provider).__name__
    return label
