"""日志分析：把一堆日志条目压成结论。

给两个地方用：
- 命令行 `python -m kernel.obs.analyze --since 2h`（控制台直接看）
- HTTP `/api/logs/analyze`（面板/脚本消费）

分析项：级别分布、模块分布、错误聚合（同类归并）、慢操作分位、时间分布。
"""
from __future__ import annotations

import re
import time
from collections import Counter, defaultdict
from typing import Any, Iterable

_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# 归一化错误消息：把数字、ID、路径抹掉，才能把同类错误聚到一起
_NOISE = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}[\w-]*", re.I), "<uuid>"),
    (re.compile(r"\bROBOT1\.0_[A-Za-z0-9._\-]+"), "<msgid>"),
    (re.compile(r"\b\d{4,}\b"), "<n>"),
    (re.compile(r"0x[0-9a-f]+", re.I), "<hex>"),
    (re.compile(r"[\w./\\-]+\.(py|json|toml|html|png|jpg)(:\d+)?"), "<file>"),
]

# 常见错误的排查提示（命中关键词就给建议，不命中也照常输出）
_HINTS = [
    (r"send failed\s*4\d\d", "检查 msg_id / msg_seq 是否合法、内容是否超长"),
    (r"send failed\s*5\d\d", "官方侧异常，看是否被限流，稍后重试"),
    (r"TimeoutError|timeout", "上游慢或网络抖动，考虑加缓存或调大超时"),
    (r"ConnectionError|ConnectError", "DNS/网络不可达，检查代理与白名单"),
    (r"database is locked", "SQLite 写竞争，检查是否绕过了单写锁"),
    (r"json|JSONDecode", "上游返回非预期格式，先打原始报文再解析"),
    (r"401|403|unauthor", "凭据失效或权限不足"),
    (r"429|rate limit", "被限流，检查令牌桶配额"),
]


def _norm(msg: str) -> str:
    s = msg or ""
    for pat, rep in _NOISE:
        s = pat.sub(rep, s)
    return s.strip()[:160]


def _epoch(ts: str) -> float:
    try:
        return time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return 0.0


def _hint(text: str) -> str:
    for pat, tip in _HINTS:
        if re.search(pat, text, re.I):
            return tip
    return ""


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, int(round((len(sorted_vals) - 1) * p))))
    return sorted_vals[k]


def summarize(items: Iterable[dict], *, bucket_minutes: int = 10,
              slow_ms: float = 500.0, top_n: int = 8) -> dict:
    """核心分析。items 为日志条目（新→旧或旧→新都行）。"""
    items = list(items)
    levels: Counter = Counter()
    loggers: Counter = Counter()
    errors: dict[str, dict] = {}
    slow: list[tuple[float, str, str]] = []
    buckets: Counter = Counter()

    for it in items:
        lv = (it.get("level") or "INFO").upper()
        levels[lv] += 1
        logger = it.get("logger") or "-"
        loggers[logger] += 1
        ts = it.get("ts") or ""
        if ts:
            ep = _epoch(ts)
            if ep:
                buckets[int(ep // (bucket_minutes * 60))] += 1

        if lv in ("ERROR", "CRITICAL"):
            key = f"{logger} | {_norm(it.get('msg', ''))}"
            row = errors.setdefault(key, {"count": 0, "first": ts, "last": ts,
                                          "sample": it.get("msg", ""),
                                          "hint": _hint(str(it.get("msg", "")) )})
            row["count"] += 1
            row["last"] = ts

        fields = it.get("fields") or {}
        cost = 0.0
        if isinstance(fields, dict):
            for k in ("cost_ms", "cost", "elapsed_ms", "duration_ms"):
                if k in fields:
                    try:
                        cost = float(fields[k])
                    except (TypeError, ValueError):
                        cost = 0.0
                    break
        if cost >= slow_ms:
            slow.append((cost, logger, str(it.get("msg", ""))[:60]))

    slow_sorted = sorted(slow)
    costs = [c for c, _, _ in slow_sorted]

    return {
        "total": len(items),
        "levels": dict(levels),
        "level_order": [lv for lv in _LEVELS if levels.get(lv)],
        "loggers": [{"name": k, "count": v} for k, v in loggers.most_common(top_n)],
        "errors": sorted(
            [{"key": k, **v} for k, v in errors.items()],
            key=lambda x: -x["count"])[:top_n],
        "slow": {
            "count": len(slow),
            "p50": _percentile(costs, 0.50),
            "p95": _percentile(costs, 0.95),
            "p99": _percentile(costs, 0.99),
            "max": costs[-1] if costs else 0.0,
            "top": [{"cost_ms": c, "logger": l, "msg": m}
                    for c, l, m in sorted(slow, reverse=True)[:top_n]],
        },
        "timeline": [{"ts": k * bucket_minutes * 60, "count": v}
                     for k, v in sorted(buckets.items())],
        "bucket_minutes": bucket_minutes,
    }


def render_report(data: dict, *, title: str = "日志分析", source: str = "") -> str:
    """把分析结果画成控制台报表（等宽对齐 + ASCII 柱）。"""
    out: list[str] = []
    total = max(1, data["total"])
    bar_w = 28

    def bar(n: int) -> str:
        return "█" * max(0, min(bar_w, int(round(n / total * bar_w))))

    out.append(f"══ {title} ══")
    meta = [f"样本 {data['total']} 条"]
    if source:
        meta.append(f"来源 {source}")
    if data.get("bucket_minutes"):
        meta.append(f"分桶 {data['bucket_minutes']} 分钟")
    out.append("  " + " · ".join(meta))
    out.append("")

    out.append("级别分布")
    for lv in data["level_order"]:
        n = data["levels"].get(lv, 0)
        pct = n / total * 100
        out.append(f"  {lv:<9}{n:>7}  {bar(n):<28} {pct:5.1f}%")
    out.append("")

    out.append("模块 Top")
    for row in data["loggers"]:
        out.append(f"  {row['name']:<28}{row['count']:>7}  {bar(row['count'])}")
    out.append("")

    if data["errors"]:
        out.append("错误聚合（同类已归并）")
        for e in data["errors"]:
            out.append(f"  ×{e['count']:<5}{e['key']}")
            if e.get("sample"):
                out.append(f"        样本 {e['sample'][:80]}")
            if e.get("hint"):
                out.append(f"        建议 {e['hint']}")
            out.append(f"        最近 {e['last']}")
        out.append("")
    else:
        out.append("错误聚合  无 ERROR")
        out.append("")

    s = data["slow"]
    if s["count"]:
        out.append("慢操作（≥ 阈值）")
        out.append(f"  数量 {s['count']}  P50 {s['p50']:.0f}ms  "
                   f"P95 {s['p95']:.0f}ms  P99 {s['p99']:.0f}ms  最大 {s['max']:.0f}ms")
        for t in s["top"]:
            out.append(f"  {t['cost_ms']:>7.0f}ms  {t['logger']:<24}{t['msg']}")
        out.append("")

    if data["timeline"]:
        peak = max(t["count"] for t in data["timeline"])
        out.append("时间分布")
        for t in data["timeline"][-24:]:
            label = time.strftime("%m-%d %H:%M", time.localtime(t["ts"]))
            width = int(round(t["count"] / peak * 20)) if peak else 0
            out.append(f"  {label}  {'▇' * width:<20} {t['count']}")
        out.append("")

    tips = [e["hint"] for e in data["errors"] if e.get("hint")]
    if tips:
        out.append("结论")
        for tip in dict.fromkeys(tips):
            out.append(f"  - {tip}")
    return "\n".join(out)


if __name__ == "__main__":          # python -m kernel.obs.analyze
    from kernel.obs.analyze_cli import main as _main

    raise SystemExit(_main())
