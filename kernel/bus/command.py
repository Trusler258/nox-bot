"""命令表：注册、别名、冲突检测、按 owner 摘除。

规模预期 300+ 条（V2 实测 309 条），所以别名合并与冲突告警是必需的，
不能让"后加载的插件悄悄盖掉前一个插件的命令"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from kernel.obs.logger import get_logger
from kernel.text import normalize_text

log = get_logger("kernel.command")

CommandHandler = Callable[..., Awaitable[Any] | Any]

# 默认前缀；实际值来自 config/app.toml 的 [commands].prefixes
DEFAULT_PREFIXES = ("/~", "/", ".", "#", "!")


@dataclass(slots=True)
class Command:
    name: str
    handler: CommandHandler
    owner: str
    perm: str = "user"          # owner | admin | group_admin | user
    aliases: tuple[str, ...] = ()
    help: str = ""
    shadowed: bool = False


@dataclass(slots=True)
class _Entry:
    cmd: Command
    names: tuple[str, ...]


class CommandRegistry:
    def __init__(self, prefixes: tuple[str, ...] | list[str] | None = None,
                 *, case_sensitive: bool = False) -> None:
        # 长前缀优先，避免 "/" 抢走 "/~xxx"
        self.prefixes = tuple(sorted(prefixes or DEFAULT_PREFIXES, key=len, reverse=True))
        self.case_sensitive = case_sensitive
        self._by_name: dict[str, Command] = {}
        self._entries: list[_Entry] = []

    def register(self, name: str, handler: CommandHandler, *, owner: str = "",
                 aliases: tuple[str, ...] = (), perm: str = "user",
                 help: str = "") -> Command:
        norm = (lambda x: x) if self.case_sensitive else (lambda x: x.lower())
        names = tuple(dict.fromkeys((norm(name), *(norm(a) for a in aliases))))
        cmd = Command(name=norm(name), handler=handler, owner=owner,
                      perm=perm, aliases=aliases, help=help)
        for n in names:
            prev = self._by_name.get(n)
            if prev is not None and prev.owner != owner:
                # 不静默覆盖：保留先注册者，新来的标记 shadowed
                cmd.shadowed = True
                log.warning("命令名冲突，已忽略后者", extra={"fields": {
                    "name": n, "kept": prev.owner, "ignored": owner}})
                continue
            self._by_name[n] = cmd
        self._entries.append(_Entry(cmd, names))
        return cmd

    def remove_owner(self, owner: str) -> int:
        removed = 0
        keep: list[_Entry] = []
        for e in self._entries:
            if e.cmd.owner == owner:
                for n in e.names:
                    if self._by_name.get(n) is e.cmd:
                        del self._by_name[n]
                removed += 1
            else:
                keep.append(e)
        self._entries = keep
        return removed

    def parse(self, text: str) -> tuple[str, str] | None:
        """把 "／～wdsj 张三 bw" 解析成 ("wdsj", "张三 bw")；不是指令返回 None。"""
        t = normalize_text(text)
        if not t:
            return None
        prefix = next((p for p in self.prefixes if t.startswith(p)), None)
        if prefix is None:
            return None
        body = t[len(prefix):]
        # "/~cmd" 这种双字符前缀会被 "/" 先吃掉 "~"，这里补偿
        body = body.lstrip("~").lstrip()
        if not body:
            return None
        head, _, rest = body.partition(" ")
        return (head if self.case_sensitive else head.lower()), rest.strip()

    def resolve(self, name: str) -> Command | None:
        return self._by_name.get(name if self.case_sensitive else name.lower())

    @property
    def names(self) -> list[str]:
        """当前注册的全部名字（含别名），给冒烟测试与面板用。"""
        return sorted(self._by_name)

    @property
    def count(self) -> int:
        return len(self._by_name)
