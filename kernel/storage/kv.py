"""KV 存储（§8.4）：`data/kv/<ns>.json`，一个命名空间一个文件。

设计文档定的介质分工：

| 介质 | 适合 | 不适合 |
|---|---|---|
| SQLite | 要查询、聚合、事务、上万行 | 单条大文本、图片 |
| KV | 键值、整读整写、< 500 条 | 高频写、并发写 |

所以"群开关""默认玩家""订阅列表"这类小而少改的状态走这里，
不要往 SQLite 里塞一行 JSON，也不要往 data/ 根目录扔 json 文件。

文件是**整文件读写 + 原子替换**：先写 .tmp、fsync、再 rename，
断电时要么旧文件、要么新文件，不会出现半截 JSON。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from kernel.obs.logger import get_logger
from kernel.storage.atomic import atomic_write_json

log = get_logger("kernel.kv")


class KV:
    """按命名空间分文件的键值存储。

    `KV(root/"data"/"kv")` 后每个 `ns` 对应 `data/kv/<ns>.json`。
    读走内存缓存（按 mtime 失效），写立刻落盘。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict]] = {}   # ns → (mtime, data)

    # ── 内部 ──────────────────────────────────────────────
    def _file(self, ns: str) -> Path:
        safe = "".join(c for c in str(ns) if c.isalnum() or c in "-_.") or "default"
        return self.root / f"{safe}.json"

    def _load(self, ns: str) -> dict:
        f = self._file(ns)
        if not f.is_file():
            return {}
        try:
            mtime = f.stat().st_mtime
        except OSError:
            return {}
        hit = self._cache.get(ns)
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("顶层必须是对象")
        except Exception:
            # 坏文件不静默重建：留一份 .bad 供排查，再从空对象开始
            log.error("KV 文件解析失败，已备份为 .bad", exc_info=True,
                      extra={"fields": {"ns": ns, "path": str(f)}})
            try:
                f.replace(f.with_suffix(".json.bad"))
            except OSError:
                pass
            data = {}
        self._cache[ns] = (mtime, data)
        return data

    def _save(self, ns: str, data: dict) -> None:
        f = self._file(ns)
        atomic_write_json(f, data)
        try:
            self._cache[ns] = (f.stat().st_mtime, data)
        except OSError:
            self._cache.pop(ns, None)

    # ── 对外 ──────────────────────────────────────────────
    def get(self, ns: str, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._load(ns).get(str(key), default)

    def set(self, ns: str, key: str, value: Any) -> None:
        with self._lock:
            data = dict(self._load(ns))
            data[str(key)] = value
            self._save(ns, data)

    def delete(self, ns: str, key: str) -> bool:
        with self._lock:
            data = dict(self._load(ns))
            if str(key) not in data:
                return False
            data.pop(str(key), None)
            self._save(ns, data)
            return True

    def all(self, ns: str) -> dict:
        with self._lock:
            return dict(self._load(ns))

    def update(self, ns: str, patch: dict) -> dict:
        """整批更新（读-改-写一次），并发写少的场景够用。"""
        with self._lock:
            data = dict(self._load(ns))
            data.update({str(k): v for k, v in patch.items()})
            self._save(ns, data)
            return dict(data)

    def clear(self, ns: str) -> int:
        with self._lock:
            n = len(self._load(ns))
            self._save(ns, {})
            return n

    def namespaces(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json"))

    def stats(self) -> dict:
        files = list(self.root.glob("*.json"))
        return {
            "namespaces": len(files),
            "bytes": sum(f.stat().st_size for f in files if f.is_file()),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
