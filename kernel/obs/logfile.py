"""日志文件读取：按条件过滤、翻页、列出文件。

不做流式扫描全部历史：默认从尾部读最近 N 行，够排查用。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class LogFile:
    name: str
    size: int
    mtime: float

    def as_dict(self) -> dict:
        return {"name": self.name, "size": self.size, "mtime": self.mtime,
                "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.mtime))}


def list_log_files(log_dir: str | Path) -> list[LogFile]:
    d = Path(log_dir)
    if not d.exists():
        return []
    files = [LogFile(f.name, f.stat().st_size, f.stat().st_mtime)
             for f in d.glob("*.jsonl") if f.is_file()]
    return sorted(files, key=lambda f: f.mtime, reverse=True)


def read_lines_tail(path: Path, *, max_bytes: int = 2_000_000) -> list[str]:
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()                       # 丢掉被截断的首行
        data = f.read()
    return data.decode("utf-8", errors="replace").splitlines()


def query_file(path: str | Path, *, level: str | None = None, logger: str | None = None,
               q: str | None = None, trace: str | None = None,
               since: float | None = None, limit: int = 200) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    want = (level or "").upper()
    out: list[dict] = []
    for line in reversed(read_lines_tail(p)):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if want and item.get("level") != want:
            continue
        if logger and not str(item.get("logger", "")).startswith(logger):
            continue
        if trace and item.get("trace_id") != trace:
            continue
        if q:
            blob = f"{item.get('logger')} {item.get('msg')} {item.get('fields')}"
            if q not in blob:
                continue
        if since:
            ts = item.get("ts", "")
            try:
                if time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) < since:
                    continue
            except Exception:
                pass
        out.append(item)
        if len(out) >= limit:
            break
    return out
