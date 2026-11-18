"""总线：优先级、异常隔离、超时、按 owner 摘除。"""
import asyncio

from kernel.bus.eventbus import EventBus
from kernel.event import Event, Message


def ev(text="/~x"):
    return Event(id="1", platform="mock", kind="message.group",
                 group_id="g", user_id="u", message=Message.from_text(text))


def test_priority_order_and_owner_cleanup():
    bus = EventBus()
    order = []

    async def low(e):
        order.append("low")

    async def high(e):
        order.append("high")

    bus.subscribe("message", low, priority=1, owner="a")
    bus.subscribe("message", high, priority=10, owner="a")
    asyncio.run(bus.emit(ev()))
    assert order == ["high", "low"]
    assert bus.unsubscribe_owner("a") == 2
    assert bus.size == 0


def test_exception_is_isolated():
    bus = EventBus()
    hit = []

    async def bad(e):
        raise RuntimeError("boom")

    async def good(e):
        hit.append(1)

    bus.subscribe("message", bad, priority=10, owner="a")
    bus.subscribe("message", good, priority=1, owner="b")
    asyncio.run(bus.emit(ev()))
    assert hit == [1]  # 前一个炸了，后一个照常


def test_timeout_is_isolated():
    bus = EventBus()
    hit = []

    async def slow(e):
        await asyncio.sleep(1)

    async def good(e):
        hit.append(1)

    bus.subscribe("message", slow, priority=10, owner="a", timeout=0.05)
    bus.subscribe("message", good, priority=1, owner="b")
    asyncio.run(bus.emit(ev()))
    assert hit == [1]


def test_pattern_prefix_match():
    bus = EventBus()
    hit = []

    async def h(e):
        hit.append(e.kind)

    bus.subscribe("message", h, owner="a")
    asyncio.run(bus.emit(Event(id="2", platform="mock", kind="message.private")))
    asyncio.run(bus.emit(Event(id="3", platform="mock", kind="notice")))
    assert hit == ["message.private"]
