"""能力注册表：插件之间唯一的调用通道。

规矩：插件不 import 插件，只按名字取服务。取不到就加载失败（或按 optional 降级）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from kernel.obs.logger import get_logger

log = get_logger("kernel.services")


@dataclass(slots=True)
class Service:
    name: str
    version: int
    impl: Any
    owner: str


class ServiceRegistry:
    def __init__(self) -> None:
        self._items: dict[tuple[str, int], Service] = {}

    def provide(self, name: str, impl: Any, *, version: int = 1, owner: str = "") -> None:
        key = (name, version)
        if key in self._items and self._items[key].owner != owner:
            log.warning("服务名重复，后提供者覆盖", extra={"fields": {
                "name": name, "version": version,
                "old": self._items[key].owner, "new": owner}})
        self._items[key] = Service(name=name, version=version, impl=impl, owner=owner)

    def require(self, name: str, *, version: int | None = None) -> Any:
        """按名取服务。不指定版本时取最高版本。"""
        if version is not None:
            svc = self._items.get((name, version))
            if svc is None:
                raise LookupError(f"服务缺失: {name}@{version}")
            return svc.impl
        candidates = [v for (n, _), v in self._items.items() if n == name]
        if not candidates:
            raise LookupError(f"服务缺失: {name}")
        best = max(candidates, key=lambda s: s.version)
        return best.impl

    def has(self, name: str, *, version: int | None = None) -> bool:
        try:
            self.require(name, version=version)
            return True
        except LookupError:
            return False

    def release_owner(self, owner: str) -> int:
        keys = [k for k, v in self._items.items() if v.owner == owner]
        for k in keys:
            del self._items[k]
        return len(keys)

    @property
    def count(self) -> int:
        return len(self._items)
