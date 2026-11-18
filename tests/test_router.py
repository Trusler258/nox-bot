"""多平台路由：按 reply_to 路由表 / target 形状选适配器，能力按平台隔离。"""
import asyncio

from kernel.adapters.router import RouterAdapter, build
from kernel.event import Capabilities, Message


class _Fake:
    def __init__(self, name, caps, routes=None):
        self.name = name
        self.capabilities = caps
        self._routes = routes or {}
        self.state = "ready"
        self.sent = []
        self.event_stats = {name: 1}

    async def start(self):
        self.state = "ready"

    async def stop(self):
        self.state = "stopped"

    async def send(self, target, message, *, reply_to=None):
        self.sent.append((target, reply_to))
        return {"ok": self.name}


def _pair():
    off = _Fake("qq_official",
                Capabilities(quote=False, at=False, image=True, markdown=True,
                             buttons=True, max_text_len=1500),
                routes={"ROBOT1.0_abc": ("group", "G_OPENID")})
    nc = _Fake("napcat",
               Capabilities(quote=True, at=True, image=True, markdown=False,
                            buttons=False, max_text_len=4000))
    return RouterAdapter({"qq_official": off, "napcat": nc}), off, nc


def test_reply_to_wins_over_target_shape():
    r, off, nc = _pair()
    asyncio.run(r.send("123456", Message.from_text("x"), reply_to="ROBOT1.0_abc"))
    assert len(off.sent) == 1 and not nc.sent


def test_numeric_target_goes_to_napcat():
    r, off, nc = _pair()
    asyncio.run(r.send("704503936", Message.from_text("x")))
    assert len(nc.sent) == 1 and not off.sent


def test_openid_target_goes_to_official():
    r, off, nc = _pair()
    asyncio.run(r.send("6F879C84D5478369F4F8FC799C394648", Message.from_text("x")))
    assert len(off.sent) == 1 and not nc.sent


def test_capabilities_are_per_platform():
    r, _off, _nc = _pair()
    assert r.capabilities_for("qq_official").markdown is True
    assert r.capabilities_for("napcat").markdown is False
    assert r.capabilities_for("unknown").markdown is True   # 退回第一个


def test_single_adapter_is_not_wrapped():
    off = _Fake("qq_official",
                Capabilities(quote=False, at=False, image=True, markdown=True,
                             buttons=True, max_text_len=1500))
    assert build({"qq_official": off}) is off


def test_state_aggregates():
    r, off, nc = _pair()
    assert r.state == "ready"
    nc.state = "error"
    assert r.state == "partial"
