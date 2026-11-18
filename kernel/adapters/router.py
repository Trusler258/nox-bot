"""适配器路由：一个进程同时挂多个平台（官方渠道 + NapCat）。

现状：内核原本只能挂一个适配器（`[adapters] enabled` 只取第一个）。要把
NapCat 和官方渠道并到一个进程里，多出来的问题只有两个：

1. **发给谁**：`send(target, message)` 里没有平台信息。这里按两条线索判断——
   先看 `reply_to` 落在哪个适配器的路由表里（被动回复必然命中），
   再看 target 的形状（QQ 号/群号是纯数字，官方 openid 是 32 位十六进制）。
2. **能不能发 markdown**：两个平台的能力矩阵不一样（NapCat 没有 markdown /
   没有按钮）。所以 `capabilities` 必须是**按平台**取的，不能取并集，
   否则 wdsj 会把 markdown 内嵌图发给 NapCat。插件用 `ctx.capabilities`
   或 `capabilities_for(platform)` 取。
"""
from __future__ import annotations

from typing import Any

from kernel.event import Capabilities, Message
from kernel.obs.logger import get_logger

log = get_logger("kernel.adapter.router")

# 最保守的能力矩阵：不知道平台时按这个来（纯文本 + 图片）
_FALLBACK_CAPS = Capabilities(quote=False, at=False, image=True,
                              markdown=False, buttons=False, max_text_len=1500)


class RouterAdapter:
    """多适配器门面。对外签名与单适配器一致（start/stop/send/state）。"""

    name = "router"

    def __init__(self, adapters: dict[str, Any]) -> None:
        self.adapters = dict(adapters)
        # 与 MockAdapter 一致用列表：`--demo` 自检会遍历它打印发出去的报文
        self.sent: list[dict] = []

    # ── 生命周期 ──────────────────────────────────────────
    async def start(self) -> None:
        for name, a in self.adapters.items():
            try:
                await a.start()
                log.info("适配器已启动", extra={"fields": {"adapter": name}})
            except Exception:
                # 一个平台连不上不能拖着另一个一起不工作
                log.error("适配器启动失败", exc_info=True,
                          extra={"fields": {"adapter": name}})

    async def stop(self) -> None:
        for name, a in self.adapters.items():
            try:
                await a.stop()
            except Exception:
                log.warning("适配器停止失败", exc_info=True,
                            extra={"fields": {"adapter": name}})

    async def restart(self) -> None:
        for a in self.adapters.values():
            if hasattr(a, "restart"):
                await a.restart()

    # ── 能力 ──────────────────────────────────────────────
    @property
    def capabilities(self) -> Capabilities:
        """默认能力：取第一个适配器的。需要精确判定请用 capabilities_for。"""
        for a in self.adapters.values():
            return getattr(a, "capabilities", _FALLBACK_CAPS)
        return _FALLBACK_CAPS

    def capabilities_for(self, platform: str | None) -> Capabilities:
        a = self.adapters.get(str(platform or ""))
        return getattr(a, "capabilities", _FALLBACK_CAPS) if a else self.capabilities

    # ── 路由 ──────────────────────────────────────────────
    def adapter_for(self, target: str, reply_to: str | None = None) -> Any:
        """按 reply_to 的路由表判断；查不到再看 target 的形状。"""
        if reply_to:
            for a in self.adapters.values():
                routes = getattr(a, "_routes", None)
                if isinstance(routes, dict) and reply_to in routes:
                    return a
        t = str(target or "")
        # 官方 openid：32 位十六进制；NapCat 的 QQ 号/群号是纯数字
        if t.isdigit():
            for name in ("napcat",):
                if name in self.adapters:
                    return self.adapters[name]
        else:
            for name in ("qq_official",):
                if name in self.adapters:
                    return self.adapters[name]
        return next(iter(self.adapters.values()))

    def platform_of(self, target: str, reply_to: str | None = None) -> str:
        return str(getattr(self.adapter_for(target, reply_to), "name", ""))

    async def send(self, target: str, message: Message, *,
                   reply_to: str | None = None,
                   platform: str | None = None) -> dict:
        if platform and platform in self.adapters:
            a = self.adapters[platform]
        else:
            a = self.adapter_for(target, reply_to)
        result = await a.send(target, message, reply_to=reply_to)
        self.sent.append({"platform": str(getattr(a, "name", "")),
                          "target": target, "reply_to": reply_to})
        return result

    # ── 观测 ──────────────────────────────────────────────
    @property
    def state(self) -> str:
        states = [str(getattr(a, "state", "?")) for a in self.adapters.values()]
        if not states:
            return "stopped"
        if all(s == "ready" for s in states):
            return "ready"
        if any(s == "ready" for s in states):
            return "partial"
        return states[0]

    @property
    def event_stats(self) -> dict:
        out: dict = {}
        for name, a in self.adapters.items():
            stats = getattr(a, "event_stats", None)
            if isinstance(stats, dict):
                for k, v in stats.items():
                    out[f"{name}.{k}"] = v
        return out

    @property
    def app_id(self) -> str:
        """官方渠道的 AppID（NapCat 没有）。换 AppID 要清旧 openid 时用。"""
        for a in self.adapters.values():
            v = str(getattr(a, "app_id", "") or "")
            if v:
                return v
        return ""


def build(adapters: dict[str, Any]) -> Any:
    """单适配器就直接返回它（不套一层），保持既有行为与日志。"""
    if len(adapters) == 1:
        return next(iter(adapters.values()))
    return RouterAdapter(adapters)
