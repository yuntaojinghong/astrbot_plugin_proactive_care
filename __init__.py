"""微光 · 主动关怀。

AstrBot 把插件目录当作 Python 包来加载（``<插件目录>.main``），
所以这里保留一个 ``__init__.py``，让 ``main.py`` 里的相对导入成立。
真正的实现都在 :mod:`main` 与 :mod:`proactive` 里，本文件不导入它们，
避免导入期的循环依赖。
"""

__version__ = "1.1.1"
