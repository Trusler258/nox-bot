"""日志分析：聚合、分位、提示。"""
import json
import time

from kernel.obs.analyze import render_report, summarize
from kernel.obs.analyze_cli import main as cli_main


def item(level="INFO", logger="kernel.test", msg="ok", cost=None, ts="2026-11-16T09:00:00.000"):
    fields = {}
    if cost is not None:
        fields["cost_ms"] = cost
    return {"ts": ts, "level": level, "logger": logger, "msg": msg, "fields": fields}


def test_levels_and_loggers():
    items = [item(), item(), item(level="WARNING"), item(level="ERROR", logger="plugin.wdsj")]
    d = summarize(items)
    assert d["total"] == 4
    assert d["levels"]["INFO"] == 2
    assert d["level_order"] == ["INFO", "WARNING", "ERROR"]
    assert d["loggers"][0]["name"] == "kernel.test"


def test_error_grouping_normalizes_ids():
    items = [
        item(level="ERROR", logger="adapter.qq", msg="send failed 400 group 1234567"),
        item(level="ERROR", logger="adapter.qq", msg="send failed 400 group 7654321"),
        item(level="ERROR", logger="kernel.db", msg="database is locked"),
    ]
    d = summarize(items)
    top = d["errors"][0]
    assert top["count"] == 2                     # 两个不同群号被归一成同类
    assert "建议" not in top                      # 数据里不带"建议"字段
    assert top["hint"]                            # 但有 hint


def test_slow_percentiles_and_top():
    items = [item(cost=100), item(cost=600), item(cost=1200), item(cost=5000)]
    d = summarize(items, slow_ms=500)
    s = d["slow"]
    assert s["count"] == 3
    assert s["max"] == 5000
    assert s["top"][0]["cost_ms"] == 5000
    assert 1200 <= s["p99"] <= 5000


def test_timeline_buckets():
    base = time.mktime(time.strptime("2026-11-16T09:00:00", "%Y-%m-%dT%H:%M:%S"))
    items = []
    for i in range(5):
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(base + i * 60)) + ".000"
        items.append(item(ts=ts))
    d = summarize(items, bucket_minutes=10)
    assert len(d["timeline"]) == 1                # 5 分钟都落在同一个 10 分钟桶


def test_hint_mapping():
    d = summarize([item(level="ERROR", msg="TimeoutError: read timeout")])
    assert "超时" in d["errors"][0]["hint"] or "慢" in d["errors"][0]["hint"]


def test_render_report_contains_sections():
    d = summarize([item(), item(level="ERROR", msg="send failed 400")])
    text = render_report(d, source="app.jsonl")
    assert "级别分布" in text and "错误聚合" in text and "建议" in text


def test_cli_with_file(tmp_path):
    f = tmp_path / "app-2026-11-16.jsonl"
    rows = [item(level="ERROR", msg="send failed 400"),
            item(level="INFO", cost=900)]
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    rc = cli_main(["--file", str(f), "--slow", "500"])
    assert rc == 0


def test_cli_no_data(tmp_path):
    rc = cli_main(["--dir", str(tmp_path), "--date", "2020-01-01"])
    assert rc == 1
