"""微光 · 主动关怀 —— 核心实现。

分层：

- :mod:`config`     配置读取、默认值合并与校验
- :mod:`store`      SQLite 持久化（会话 / 消息 / 记忆 / 历史 / 定时规则）
- :mod:`filters`    消息准入与匿名隔离规则
- :mod:`textutil`   文本归一化、时间与相似度工具
- :mod:`llm`        模型供应商解析与调用
- :mod:`memory`     长期记忆的抽取、排序与检索
- :mod:`context`    会话上下文拼装
- :mod:`generator`  主动消息生成
- :mod:`scheduler`  调度循环与触发判定
"""

from .config import Config
from .store import Store

__all__ = ["Config", "Store"]
