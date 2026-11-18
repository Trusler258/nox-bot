"""HTTP 宿主与面板插件：路由挂载、鉴权、日志与分析接口。"""
import asyncio
import json
import logging
from types import SimpleNamespace

import httpx

from kernel.obs.buffer import RingBuffer
from kernel.web.host import WebHost


def run(coro):
    return asyncio.run(coro)


def test_host_auth_and_route_mount(tmp_path):
    buf = RingBuffer(5)

    async def hello(request):
        from aiohttp import web
        return web.json_response({"ok": True, "msg": "hi"})

    async def main():
        host = WebHost(port=59391, token="s3cret", health_fn=lambda: {"x": 1})
        host.route("GET", "/api/hello", hello)
        await host.start()
        async with httpx.AsyncClient(timeout=5) as cli:
            no = await cli.get("http://127.0.0.1:59391/api/hello")
            ok = await cli.get("http://127.0.0.1:59391/api/hello",
                               headers={"X-API-Token": "s3cret"})
            health = await cli.get("http://127.0.0.1:59391/healthz")
        await host.stop()
        return no.status_code, ok.json(), health.json()

    no, ok, health = run(main())
    assert no == 401
    assert ok["msg"] == "hi"
    assert health["ok"] is True and health["x"] == 1


def test_host_health_needs_no_token():
    async def main():
        host = WebHost(port=59392, token="t")
        await host.start()
        async with httpx.AsyncClient(timeout=5) as cli:
            r = await cli.get("http://127.0.0.1:59392/healthz")
        await host.stop()
        return r.status_code

    assert run(main()) == 200
