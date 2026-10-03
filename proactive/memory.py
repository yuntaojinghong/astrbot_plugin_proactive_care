"""长期记忆：抽取、去重、排序与检索。

设计取舍：

- **抽取是异步攒批的**，不是每条消息都调用模型，否则 token 成本会失控。
  攒够 ``extract_every`` 条新消息才抽一次，并且允许在面板上手动触发。
- **去重靠归一化后的文本做主键**（``memories(umo, norm)`` 唯一索引）。
  重复出现的事实不会堆成十条，而是把重要度取较大值，避免记忆库被废话塞满。
- **检索不用向量库**。中文场景下「二元字组重叠 + 重要度 + 时间衰减」
  已经能把最近聊到的、重要的记忆排到前面，而且零依赖、行为可预测。
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import re

from .textutil import keyword_overlap, normalize, now_ts

EXTRACT_SYSTEM = """你是一个群聊记忆整理器。你的任务是从对话记录里提取**值得长期记住**的信息。

只提取满足以下条件的内容：
- 具体的事实、偏好、约定、计划、身份信息、重要事件
- 对未来的对话有帮助（例如「小李下周三要出差」「群里在准备 10 月的聚会」）

不要提取：
- 寒暄、表情、语气词、玩笑话
- 一次性且无后续价值的闲聊
- 无法确认是否真实的内容（推测、玩笑、反讽）

输出格式：严格的 JSON 数组，不要任何解释文字、不要 Markdown 代码块。
数组中每个元素形如：
{"kind": "fact", "content": "小李下周三要去上海出差", "importance": 0.7}

字段说明：
- kind：fact（事实）/ event（事件）/ preference（偏好）/ topic（长期话题）
- content：一句完整、脱离上下文也能读懂的话，主语要明确，不超过 40 字
- importance：0 到 1 之间的数字，越重要越高

如果没有任何值得记住的内容，输出空数组 []。"""

EXTRACT_PROMPT = """以下是最近的群聊记录：

{transcript}

请提取值得长期记住的信息，输出 JSON 数组。"""

# 兼容模型偶尔不守规矩的情况：从回复里把 JSON 数组抠出来
_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*\]")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.、)])\s*")
_KIND_ALIAS = {
    "fact": "fact",
    "事实": "fact",
    "event": "event",
    "事件": "event",
    "preference": "preference",
    "偏好": "preference",
    "喜好": "preference",
    "topic": "topic",
    "话题": "topic",
}


class MemoryManager:
    def __init__(self, config, store, llm, context_builder=None, logger=None):
        self.config = config
        self.store = store
        self.llm = llm
        self.context = context_builder
        self.logger = logger
        # 正在抽取的会话，避免同一群并发跑多次模型调用
        self._extracting: set[str] = set()

    # ==================================================================
    #  抽取
    # ==================================================================
    async def maybe_extract(self, umo: str, session: dict | None = None) -> int:
        """攒够一定条数才抽取。返回本次新增/更新的记忆条数。

        .. warning::
            本方法**会等待模型返回**（数秒到数十秒），因此它是消息处理路径上
            最重的一步。调用方（``main._record_incoming``）是 ``await`` 它的，
            而 AstrBot 的 EventBus 对每个事件起一个独立任务、**没有并发上限**，
            所以「模型慢 + 消息多」时任务会堆积，2 核 2G 的机器容易被拖垮。

            这里加三道自我保护：

            1. **同一群不并发**：该群已有抽取在跑就直接返回，避免叠加
            2. **最短间隔**：距上次抽取不足 ``extract_min_interval`` 秒就跳过，
               防止消息密集时一条接一条地触发长调用
            3. 条数门槛仍然生效

            要彻底消除该阻塞，把面板里「记忆抽取」关掉即可——
            群管与主动发言都不依赖它。
        """
        if not self.config.memory_enable:
            return 0
        if umo in self._extracting:
            return 0

        session = session or await self.store.get_session(umo) or {}

        # 最短间隔：避免密集消息连续触发
        interval = int(getattr(self.config, "extract_min_interval", 0) or 0)
        if interval > 0:
            last = float(session.get("last_extract_ts") or 0)
            if last and (now_ts() - last) < interval:
                return 0

        since = float(session.get("last_extract_ts") or 0)
        pending = await self.store.count_messages(umo, since_ts=since)
        if pending < self.config.extract_every:
            return 0

        return await self.extract(umo, session=session)

    async def extract(self, umo: str, session: dict | None = None) -> int:
        """立刻抽取一次，不判断条数阈值（面板手动触发与 /记忆 指令走这条路）。

        并发去重放在**这里**而不是 ``maybe_extract``：``extract`` 还有别的调用方
        （面板按钮、``/记忆 抽取`` 指令），只在 maybe_extract 挡一道，
        别的入口同时点一下照样会叠加多次模型调用。
        """
        if not self.config.memory_enable:
            return 0
        if umo in self._extracting:
            return 0
        self._extracting.add(umo)
        try:
            return await self._extract_inner(umo, session=session)
        finally:
            self._extracting.discard(umo)

    async def _extract_inner(self, umo: str, session: dict | None = None) -> int:
        session = session or await self.store.get_session(umo) or {}
        since = float(session.get("last_extract_ts") or 0)
        # 上限可配：越多条 → 提示词越大 → token 与内存峰值越高（200 条约 1 万字符）
        limit = int(getattr(self.config, "extract_max_messages", 200) or 200)
        rows = await self.store.recent_messages(umo, limit=limit, since_ts=since)
        if not rows:
            return 0

        # 只保留最近 limit 条：recent_messages 从旧到新返回，取尾部即最新的一批
        if len(rows) > limit:
            rows = rows[-limit:]

        transcript = self._render(rows)
        if not transcript.strip():
            return 0

        raw = await self.llm.chat(
            prompt=EXTRACT_PROMPT.format(transcript=transcript),
            system_prompt=EXTRACT_SYSTEM,
            umo=umo,
            temperature=0.2,
        )
        await self.store.update_session(umo, last_extract_ts=now_ts())
        if not raw:
            return 0

        items = parse_items(raw)
        if not items:
            return 0

        stamp = now_ts()
        saved = 0
        for item in items:
            content = item["content"]
            norm = normalize(content)
            if not norm:
                continue
            await self.store.upsert_memory(
                umo,
                content=content,
                norm=norm,
                kind=item["kind"],
                importance=item["importance"],
                ts=stamp,
            )
            saved += 1
        if saved:
            self._info(f"抽取到 {saved} 条记忆（{umo}）")
            if self.config.auto_cleanup:
                await self.store.prune_memories(umo, keep=300)
        return saved

    @staticmethod
    def _render(rows: list[dict]) -> str:
        from .context import ContextBuilder

        return ContextBuilder.render(rows)

    # ==================================================================
    #  检索
    # ==================================================================
    async def retrieve(self, umo: str, query: str = "", limit: int | None = None) -> list[dict]:
        """按查询串取最相关的若干条记忆，并累加命中计数。"""
        limit = self.config.max_inject if limit is None else limit
        if limit <= 0 or not self.config.memory_enable:
            return []
        rows = await self.store.list_memories(umo=umo, limit=400)
        if not rows:
            return []
        moment = _dt.datetime.now()
        ranked = sorted(rows, key=lambda row: self.score(row, query, moment), reverse=True)
        picked = [row for row in ranked[:limit] if self.score(row, query, moment) > 0]
        if picked:
            await self.store.touch_memories([int(row["id"]) for row in picked if "id" in row])
        return picked

    def score(self, memory: dict, query: str, moment: _dt.datetime | None = None) -> float:
        """重要度 + 时间衰减 + 与当前话题的重叠度。"""
        moment = moment or _dt.datetime.now()
        try:
            importance = float(memory.get("importance") or 0.5)
        except (TypeError, ValueError):
            importance = 0.5
        importance = min(1.0, max(0.0, importance))

        half_life = self.config.decay_days
        if half_life > 0:
            created = float(memory.get("created_ts") or 0)
            age_days = max(0.0, (moment.timestamp() - created) / 86400) if created else 0.0
            recency = math.exp(-math.log(2) * age_days / half_life)
        else:
            recency = 1.0

        base = importance * (0.35 + 0.65 * recency)
        if query:
            base += 0.8 * keyword_overlap(query, str(memory.get("content") or ""))
        hits = int(memory.get("hits") or 0)
        if hits:
            base += min(0.15, 0.02 * hits)
        return base

    # ==================================================================
    #  维护
    # ==================================================================
    async def stats(self, umo: str) -> dict:
        rows = await self.store.list_memories(umo=umo, limit=2000)
        if not rows:
            return {"total": 0, "kinds": {}, "avg_importance": 0.0}
        kinds: dict[str, int] = {}
        total_importance = 0.0
        for row in rows:
            kinds[row.get("kind") or "fact"] = kinds.get(row.get("kind") or "fact", 0) + 1
            try:
                total_importance += float(row.get("importance") or 0.5)
            except (TypeError, ValueError):
                total_importance += 0.5
        return {
            "total": len(rows),
            "kinds": kinds,
            "avg_importance": round(total_importance / len(rows), 3),
        }

    def _info(self, message: str) -> None:
        if self.logger is not None:
            try:
                self.logger.info(f"[微光] {message}")
            except Exception:
                pass


# ======================================================================
#  解析（独立函数，方便单测）
# ======================================================================
def parse_items(raw: str) -> list[dict]:
    """把模型回复解析成记忆条目。JSON 失败时退回按行解析，尽量不白跑一次请求。"""
    text = (raw or "").strip()
    if not text:
        return []

    payload: list = []
    matched = _JSON_ARRAY_RE.search(text)
    if matched:
        try:
            loaded = json.loads(matched.group(0))
            if isinstance(loaded, list):
                payload = loaded
        except json.JSONDecodeError:
            payload = []

    if not payload:
        payload = _parse_lines(text)

    items: list[dict] = []
    seen: set[str] = set()
    for entry in payload:
        item = _coerce(entry)
        if not item:
            continue
        key = normalize(item["content"])
        if not key or key in seen:
            continue
        seen.add(key)
        items.append(item)
        if len(items) >= 8:  # 单次抽取的硬上限，防止模型一次吐几十条
            break
    return items


def _parse_lines(text: str) -> list:
    """把「一坨文字」按行拆成候选条目。

    只在文本确实长得像列表时才启用。否则模型回一句「没有什么值得记住的」，
    就会被当成一条记忆存进去——这类脏数据比没有记忆更糟。
    """
    lines = text.splitlines()
    bulleted = [line for line in lines if _BULLET_RE.match(line.strip())]
    if not bulleted:
        return []
    return [_BULLET_RE.sub("", line).strip() for line in bulleted if line.strip()]


def _coerce(entry) -> dict | None:
    if isinstance(entry, str):
        content = entry.strip()
        kind, importance = "fact", 0.5
    elif isinstance(entry, dict):
        content = str(entry.get("content") or entry.get("text") or entry.get("记忆") or "").strip()
        kind = _KIND_ALIAS.get(str(entry.get("kind") or entry.get("type") or "fact").strip().lower(), "fact")
        importance = _clamp(entry.get("importance", entry.get("weight", 0.5)))
    else:
        return None

    content = content.strip().strip("\"'“”「」")
    if len(content) < 4 or len(content) > 120:
        return None
    if content in {"无", "没有", "暂无", "略"}:
        return None
    return {"kind": kind, "content": content, "importance": importance}


def _clamp(value, default: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number > 1.0:  # 模型偶尔给 0~10 分制
        number = number / 10.0
    return round(min(1.0, max(0.0, number)), 3)
