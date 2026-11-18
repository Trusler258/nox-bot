"""NapCat 适配器（OneBot v11 正向 WebSocket）。

按 OneBot 11 文档实现：
- 连接 ws://host:port?access_token=xxx（兼容 Authorization 头）
- 上报：post_type=message / message_sent / meta_event / notice / request
- 调用：{"action": "...", "params": {...}, "echo": "id"}，按 echo 匹配响应
- 发送用消息段数组，不用 CQ 字符串

未联机验证（本地没有 NapCat 实例），映射逻辑全部做成纯函数并覆盖单测。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

from kernel.adapters.cq import parse_cq, to_onebot_message
from kernel.event import Capabilities, Event, Message, Segment
from kernel.obs.logger import get_logger
from kernel.text import normalize_text

log = get_logger("kernel.adapter.napcat")


def _sender_name(p: dict) -> str:
    """群名片优先（群友改过名片），其次昵称，最后 QQ 号。

    V2 的插件大量用 sender_name 拼文案，没有它会显示"None 掷出了 6 点"。
    """
    s = p.get("sender") or {}
    return str(s.get("card") or s.get("nickname") or p.get("user_id", ""))


class NapCatAdapter:
    name = "napcat"
    capabilities = Capabilities(quote=True, at=True, image=True,
                                markdown=False, buttons=False, max_text_len=4000)

    def __init__(self, ws_url: str, token: str = "", *,
                 on_event: Callable[[Event], Awaitable[Any]] | None = None,
                 reconnect_initial_ms: int = 1000,
                 reconnect_max_ms: int = 30000) -> None:
        self.ws_url = ws_url
        self.token = token
        self.on_event = on_event
        self.reconnect_initial = reconnect_initial_ms / 1000
        self.reconnect_max = reconnect_max_ms / 1000

        self._ws = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._seq = 0
        self._pending: dict[str, asyncio.Future] = {}
        self.self_id = 0
        self.state = "stopped"

    # ── 生命周期 ──────────────────────────────────────────
    async def start(self) -> None:
        self._running = True
        self.state = "connecting"
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self.state = "stopped"

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def _run_forever(self) -> None:
        import websockets

        backoff = self.reconnect_initial
        while self._running:
            try:
                url = self.ws_url
                headers = {}
                if self.token:
                    sep = "&" if "?" in url else "?"
                    url = f"{url}{sep}access_token={self.token}"
                    headers["Authorization"] = f"Bearer {self.token}"
                async with websockets.connect(url, max_size=2 ** 24,
                                              additional_headers=headers or None) as ws:
                    self._ws = ws
                    self.state = "connected"
                    backoff = self.reconnect_initial
                    log.info("已连接 NapCat", extra={"fields": {"url": self.ws_url}})
                    async for raw in ws:
                        await self._handle_raw(raw)
            except asyncio.CancelledError:
                break
            except Exception:
                self.state = "error"
                log.warning("NapCat 连接断开，%ss 后重连", backoff, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(self.reconnect_max, backoff * 2)

    # ── 收 ────────────────────────────────────────────────
    async def _handle_raw(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except ValueError:
            return
        if "echo" in payload and payload.get("echo") in self._pending:
            fut = self._pending.pop(payload["echo"])
            if not fut.done():
                fut.set_result(payload)
            return

        event = self.map_event(payload)
        if event is not None and self.on_event is not None:
            await self.on_event(event)

    def map_event(self, p: dict) -> Event | None:
        """OneBot 上报 → 内核 Event。纯函数，便于测试。"""
        if p.get("post_type") == "meta_event":
            if p.get("meta_event_type") == "lifecycle":
                self.self_id = int(p.get("self_id", 0) or 0)
                self.state = "ready"
            return None
        if p.get("post_type") != "message":
            return None

        is_group = p.get("message_type") == "group"
        raw = p.get("raw_message") or ""
        segments: list[Segment]
        if isinstance(p.get("message"), list):
            segments = [Segment(str(s.get("type")), dict(s.get("data") or {}))
                        for s in p["message"]]
        else:
            segments = parse_cq(raw)

        text = normalize_text("".join(s.data.get("text", "") for s in segments
                                      if s.type == "text"))
        if not text and any(s.type in ("image", "record", "file") for s in segments):
            text = ""                                # 纯媒体消息，交给插件自己判断

        mention = any(s.type == "at" and str(s.data.get("qq", "")) == str(self.self_id)
                      for s in segments)
        quote_id = next((s.data.get("id", "") for s in segments if s.type == "reply"), None)

        return Event(
            id=str(p.get("message_id", "")),
            platform=self.name,
            kind="message.group" if is_group else "message.private",
            group_id=str(p.get("group_id", "")) if is_group else None,
            user_id=str(p.get("user_id", "")),
            sender_name=_sender_name(p),
            message=Message(segments=segments, raw=p),
            mentions_me=mention,
            quote_id=quote_id or None,
            ts=float(p.get("time", time.time())),
        )

    # ── 发 ────────────────────────────────────────────────
    @staticmethod
    def _materialize(segments: list[Segment]) -> list[Segment]:
        """本地路径的图片/语音转成 OneBot 认识的 file:// URI。"""
        out = []
        for s in segments:
            if s.type in ("image", "record") and s.data.get("url"):
                out.append(Segment(s.type, {"file": str(s.data["url"])}))
            elif s.type in ("image", "record") and s.data.get("path"):
                path = str(s.data["path"]).replace("\\", "/")
                if not path.startswith("file://"):
                    path = "file:///" + path.lstrip("/")
                out.append(Segment(s.type, {"file": path}))
            else:
                out.append(s)
        return out

    async def call(self, action: str, params: dict, timeout: float = 15.0) -> dict:
        if self._ws is None:
            raise RuntimeError("NapCat 未连接")
        self._seq += 1
        echo = f"v3-{self._seq}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        await self._ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
        try:
            resp = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(echo, None)
        if resp.get("status") == "failed" or resp.get("retcode") not in (0, None):
            raise RuntimeError(f"{action} failed: {resp.get('retcode')} {resp.get('message')}")
        return resp

    async def send(self, target: str, message: Message, *,
                   reply_to: str | None = None) -> dict:
        segs = to_onebot_message(self._materialize(message.segments))
        if reply_to:
            segs.insert(0, {"type": "reply", "data": {"id": str(reply_to)}})
        if target.isdigit():
            return await self.call("send_group_msg", {"group_id": int(target),
                                                      "message": segs})
        return await self.call("send_private_msg", {"user_id": int(target),
                                                    "message": segs})
