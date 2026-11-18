"""原子写盘（§8.5）：写 tmp → fsync → rename。

断电时要么是旧文件、要么是新文件，不会出现半截 JSON。
"""
from __future__ import annotations

import os
from pathlib import Path


def atomic_write(path: str | Path, data: str | bytes, *,
                 encoding: str = "utf-8") -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    raw = data.encode(encoding) if isinstance(data, str) else data
    with open(tmp, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def atomic_write_json(path: str | Path, obj, *, indent: int = 2) -> None:
    import json
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=indent) + "\n")
