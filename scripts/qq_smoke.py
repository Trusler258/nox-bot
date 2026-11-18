"""官方 QQ 适配器联机冒烟：只握手、不发言。

用法（凭据走环境变量，不要写进文件）：
    set QQ_APPID=... & set QQ_SECRET=...
    python scripts/qq_smoke.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kernel.adapters.qq_official import QQOfficialAdapter  # noqa: E402
from kernel.event import Message  # noqa: E402
from kernel.obs.logger import setup_logging  # noqa: E402


async def main() -> int:
    setup_logging("INFO", None, json_mode=False)
    app_id, secret = os.environ.get("QQ_APPID", ""), os.environ.get("QQ_SECRET", "")
    if not app_id or not secret:
        print("缺少 QQ_APPID / QQ_SECRET")
        return 2

    got: list = []

    async def on_event(e):
        got.append(e)
        print(f"[event] {e.kind} group={e.group_id} user={e.user_id} text={e.text!r}")

    a = QQOfficialAdapter(app_id, secret, on_event=on_event)
    await a.start()
    for _ in range(40):                     # 最多等 20 秒
        await asyncio.sleep(0.5)
        if a.state == "ready":
            break
    print(f"[state] {a.state}")
    ok = a.state == "ready"
    await a.stop()
    print("[结论] " + ("PASS —— 握手成功" if ok else "FAIL —— 未进入 ready"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
