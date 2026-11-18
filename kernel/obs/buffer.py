"""日志环形缓冲：给 HTTP 接口读最近 N 条，不用翻文件。"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

from kernel.obs.logger import get_trace


def record_to_dict(record: logging.LogRecord) -> dict[str, Any]:
    item: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
              + f".{int(record.msecs):03d}",
        "level": record.levelname,
        "logger": record.name,
        "src": f"{record.module}:{record.funcName}:{record.lineno}",
        "msg": record.getMessage(),
    }
    tid = getattr(record, "trace_id", "") or get_trace()
    if tid:
        item["trace_id"] = tid
    fields = getattr(record, "fields", None)
    if isinstance(fields, dict):
        item["fields"] = fields
    if record.exc_info:
        item["exc"] = logging.Formatter().formatException(record.exc_info)
    return item


class RingBuffer(logging.Handler):
    """挂到 root logger 上，保留最近 capacity 条。线程安全（HTTP 线程会读）。"""

    def __init__(self, capacity: int = 2000, level: int = logging.DEBUG) -> None:
        super().__init__(level=level)
        self._items: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self.capacity = capacity

    def emit(self, record: logging.LogRecord) -> None:
        try:
            item = record_to_dict(record)
        except Exception:
            return
        with self._lock:
            self._items.append(item)

    def query(self, *, level: str | None = None, logger: str | None = None,
              q: str | None = None, trace: str | None = None,
              since: float | None = None, limit: int = 200) -> list[dict]:
        with self._lock:
            items = list(self._items)
        out = []
        want = (level or "").upper()
        for it in reversed(items):                    # 新→旧
            if want and it.get("level") != want:
                continue
            if logger and not str(it.get("logger", "")).startswith(logger):
                continue
            if trace and it.get("trace_id") != trace:
                continue
            if q:
                blob = f"{it.get('logger')} {it.get('msg')} {it.get('fields')}"
                if q not in blob:
                    continue
            if since and _ts_to_epoch(it["ts"]) < since:
                continue
            out.append(it)
            if len(out) >= limit:
                break
        return out

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


def _ts_to_epoch(ts: str) -> float:
    try:
        return time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return 0.0
