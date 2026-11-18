"""@ 之后的空格兼容性：0 个、1 个、2 个、全角、零宽，全部要能正确取出正文。

来源：2026-11-16 官方 QQ 机器人实测，content 形如 "  111"（两个空格）。
用户的三种习惯输入都要兼容：
    @test1text      → 无空格
    @test1 text     → 1 个
    @test1  text    → 2 个
"""
import pytest

from kernel.text import normalize_text, strip_leading_whitespace


@pytest.mark.parametrize("raw,expected", [
    ("111", "111"),
    (" 111", "111"),
    ("  111", "111"),
    ("\t111", "111"),
    ("\u3000111", "111"),          # 全角空格
    ("\xa0111", "111"),            # 不间断空格
    ("\u200b 111", "111"),         # 零宽空格 + 普通空格
    ("\u200b111", "111"),
    ("111 ", "111"),
    ("  111  ", "111"),
    ("", ""),
    (None, ""),
    ("a  b", "a  b"),              # 中间空格不动
])
def test_normalize_text(raw, expected):
    assert normalize_text(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("  111  ", "111  "),          # 只去前导，尾部原样
    ("\u3000\u3000111", "111"),
])
def test_strip_leading_only(raw, expected):
    assert strip_leading_whitespace(raw) == expected


def test_command_parse_with_leading_spaces():
    from kernel.bus.command import CommandRegistry

    reg = CommandRegistry()
    assert reg.parse("  /~echo hi") == ("echo", "hi")
    assert reg.parse("/~echo   hi") == ("echo", "hi")
    assert reg.parse("/echo hi") == ("echo", "hi")
    assert reg.parse("普通聊天") is None
