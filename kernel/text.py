"""文本归一化。

平台差异全在这里消化，插件拿到的永远是干净文本。

★ 2026-11-16 实测（官方 QQ 机器人）：群里 @ 机器人后，事件里的 content 形如
   "@test1text" / "@test1 text" / "@test1  text" —— 空格数量完全取决于发送者习惯，
   有时还是 0 个。所以不能假设"一定有一个空格"，必须容忍任意数量（含 0）。
"""
from __future__ import annotations

import re

# 零宽与双向控制符：粘贴、输入法、富文本会带进来，肉眼不可见但会让前缀匹配失败
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")

# 前导空白：半角空格、全角空格、不间断空格、制表符、换行
_LEADING_WS = re.compile(r"^[\s\u3000\xa0]+")

# 尾部空白同理
_TRAILING_WS = re.compile(r"[\s\u3000\xa0]+$")


def normalize_text(raw: str | None) -> str:
    """去掉零宽字符与首尾空白。

    只处理首尾，不动中间——中间的空格是用户语义的一部分（"a  b" 不能被压成 "a b"）。
    """
    if not raw:
        return ""
    s = _ZERO_WIDTH.sub("", str(raw))
    s = _LEADING_WS.sub("", s)
    s = _TRAILING_WS.sub("", s)
    return s


def strip_leading_whitespace(raw: str) -> str:
    """只去前导空白与零宽字符，保留正文内部与尾部原样。日志排查时用。"""
    if not raw:
        return ""
    return _LEADING_WS.sub("", _ZERO_WIDTH.sub("", str(raw)))
