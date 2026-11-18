"""日志读取：内存环与文件两种来源的过滤。"""
import json
import logging

from kernel.obs.buffer import RingBuffer
from kernel.obs.logfile import query_file


def make_record(msg, level=logging.INFO, logger="kernel.test", **fields):
    rec = logging.LogRecord(logger, level, __file__, 1, msg, None, None)
    rec.fields = fields
    return rec


def test_buffer_keeps_capacity_and_order():
    buf = RingBuffer(capacity=3)
    for i in range(5):
        buf.emit(make_record(f"m{i}"))
    items = buf.query(limit=10)
    assert [i["msg"] for i in items] == ["m4", "m3", "m2"]   # 新→旧，且只留 3 条


def test_buffer_filter_level_logger_query():
    buf = RingBuffer(capacity=50)
    buf.emit(make_record("start ok", logging.INFO, "kernel.runtime"))
    buf.emit(make_record("slow handler", logging.WARNING, "kernel.bus", cost=2300))
    buf.emit(make_record("boom", logging.ERROR, "plugin.wdsj", player="x"))

    assert len(buf.query(level="WARNING")) == 1
    assert len(buf.query(logger="plugin")) == 1
    assert buf.query(q="2300")[0]["level"] == "WARNING"
    assert buf.query(q="wdsj")[0]["msg"] == "boom"


def test_trace_filter():
    buf = RingBuffer(capacity=10)
    rec = make_record("with trace")
    rec.trace_id = "abc123"
    buf.emit(rec)
    dummy = make_record("no trace")
    dummy.trace_id = ""
    buf.emit(dummy)
    assert len(buf.query(trace="abc123")) == 1


def test_file_query(tmp_path):
    f = tmp_path / "app-2026-11-16.jsonl"
    lines = [
        {"ts": "2026-11-16T09:00:00.000", "level": "INFO", "logger": "a", "msg": "one"},
        {"ts": "2026-11-16T09:01:00.000", "level": "ERROR", "logger": "b", "msg": "two",
         "fields": {"k": "v"}},
        {"ts": "2026-11-16T09:02:00.000", "level": "INFO", "logger": "a", "msg": "three"},
    ]
    f.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines), encoding="utf-8")

    assert len(query_file(f, limit=10)) == 3
    assert query_file(f, level="ERROR")[0]["msg"] == "two"
    assert query_file(f, logger="a", limit=1)[0]["msg"] == "three"   # 倒序取最新
    assert query_file(f, q="v")[0]["level"] == "ERROR"
