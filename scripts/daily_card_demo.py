"""日榜卡渲染冒烟：从 SQLite 算日榜 → 渲染出图。

用法：python scripts/daily_card_demo.py [are] [日期 YYYY-MM-DD]
      不带 are 是起床战争，带 are 是竞技场。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kernel.obs.logger import setup_logging      # noqa: E402
from kernel.platform_probe import probe          # noqa: E402
from kernel.render.service import RenderService  # noqa: E402
from kernel.storage.db import Store              # noqa: E402


def _ensure_ns():
    if "v3_test" not in sys.modules:
        root = types.ModuleType("v3_test")
        root.__path__ = []                          # type: ignore[attr-defined]
        sys.modules["v3_test"] = root


def load(pid: str, leaf: str = "main"):
    _ensure_ns()
    pkg = f"v3_test.{pid}"
    if pkg not in sys.modules:
        m = types.ModuleType(pkg)
        m.__path__ = [str(ROOT / "plugins" / pid)]   # type: ignore[attr-defined]
        sys.modules[pkg] = m
    name = f"{pkg}.{leaf}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "plugins" / pid / f"{leaf}.py")
    mod = importlib.util.module_from_spec(spec)      # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)                     # type: ignore[union-attr]
    return mod


async def main() -> int:
    setup_logging("INFO", None, console_format="text")
    rest = [a for a in sys.argv[1:] if a]
    arena = bool(rest) and rest[0].lower() == "are"
    if arena:
        rest = rest[1:]
    day = rest[0] if rest else None

    daily = load("wdsj", "daily")
    daily_card = load("wdsj", "daily_card")

    db = Store(ROOT / "data" / "db" / "main.db")
    if arena:
        rank = daily.build_arena_daily(db, label_date=day)
        builder = lambda p: daily_card.arena_html(                       # noqa: E731
            daily.DailyRank(**p["rank"]), hours=p["hours"])
        name = "wdsj_arena_daily"
    else:
        rank = daily.build_daily(db, label_date=day)
        builder = lambda p: daily_card.bedwars_html(                     # noqa: E731
            daily.DailyRank(**p["rank"]), hours=p["hours"])
        name = "wdsj_daily"

    print(f"[日榜] 日期={rank.label_date} 区间={rank.time_start}→{rank.time_end} "
          f"上榜={len(rank.rows)} 新人={len(rank.new_players)} 回退={rank.fallback}")
    for r in rank.rows[:3]:
        print(f"       {r[0]}  击杀{r[1].get('kills', 0):+d}  "
              f"胜场{r[1].get('wins', 0):+d}")

    env = probe()
    r = RenderService(out_dir=ROOT / "data/store/cards", chromium_path=env.chromium_path)
    r.register(name, builder, selector="body")
    await r.start()
    from dataclasses import asdict
    result = await r.render(name, {"rank": asdict(rank), "hours": 4})
    await r.stop()
    if result is None:
        print("[结论] FAIL —— 渲染失败")
        return 1
    print(f"[结论] PASS  {result.path}  {result.path.stat().st_size}B  {result.cost_ms}ms")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
