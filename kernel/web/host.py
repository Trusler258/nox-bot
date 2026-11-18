"""HTTP 宿主（内核部件）。

只负责：监听、鉴权、挂载插件注册的路由、健康检查。
具体接口（日志、统计、模块控制）由插件注册，内核不内置业务路由。
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from aiohttp import web

from kernel.obs.logger import get_logger

log = get_logger("kernel.web")

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class WebHost:
    """可被 ModuleRegistry 单点重启。"""

    name = "web"

    def __init__(self, *, host: str = "127.0.0.1", port: int = 59310, token: str = "",
                 health_fn: Callable[[], dict] | None = None) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.health_fn = health_fn
        self._routes: list[tuple[str, str, Handler]] = []
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.started_at = 0.0
        self.state = "stopped"

    # ── 插件注册路由的入口 ────────────────────────────────
    def route(self, method: str, path: str, handler: Handler) -> None:
        self._routes.append((method.upper(), path, handler))
        log.debug("路由已注册", extra={"fields": {"method": method.upper(),
                                                 "path": path,
                                                 "total": len(self._routes)}})

    def path(self) -> str:
        return f"http://{self.host}:{self.port}"

    # ── 生命周期 ──────────────────────────────────────────
    async def start(self) -> None:
        app = web.Application(middlewares=[self._auth])
        for method, path, handler in self._routes:
            app.router.add_route(method, path, handler)
        app.router.add_get("/healthz", self._healthz)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        self.started_at = time.time()
        self.state = "running"
        log.info("HTTP 宿主已启动", extra={"fields": {
            "host": self.host, "port": self.port,
            "auth": bool(self.token), "routes": len(self._routes)}})

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
        self.state = "stopped"

    # ── 中间件 ────────────────────────────────────────────
    @web.middleware
    async def _auth(self, request: web.Request, handler):
        if request.path == "/healthz":
            return await handler(request)
        if self.token:
            got = request.headers.get("X-API-Token") or request.query.get("token", "")
            if got != self.token:
                return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        else:
            peer = request.remote or ""
            if peer not in ("127.0.0.1", "::1", "localhost"):
                return web.json_response(
                    {"ok": False, "error": "未配置 token，仅允许本机访问"}, status=403)
        return await handler(request)

    async def _healthz(self, request: web.Request) -> web.Response:
        data: dict[str, Any] = {"ok": True,
                                "uptime_s": round(time.time() - self.started_at, 1)
                                if self.started_at else 0}
        if self.health_fn:
            try:
                data.update(self.health_fn())
            except Exception:
                data["health_error"] = True
        return web.json_response(data)
