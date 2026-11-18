"""渲染服务：模板注册、缓存、失败降级（不启动真实浏览器）。"""
import asyncio
from pathlib import Path

from kernel.render.service import RenderService


def svc(tmp_path, **kw):
    return RenderService(out_dir=tmp_path, **kw)


def test_register_and_missing_template(tmp_path):
    s = svc(tmp_path)
    s.register("hello", lambda p: f"<h1>{p['name']}</h1>")
    assert s.templates() == ["hello"]
    assert asyncio.run(s.render("nope", {})) is None


def test_builder_error_returns_none(tmp_path):
    s = svc(tmp_path)

    def boom(payload):
        raise ValueError("bad payload")

    s.register("boom", boom)
    assert asyncio.run(s.render("boom", {})) is None
    assert s.failures == 1


def test_cache_hit_skips_render(tmp_path, monkeypatch):
    s = svc(tmp_path, cache_ttl_s=600)
    s.register("hello", lambda p: f"<h1>{p['n']}</h1>")
    calls = {"n": 0}

    async def fake(template, html, key, viewport):
        calls["n"] += 1
        p = tmp_path / f"{key.replace(':', '_')}.png"
        p.write_bytes(b"PNG")
        return p

    monkeypatch.setattr(s, "_render_html", fake)

    r1 = asyncio.run(s.render("hello", {"n": 1}))
    r2 = asyncio.run(s.render("hello", {"n": 1}))
    r3 = asyncio.run(s.render("hello", {"n": 2}))

    assert r1 and r1.cached is False
    assert r2 and r2.cached is True          # 同参数命中缓存
    assert r3 and r3.cached is False         # 参数变了重新渲染
    assert calls["n"] == 2


def test_cache_expiry(tmp_path, monkeypatch):
    s = svc(tmp_path, cache_ttl_s=0)          # 0 = 不缓存
    s.register("hello", lambda p: "<h1>x</h1>")
    calls = {"n": 0}

    async def fake(template, html, key, viewport):
        calls["n"] += 1
        p = tmp_path / f"{key.replace(':', '_')}.png"
        p.write_bytes(b"PNG")
        return p

    monkeypatch.setattr(s, "_render_html", fake)
    asyncio.run(s.render("hello", {}))
    asyncio.run(s.render("hello", {}))
    assert calls["n"] == 2


def test_render_timeout_returns_none(tmp_path, monkeypatch):
    s = svc(tmp_path, timeout_ms=50)
    s.register("slow", lambda p: "<h1>x</h1>")

    async def slow(*a, **kw):
        await asyncio.sleep(1)
        return Path("/nope.png")

    monkeypatch.setattr(s, "_render_html", slow)
    assert asyncio.run(s.render("slow", {})) is None
    assert s.failures == 1
