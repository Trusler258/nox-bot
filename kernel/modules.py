"""可重启单元的注册表（设计文档 §17）。

规矩：
- 每个单元实现 stop() / start()，stop 必须幂等
- 重启按依赖逆序停、正序启
- 事件总线不在此列：它的"重启"等于重建插件运行时
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from kernel.obs.logger import get_logger

log = get_logger("kernel.modules")


@runtime_checkable
class Restartable(Protocol):
    async def stop(self) -> None: ...
    async def start(self) -> None: ...


@dataclass
class Unit:
    name: str
    obj: Any
    order: int = 100           # 越大越先停、越后启
    state: str = "unknown"


@dataclass
class ModuleRegistry:
    _units: dict[str, Unit] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def register(self, name: str, obj: Any, *, order: int = 100) -> None:
        self._units[name] = Unit(name=name, obj=obj, order=order)

    def set_state(self, name: str, state: str) -> None:
        unit = self._units.get(name)
        if unit is not None:
            unit.state = state

    def unregister(self, name: str) -> None:
        self._units.pop(name, None)

    def names(self) -> list[str]:
        return sorted(self._units, key=lambda n: -self._units[n].order)

    async def restart(self, name: str) -> bool:
        unit = self._units.get(name)
        if unit is None:
            log.error("未知单元", extra={"fields": {"unit": name}})
            return False
        async with self._lock:
            unit.state = "stopping"
            try:
                await unit.obj.stop()
            except Exception:
                log.error("停止失败", exc_info=True, extra={"fields": {"unit": name}})
            unit.state = "starting"
            try:
                await unit.obj.start()
                unit.state = "running"
            except Exception:
                unit.state = "error"
                log.error("启动失败", exc_info=True, extra={"fields": {"unit": name}})
                return False
        log.info("单元已重启", extra={"fields": {"unit": name}})
        return True

    async def restart_all(self) -> dict[str, bool]:
        """按依赖逆序停、正序启；中途失败不影响其他单元。"""
        result: dict[str, bool] = {}
        for name in self.names():
            unit = self._units[name]
            try:
                await unit.obj.stop()
                unit.state = "stopped"
            except Exception:
                log.error("停止失败", exc_info=True, extra={"fields": {"unit": name}})
        for name in reversed(self.names()):
            unit = self._units[name]
            try:
                await unit.obj.start()
                unit.state = "running"
                result[name] = True
            except Exception:
                unit.state = "error"
                result[name] = False
                log.error("启动失败", exc_info=True, extra={"fields": {"unit": name}})
        return result

    def snapshot(self) -> dict[str, str]:
        return {n: u.state for n, u in self._units.items()}
