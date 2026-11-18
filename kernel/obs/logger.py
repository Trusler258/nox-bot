"""结构化日志：一行一条 JSON，trace_id 贯穿全链路。

★ 设计约束：日志一律不截断（这是 V2 踩过的坑：截断后看不到原始 content，
  排查时只能靠猜）。需要短视图请在展示层做，不要在这里切。
"""
from __future__ import annotations

import json
import logging
import sys
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

_trace_id: ContextVar[str] = ContextVar("trace_id", default="")


def set_trace(trace_id: str) -> None:
    _trace_id.set(trace_id)


def get_trace() -> str:
    return _trace_id.get()


def _kv(value: Any) -> str:
    """字段值的短表示：容器压成紧凑 JSON，其余原样（不截断）。"""
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return str(value)
    return str(value)


class ConsoleFormatter(logging.Formatter):
    """标准纯文本日志，格式与 V2 一致：

        [2026-11-16 15:12:00.152] [INFO    ] (dispatch:handle:51) 指令解析: cmd=wdsj

    四个位置固定：方括号时间、方括号定宽级别、圆括号来源、消息。
    结构化字段以 `k=v` 空格分隔追加在消息后（V2 也是这么写的）。
    只在终端上色——journald 或重定向到文件时输出 ANSI 会变成乱码。
    """

    COLORS = {
        "DEBUG": "\033[38;5;244m",
        "INFO": "\033[38;5;39m",
        "WARNING": "\033[38;5;214m",
        "ERROR": "\033[38;5;203m",
        "CRITICAL": "\033[1;38;5;203m",
    }
    TIME_C = "\033[38;5;240m"
    SRC_C = "\033[38;5;245m"
    RESET = "\033[0m"

    def __init__(self, *, color: bool | None = None) -> None:
        super().__init__()
        self.color = sys.stdout.isatty() if color is None else color

    def format(self, record: logging.LogRecord) -> str:
        ts = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
              + f".{int(record.msecs):03d}")
        level = f"[{record.levelname:<8}]"
        src = f"({record.module}:{record.funcName}:{record.lineno})"
        msg = record.getMessage()

        fields = getattr(record, "fields", None)
        if isinstance(fields, dict) and fields:
            kvs = " ".join(f"{k}={_kv(v)}" for k, v in fields.items()
                           if v is not None and v != "")
            if kvs:
                msg = f"{msg}: {kvs}"

        tid = getattr(record, "trace_id", "") or _trace_id.get()
        if tid:
            msg = f"{msg} [trace={tid[:8]}]"

        if self.color:
            c = self.COLORS.get(record.levelname, "")
            line = (f"{self.TIME_C}[{ts}]{self.RESET} {c}{level}{self.RESET} "
                    f"{self.SRC_C}{src}{self.RESET} {msg}")
        else:
            line = f"[{ts}] {level} {src} {msg}"

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
                  + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "src": f"{record.module}:{record.funcName}:{record.lineno}",
            "msg": record.getMessage(),
        }
        tid = _trace_id.get()
        if tid:
            payload["trace_id"] = tid
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload["fields"] = extra
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


# 这些库在 DEBUG 下会把每帧 keepalive、每次连接池复用都打出来，
# 实测 400 条日志里 392 条是它们——真正有用的 8 条被埋掉。
NOISY_LOGGERS = ("websockets", "websockets.client", "websockets.server",
                 "httpcore", "httpcore.http11", "httpcore.connection",
                 "httpx", "asyncio", "urllib3", "aiohttp", "playwright",
                 "PIL", "multipart")


def setup_logging(level: str = "INFO", log_dir: str | Path | None = None,
                  json_mode: bool = True, *, console_format: str = "text",
                  quiet: tuple[str, ...] | list[str] = NOISY_LOGGERS,
                  quiet_level: str = "WARNING") -> logging.Logger:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    # 先压噪音：自己的日志一律保留，第三方只留 WARNING 及以上
    for name in quiet:
        logging.getLogger(name).setLevel(quiet_level.upper())

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(JsonFormatter() if console_format == "json" else ConsoleFormatter())
    root.addHandler(stream)

    if log_dir:
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        # 文件固定 JSON：接口与 analyze 都依赖这个格式
        fh = logging.FileHandler(d / f"app-{time.strftime('%Y-%m-%d')}.jsonl",
                                 encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)

    return logging.getLogger("nox")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
