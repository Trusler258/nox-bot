"""配置加载：defaults → config/app.toml → 环境变量覆盖。

M0 只做全局配置；插件配置（config/plugins/<id>.toml）留到 M1。
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "app": {"name": "幻梦", "profile": "full", "timezone": "Asia/Shanghai",
            "data_dir": "data", "log_level": "INFO"},
    "runtime": {"shutdown_timeout_s": 15, "worker_threads": 8},
    "adapters": {"enabled": [], "inbound_queue_size": 200},
    "scheduler": {"tick_s": 5, "lease_seconds": 120, "job_retention_days": 7},
    "obs": {"log_dir": "logs", "log_json": True},
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_overrides(prefix: str = "V3_") -> dict:
    """V3_APP_LOG_LEVEL=DEBUG → {"app": {"log_level": "DEBUG"}}（只支持两层）。

    也认少量常用短名（见 _ENV_ALIAS）。没有这个表时
    `V3_LOG_LEVEL` 会被拆成 `{"log": {"level"}}` 静默丢进错的段。
    """
    aliases = {
        "LOG_LEVEL": ("app", "log_level"),
        "LOG_DIR": ("obs", "log_dir"),
        "WEB_PORT": ("web", "port"),
    }
    out: dict[str, Any] = {}
    for key, val in os.environ.items():
        if not key.startswith(prefix):
            continue
        tail = key[len(prefix):]
        if tail in aliases:
            sect, field = aliases[tail]
            out.setdefault(sect, {})[field] = val
            continue
        path = tail.lower().split("_", 1)
        if len(path) != 2:
            continue
        sect, field = path
        out.setdefault(sect, {})[field] = val
    return out


def load_config(root: str | Path) -> dict:
    root = Path(root)
    cfg = dict(DEFAULTS)
    file = root / "config" / "app.toml"
    if file.exists():
        with open(file, "rb") as f:
            cfg = _deep_merge(cfg, tomllib.load(f))
    cfg = _deep_merge(cfg, _env_overrides())
    return cfg


def project_root() -> Path:
    """仓库根：本文件在 kernel/config/loader.py，上溯三层。"""
    return Path(__file__).resolve().parents[2]
