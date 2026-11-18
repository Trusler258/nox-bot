"""§5.3 reload 期间的事件暂存：上限 100、超时丢弃、重放。"""
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kernel.bus.eventbus import EventBus  # noqa: E402
from kernel.event import Event  # noqa: E402


def _ev(i: int = 0) -> Event:
    return Event(id=str(i), platform="mock", kind="message.group",
                 group_id="g", user_id="u")


def test_pause_buffers_events():
    bus = EventBus()
    got: list[str] = []

    async def h(e):
        got.append(e.id)

    bus.subscribe("message", h, owner="p1")
    assert bus.pause_owner("p1") == 1

    # 暂停期间不投递，但事件被存下来
    asyncio.run(bus.emit(_ev(1)))
    assert got == []
    assert len(bus._paused["p1"].events) == 1


def test_resume_replays():
    bus = EventBus()
    got: list[str] = []

    async def h(e):
        got.append(e.id)

    bus.subscribe("message", h, owner="p1")
    bus.pause_owner("p1")
    asyncio.run(bus.emit(_ev(7)))

    # resume 里用 create_task 异步重放，需要给它一个事件循环跑完
    async def scenario():
        n = bus.resume_owner("p1")
        await asyncio.sleep(0.05)
        return n

    n = asyncio.run(scenario())
    assert n == 1
    assert got == ["7"]


def test_buffer_capacity_drops_oldest():
    bus = EventBus()
    bus.subscribe("message", lambda e: None, owner="p1")
    bus.pause_owner("p1", maxsize=3)
    for i in range(5):
        asyncio.run(bus.emit(_ev(i)))
    ids = [e.id for e in bus._paused["p1"].events]
    assert ids == ["2", "3", "4"]          # 保留最新三条


def test_timeout_discarded_on_resume():
    bus = EventBus()
    got: list[str] = []

    async def h(e):
        got.append(e.id)

    bus.subscribe("message", h, owner="p1")
    bus.pause_owner("p1")

    old = _ev(1)
    old.ts = time.time() - 120             # 两分钟前的消息
    bus._paused["p1"].events.append(old)

    async def scenario():
        return bus.resume_owner("p1", max_age=30)

    assert asyncio.run(scenario()) == 0    # 超时直接丢，不重放
    assert got == []


def test_pause_returns_zero_without_subscriptions():
    bus = EventBus()
    assert bus.pause_owner("nobody") == 0
    assert "nobody" not in bus._paused


def test_paused_owners_listed():
    bus = EventBus()
    bus.subscribe("message", lambda e: None, owner="a")
    bus.subscribe("notice", lambda e: None, owner="b")
    bus.pause_owner("a")
    bus.pause_owner("b")
    assert sorted(bus.paused_owners()) == ["a", "b"]
