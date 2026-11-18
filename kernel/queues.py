"""入站队列与背压。

三条约束（设计文档 §6）：
1. 同一群串行，不同群并行——一个群刷屏不能拖慢别的群
2. 队列满时丢最老的并计数，不能无限堆积把内存吃掉
3. 出站按平台限速，失败重试有上限
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from kernel.event import Event
from kernel.obs.logger import get_logger

log = get_logger("kernel.queues")


@dataclass
class QueueStats:
    depth: int = 0
    dropped: int = 0
    handled: int = 0
    peak: int = 0


@dataclass
class _Bucket:
    queue: asyncio.Queue
    task: asyncio.Task | None = None
    stats: QueueStats = field(default_factory=QueueStats)


class InboundPump:
    """按 (平台, 群) 分桶的入站泵。"""

    def __init__(self, bus, *, maxsize: int = 200, name: str = "inbound") -> None:
        self.bus = bus
        self.maxsize = maxsize
        self.name = name
        self._buckets: dict[str, _Bucket] = {}
        self._running = False
        self.dropped_total = 0

    def _key(self, event: Event) -> str:
        return f"{event.platform}:{event.group_id or event.user_id or 'unknown'}"

    def _bucket(self, key: str) -> _Bucket:
        b = self._buckets.get(key)
        if b is None:
            b = _Bucket(queue=asyncio.Queue(maxsize=self.maxsize))
            self._buckets[key] = b
        return b

    def submit(self, event: Event) -> bool:
        """非阻塞投递。返回 False 表示发生了丢弃（丢最老的一条）。"""
        b = self._bucket(self._key(event))
        dropped = False
        try:
            b.queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                b.queue.get_nowait()          # 丢最老
                b.stats.dropped += 1
                self.dropped_total += 1
                dropped = True
                log.warning("队列已满，丢弃最老事件", extra={"fields": {
                    "bucket": self._key(event), "maxsize": self.maxsize,
                    "dropped_total": self.dropped_total}})
            except asyncio.QueueEmpty:
                pass
            try:
                b.queue.put_nowait(event)
            except asyncio.QueueFull:
                return False
        log.debug("收到消息", extra={"fields": {
            "platform": event.platform, "kind": event.kind,
            "group": event.group_id or "-", "user": event.user_id,
            "chars": len(event.text or ""), "text": event.text or ""}})
        b.stats.depth = b.queue.qsize()
        b.stats.peak = max(b.stats.peak, b.stats.depth)
        if self._running and b.task is None:
            b.task = asyncio.create_task(self._worker(self._key(event), b))
        return not dropped

    async def _worker(self, key: str, b: _Bucket) -> None:
        while True:
            try:
                event = await asyncio.wait_for(b.queue.get(), timeout=30)
            except asyncio.TimeoutError:
                b.task = None
                return                          # 空闲退出，下次投递再拉起
            try:
                await self.bus.emit(event)
                b.stats.handled += 1
            except Exception:
                log.error("事件处理失败", exc_info=True, extra={"fields": {"bucket": key}})
            finally:
                b.stats.depth = b.queue.qsize()

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        for b in self._buckets.values():
            if b.task:
                b.task.cancel()
                try:
                    await b.task
                except (asyncio.CancelledError, Exception):
                    pass
                b.task = None

    def stats(self) -> dict[str, Any]:
        return {
            "buckets": len(self._buckets),
            "depth": sum(b.queue.qsize() for b in self._buckets.values()),
            "dropped": self.dropped_total,
            "handled": sum(b.stats.handled for b in self._buckets.values()),
            "peak": max((b.stats.peak for b in self._buckets.values()), default=0),
        }


class RateLimiter:
    """令牌桶：给每个平台一个出站速率上限。"""

    def __init__(self, rate_per_min: int, burst: int | None = None) -> None:
        self.rate = max(1, rate_per_min) / 60.0     # 每秒补充
        self.capacity = float(burst or max(1, rate_per_min // 6))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                need = (cost - self._tokens) / self.rate
                await asyncio.sleep(min(need, 1.0))


class OutboundSender:
    """出站队列：限速 + 重试，失败进死信不阻塞后续消息。"""

    @property
    def capabilities(self):
        """透出适配器的能力矩阵（markdown / buttons / image…）。"""
        return getattr(self.adapter, "capabilities", None)

    def capabilities_for(self, platform: str | None):
        """多平台时按平台取能力矩阵（NapCat 没有 markdown，别发富文本）。"""
        fn = getattr(self.adapter, "capabilities_for", None)
        if callable(fn):
            return fn(platform)
        return self.capabilities

    def __init__(self, adapter, *, rate_per_min: int = 30, retry_max: int = 3,
                 maxsize: int = 500) -> None:
        self.adapter = adapter
        self.limiter = RateLimiter(rate_per_min)
        self.retry_max = retry_max
        self.maxsize = maxsize
        self._q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._task: asyncio.Task | None = None
        self.dead: list[dict] = []
        self.sent = 0

    async def send(self, target: str, message, *, reply_to: str | None = None) -> None:
        try:
            self._q.put_nowait({"target": target, "message": message, "reply_to": reply_to})
        except asyncio.QueueFull:
            log.warning("出站队列已满，丢弃", extra={"fields": {"target": target}})

    async def _loop(self) -> None:
        while True:
            item = await self._q.get()
            for attempt in range(1, self.retry_max + 1):
                await self.limiter.acquire()
                try:
                    await self.adapter.send(item["target"], item["message"],
                                            reply_to=item["reply_to"])
                    self.sent += 1
                    msg = item["message"]
                    log.debug("已发送", extra={"fields": {
                        "target": item["target"],
                        "kind": "image" if getattr(msg, "only_image", False) else "text",
                        "chars": 0 if getattr(msg, "only_image", False) else len(msg.text)}})
                    break
                except Exception as e:
                    if attempt == self.retry_max:
                        self.dead.append({**item, "error": f"{type(e).__name__}: {e}"})
                        log.error("出站失败，进死信", exc_info=True,
                                  extra={"fields": {"target": item["target"],
                                                    "attempts": attempt}})
                    else:
                        log.warning("发送失败，准备重试", extra={"fields": {
                            "target": item["target"], "attempt": attempt,
                            "error": f"{type(e).__name__}: {e}"}})
                        await asyncio.sleep(0.5 * attempt)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
