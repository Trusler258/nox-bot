"""CQ 码解析（OneBot v11）。

只做解析，不做生成：出站统一用消息段数组，比拼字符串安全。
"""
from __future__ import annotations

import re
from typing import Any

from kernel.event import Segment

_CQ = re.compile(r"\[CQ:([a-zA-Z0-9_.-]+)((?:,[^\]]*)?)\]")

_ESC = {"&amp;": "&", "&#91;": "[", "&#93;": "]", "&#44;": ","}


def _unescape(s: str) -> str:
    for k, v in _ESC.items():
        s = s.replace(k, v)
    return s


def _params(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not raw:
        return out
    for part in raw.lstrip(",").split(","):
        if not part:
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = _unescape(v.strip())
    return out


def parse_cq(text: str) -> list[Segment]:
    """把 "你好[CQ:at,qq=1] [CQ:image,file=a.jpg]" 拆成段。

    纯文本段与富媒体段分开；文本段的 text 已反转义。
    """
    segments: list[Segment] = []
    pos = 0
    for m in _CQ.finditer(text or ""):
        if m.start() > pos:
            plain = _unescape(text[pos:m.start()])
            if plain:
                segments.append(Segment("text", {"text": plain}))
        ctype = m.group(1)
        data = _params(m.group(2))
        if ctype == "at":
            segments.append(Segment("at", {"qq": data.get("qq", "")}))
        elif ctype == "reply":
            segments.append(Segment("reply", {"id": data.get("id", "")}))
        elif ctype == "image":
            segments.append(Segment("image", {"file": data.get("file", ""),
                                              "url": data.get("url", "")}))
        elif ctype == "face":
            segments.append(Segment("face", {"id": data.get("id", "")}))
        elif ctype in ("record", "video", "file"):
            segments.append(Segment(ctype, data))
        else:
            segments.append(Segment(ctype, data))
        pos = m.end()
    if pos < len(text or ""):
        plain = _unescape(text[pos:])
        if plain:
            segments.append(Segment("text", {"text": plain}))
    return segments


def to_onebot_message(segments: list[Segment]) -> list[dict[str, Any]]:
    """转成 OneBot v11 的消息段数组。"""
    out = []
    for s in segments:
        if s.type == "text":
            out.append({"type": "text", "data": {"text": s.data.get("text", "")}})
        elif s.type == "at":
            out.append({"type": "at", "data": {"qq": s.data.get("qq", "")}})
        elif s.type == "image":
            out.append({"type": "image", "data": {"file": s.data.get("file", "")}})
        elif s.type == "reply":
            out.append({"type": "reply", "data": {"id": s.data.get("id", "")}})
    return out
