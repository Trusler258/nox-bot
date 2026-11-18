"""QQ 官方机器人适配器（WebSocket 长连接）。

协议细节全部来自 2026-11-16 的实测（设计文档 §3.1）：
- 握手：op10 hello → op2 identify（token 形如 "QQBot <access_token>"，intents=1<<25）→ op0 READY
- 事件：GROUP_AT_MESSAGE_CREATE / C2C_MESSAGE_CREATE，**只有被 @ 才下发**
- content 前面可能带 0/1/2 个空格，统一走 normalize_text
- 被动回复必须带 msg_id 与 msg_seq（同一 msg_id 递增），否则接口拒绝
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from kernel.event import Capabilities, Event, Message, Segment
from kernel.media import DailyLimitExceeded, FileTooLarge, UploadFailed
from kernel.obs.logger import get_logger
from kernel.text import normalize_text

log = get_logger("kernel.adapter.qq_official")

# 二进制直传（file_data）上限 5MB，超过要走分片上传（官方文档明确）
_BINARY_UPLOAD_LIMIT = 5 * 1024 * 1024
# md5_10m 取文件前 10002432 字节（约 10MB），预上传时必填
_MD5_10M_SIZE = 10_002_432

# 官方富媒体 file_type：1=图片 2=视频 3=语音 4=文件
_FILE_TYPES = {"image": 1, "video": 2, "voice": 3, "audio": 3, "file": 4}


# 平台错误码 → 领域异常。40093002 等明天；850031 换文件。
_BIZ_DAILY_LIMIT = 40093002
_BIZ_TOO_LARGE = 850031


def _raise_media_error(status: int, body: str, what: str) -> None:
    """把平台返回翻成能区分处置方式的异常。"""
    low = body or ""
    if str(_BIZ_DAILY_LIMIT) in low:
        raise DailyLimitExceeded(f"{what}：平台每日文件容量已用完（40093002）")
    if str(_BIZ_TOO_LARGE) in low:
        raise FileTooLarge(f"{what}：文件超过平台单文件上限 200MB（850031）")
    raise UploadFailed(f"{what} {status}: {low[:300]}")


# markdown 源码削成纯文本用的正则（只在 markdown 发送失败、回退纯文本时用）
_MD_IMG = re.compile(r"!\[[^\]]*\]\(([^)\s]+)[^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)[^)]*\)")
_MD_TABLE_SEP = re.compile(r"^[ \t]*\|?[ \t:|-]{3,}\|?[ \t]*\n?", re.M)
_MD_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$", re.M)
_MD_LINE_MARK = re.compile(r"^[ \t]{0,3}(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)", re.M)
# 只去成对的强调标记：单个 `_` `*` 要留着（`bw_kills` 这类标识符会被毁掉）
_MD_EMPH = re.compile(r"\*\*|__|~~|`")


def _strip_md(text: str) -> str:
    """markdown 不可用时，把源码削成还能读的纯文本。

    只求不难看，不追求还原：图片留 URL，链接留「文字 URL」，表格去掉竖线，
    行首的 # > - 1. 去掉，成对的 ** __ ~~ ` 去掉。
    """
    if not text:
        return ""
    s = _MD_IMG.sub(lambda m: m.group(1), text)
    s = _MD_LINK.sub(lambda m: f"{m.group(1)} {m.group(2)}", s)
    s = _MD_TABLE_SEP.sub("", s)
    s = _MD_TABLE_ROW.sub(
        lambda m: "  ".join(x.strip() for x in m.group(1).split("|") if x.strip()), s)
    s = _MD_LINE_MARK.sub("", s)
    s = _MD_EMPH.sub("", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def slice_parts(raw: bytes, parts: list[dict], stride: int) -> list[tuple[int, bytes]]:
    """把文件切成平台要求的各分片，返回 [(part_index, chunk)]。

    ★ 平台返回的 `index` 从 **1 开始**（实测：6MB 文件给了 1 个 part，index=1），
      所以不能用 `index * block_size` 算偏移，只能按 index 升序顺序累加。
      最后一片的 block_size 是剩余字节数，也由顺序累加自然处理。
    """
    out: list[tuple[int, bytes]] = []
    off = 0
    for part in sorted(parts, key=lambda p: int(p.get("index") or 0)):
        idx = int(part.get("index") or 0)
        size = int(part.get("block_size") or 0) or stride
        chunk = raw[off: off + size]
        off += len(chunk)
        out.append((idx, chunk))
    return out


def _media_segment(message) -> tuple[str, dict] | None:
    """单段富媒体消息 → (类型, 数据)；纯文本或混合段返回 None。"""
    segs = getattr(message, "segments", None) or []
    if len(segs) != 1:
        return None
    seg = segs[0]
    if seg.type in _FILE_TYPES:
        return seg.type, seg.data
    return None

API = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
INTENTS = 1 << 25          # 群 @ 消息 + C2C


class QQOfficialAdapter:
    name = "qq_official"
    # 官方渠道：无 CQ 码、@ 判定靠事件类型（只有 @ 才下发）、支持 markdown
    # buttons=True：官方支持内嵌键盘，但要单独开「消息按钮」权限；
    # 没开时发送会整条失败，适配器里会自动去掉按钮重试（见 send 的降级）
    capabilities = Capabilities(quote=False, at=False, image=True,
                                markdown=True, buttons=True, max_text_len=1500)

    def __init__(self, app_id: str, app_secret: str, *,
                 on_event: Callable[[Event], Awaitable[Any]] | None = None,
                 sandbox: bool = False, heartbeat_margin_s: float = 5.0,
                 markdown: str = "auto") -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.on_event = on_event
        self.sandbox = sandbox
        self.heartbeat_margin = heartbeat_margin_s
        # 文本发送策略：auto（先 markdown，失败回退纯文本）/ on（强制 markdown）
        # / off（纯文本）。官方渠道的排版一律以 QQ markdown 为准，见 send()。
        self.markdown_policy = (markdown or "auto").lower()
        # 每个 scene（group/c2c）的 markdown 能力：unknown / on / off。
        # 官方要单独开「markdown 消息」权限，没开的群整条会 4xx；记住后
        # 不再每条都白跑一次失败请求。
        self._md_state: dict[str, str] = {}

        self._token = ""
        self._token_expire = 0.0
        self._ws = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._seq: int | None = None
        self._msg_seq: dict[str, int] = {}
        self._routes: dict[str, tuple[str, str]] = {}     # msg_id → (kind, openid)
        self.state = "stopped"
        self._bot_id = ""                 # READY 里的机器人 id，用于判定 mentions
        self._bot_name = ""
        # 收到的原始事件类型计数：排查"为什么没反应"时先看这里
        self.event_stats: dict[str, int] = {}
        self._seen_types: set[str] = set()

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
        backoff = 1.0
        while self._running:
            try:
                await self._connect_once()
                backoff = 1.0
            except asyncio.CancelledError:
                break
            except Exception:
                self.state = "error"
                log.error("连接断开，%ss 后重连", backoff, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)

    # ── HTTP ──────────────────────────────────────────────
    async def _get_token(self) -> str:
        import httpx

        if self._token and time.time() < self._token_expire - 60:
            return self._token
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.post(TOKEN_URL, json={"appId": self.app_id,
                                                "clientSecret": self.app_secret})
            r.raise_for_status()
            data = r.json()
        self._token = data["access_token"]
        self._token_expire = time.time() + float(data.get("expires_in", 7200))
        log.info("access_token 已刷新", extra={"fields": {
            "expire_in": int(self._token_expire - time.time())}})
        return self._token

    async def _get_gateway(self) -> str:
        import httpx

        token = await self._get_token()
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.get(f"{API}/gateway", headers={"Authorization": f"QQBot {token}"})
            r.raise_for_status()
            return r.json()["url"]

    # ── 连接 ──────────────────────────────────────────────
    async def _connect_once(self) -> None:
        import websockets

        token = await self._get_token()
        url = await self._get_gateway()
        async with websockets.connect(url, max_size=2 ** 22) as ws:
            self._ws = ws
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            interval = hello["d"]["heartbeat_interval"] / 1000
            log.info("网关已连接", extra={"fields": {"heartbeat_s": round(interval, 1)}})

            await ws.send(json.dumps({"op": 2, "d": {
                "token": f"QQBot {token}", "intents": INTENTS, "shard": [0, 1],
                "properties": {"$os": "linux", "$browser": "huanmeng-v3",
                               "$device": "server"}}}))

            hb = asyncio.create_task(self._heartbeat(ws, interval))
            try:
                async for raw in ws:
                    await self._handle_raw(raw)
            finally:
                hb.cancel()

    async def _heartbeat(self, ws, interval: float) -> None:
        while True:
            await asyncio.sleep(max(5.0, interval - self.heartbeat_margin))
            try:
                await ws.send(json.dumps({"op": 1, "d": self._seq}))
            except Exception:
                return

    # ── 事件映射 ──────────────────────────────────────────
    async def _handle_raw(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except ValueError:
            log.warning("收到非 JSON 报文")
            return
        op = payload.get("op")
        if payload.get("s") is not None:
            self._seq = payload["s"]
        if op == 0:
            t = str(payload.get("t", ""))
            self.event_stats[t] = self.event_stats.get(t, 0) + 1
            if t not in self._seen_types:
                # 每种事件第一次出现打一行：查"机器人没反应"时看这里最快
                self._seen_types.add(t)
                log.info("收到新类型事件", extra={"fields": {
                    "t": t, "keys": sorted((payload.get("d") or {}).keys())[:12]}})
            event = self.map_event(t, payload.get("d") or {})
            if event is not None and self.on_event is not None:
                import inspect
                res = self.on_event(event)
                if inspect.isawaitable(res):
                    await res
        elif op == 11:
            pass
        elif op in (7, 9):
            log.warning("网关要求重连或 intents 被拒", extra={"fields": {"op": op,
                                                                     "d": payload.get("d")}})
        elif op == 10:
            log.info("重复收到 hello")

    def _mentioned(self, d: dict, content: str) -> bool:
        """事件里是否 @ 了本机器人。

        ★ 正文里的 mention 是 `<@member_openid>`（32 位十六进制），**不是**
          机器人数字 id —— 拿 appid/机器人 id 去正文里找永远匹配不上。
          正解是看事件体的 `mentions: []User`：每项带 bot 标志和 id。
        """
        for m in (d.get("mentions") or []):
            if m.get("bot") is not True:
                continue
            mid = str(m.get("id") or "")
            # 有数字 id 就精确比对；拿不到才退回"任意被 @ 的机器人"
            if mid and self._bot_id:
                if mid == self._bot_id:
                    return True
            else:
                return True
        # 兜底：正文里直接写了 @名字（有些客户端发纯文本）
        if content and self._bot_name and f"@{self._bot_name}" in content:
            return True
        return False

    @staticmethod
    def _from_bot(author: dict) -> bool:
        """发送者是不是机器人。

        ★ 2026-11-18 线上：同一群里还有一个官方渠道机器人，它发的回复会以
          GROUP_MESSAGE_CREATE 下发到我们这边（日志里能看到别的 bot 的原文）。
          不过滤的话，两个机器人会互相当成用户——开了 LLM 就是无限对答。
          官方报文 author 里带 `bot` 字段（实测值 False/True）。
        """
        v = author.get("bot")
        return bool(v) and str(v).lower() not in ("0", "false", "none", "")

    def _next_seq(self, reply_to: str | None) -> int:
        """被动回复的 msg_seq：同一条 msg_id 内必须严格递增。"""
        key = reply_to or ""
        seq = self._msg_seq.get(key, 0) + 1
        self._msg_seq[key] = seq
        return seq

    def _md_allowed(self, scene: str) -> bool:
        """这个 scene 能不能发 markdown（policy 优先，其次是探测结果）。"""
        if self.markdown_policy == "off":
            return False
        if self.markdown_policy == "on":
            return True
        return self._md_state.get(scene, "unknown") != "off"

    def map_event(self, t: str, d: dict) -> Event | None:
        """把官方报文映射成内核 Event。纯函数，便于测试。"""
        if t == "READY":
            bot = d.get("user") or {}
            self._bot_id = str(bot.get("id") or "")
            self._bot_name = str(bot.get("username") or "")
            log.info("READY", extra={"fields": {"bot": self._bot_name,
                                                "id": self._bot_id}})
            self.state = "ready"
            return None

        # 全量消息（群主授权"查看群内全部对话"后，非 @ 消息也下发）
        if t == "GROUP_MESSAGE_CREATE":
            gid = d.get("group_openid", "")
            author = d.get("author") or {}
            uid = author.get("member_openid", "")
            mid = d.get("id", "")
            if self._from_bot(author):
                log.debug("忽略机器人消息", extra={"fields": {
                    "group": gid, "from": author.get("username", ""),
                    "text": normalize_text(d.get("content"))}})
                return None
            self._routes[mid] = ("group", gid)
            content = normalize_text(d.get("content"))
            return Event(
                id=mid, platform=self.name, kind="message.group",
                group_id=gid, user_id=uid,
                sender_name=author.get("username") or author.get("member_nick", ""),
                message=Message(segments=[Segment("text", {"text": content})]),
                mentions_me=self._mentioned(d, content),   # 看事件体的 mentions
                ts=time.time())

        # 拉群/踢群：至少留痕，并且这是唯一能拿到群 openid 的时机
        if t in ("GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT"):
            log.info("机器人进出群", extra={"fields": {
                "t": t, "group": d.get("group_openid", ""),
                "op_member": (d.get("op_member_openid") or "")}})
            return None
        if t == "GROUP_AT_MESSAGE_CREATE":
            gid = d.get("group_openid", "")
            author = d.get("author") or {}
            uid = author.get("member_openid", "")
            mid = d.get("id", "")
            if self._from_bot(author):
                log.debug("忽略机器人消息", extra={"fields": {
                    "group": gid, "from": author.get("username", ""),
                    "text": normalize_text(d.get("content"))}})
                return None
            self._routes[mid] = ("group", gid)
            return Event(
                id=mid, platform=self.name, kind="message.group",
                group_id=gid, user_id=uid,
                sender_name=author.get("username", ""),
                message=Message(segments=[Segment("text", {"text": normalize_text(d.get("content"))})]),
                mentions_me=True,                     # 只有被 @ 才下发
                ts=time.time())
        if t == "C2C_MESSAGE_CREATE":
            author = d.get("author") or {}
            if self._from_bot(author):
                log.debug("忽略机器人私聊", extra={"fields": {
                    "from": author.get("username", "")}})
                return None
            uid = author.get("user_openid", "")
            mid = d.get("id", "")
            self._routes[mid] = ("c2c", uid)
            return Event(
                id=mid, platform=self.name, kind="message.private",
                user_id=uid,
                sender_name=author.get("username", ""),
                message=Message(segments=[Segment("text", {"text": normalize_text(d.get("content"))})]),
                mentions_me=True, ts=time.time())
        return None

    # ── 发送 ──────────────────────────────────────────────
    async def _download_image(self, url: str) -> str:
        """网图落盘。临时文件随系统清理，命名带 qqimg_ 前缀便于排查。"""
        import os
        import tempfile
        from pathlib import Path

        import httpx

        suffix = Path(url.split("?")[0]).suffix or ".png"
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="qqimg_")
        os.close(fd)
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as cli:
                r = await cli.get(url)
                r.raise_for_status()
                Path(path).write_bytes(r.content)
        except Exception:
            Path(path).unlink(missing_ok=True)
            raise
        return path

    async def _upload_chunked(self, token: str, openid: str, kind: str,
                              raw: bytes, file_type: int,
                              file_name: str) -> str:
        """分片上传（官方推荐流程，>5MB 必用）。

        upload_prepare → 逐片 PUT 预签名 URL → upload_part_finish
        → 带 upload_id 调 files 合并，拿 file_info。

        ★ 预签名 URL 是 COS 的，PUT 时**不能**带 Authorization，
          带了会被 COS 拒绝（签名不匹配）。
        """
        import hashlib

        import httpx

        base = (f"{API}/v2/groups/{openid}" if kind == "group"
                else f"{API}/v2/users/{openid}")
        head = {"Authorization": f"QQBot {token}"}
        md5 = hashlib.md5(raw).hexdigest()

        async with httpx.AsyncClient(timeout=60) as cli:
            r = await cli.post(base + "/upload_prepare", headers=head, json={
                "file_type": int(file_type),
                "file_size": str(len(raw)),
                "file_name": file_name,
                "md5": md5,
                "sha1": hashlib.sha1(raw).hexdigest(),
                "md5_10m": hashlib.md5(raw[:_MD5_10M_SIZE]).hexdigest(),
            })
            if r.status_code >= 400:
                _raise_media_error(r.status_code, r.text, "预上传失败")
            prep = r.json()

        upload_id = str(prep.get("upload_id") or "")
        parts = prep.get("parts") or []
        if not upload_id or not parts:
            raise RuntimeError(f"upload_prepare 返回不完整: {str(prep)[:300]}")
        # ★ parts 的 index **从 1 开始**（不是 0），且最后一片的 block_size
        #   是剩余字节数。用 index 乘步长算偏移会把整段读错，所以按 index
        #   排序后顺序累加偏移，跟平台的编号规则解耦。
        stride = int(prep.get("block_size") or parts[0].get("block_size") or 0)

        # ★ presigned_url 是**每片一个**，必须按 index 取回。
        #   2026-11-18 线上：这里原本引用了循环外的 `part`（不存在），
        #   NameError 让所有 >5MB 的文件直接进死信（用户只看到文字，没有附件）。
        by_index = {int(p.get("index") or 0): p for p in parts}

        async with httpx.AsyncClient(timeout=300) as cos:
            for idx, chunk in slice_parts(raw, parts, stride):
                if not chunk:
                    continue
                part = by_index.get(idx) or {}
                presigned = str(part.get("presigned_url") or "")
                if not presigned:
                    raise RuntimeError(
                        f"分片 {idx} 没有 presigned_url: {str(part)[:200]}")
                put = await cos.put(presigned, content=chunk)
                if put.status_code >= 400:
                    raise RuntimeError(f"分片 {idx} PUT 失败 {put.status_code}: "
                                       f"{put.text[:200]}")
                for attempt in range(3):
                    fin = await cos.post(base + "/upload_part_finish", headers=head,
                                         json={"upload_id": upload_id,
                                               "part_index": idx,
                                               "block_size": str(len(chunk)),
                                               "md5": hashlib.md5(chunk).hexdigest()})
                    if fin.status_code < 400:
                        break
                    # 40093001 是"BDH 通道异常"，可重试
                    if attempt == 2:
                        raise RuntimeError(f"分片 {idx} 确认失败 {fin.status_code}: "
                                           f"{fin.text[:200]}")
                    await asyncio.sleep(1.0 + attempt)

        async with httpx.AsyncClient(timeout=120) as cli:
            r = await cli.post(base + "/files", headers=head, json={
                "file_type": int(file_type),
                "file_name": file_name,
                "upload_id": upload_id,
                "srv_send_msg": False,
            })
        if r.status_code >= 400:
            _raise_media_error(r.status_code, r.text, "合并分片失败")
        body = r.json()
        info = body.get("file_info") or (body.get("data") or {}).get("file_info")
        if not info:
            raise RuntimeError(f"合并返回无 file_info: {str(body)[:300]}")
        log.info("分片上传完成", extra={"fields": {
            "name": file_name, "bytes": len(raw), "parts": len(parts)}})
        return info

    async def _upload_image(self, token: str, openid: str, path: str,
                            kind: str) -> str:
        return await self._upload_media(token, openid, path, kind, 1)

    async def _upload_media(self, token: str, openid: str, path: str,
                            kind: str, file_type: int = 1,
                            file_name: str = "") -> str:
        """上传本地图片，返回 file_info。

        ★ 官方接口要的是 **JSON + base64**（file_data），不是 multipart：
          传 multipart 时服务端收不到 file_data，会转去按 url 下载并报
          40093007「富媒体文件下载失败」。
        """
        import base64

        import httpx

        url = (f"{API}/v2/groups/{openid}/files" if kind == "group"
               else f"{API}/v2/users/{openid}/files")
        with open(path, "rb") as f:
            raw = f.read()
        # 超过 5MB 的二进制直传会失败（850031），走分片上传
        if len(raw) > _BINARY_UPLOAD_LIMIT:
            return await self._upload_chunked(token, openid, kind, raw,
                                              file_type, file_name or Path(path).name)

        # ★ file_name 在"二进制直传"时是**必填**（文档原文）。
        #   不传的话平台侧文件没有名字，客户端收不到可展示的附件。
        name = file_name or Path(path).name
        payload = {
            # 1=图片 2=视频 3=语音 4=文件（mp3 之类的音频走 4，不是 3）
            "file_type": int(file_type),
            "file_name": name,
            "file_data": base64.b64encode(raw).decode("ascii"),
            "srv_send_msg": False,                           # 只取 file_info，随后被动回复
        }
        async with httpx.AsyncClient(timeout=60) as cli:
            r = await cli.post(url, json=payload,
                               headers={"Authorization": f"QQBot {token}"})
        if r.status_code >= 400:
            _raise_media_error(r.status_code, r.text, "上传失败")
        body = r.json()
        info = body.get("file_info") or (body.get("data") or {}).get("file_info")
        if not info:
            raise RuntimeError(f"upload 返回无 file_info: {str(body)[:300]}")
        return info

    async def send(self, target: str, message: Message, *,
                   reply_to: str | None = None) -> dict:
        import httpx

        token = await self._get_token()

        # ★ markdown 富文本（msg_type=2）：图用 ![](url) 内嵌，按钮挂同一条消息。
        #   QQ 的富媒体消息（msg_type=7）不渲染 keyboard —— 图和按钮要落在一起，
        #   只能走这条路（V2 的做法，见 qq_official_bot._msg_body）。
        if message.has("markdown"):
            content = message.markdown
            kind, openid = self._routes.get(reply_to or "", ("group", target))
            seq = self._msg_seq.get(reply_to or "", 0) + 1
            self._msg_seq[reply_to or ""] = seq
            url = (f"{API}/v2/groups/{openid}/messages" if kind == "group"
                   else f"{API}/v2/users/{openid}/messages")

            def _body(with_kb: bool) -> dict:
                b: dict[str, Any] = {"content": "", "msg_type": 2,
                                     "markdown": {"content": content},
                                     "msg_seq": seq}
                if with_kb and message.keyboard:
                    b["keyboard"] = message.keyboard
                if reply_to:
                    b["msg_id"] = reply_to
                return b

            async with httpx.AsyncClient(timeout=20) as cli:
                r = await cli.post(url, json=_body(True),
                                   headers={"Authorization": f"QQBot {token}"})
                # 按钮权限没开时带 keyboard 会整条失败（V2 同款退化）：
                # 去掉按钮重试一次，再不行就当 markdown 失败。
                if r.status_code >= 400 and message.keyboard:
                    log.warning("markdown+按钮发送失败，去按钮重试",
                                extra={"fields": {"status": r.status_code,
                                                  "body": r.text[:200]}})
                    r = await cli.post(url, json=_body(False),
                                       headers={"Authorization": f"QQBot {token}"})
                if r.status_code >= 400:
                    log.warning("markdown 发送失败，回退纯文本",
                                extra={"fields": {"status": r.status_code,
                                                  "body": r.text[:200]}})
                    self._md_state[kind] = "off"
                    fallback = _strip_md(content)
                    b2 = {"content": fallback, "msg_type": 0, "msg_seq": seq}
                    if reply_to:
                        b2["msg_id"] = reply_to
                    r = await cli.post(url, json=b2,
                                       headers={"Authorization": f"QQBot {token}"})
                    if r.status_code >= 400:
                        raise RuntimeError(f"send md failed {r.status_code}: {r.text[:200]}")
                    return r.json()
                self._md_state[kind] = "on"
                return r.json()

        # ★ 图文卡片（msg_type=8）：官方 2026-10-28 新增，**仅群聊**。
        #   单聊的 msg_type 只有 0/2/6/7，发 8 会失败，所以这里拍平成文本。
        if message.card:
            kind, openid = self._routes.get(reply_to or "", ("group", target))
            seq = self._msg_seq.get(reply_to or "", 0) + 1
            self._msg_seq[reply_to or ""] = seq

            if kind == "group":
                url = f"{API}/v2/groups/{openid}/messages"
                body: dict[str, Any] = {"msg_type": 8, "card": message.card,
                                        "msg_seq": seq}
            else:
                c = message.card.get("content") or {}
                url = f"{API}/v2/users/{openid}/messages"
                text = "\n".join(x for x in (c.get("title", ""),
                                              c.get("description", ""),
                                              c.get("url", "")) if x)
                body = {"content": text, "msg_type": 0, "msg_seq": seq}
            if reply_to:
                body["msg_id"] = reply_to

            async with httpx.AsyncClient(timeout=20) as cli:
                r = await cli.post(url, json=body,
                                   headers={"Authorization": f"QQBot {token}"})
            if r.status_code >= 400:
                log.warning("图文卡片发送失败，回退纯文本",
                            extra={"fields": {"status": r.status_code,
                                              "body": r.text[:200]}})
                c = message.card.get("content") or {}
                text = "\n".join(x for x in (c.get("title", ""),
                                              c.get("description", ""),
                                              c.get("url", "")) if x)
                b2 = {"content": text[: self.capabilities.max_text_len],
                      "msg_type": 0, "msg_seq": seq}
                if reply_to:
                    b2["msg_id"] = reply_to
                async with httpx.AsyncClient(timeout=20) as cli:
                    r = await cli.post(url, json=b2,
                                       headers={"Authorization": f"QQBot {token}"})
                if r.status_code >= 400:
                    raise RuntimeError(f"send card failed {r.status_code}: {r.text[:200]}")
                return r.json()
            return r.json()

        # ★ 富媒体消息（图/视频/语音/文件）统一走 msg_type=7。
        #   mp3 这类文件用 file_type=4，用 1 会被平台按图片处理而失败。
        media = _media_segment(message)
        if media is not None:
            seg_type, seg_data = media
            path = seg_data.get("path") or seg_data.get("file")
            if not path and seg_data.get("url"):
                # 官方渠道只能上传本地文件，网图先落盘
                path = await self._download_image(str(seg_data["url"]))
            if not path:
                raise RuntimeError(f"{seg_type} 消息缺少 path/url")
            kind, openid = self._routes.get(reply_to or "", ("group", target))
            info = await self._upload_media(token, openid, path, kind,
                                            _FILE_TYPES.get(seg_type, 4))
            seq = self._msg_seq.get(reply_to or "", 0) + 1
            self._msg_seq[reply_to or ""] = seq
            body = {"msg_type": 7, "media": {"file_info": info}, "msg_seq": seq}
            if reply_to:
                body["msg_id"] = reply_to
            url = (f"{API}/v2/groups/{openid}/messages" if kind == "group"
                   else f"{API}/v2/users/{openid}/messages")
            async with httpx.AsyncClient(timeout=20) as cli:
                r = await cli.post(url, json=body,
                                   headers={"Authorization": f"QQBot {token}"})
            if r.status_code >= 400:
                raise RuntimeError(f"send image failed {r.status_code}: {r.text[:200]}")
            return r.json()

        text = message.text
        if len(text) > self.capabilities.max_text_len:
            text = text[: self.capabilities.max_text_len - 1] + "…"

        kind, openid = self._routes.get(reply_to or "", ("group", target))
        url = (f"{API}/v2/groups/{openid}/messages" if kind == "group"
               else f"{API}/v2/users/{openid}/messages")

        # ★ 2026-11-18 定调：官方渠道的文本**默认走 QQ markdown**（msg_type=2）。
        #   只有 markdown 才会把链接画成可点样式，加粗/列表/代码块也才有意义；
        #   纯文本（msg_type=0）只能靠客户端"自动识别链接"，排版能力为零。
        #   没开「markdown 消息」权限的群/私聊会整条 4xx → 记住该 scene 失败，
        #   之后直接走纯文本，不再每条白跑一次失败请求（V2 的 _MD_STATE 同款）。
        if self._md_allowed(kind):
            md_body: dict[str, Any] = {
                "content": "",
                "msg_type": 2,
                "markdown": {"content": text},
                "msg_seq": self._next_seq(reply_to),
            }
            if reply_to:
                md_body["msg_id"] = reply_to
            async with httpx.AsyncClient(timeout=20) as cli:
                r = await cli.post(url, json=md_body,
                                   headers={"Authorization": f"QQBot {token}"})
            if r.status_code < 400:
                self._md_state[kind] = "on"
                return r.json()
            self._md_state[kind] = "off"
            log.warning("markdown 不可用，回退纯文本", extra={"fields": {
                "scene": kind, "status": r.status_code, "body": r.text[:200]}})
            # 这条是按 markdown 写的，纯文本下得把 ** ` 之类的标记削掉
            text = _strip_md(text)

        body: dict[str, Any] = {"content": text, "msg_type": 0,
                                "msg_seq": self._next_seq(reply_to)}
        if reply_to:
            body["msg_id"] = reply_to          # 被动回复必须带

        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.post(url, json=body,
                               headers={"Authorization": f"QQBot {token}"})
        if r.status_code >= 400:
            raise RuntimeError(f"send failed {r.status_code}: {r.text[:200]}")
        return r.json()
