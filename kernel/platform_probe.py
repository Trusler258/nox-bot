"""平台探测：启动读一次，之后全项目读这份结果。

★ 2026-11-16 决定：内核允许平台分支，但只允许读这里的结果，
  禁止各处重复 `platform.system()`（见设计文档 §15）。
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PlatformProfile:
    os_name: str
    os_version: str
    python_version: str
    is_container: bool
    path_max: int
    fs_case_sensitive: bool
    default_encoding: str
    has_systemd: bool
    has_launchd: bool
    chromium_path: str
    cpu_count: int

    @property
    def is_windows(self) -> bool:
        return self.os_name == "Windows"

    @property
    def long_path_prefix(self) -> str:
        return "\\\\?\\" if self.is_windows and self.path_max < 300 else ""

    def as_dict(self) -> dict:
        return {
            "os": f"{self.os_name} {self.os_version}",
            "python": self.python_version,
            "container": self.is_container,
            "path_max": self.path_max,
            "case_sensitive": self.fs_case_sensitive,
            "encoding": self.default_encoding,
            "systemd": self.has_systemd,
            "launchd": self.has_launchd,
            "chromium": self.chromium_path or "(未找到)",
            "cpus": self.cpu_count,
        }


def _is_container() -> bool:
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8", errors="ignore") as f:
            return "docker" in f.read() or "kubepod" in f.read()
    except OSError:
        return False


def _find_chromium() -> str:
    candidates = [
        "/usr/bin/chromium-browser", "/usr/bin/chromium", "/usr/bin/google-chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    return ""


def probe() -> PlatformProfile:
    os_name = platform.system()
    return PlatformProfile(
        os_name=os_name,
        os_version=platform.release(),
        python_version=".".join(map(str, sys.version_info[:3])),
        is_container=_is_container(),
        path_max=260 if os_name == "Windows" else 4096,
        fs_case_sensitive=os_name != "Windows",
        default_encoding=sys.getdefaultencoding(),
        has_systemd=os.path.exists("/run/systemd/system"),
        has_launchd=os_name == "Darwin",
        chromium_path=_find_chromium(),
        cpu_count=os.cpu_count() or 1,
    )
