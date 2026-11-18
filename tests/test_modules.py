"""模块级重启：单独重启一个单元，不影响其他单元。"""
import asyncio

from kernel.modules import ModuleRegistry


class FakeUnit:
    def __init__(self, name):
        self.name = name
        self.starts = 0
        self.stops = 0
        self.running = False
        self.fail_start = False

    async def start(self):
        if self.fail_start:
            raise RuntimeError("start failed")
        self.starts += 1
        self.running = True

    async def stop(self):
        self.stops += 1
        self.running = False


def test_restart_only_touches_target():
    async def main():
        reg = ModuleRegistry()
        a, b = FakeUnit("adapter"), FakeUnit("scheduler")
        reg.register("adapter", a, order=200)
        reg.register("scheduler", b, order=100)
        await a.start()
        await b.start()
        ok = await reg.restart("adapter")
        return ok, a, b

    ok, a, b = asyncio.run(main())
    assert ok is True
    assert a.stops == 1 and a.starts == 2      # 停一次再起一次
    assert b.stops == 0 and b.starts == 1      # 没被波及


def test_restart_unknown_unit_returns_false():
    async def main():
        return await ModuleRegistry().restart("nope")

    assert asyncio.run(main()) is False


def test_restart_all_orders_and_isolates_failure():
    async def main():
        reg = ModuleRegistry()
        a, b = FakeUnit("adapter"), FakeUnit("render")
        b.fail_start = True
        reg.register("adapter", a, order=200)
        reg.register("render", b, order=50)
        return await reg.restart_all(), a, b

    result, a, b = asyncio.run(main())
    assert result["adapter"] is True
    assert result["render"] is False           # 一个失败不影响另一个
    assert a.running is True


def test_snapshot_states():
    async def main():
        reg = ModuleRegistry()
        u = FakeUnit("x")
        reg.register("x", u)
        await reg.restart("x")
        return reg.snapshot()

    assert asyncio.run(main()) == {"x": "running"}
