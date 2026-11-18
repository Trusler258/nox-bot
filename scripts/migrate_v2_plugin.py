"""V2 插件批量迁移工具：机械替换 + 报告剩余问题。

用法：python scripts/migrate_v2_plugin.py <插件名>

只做**机械可判定**的替换，替换完把剩余的外部依赖列出来，
剩下的手工修。规则都来自 nasa/box/dice/relay 这四个已经搬完的插件。
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

V2 = Path("G:/py/qqbot/plugins")
V3 = Path(__file__).resolve().parents[1] / "plugins"

REPLACEMENTS: list[tuple[str, str]] = [
    # 1) 类型注解：V3 的 ctx 不是这个类
    ("from core.plugin.api import PluginContext\n", ""),
    # 2) logger → log（V3 的别名也兼容，但统一路）
    ("self.ctx.logger", "self.ctx.log"),
    # 3) 构造：ctx 改由 on_load 传入
    ("    def __init__(self, ctx: PluginContext):\n        self.ctx = ctx\n",
     "    def __init__(self) -> None:\n        self.ctx = None\n"),
    ("    def __init__(self, ctx: PluginContext):\n        self.ctx = ctx\n",
     "    def __init__(self) -> None:\n        self.ctx = None\n"),
    # 4) 生命周期：V3 是同步 on_load(ctx)
    ("    async def on_load(self):",
     "    def on_load(self, ctx) -> None:\n        self.ctx = ctx"),
    # 5) logger 模块
    ("from core.logger import get_logger",
     "from kernel.obs.logger import get_logger"),
    # 6) V2 的 ctx.config(key, default) 是方法，V3 的 ctx.config 是 dict，取值用 ctx.conf
    ("self.ctx.config(", "self.ctx.conf("),
]

# register_command(name, desc, handler) → ctx.command(name, v2_command(handler), help=desc)
_REG = re.compile(
    r"self\.ctx\.capability\.register_command\(\s*"
    r"([^,]+?)\s*,\s*"
    r"(\"[^\"]*\"|'[^']*')\s*,\s*"
    r"(self\.\w+)\s*,?\s*\)",
    re.S)

# V2 的发送服务 → V3 的 ctx.message
_SEND = [
    (re.compile(r"send_by_chat_type\(([^,]+),\s*([^,]+),\s*([^,]+),\s*[^)]+\)"),
     r"self.ctx.message.send(\1, \2, \3)"),
    (re.compile(r"\bsend_file\("), "self.ctx.message.send_file("),
    (re.compile(r"\bsend_group_msg\(([^,]+),\s*([^)]+)\)"),
     r"self.ctx.message.send(\1, \2, True)"),
    (re.compile(r"\bsend_private_msg\(([^,]+),\s*([^)]+)\)"),
     r"self.ctx.message.send(\1, \2, False)"),
]

# 搬完后这些 import 应该没了；还有的就是需要手工处理的
_FOREIGN = re.compile(
    r"^\s*(from|import)\s+(core|services|modules|utils)(\.|\s)", re.M)


def migrate(name: str) -> int:
    src_dir = V2 / name
    dst_dir = V3 / name
    if not src_dir.is_dir():
        print(f"[错误] V2 里没有 {src_dir}")
        return 1

    dst_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for f in src_dir.iterdir():
        if f.suffix == ".py" or f.name in ("manifest.json",):
            shutil.copy2(f, dst_dir / f.name)
            copied.append(f.name)
    print(f"[复制] {', '.join(copied)}")

    main = dst_dir / "main.py"
    if not main.is_file():
        print("[错误] 没有 main.py")
        return 1

    s = main.read_text(encoding="utf-8")
    for old, new in REPLACEMENTS:
        s = s.replace(old, new)

    def _reg(m: re.Match) -> str:
        return (f"self.ctx.command({m.group(1)}, v2_command({m.group(3)}), "
                f"help={m.group(2)})")
    s = _REG.sub(_reg, s)

    for pat, rep in _SEND:
        s = pat.sub(rep, s)

    # 引入 v2_command 适配层
    if "v2_command" in s and "from kernel.plugins.compat import v2_command" not in s:
        s = s.replace("from __future__ import annotations\n",
                      "from __future__ import annotations\n\n"
                      "from kernel.plugins.compat import v2_command\n", 1)

    main.write_text(s, encoding="utf-8", newline="\n")

    # 报告剩余问题
    print("[剩余外部依赖]（需要手工改）")
    found = False
    for f in sorted(dst_dir.glob("*.py")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if _FOREIGN.match(line):
                print(f"  {f.name}: {line.strip()}")
                found = True
    if not found:
        print("  无")

    # 报告未替换的 register_command / sender
    tail = main.read_text(encoding="utf-8")
    for kw in ("capability.register_command", "services.sender",
               "core.plugin.cores", "modules.changelog"):
        if kw in tail:
            print(f"  ⚠ {main.name} 里还有 {kw}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python scripts/migrate_v2_plugin.py <插件名>")
        raise SystemExit(2)
    raise SystemExit(migrate(sys.argv[1]))
