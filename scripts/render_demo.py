"""渲染服务联机冒烟：用系统 Chromium 出一张图。

用法：python scripts/render_demo.py [输出路径]
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kernel.obs.logger import setup_logging  # noqa: E402
from kernel.platform_probe import probe  # noqa: E402
from kernel.render.service import RenderService  # noqa: E402


def demo_html(payload: dict) -> str:
    rows = "".join(
        f"<tr><td>{k}</td><td class='v'>{v}</td></tr>" for k, v in payload.items())
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{{margin:0;background:#12141a;color:#e8e3db;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
.card{{width:420px;padding:22px;background:linear-gradient(160deg,#1b1f27,#151820);
border-radius:16px;border:1px solid #2a2f3a}}
h1{{font-size:17px;margin:0 0 14px;font-weight:600;letter-spacing:.02em}}
table{{width:100%;border-collapse:collapse}}
td{{padding:7px 0;border-bottom:1px solid #232833;font-size:13px}}
td:first-child{{color:#8b949e}}
td.v{{text-align:right;color:#58a6ff;font-variant-numeric:tabular-nums}}
</style></head><body><div class="card"><h1>渲染服务自检</h1>
<table>{rows}</table></div></body></html>"""


async def main() -> int:
    setup_logging("INFO", None, console_format="text")
    env = probe()
    out = sys.argv[1] if len(sys.argv) > 1 else "data/store/cards/_demo.png"
    s = RenderService(out_dir=Path(out).parent, chromium_path=env.chromium_path)
    s.register("demo", demo_html, selector=".card")
    await s.start()
    t0 = time.perf_counter()
    r = await s.render("demo", {"玩家": "_yyyay_", "模板": "bedwars-stats",
                                "击杀": 5354, "胜场": 810})
    cost = int((time.perf_counter() - t0) * 1000)
    r2 = await s.render("demo", {"玩家": "_yyyay_", "模板": "bedwars-stats",
                                 "击杀": 5354, "胜场": 810})
    await s.stop()
    if r is None:
        print("[结论] FAIL —— 渲染返回 None")
        return 1
    print(f"[结论] PASS  {r.path}  {r.path.stat().st_size}B  首次 {cost}ms  "
          f"缓存 {r2.cost_ms}ms cached={r2.cached}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
