"""日志分析 CLI。

用法：
    python -m kernel.obs.analyze --since 2h
    python -m kernel.obs.analyze --date 2026-11-16 --level ERROR --json
    python -m kernel.obs.analyze --file logs/app-2026-11-16.jsonl --slow 1000
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from kernel.obs.analyze import render_report, summarize
from kernel.obs.logfile import list_log_files, query_file


def parse_since(text: str) -> float:
    """支持 30m / 2h / 7d，返回起始时间戳。"""
    text = (text or "").strip().lower()
    if not text:
        return 0.0
    unit = text[-1]
    try:
        n = float(text[:-1])
    except ValueError:
        return 0.0
    mult = {"m": 60, "h": 3600, "d": 86400}.get(unit)
    if mult is None:
        return 0.0
    return time.time() - n * mult


def collect(args) -> tuple[list[dict], str]:
    if args.file:
        p = Path(args.file)
        return query_file(p, level=args.level, logger=args.logger, q=args.q,
                          limit=args.limit, since=parse_since(args.since)), p.name

    log_dir = Path(args.dir)
    since = parse_since(args.since)
    if args.date:
        files = [log_dir / f"app-{args.date}.jsonl"]
    elif since:
        files = [f for f in list_log_files(log_dir)
                 if f.mtime >= since or True][:3]
        files = [log_dir / f.name for f in files]
    else:
        files = [log_dir / f.name for f in list_log_files(log_dir)[:1]]

    items: list[dict] = []
    for p in files:
        items.extend(query_file(p, level=args.level, logger=args.logger, q=args.q,
                                limit=args.limit, since=since or None))
    items.sort(key=lambda x: x.get("ts", ""))
    return items, ", ".join(p.name for p in files) or "(无文件)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="log-analyze", description="日志分析")
    ap.add_argument("--dir", default="logs", help="日志目录")
    ap.add_argument("--file", help="直接指定某个 jsonl")
    ap.add_argument("--date", help="指定日期 YYYY-MM-DD")
    ap.add_argument("--since", default="", help="时间范围：30m / 2h / 7d")
    ap.add_argument("--level", help="只看某级别")
    ap.add_argument("--logger", help="按模块前缀过滤")
    ap.add_argument("--q", help="关键字")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--slow", type=float, default=500.0, help="慢操作阈值 ms")
    ap.add_argument("--bucket", type=int, default=10, help="时间分桶（分钟）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而不是报表")
    args = ap.parse_args(argv)

    items, source = collect(args)
    if not items:
        print(f"没有匹配的日志（来源 {source}）")
        return 1

    data = summarize(items, bucket_minutes=args.bucket, slow_ms=args.slow)
    if args.json:
        print(json.dumps({"source": source, **data}, ensure_ascii=False, indent=2))
    else:
        print(render_report(data, source=source))
    return 0


if __name__ == "__main__":
    sys.exit(main())
