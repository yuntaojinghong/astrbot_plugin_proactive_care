"""清理 system_reminder：把用户日志里那段真实文本当输入。

用户看到的机器人回复正文（从日志里抄的原文）：

    死不了。

    我这边服务器只要还接着电，你想让我死都没门。倒是你，明天还上课吧？大半夜不睡在这儿骂鱼。

    行了，服了你了，我也睡了

    <system_reminder>Group name: 大实验树洞✨
    Current datetime: 2026-10-03 22:39 (CST), Weekday: Saturday</system_reminder>
    晚安。

以及更糟的一种（把整段群聊上下文也抄出来了）：

    ...晚安——记得明天回来找我复仇（不是）。
    <system_reminder>You are in a group chat. Belows are group chat context after your last reply:
    --- Begin CONTEXT---
    [准高一.../22:43:13]:  [Image]
    [高一.../22:43:04]:  [Image]
    --- End CONTEXT ---
    </system_reminder>

这些内容会被写进对话历史，下一轮又当"上文"喂回去 —— 越滚越长，人格漂移。
"""

from __future__ import annotations

from proactive.sanitize import scrub_request, strip_system_reminders

# ---- 用户日志里的真实样本 ----
REAL_ONE = """死不了。

我这边服务器只要还接着电，你想让我死都没门。倒是你，明天还上课吧？大半夜不睡在这儿骂鱼。

行了，服了你了，我也睡了

<system_reminder>Group name: 大实验树洞✨
Current datetime: 2026-10-03 22:39 (CST), Weekday: Saturday</system_reminder>
晚安。"""

REAL_TWO = """去吧，明早别睡过头。

晚安——记得明天回来找我复仇（不是）。
<system_reminder>You are in a group chat. Belows are group chat context after your last reply:
--- Begin CONTEXT---
[准高一我都从你老婆身上下来了你还嚷什么/22:43:13]:  [Image]
[高一 你都给我起神ID了还不是教主吗/22:43:04]:  [Image]
--- End CONTEXT ---
</system_reminder>"""


def test_strips_datetime_reminder():
    out = strip_system_reminders(REAL_ONE)
    assert "<system_reminder>" not in out
    assert "Current datetime" not in out
    assert "Group name" not in out
    # 正文必须完整保留
    assert "死不了。" in out
    assert "大半夜不睡在这儿骂鱼" in out
    assert out.endswith("晚安。"), repr(out[-20:])


def test_strips_context_reminder():
    out = strip_system_reminders(REAL_TWO)
    assert "<system_reminder>" not in out
    assert "Begin CONTEXT" not in out
    assert "End CONTEXT" not in out
    assert "[Image]" not in out
    assert "复仇" in out


def test_plain_text_untouched():
    for text in ("你好呀", "今天天气不错，出去走走？", "", "多行\n文本\n没问题"):
        assert strip_system_reminders(text) == text


def test_partial_or_truncated_reminder():
    # 只有开标签（被截断）
    out = strip_system_reminders("正文\n<system_reminder>Group name: x")
    assert "<system_reminder>" not in out and "正文" in out
    # 只有闭标签的残留
    out2 = strip_system_reminders("正文</system_reminder>")
    assert "</system_reminder>" not in out2 and "正文" in out2


def test_multiple_blocks():
    text = ("A<system_reminder>1</system_reminder>"
            "B<system_reminder>2</system_reminder>C")
    assert strip_system_reminders(text) == "ABC"


def test_no_runaway_blank_lines():
    text = "上\n\n<system_reminder>x</system_reminder>\n\n\n\n下"
    out = strip_system_reminders(text)
    assert "\n\n\n" not in out
    assert "上" in out and "下" in out


# ----------------------------------------------------------------------
#  scrub_request：就地清理请求对象
# ----------------------------------------------------------------------
class _Part:
    def __init__(self, text):
        self.text = text


class _Req:
    def __init__(self, parts=None, contexts=None):
        self.extra_user_content_parts = parts if parts is not None else []
        self.contexts = contexts if contexts is not None else []


def test_scrub_removes_pure_reminder_part():
    """整段都是 reminder 的注入片段应当被整个移除，而不是留个空壳。"""
    req = _Req(parts=[
        _Part("正常的注入内容"),
        _Part("<system_reminder>Group name: x\nCurrent datetime: y</system_reminder>"),
    ])
    removed = scrub_request(req)
    assert removed >= 1
    texts = [getattr(p, "text", "") for p in req.extra_user_content_parts]
    assert "正常的注入内容" in texts
    assert not any("system_reminder" in t for t in texts)


def test_scrub_cleans_history_contexts():
    """模型抄进历史的 reminder 必须被清掉，否则会一直滚下去。"""
    req = _Req(contexts=[
        {"role": "assistant", "content": REAL_ONE},
        {"role": "user", "content": "正常提问"},
    ])
    scrub_request(req)
    assert "system_reminder" not in req.contexts[0]["content"]
    assert "死不了。" in req.contexts[0]["content"]
    assert req.contexts[1]["content"] == "正常提问"


def test_scrub_noop_when_clean():
    req = _Req(parts=[_Part("干净内容")], contexts=[{"content": "也很干净"}])
    assert scrub_request(req) == 0
    assert req.extra_user_content_parts[0].text == "干净内容"


def test_scrub_tolerates_weird_objects():
    """请求对象形态不可控，清理逻辑绝不能因此抛异常。"""
    assert scrub_request(None) == 0
    assert scrub_request(object()) == 0

    class Weird:
        extra_user_content_parts = "不是 list"
        contexts = 42

    assert scrub_request(Weird()) == 0
