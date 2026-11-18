"""指令前缀可配置：改配置就能换前缀，不需要改代码。"""
from kernel.bus.command import CommandRegistry


def test_default_prefixes():
    reg = CommandRegistry()
    assert reg.parse("/~echo hi") == ("echo", "hi")
    assert reg.parse(".echo hi") == ("echo", "hi")


def test_custom_prefixes_only():
    reg = CommandRegistry(prefixes=["::"])
    assert reg.parse("::echo hi") == ("echo", "hi")
    assert reg.parse("/~echo hi") is None      # 旧前缀失效
    assert reg.parse(".echo hi") is None


def test_long_prefix_wins_over_short():
    """同时配了 "/" 与 "/~" 时，"/~cmd" 不能被 "/" 吃掉 "~"。"""
    reg = CommandRegistry(prefixes=["/", "/~"])
    assert reg.prefixes[0] == "/~"
    assert reg.parse("/~echo hi") == ("echo", "hi")


def test_case_sensitivity_switch():
    insensitive = CommandRegistry(prefixes=["!"])
    insensitive.register("Echo", lambda e, a: None, owner="t")
    assert insensitive.resolve("echo") is not None

    sensitive = CommandRegistry(prefixes=["!"], case_sensitive=True)
    sensitive.register("Echo", lambda e, a: None, owner="t")
    assert sensitive.resolve("echo") is None
    assert sensitive.resolve("Echo") is not None
