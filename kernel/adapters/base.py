"""适配器基类。平台差异只在这里出现，往上是统一的 Event / Message。"""
from __future__ import annotations

from typing import Protocol

from kernel.event import Capabilities, Event, Message


class Adapter(Protocol):
    name: str
    capabilities: Capabilities

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, target: str, message: Message,
                   *, reply_to: str | None = None) -> dict: ...


class MockAdapter:
    """M0 用：不连任何平台，只把发出去的消息记在内存里，另提供 feed() 注入事件。"""

    name = "mock"
    capabilities = Capabilities(quote=True, at=True, image=True, max_text_len=4000)

    def __init__(self, bus) -> None:
        self.bus = bus
        self.sent: list[dict] = []
        self.running = False

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def send(self, target: str, message: Message, *, reply_to: str | None = None) -> dict:
        rec = {"target": target, "text": message.text, "reply_to": reply_to}
        self.sent.append(rec)
        return rec

    async def feed(self, event: Event) -> int:
        """把事件喂给总线，模拟平台推送。"""
        return await self.bus.emit(event)
