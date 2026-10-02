"""把调度器与存储用到的「当前时间」钉死，让时段相关的用例不再随挂钟时间飘。

为什么需要这个：``proactive.scheduler`` 与 ``proactive.textutil`` 都是通过
``datetime.datetime.now()`` 取当前时间。像「错过太久的排期」这类用例，
如果直接拿真实时间做算术（例如 ``now - 6 小时``），在凌晨 0~6 点之间
减出来的时刻会落到**同一天的未来**，用例就会莫名其妙地失败——
跟代码对错无关，纯粹是跑测试的时间不对。
"""

from __future__ import annotations

import datetime as _dt


class FrozenDateTime(_dt.datetime):
    """``now()`` 返回固定时刻的 datetime 子类。

    ``now_ts()``（textutil）内部走的是 ``datetime.datetime.now().timestamp()``，
    而它是通过 ``from datetime import datetime`` 拿到的引用，因此这里必须
    替换 ``datetime.datetime`` 本身，不能只换模块属性。
    """

    _frozen: _dt.datetime = _dt.datetime(2026, 6, 15, 12, 0, 0)

    @classmethod
    def now(cls, tz=None):  # noqa: D102 - 对齐 datetime 接口
        return cls._frozen

    @classmethod
    def freeze(cls, moment: _dt.datetime) -> None:
        cls._frozen = moment


def freeze_now(monkeypatch, moment: _dt.datetime | None = None):
    """把「现在」钉在 ``moment``（默认 2026-06-15 12:00，周一中午）。

    周中 + 白天，能避开「静默时段」和「按星期过滤」两类干扰。
    """
    moment = moment or _dt.datetime(2026, 6, 15, 12, 0, 0)
    FrozenDateTime.freeze(moment)
    # 只改 ``datetime.datetime`` 这个类。
    # 各模块的 ``import datetime as _dt`` 拿到的是同一个模块对象，
    # 所以改这一处对 scheduler / textutil / store 全都生效；
    # 而 ``_dt.datetime``、``_dt.timedelta`` 这些正常用法也不受影响。
    monkeypatch.setattr(_dt, "datetime", FrozenDateTime)
    return moment
