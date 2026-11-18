"""§13 隔离指标：单个插件崩溃/卡死不能影响其他插件与主流程。"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kernel.bus.eventbus import EventBus  # noqa: E402
from kernel.event import Event  # noqa: E402


def _ev() -> Event:
    return Event(id="1", platform="mock", kind="message.group", group_id="g", user_id="u")


def test_exception_in_one_handler_does_not_block_others():
    bus = EventBus()
    calls: list[str] = []

    async def boom(_e):
        calls.append("boom")
        raise RuntimeError("插件炸了")

    async def ok(_e):
        calls.append("ok")

    bus.subscribe("message", boom, owner="bad")
    bus.subscribe("message", ok, owner="good")

    delivered = asyncio.run(bus.emit(_ev()))
    assert delivered == 2
    assert calls == ["boom", "ok"]        # 后面的照常执行


def test_timeout_in_one_handler_does_not_block_others():
    bus = EventBus()
    calls: list[str] = []

    async def slow(_e):
        calls.append("slow")
        await asyncio.sleep(5)

    async def ok(_e):
        calls.append("ok")

    bus.subscribe("message", slow, owner="slow", timeout=0.05)
    bus.subscribe("message", ok, owner="good")

    asyncio.run(bus.emit(_ev()))
    assert calls == ["slow", "ok"]


def test_sync_handler_exception_isolated():
    """同步 handler 抛异常同样不影响其他订阅者。"""
    bus = EventBus()
    calls: list[str] = []

    def boom(_e):
        raise ValueError("同步炸")

    def ok(_e):
        calls.append("ok")

    bus.subscribe("message", boom, owner="bad")
    bus.subscribe("message", ok, owner="good")

    asyncio.run(bus.emit(_ev()))
    assert calls == ["ok"]


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    calls: list[str] = []

    async def h(_e):
        calls.append("hit")

    handle = bus.subscribe("message", h, owner="h")
    asyncio.run(bus.emit(_ev()))
    handle.dispose()
    asyncio.run(bus.emit(_ev()))
    assert calls == ["hit"]               # 摘除后不再收到
