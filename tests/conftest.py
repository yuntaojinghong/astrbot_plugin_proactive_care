"""测试公共设施。

这里会在导入被测代码**之前**往 ``sys.modules`` 里塞一套最小 AstrBot 桩，
这样在没装 AstrBot 的环境（比如 CI）里也能验证插件能否正常导入与实例化。
桩只补插件真正用到的接口，不模拟行为，避免测试跑偏。
"""

from __future__ import annotations

import os
import sys
import types

import pytest

PACKAGE_NAME = "astrbot_plugin_proactive_care"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARENT = os.path.dirname(REPO_ROOT)

# 让 `proactive.*` 能直接导入（store / memory 等模块的单测用）
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
# 让插件能按包路径导入，才能走通 main.py 里的相对导入
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)


# ======================================================================
#  AstrBot 桩
# ======================================================================
def _install_stubs() -> None:
    if "astrbot" in sys.modules:
        return

    def module(name: str, package: bool = False) -> types.ModuleType:
        mod = types.ModuleType(name)
        if package:
            mod.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = mod
        return mod

    class _Logger:
        def __init__(self):
            self.records: list[tuple[str, str]] = []

        def _log(self, level, *args, **kwargs):
            self.records.append((level, " ".join(str(a) for a in args)))

        def debug(self, *a, **k):
            self._log("debug", *a, **k)

        def info(self, *a, **k):
            self._log("info", *a, **k)

        def warning(self, *a, **k):
            self._log("warning", *a, **k)

        def error(self, *a, **k):
            self._log("error", *a, **k)

        def exception(self, *a, **k):
            self._log("exception", *a, **k)

    root = module("astrbot", package=True)
    api = module("astrbot.api", package=True)
    api.logger = _Logger()
    root.api = api

    # ---- astrbot.api.event ----
    event_mod = module("astrbot.api.event")

    class EventResult:
        """桩：只保留文本，够断言即可。"""

        def __init__(self, text=""):
            self.text = text

        def __repr__(self):
            return f"EventResult({self.text!r})"

    class AstrMessageEvent:
        def __init__(self, **kwargs):
            self.unified_msg_origin = kwargs.get("umo", "")
            self.message_str = kwargs.get("text", "")
            self._group_id = kwargs.get("group_id", "")
            self._sender_id = kwargs.get("sender_id", "")
            self._sender_name = kwargs.get("sender_name", "")
            self._self_id = kwargs.get("self_id", "")
            self._result = kwargs.get("result")
            # 对齐真实接口：@机器人 / 唤醒词触发时为 True
            self.is_wake = bool(kwargs.get("wake", False))
            self.stopped = False

        def is_wake_up(self):
            """桩：真实 AstrMessageEvent 有这个无参方法。"""
            return self.is_wake

        def get_group_id(self):
            return self._group_id

        def get_sender_id(self):
            return self._sender_id

        def get_sender_name(self):
            return self._sender_name

        def get_self_id(self):
            return self._self_id

        def get_result(self):
            return self._result

        def plain_result(self, text):
            return EventResult(text)

        def image_result(self, path):
            return EventResult(str(path))

        def chain_result(self, chain):
            return EventResult(str(chain))

        def stop_event(self):
            self.stopped = True

    class _EventMessageType:
        ALL = "all"
        GROUP_MESSAGE = "group"
        PRIVATE_MESSAGE = "private"

    class _PermissionType:
        ADMIN = "admin"
        MEMBER = "member"

    class _Filter:
        EventMessageType = _EventMessageType
        PermissionType = _PermissionType
        PlatformAdapterType = types.SimpleNamespace(AIOCQHTTP="aiocqhttp", ALL="all")

        @staticmethod
        def _identity(*args, **kwargs):
            def wrapper(func):
                return func

            return wrapper

        command = _identity
        command_group = _identity
        event_message_type = _identity
        platform_adapter_type = _identity
        permission_type = _identity
        on_astrbot_loaded = _identity
        on_llm_request = _identity
        on_llm_response = _identity
        after_message_sent = _identity
        on_decorating_result = _identity
        llm_tool = _identity

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = list(chain or [])

        def message(self, text):
            self.chain.append(Plain(text))
            return self

        def __repr__(self):
            return "".join(getattr(item, "text", "") for item in self.chain)

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.filter = _Filter()
    api.event = event_mod

    # ---- astrbot.api.message_components ----
    components = module("astrbot.api.message_components")

    class Plain:
        def __init__(self, text=""):
            self.text = text

    class Image:  # noqa: D101 - 桩
        pass

    components.Plain = Plain
    components.Image = Image
    components.MessageChain = MessageChain
    api.message_components = components

    # ---- astrbot.api.star ----
    star_mod = module("astrbot.api.star")

    class Context:
        def __init__(self, **kwargs):
            self.sent: list[tuple[str, object]] = []
            self.routes: list[tuple] = []
            self.providers: list = []
            self.answer = kwargs.get("answer", "在的，刚想到你们了")

        async def send_message(self, umo, chain):
            self.sent.append((umo, chain))
            return True

        def register_web_api(self, route, handler, methods, desc):
            self.routes.append((route, handler, tuple(methods), desc))

        def get_all_providers(self):
            return list(self.providers)

        def get_provider_by_id(self, provider_id=None):
            return None

        async def get_using_provider_async(self, umo=None):
            return None

    class Star:
        def __init__(self, context):
            self.context = context
            self.name = "astrbot_plugin_proactive_care"

    class AstrBotConfig(dict):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.saved = 0

        def save_config(self):
            self.saved += 1

    star_mod.Context = Context
    star_mod.Star = Star
    api.star = star_mod
    api.AstrBotConfig = AstrBotConfig

    # ---- astrbot.api.web ----
    web_mod = module("astrbot.api.web")

    def json_response(payload):
        return {"__response__": "json", "payload": payload}

    def error_response(message, status_code=400):
        return {"__response__": "error", "message": message, "status_code": status_code}

    class _Request:
        def __init__(self):
            self.query = _Query()
            self.json_body = {}

        async def json(self, default=None):
            return self.json_body if self.json_body is not None else (default or {})

    class _Query:
        def get(self, key, default=None, type=None):  # noqa: A002 - 对齐真实接口
            return default

        def getlist(self, key):
            return []

    web_mod.json_response = json_response
    web_mod.error_response = error_response
    web_mod.request = _Request()
    web_mod.file_response = lambda *a, **k: None
    web_mod.stream_response = lambda *a, **k: None
    api.web = web_mod

    # ---- astrbot.core.utils.astrbot_path ----
    util_pkg = module("astrbot.core", package=True)
    utils_pkg = module("astrbot.core.utils", package=True)
    path_mod = module("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_data_path = lambda: os.path.join(REPO_ROOT, ".test_data")
    path_mod.get_astrbot_plugin_data_path = lambda: os.path.join(REPO_ROOT, ".test_data", "plugin_data")
    util_pkg.utils = utils_pkg
    utils_pkg.astrbot_path = path_mod

    # ---- 顶层导出 ----
    root.api = api
    root.core = util_pkg


_install_stubs()


@pytest.fixture()
def store(tmp_path):
    """一个落在临时目录里的真实 SQLite 存储。"""
    from proactive.store import Store

    instance = Store(str(tmp_path))
    yield instance
    instance.close()


@pytest.fixture()
def cfg():
    from proactive.config import Config

    def build(raw=None):
        return Config(raw or {})

    return build


@pytest.fixture()
def plugin(tmp_path, monkeypatch):
    """用桩 Context 实例化完整插件，数据写进临时目录。"""
    path_mod = sys.modules["astrbot.core.utils.astrbot_path"]
    monkeypatch.setattr(path_mod, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    monkeypatch.setattr(path_mod, "get_astrbot_data_path", lambda: str(tmp_path))

    from astrbot.api.star import Context

    import importlib

    plugin_main = importlib.import_module(f"{PACKAGE_NAME}.main")

    context = Context()
    instance = plugin_main.ProactiveCarePlugin(
        context, {"basic": {"enabled": True, "group_whitelist": ["all"]}}
    )
    yield instance, context
    instance.store.close()
