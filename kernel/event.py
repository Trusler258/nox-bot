"""事件模型。

内核只认这一套，平台差异由适配器消化成 Event / Message / Segment。
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Segment:
    """消息段。type 取 text / image / at / reply / file / markdown / voice。"""
    type: str
    data: dict = field(default_factory=dict)


@dataclass(slots=True)
class Message:
    segments: list[Segment] = field(default_factory=list)
    raw: Any = None
    # 图文卡片（官方 msg_type=8，仅群聊）。结构见官方「发送群聊消息」：
    #   {"type": "tuwen", "content": {title, description, url, pic_url}}
    card: dict | None = None
    # 官方渠道的内嵌键盘（按钮）。格式照 V2：
    #   {"content": {"rows": [{"buttons": [{"id","render_data","action"}]}]}}
    keyboard: dict | None = None

    @property
    def text(self) -> str:
        """把所有 text 段拼起来（图片等段忽略）。"""
        return "".join(s.data.get("text", "") for s in self.segments if s.type == "text")

    def has(self, seg_type: str) -> bool:
        return any(s.type == seg_type for s in self.segments)

    @classmethod
    def from_text(cls, text: str) -> "Message":
        return cls(segments=[Segment("text", {"text": text})])

    @classmethod
    def from_image(cls, path: str | Path) -> "Message":
        """单图消息：只有一张图，不带任何文字（官方渠道按 msg_type=7 发）。"""
        return cls(segments=[Segment("image", {"path": str(path)})])

    @classmethod
    def from_card(cls, *, title: str, description: str,
                  url: str = "", pic_url: str = "",
                  card_type: str = "tuwen") -> "Message":
        """图文卡片消息（官方 msg_type=8，**仅群聊**，单聊不支持）。

        平台硬约束：四个字段都要有；标题 1~50 字、描述 1~200 字，
        两个链接必须 https 且不超过 1024 字符。这里先截断，别等平台报错。
        """
        def _clip(v: str, n: int) -> str:
            v = (v or "").strip()
            return v if len(v) <= n else v[: n - 1] + "…"

        content = {
            "title": _clip(title, 50),
            "description": _clip(description, 200),
            "url": _clip(url, 1024),
            "pic_url": _clip(pic_url, 1024),
        }
        return cls(card={"type": card_type, "content": content})

    @property
    def has_card(self) -> bool:
        return bool(self.card)

    @classmethod
    def from_markdown(cls, content: str, *, keyboard: dict | None = None) -> "Message":
        """markdown 富文本消息（官方渠道 msg_type=2）。

        带 keyboard 时图必须用 markdown 内嵌 `![](url)` —— QQ 的富媒体消息
        （msg_type=7）不渲染 keyboard，图和按钮要落在同一条消息里只能走这条。
        """
        return cls(segments=[Segment("markdown", {"content": content})],
                   keyboard=keyboard)

    @property
    def only_markdown(self) -> bool:
        return len(self.segments) == 1 and self.segments[0].type == "markdown"

    @property
    def markdown(self) -> str:
        for s in self.segments:
            if s.type == "markdown":
                return str(s.data.get("content", ""))
        return ""

    @property
    def only_image(self) -> bool:
        return len(self.segments) == 1 and self.segments[0].type == "image"


@dataclass(slots=True)
class Event:
    """统一事件。id 为平台消息 id，trace_id 由内核生成并贯穿全链路。"""
    id: str
    platform: str
    kind: str                      # message.group / message.private / notice / lifecycle
    group_id: str | None = None
    user_id: str | None = None
    sender_name: str = ""          # 群名片优先，其次昵称；官方渠道是 username
    message: Message | None = None
    mentions_me: bool = False
    quote_id: str | None = None
    ts: float = field(default_factory=time.time)
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    @property
    def text(self) -> str:
        return self.message.text if self.message else ""


@dataclass(slots=True)
class Capabilities:
    """适配器能力矩阵。业务按能力判断，不按平台判断。"""
    quote: bool = False
    at: bool = False
    image: bool = False
    markdown: bool = False
    buttons: bool = False
    stream: bool = False
    max_text_len: int = 4000
