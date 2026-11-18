"""调度器：从账本取任务、执行、失败重试；启动时回收过期租约。

M0 只做「轮询 + 单 worker」，M1 再按需扩并发。
"""
from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Awaitable, Callable

from kernel.obs.logger import get_logger
from kernel.storage.db import JobLedger

log = get_logger("kernel.scheduler")

JobFn = Callable[[dict], Awaitable[Any] | Any]


class Scheduler:
    def __init__(self, ledger: JobLedger, *, owner: str = "scheduler",
                 tick: float = 1.0, lease_seconds: float = 120) -> None:
        self.ledger = ledger
        self.owner = owner
        self.tick = tick
        self.lease = lease_seconds
        self._handlers: dict[str, JobFn] = {}
        self._task: asyncio.Task | None = None
        self._stopping = False

    def register(self, job_type: str, fn: JobFn) -> None:
        self._handlers[job_type] = fn

    def unregister_all(self, prefix: str = "") -> int:
        keys = [k for k in self._handlers if k.startswith(prefix)]
        for k in keys:
            del self._handlers[k]
        return len(keys)

    async def run_once(self) -> int:
        """跑一轮：最多取一个任务。返回 1 表示执行过。"""
        job = self.ledger.claim(self.owner, self.lease, list(self._handlers) or None)
        if job is None:
            return 0
        fn = self._handlers.get(job["type"])
        if fn is None:
            self.ledger.fail(job["id"], f"没有处理者: {job['type']}", retry=False)
            return 1
        started = time.perf_counter()
        try:
            import json
            payload = json.loads(job["payload"] or "{}")
            result = fn(payload)
            if inspect.isawaitable(result):
                await result
            cost = round((time.perf_counter() - started) * 1000)
            self.ledger.complete(job["id"], f"ok {cost}ms")
            log.info("任务完成", extra={"fields": {
                "job": job["id"], "type": job["type"], "cost_ms": cost}})
        except Exception as e:
            log.error("任务失败", exc_info=True, extra={"fields": {
                "job": job["id"], "type": job["type"]}})
            self.ledger.fail(job["id"], f"{type(e).__name__}: {e}")
        return 1

    async def _loop(self) -> None:
        recovered = self.ledger.recover_expired()
        if recovered:
            log.warning("回收过期租约任务", extra={"fields": {"count": recovered}})
        while not self._stopping:
            try:
                did = await self.run_once()
                if not did:
                    await asyncio.sleep(self.tick)
            except asyncio.CancelledError:
                break
            except Exception:
                log.error("调度轮询异常", exc_info=True)
                await asyncio.sleep(self.tick)

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
