"""渲染服务：模板注册 + 浏览器复用 + 缓存 + 降级。

设计要点（§7）：
- 浏览器单例，页面按 pool_size 复用，不每次冷启动
- 缓存键 = 模板名 + 载荷内容哈希，命中直接返回文件
- 失败不外抛：返回 None，调用方降级为文字
- 可被 ModuleRegistry 单独重启（浏览器挂了重启这个模块即可）
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from kernel.obs.logger import get_logger

log = get_logger("kernel.render")


@dataclass
class RenderResult:
    path: Path
    template: str
    cached: bool
    cost_ms: int


class RenderService:
    name = "render"

    def __init__(self, *, out_dir: str | Path, chromium_path: str = "",
                 pool_size: int = 1, timeout_ms: int = 15000,
                 cache_ttl_s: int = 600, cache_max: int = 500,
                 device_scale: int = 2, args: list[str] | None = None) -> None:
        self.out_dir = Path(out_dir)
        self.chromium_path = chromium_path
        self.pool_size = max(1, pool_size)
        self.timeout_ms = timeout_ms
        self.cache_ttl = cache_ttl_s
        self.cache_max = cache_max
        self.device_scale = device_scale
        self.args = args or ["--no-sandbox", "--disable-dev-shm-usage"]
        self.state = "stopped"

        self._templates: dict[str, Callable[[dict], str]] = {}
        self._selectors: dict[str, str] = {}
        self._versions: dict[str, str] = {}
        self._cache: dict[str, tuple[Path, float]] = {}
        self._pw = None
        self._browser = None
        self._pages: asyncio.Queue = asyncio.Queue()
        self._lock = asyncio.Lock()
        self.renders = 0
        self.failures = 0

    # ── 模板 ──────────────────────────────────────────────
    def register(self, name: str, builder: Callable[[dict], str],
                 *, selector: str | None = None, version: str = "") -> None:
        """builder(payload) -> 完整 HTML 字符串；selector 指定截取哪个元素。

        `version` 参与缓存键：模板实现改了但 payload 不变时，换个版本号
        就能让旧图失效。插件热重载会走 `invalidate()` 自动清，一般不用手填。
        """
        self._templates[name] = builder
        self._versions[name] = version
        if selector:
            self._selectors[name] = selector
        log.info("渲染模板已注册", extra={"fields": {
            "template": name, "selector": selector or "page",
            "version": version or "-"}})

    def invalidate(self, template: str | None = None) -> int:
        """清掉缓存（不删已生成的 PNG，文件名会按新键重建）。

        插件 `on_unload` / `reload` 时必须调，否则改了卡片代码还在发旧图。
        """
        if template is None:
            n = len(self._cache)
            self._cache.clear()
            return n
        n = 0
        for k in [k for k in self._cache if k.startswith(template + ":")]:
            self._cache.pop(k, None)
            n += 1
        self._templates.pop(template, None)
        self._versions.pop(template, None)
        self._selectors.pop(template, None)
        return n

    def templates(self) -> list[str]:
        return sorted(self._templates)

    # ── 生命周期 ──────────────────────────────────────────
    async def start(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        await self._ensure_browser()
        self.state = "running"

    async def stop(self) -> None:
        while not self._pages.empty():
            try:
                page = self._pages.get_nowait()
                await page.close()
            except Exception:
                pass
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
        self.state = "stopped"

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def _ensure_browser(self) -> None:
        if self._browser is not None and self._browser.is_connected():
            return
        from playwright.async_api import async_playwright

        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return
            if self._pw is None:
                self._pw = await async_playwright().start()
            kwargs: dict[str, Any] = {"headless": True, "args": self.args}
            if self.chromium_path:
                kwargs["executable_path"] = self.chromium_path
            self._browser = await self._pw.chromium.launch(**kwargs)
            for _ in range(self.pool_size):
                page = await self._browser.new_page(device_scale_factor=self.device_scale)
                await self._pages.put(page)
            log.info("浏览器已就绪", extra={"fields": {
                "path": self.chromium_path or "(bundled)",
                "pages": self.pool_size}})

    # ── 渲染 ──────────────────────────────────────────────
    def _cache_key(self, template: str, payload: dict) -> str:
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        ver = self._versions.get(template, "")
        salt = f"{template}|{ver}|{blob}"
        return f"{template}:{hashlib.sha256(salt.encode()).hexdigest()[:16]}"

    def _cache_get(self, key: str) -> Path | None:
        if not self.cache_ttl:            # ttl=0 表示不缓存
            return None
        hit = self._cache.get(key)
        if not hit:
            return None
        path, ts = hit
        if self.cache_ttl and time.time() - ts > self.cache_ttl:
            self._cache.pop(key, None)
            return None
        if not path.exists():
            self._cache.pop(key, None)
            return None
        return path

    async def render(self, template: str, payload: dict, *,
                     viewport: tuple[int, int] | None = None) -> RenderResult | None:
        builder = self._templates.get(template)
        if builder is None:
            log.error("模板不存在", extra={"fields": {"template": template}})
            return None

        key = self._cache_key(template, payload)
        cached = self._cache_get(key)
        if cached is not None:
            return RenderResult(cached, template, True, 0)

        started = time.perf_counter()
        try:
            html = builder(payload)
        except Exception:
            self.failures += 1
            log.error("模板构建失败", exc_info=True, extra={"fields": {"template": template}})
            return None

        try:
            path = await asyncio.wait_for(
                self._render_html(template, html, key, viewport),
                timeout=self.timeout_ms / 1000)
        except asyncio.TimeoutError:
            self.failures += 1
            log.error("渲染超时", extra={"fields": {"template": template,
                                                   "timeout_ms": self.timeout_ms}})
            return None
        except Exception:
            self.failures += 1
            log.error("渲染失败", exc_info=True, extra={"fields": {"template": template}})
            return None

        cost = int((time.perf_counter() - started) * 1000)
        self.renders += 1
        self._cache[key] = (path, time.time())
        if len(self._cache) > self.cache_max:
            for k in sorted(self._cache, key=lambda k: self._cache[k][1])[: self.cache_max // 2]:
                self._cache.pop(k, None)
        return RenderResult(path, template, False, cost)

    async def _render_html(self, template: str, html: str, key: str,
                           viewport: tuple[int, int] | None) -> Path:
        await self._ensure_browser()
        page = await self._pages.get()
        try:
            if viewport:
                await page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
            # set_content 后 Chromium 会拒绝 file:// 子资源，模板里的图片/字体必须内联
            await page.set_content(html, wait_until="domcontentloaded")
            await page.wait_for_timeout(120)          # 等字体与布局稳定
            out = self.out_dir / f"{key.replace(':', '_')}.png"
            selector = self._selectors.get(template)
            if selector:
                el = await page.query_selector(selector)
                if el is None:
                    raise RuntimeError(f"选择器未命中: {selector}")
                await el.screenshot(path=str(out), type="png")
            else:
                await page.screenshot(path=str(out), full_page=True, type="png")
            return out
        finally:
            self._pages.put_nowait(page)

    async def screenshot_html(self, html: str, *, width: int = 800,
                              out_path: str | Path | None = None,
                              html_file: str | Path | None = None) -> Path | None:
        """任意完整 HTML → 截图。给 `html_file` 时走 `goto(file://)`。

        为什么要 goto：`set_content()` 之后 Chromium 会拒绝 `file://` 子资源，
        卡片里引用本地图片（phi 的 card_assets）就会全空。调用方负责把 HTML
        写到磁盘、内容与 `html` 一致。
        """
        await self._ensure_browser()
        page = await self._pages.get()
        try:
            await page.set_viewport_size({"width": int(width), "height": 900})
            if html_file:
                await page.goto(Path(html_file).resolve().as_uri(),
                                wait_until="domcontentloaded")
            else:
                await page.set_content(html, wait_until="domcontentloaded")
            await page.wait_for_timeout(120)      # 等字体与本地资源就位
            if out_path is not None:
                out = Path(out_path)
            else:
                out = self.out_dir / f"raw_{int(time.time() * 1000)}.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(out), type="png", full_page=True)
            log.info("HTML 截图完成", extra={"fields": {
                "path": str(out), "bytes": out.stat().st_size,
                "mode": "goto" if html_file else "set_content"}})
            return out
        except Exception:
            log.error("HTML 截图失败", exc_info=True)
            return None
        finally:
            self._pages.put_nowait(page)

    def stats(self) -> dict:
        return {"template_count": len(self._templates), "renders": self.renders,
                "failures": self.failures, "cache": len(self._cache),
                "state": self.state}
