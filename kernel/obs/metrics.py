"""插件指标与熔断（设计文档 §9）。

每插件记录：处理数、失败数、超时数、耗时 P50/P95。
连续失败超过阈值 → 熔断一段时间，期间不再调用，面板上能看到状态。

不自动重启：熔断只是止损，恢复要靠人（或 reload），避免"坏了又自己爬起来"。
"""
from __future__ import annotations

import time
from collections import defaultdict, deque


def _pct(samples: list[int], p: float) -> int:
    if not samples:
        return 0
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, max(0, int(round(p / 100 * (len(ordered) - 1)))))
    return ordered[idx]


class PluginMetrics:
    def __init__(self, *, window: int = 200, trip_after: int = 5,
                 cooldown_s: float = 60.0) -> None:
        self.window = window
        self.trip_after = trip_after
        self.cooldown_s = cooldown_s
        self._samples: dict[str, deque[int]] = defaultdict(lambda: deque(maxlen=window))
        self._counts: dict[str, dict[str, int]] = defaultdict(
            lambda: {"handled": 0, "failed": 0, "timeout": 0})
        self._consecutive_fail: dict[str, int] = defaultdict(int)
        self._tripped_until: dict[str, float] = {}

    # ── 记录 ──────────────────────────────────────────────
    def record(self, plugin: str, cost_ms: int, *, ok: bool = True,
               timeout: bool = False) -> None:
        c = self._counts[plugin]
        c["handled"] += 1
        self._samples[plugin].append(int(cost_ms))
        if timeout:
            c["timeout"] += 1
        if ok:
            self._consecutive_fail[plugin] = 0
            return
        c["failed"] += 1
        self._consecutive_fail[plugin] += 1
        if self._consecutive_fail[plugin] >= self.trip_after:
            until = time.time() + self.cooldown_s
            self._tripped_until[plugin] = until
            self._consecutive_fail[plugin] = 0        # 重置计数，冷却后重新观察

    # ── 熔断判定 ──────────────────────────────────────────
    def tripped(self, plugin: str) -> bool:
        until = self._tripped_until.get(plugin)
        if not until:
            return False
        if time.time() < until:
            return True
        self._tripped_until.pop(plugin, None)
        return False

    def reset(self, plugin: str | None = None) -> None:
        if plugin is None:
            self._samples.clear()
            self._counts.clear()
            self._consecutive_fail.clear()
            self._tripped_until.clear()
            return
        self._samples.pop(plugin, None)
        self._counts.pop(plugin, None)
        self._consecutive_fail.pop(plugin, None)
        self._tripped_until.pop(plugin, None)

    # ── 快照 ──────────────────────────────────────────────
    def snapshot(self) -> dict:
        now = time.time()
        out: dict[str, dict] = {}
        for plugin, c in self._counts.items():
            samples = list(self._samples[plugin])
            until = self._tripped_until.get(plugin, 0)
            out[plugin] = {
                **c,
                "p50_ms": _pct(samples, 50),
                "p95_ms": _pct(samples, 95),
                "samples": len(samples),
                "consecutive_fail": self._consecutive_fail[plugin],
                "tripped": bool(until and until > now),
                "trip_remain_s": max(0, int(until - now)) if until else 0,
            }
        return out

    def sample(self) -> dict:
        """一行时序快照：交给采集器每分钟落库一次。"""
        snap = self.snapshot()
        return {
            "handled": sum(v["handled"] for v in snap.values()),
            "failed": sum(v["failed"] for v in snap.values()),
            "timeout": sum(v["timeout"] for v in snap.values()),
            "plugins": len(snap),
            "tripped": sum(1 for v in snap.values() if v["tripped"]),
        }

    def summary(self) -> dict:
        snap = self.snapshot()
        return {
            "plugins": len(snap),
            "handled": sum(v["handled"] for v in snap.values()),
            "failed": sum(v["failed"] for v in snap.values()),
            "timeout": sum(v["timeout"] for v in snap.values()),
            "tripped": [k for k, v in snap.items() if v["tripped"]],
        }
