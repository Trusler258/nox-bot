"""事件总线：订阅、优先级、超时、按 owner 摘除。

三条不可破的规矩：
1. 处理器抛异常不外溢——只记日志，不影响其他插件
2. 每个处理器有超时，慢插件不会拖死整条链路
3. 卸载插件时按 owner 批量摘除，不留残钩
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from kernel.event import Event
from kernel.obs.logger import get_logger

log = get_logger("kernel.bus")

Handler = Callable[[Event], Awaitable[Any] | Any]


@dataclass(slots=True)
class Handle:
    """注册句柄。dispose 必须幂等。"""
    kind: str
    owner: str
    _dispose: Callable[[], None] | None = None
    disposed: bool = False

    def dispose(self) -> None:
        if self.disposed:
            return
        self.disposed = True
        if self._dispose:
            self._dispose()


@dataclass(slots=True)
class _Sub:
    pattern: str
    handler: Handler
    priority: int
    owner: str
    timeout: float
    handle: Handle = field(default=None)  # type: ignore[assignment]


@dataclass(slots=True)
class _Paused:
    """reload 期间挂着的事件（§5.3 上限 100 条，超时丢弃）。"""
    owner: str
    patterns: list[str]
    since: float
    events: deque


class EventBus:
    def __init__(self, default_timeout: float = 8.0) -> None:
        self.default_timeout = default_timeout
        self._subs: list[_Sub] = []
        self._paused: dict[str, _Paused] = {}

    # ── reload 期间的事件暂存（§5.3）──────────────────────
    def pause_owner(self, owner: str, *, maxsize: int = 100) -> int:
        """reload 前调用：记住该插件的订阅形态，期间到达的事件先存着。

        返回记录的 pattern 数（0 表示该插件本来就没订阅，不用暂存）。
        """
        patterns = sorted({s.pattern for s in self._subs if s.owner == owner})
        if not patterns:
            return 0
        self._paused[owner] = _Paused(owner=owner, patterns=patterns,
                                      since=time.time(),
                                      events=deque(maxlen=maxsize))
        log.debug("已暂停投递", extra={"fields": {"owner": owner,
                                                 "patterns": patterns}})
        return len(patterns)

    def resume_owner(self, owner: str, *, max_age: float = 30.0) -> int:
        """reload 后调用：把暂存的事件异步重放。返回声称要重放的条数。

        ★ 超时（默认 30s）的事件直接丢弃——重放一条十秒前的消息只会造成
          "机器人突然回一句陈年旧话"。
        """
        p = self._paused.pop(owner, None)
        if p is None or not p.events:
            return 0
        now = time.time()
        fresh = [e for e in p.events if now - e.ts <= max_age]
        dropped = len(p.events) - len(fresh)
        log.info("重放 reload 期间的事件", extra={"fields": {
            "owner": owner, "replay": len(fresh), "dropped": dropped}})
        if fresh:
            try:
                asyncio.get_running_loop().create_task(self._replay(fresh))
            except RuntimeError:
                log.warning("没有事件循环，丢弃暂存事件",
                            extra={"fields": {"owner": owner}})
                return 0
        return len(fresh)

    async def _replay(self, events: list[Event]) -> None:
        for ev in events:
            await self.emit(ev)

    def paused_owners(self) -> list[str]:
        return list(self._paused)

    def subscribe(self, pattern: str, handler: Handler, *, priority: int = 0,
                  owner: str = "", timeout: float | None = None) -> Handle:
        timeout = self.default_timeout if timeout is None else timeout
        sub = _Sub(pattern=pattern, handler=handler, priority=priority,
                   owner=owner, timeout=timeout)
        handle = Handle(kind="event", owner=owner,
                        _dispose=lambda: self._subs.remove(sub) if sub in self._subs else None)
        sub.handle = handle
        self._subs.append(sub)
        self._subs.sort(key=lambda s: -s.priority)
        return handle

    def unsubscribe_owner(self, owner: str) -> int:
        drop = [s for s in self._subs if s.owner == owner]
        for s in drop:
            s.handle.dispose()
        return len(drop)

    def _match(self, pattern: str, kind: str) -> bool:
        if pattern in ("*", ""):
            return True
        if pattern == kind:
            return True
        # "message" 匹配 "message.group"
        return kind.startswith(pattern + ".")

    async def emit(self, event: Event) -> int:
        # 暂停中的插件先把事件存下来（§5.3）
        for owner, p in self._paused.items():
            if any(self._match(pat, event.kind) for pat in p.patterns):
                if len(p.events) == p.events.maxlen:
                    log.warning("reload 暂存已满，丢弃最老事件",
                                extra={"fields": {"owner": owner,
                                                  "maxsize": p.events.maxlen}})
                p.events.append(event)

        # 暂停中的 owner 不投递（即使订阅还在——reload 的 unload 会摘订阅，
        # 但摘之前可能有并发事件，这里双保险）
        targets = [s for s in self._subs
                   if self._match(s.pattern, event.kind)
                   and s.owner not in self._paused]
        for sub in targets:
            started = time.perf_counter()
            try:
                result = sub.handler(event)
                if asyncio.iscoroutine(result):
                    await asyncio.wait_for(result, timeout=sub.timeout)
            except asyncio.TimeoutError:
                log.warning("处理器超时", extra={"fields": {
                    "owner": sub.owner, "pattern": sub.pattern, "timeout": sub.timeout}})
            except Exception:
                log.error("处理器异常", exc_info=True, extra={"fields": {
                    "owner": sub.owner, "pattern": sub.pattern}})
            finally:
                cost = (time.perf_counter() - started) * 1000
                if cost > 2000:
                    log.warning("处理器偏慢", extra={"fields": {
                        "owner": sub.owner, "pattern": sub.pattern, "cost_ms": round(cost)}})
        return len(targets)

    @property
    def size(self) -> int:
        return len(self._subs)
