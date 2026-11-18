"""队列与背压：满时丢最老、按群分桶、出站限速。"""
import asyncio

from kernel.bus.eventbus import EventBus
from kernel.event import Event, Message
from kernel.queues import InboundPump, OutboundSender, RateLimiter


def ev(gid="g1", text="x"):
    return Event(id=str(id(text)), platform="mock", kind="message.group",
                 group_id=gid, message=Message.from_text(text))


def test_drop_oldest_when_full():
    bus = EventBus()
    pump = InboundPump(bus, maxsize=2)
    for i in range(4):
        pump.submit(ev(text=f"m{i}"))
    # 队列容量 2，投了 4 条，至少丢 2 条
    assert pump.dropped_total >= 2


def test_per_group_isolation():
    """一个群塞满不影响另一个群的统计。"""
    pump = InboundPump(EventBus(), maxsize=1)
    for _ in range(5):
        pump.submit(ev(gid="hot", text="x"))
    pump.submit(ev(gid="cold", text="y"))
    stats = pump.stats()
    assert stats["buckets"] == 2
    assert stats["dropped"] >= 4


def test_pump_delivers_and_counts():
    bus = EventBus()
    got = []

    async def handler(e):
        got.append(e.text)

    bus.subscribe("message", handler, owner="t")

    async def main():
        pump = InboundPump(bus, maxsize=10)
        await pump.start()
        pump.submit(ev(text="hello"))
        await asyncio.sleep(0.1)
        await pump.stop()
        return pump.stats()

    stats = asyncio.run(main())
    assert got == ["hello"]
    assert stats["handled"] == 1


def test_rate_limiter_blocks_burst():
    async def main():
        rl = RateLimiter(rate_per_min=60, burst=2)   # 每秒 1 个，桶容量 2
        t0 = asyncio.get_event_loop().time()
        for _ in range(4):
            await rl.acquire()
        return asyncio.get_event_loop().time() - t0

    cost = asyncio.run(main())
    assert cost >= 1.5   # 前两个瞬时，后两个要等约 2 秒


class _FakeAdapter:
    def __init__(self, fail_times=0):
        self.sent = []
        self.fail_times = fail_times

    async def send(self, target, message, *, reply_to=None):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("boom")
        self.sent.append(message.text)
        return {}


def test_outbound_retries_then_succeeds():
    async def main():
        a = _FakeAdapter(fail_times=1)
        s = OutboundSender(a, rate_per_min=6000, retry_max=3)
        await s.start()
        await s.send("t", Message.from_text("hi"))
        await asyncio.sleep(1.2)
        await s.stop()
        return a, s

    a, s = asyncio.run(main())
    assert a.sent == ["hi"]
    assert s.dead == []


def test_outbound_dead_letter():
    async def main():
        a = _FakeAdapter(fail_times=99)
        s = OutboundSender(a, rate_per_min=6000, retry_max=2)
        await s.start()
        await s.send("t", Message.from_text("hi"))
        await asyncio.sleep(1.5)
        await s.stop()
        return s

    s = asyncio.run(main())
    assert len(s.dead) == 1 and "boom" in s.dead[0]["error"]
