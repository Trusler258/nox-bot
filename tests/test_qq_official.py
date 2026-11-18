"""QQ 官方适配器的报文映射（纯函数，不联网）。

依据 2026-11-16 实测报文（设计文档 §3.1）。
"""
from kernel.adapters.qq_official import QQOfficialAdapter

ADAPTER = QQOfficialAdapter("appid", "secret")


def group_payload(content="  111", mid="ROBOT1.0_abc"):
    return {
        "id": mid,
        "content": content,
        "group_openid": "G_OPENID",
        "group_id": "G_ID",
        "message_type": 0,
        "timestamp": "2026-11-16T08:55:15+08:00",
        "author": {"member_openid": "U_OPENID", "username": "Trusler",
                   "member_role": "owner", "bot": False},
    }


def test_group_at_message_maps():
    e = ADAPTER.map_event("GROUP_AT_MESSAGE_CREATE", group_payload())
    assert e.kind == "message.group"
    assert e.group_id == "G_OPENID"
    assert e.user_id == "U_OPENID"
    assert e.mentions_me is True          # 官方只有被 @ 才下发
    assert e.text == "111"                # 前导空格已剥掉


def test_leading_space_variants():
    for raw, expect in [("111", "111"), (" 111", "111"), ("  111", "111"),
                        ("\u3000111", "111"), ("\u200b 111", "111")]:
        e = ADAPTER.map_event("GROUP_AT_MESSAGE_CREATE", group_payload(content=raw))
        assert e.text == expect, (raw, e.text)


def test_c2c_maps_private():
    e = ADAPTER.map_event("C2C_MESSAGE_CREATE", {
        "id": "R2", "content": "hi",
        "author": {"user_openid": "U2"}})
    assert e.kind == "message.private" and e.user_id == "U2"


def test_ready_updates_state():
    assert ADAPTER.map_event("READY", {"user": {"username": "test1", "id": "1"}}) is None
    assert ADAPTER.state == "ready"


def test_unknown_event_ignored():
    # 全量消息事件现在要处理（群主授权后 @ 也可能走它），这里换一个真未知的
    assert ADAPTER.map_event("GROUP_ADD_ROBOT", {}) is None
    assert ADAPTER.map_event("SOMETHING_ELSE", {"x": 1}) is None


def test_capabilities_reflect_official_limits():
    caps = QQOfficialAdapter.capabilities
    assert caps.quote is False and caps.markdown is True
    assert caps.max_text_len == 1500


def test_group_message_create_is_mapped_with_mentions():
    """全量消息事件也要入库：群主授权"查看全部对话"后，@ 也走这个类型。

    QQ 在开了全量消息权限的群里，@ 消息可能只下发 GROUP_MESSAGE_CREATE，
    不认这个类型就等于机器人整个失聪。
    """
    from kernel.adapters.qq_official import QQOfficialAdapter

    a = QQOfficialAdapter("appid", "secret")
    a._bot_id = "2710357990829407179"
    a._bot_name = "test1"

    # ★ mention 在正文里是 `<@member_openid>`，不是数字 id；
    #   判定只能靠事件体的 mentions（bot=true 且 id 等于机器人 id）
    at = {"t": "GROUP_MESSAGE_CREATE", "d": {
        "id": "m1", "group_openid": "G1",
        "author": {"member_openid": "U1", "username": "某人"},
        "content": "<@0D61C142DE3D19DFA813B38DE9F322D2> /~whoami",
        "mentions": [{"member_openid": "0D61C142DE3D19DFA813B38DE9F322D2",
                      "id": "2710357990829407179", "bot": True}]}}
    ev = a.map_event(at["t"], at["d"])
    assert ev is not None and ev.kind == "message.group"
    assert ev.group_id == "G1" and ev.user_id == "U1"
    assert ev.mentions_me is True

    plain = {"t": "GROUP_MESSAGE_CREATE", "d": {
        "id": "m2", "group_openid": "G1",
        "author": {"member_openid": "U2"}, "content": "今天好热",
        "mentions": []}}
    ev2 = a.map_event(plain["t"], plain["d"])
    assert ev2 is not None and ev2.mentions_me is False


def test_mention_of_other_bot_is_not_me():
    """@ 的是别的机器人（bot=true 但 id 不同）不能算 @ 我。"""
    from kernel.adapters.qq_official import QQOfficialAdapter

    a = QQOfficialAdapter("appid", "secret")
    a._bot_id = "2710357990829407179"
    d = {"id": "m1", "group_openid": "G1",
         "author": {"member_openid": "U1"},
         "content": "<@0D61C142DE3D19DFA813B38DE9F322D2> /~whoami",
         "mentions": [{"member_openid": "0D61C142DE3D19DFA813B38DE9F322D2",
                       "id": "3403560730654943348", "bot": True}]}
    ev = a.map_event("GROUP_MESSAGE_CREATE", d)
    assert ev is not None and ev.mentions_me is False


def test_unknown_event_is_counted_not_crash():
    from kernel.adapters.qq_official import QQOfficialAdapter

    a = QQOfficialAdapter("appid", "secret")
    assert a.map_event("SOMETHING_NEW", {"x": 1}) is None


def test_card_message_clips_to_platform_limits():
    """平台硬约束：标题 ≤50、描述 ≤200，否则整条发送失败。"""
    from kernel.event import Message

    m = Message.from_card(title="歌" * 80, description="描述" * 300,
                          url="https://music.163.com/song?id=1",
                          pic_url="https://p1.music.126.net/a.jpg")
    assert m.has_card is True
    c = m.card["content"]
    assert len(c["title"]) == 50
    assert len(c["description"]) == 200
    assert c["url"].startswith("https://")
    assert m.card["type"] == "tuwen"


def test_card_is_not_image_or_text():
    """卡片不该被当成富媒体或文本发出去（否则会走错分支）。"""
    from kernel.event import Message

    m = Message.from_card(title="t", description="d",
                          url="https://a", pic_url="https://b")
    assert m.only_image is False
    assert m.text == ""


def test_slice_parts_handles_one_based_index():
    """平台的分片 index 从 1 开始，偏移必须按顺序累加而不是 index*步长。"""
    from kernel.adapters.qq_official import slice_parts

    raw = b"A" * 10 + b"B" * 10 + b"C" * 5
    parts = [
        {"index": 1, "block_size": "10"},
        {"index": 2, "block_size": "10"},
        {"index": 3, "block_size": "5"},
    ]
    got = slice_parts(raw, parts, 10)
    assert [i for i, _ in got] == [1, 2, 3]
    assert b"".join(c for _, c in got) == raw
    assert got[0][1] == b"A" * 10
    assert got[2][1] == b"C" * 5


def test_bot_message_ignored():
    """别的机器人发的消息不下发成事件（否则两个 bot 会互相对答）。"""
    p = group_payload(content="未知指令 /whoami，使用 /~help 查看可用指令喵~")
    p["author"]["bot"] = True
    assert ADAPTER.map_event("GROUP_MESSAGE_CREATE", p) is None
    p2 = group_payload(content="/whoami")
    p2["author"]["bot"] = True
    assert ADAPTER.map_event("GROUP_AT_MESSAGE_CREATE", p2) is None
    # 真人照旧
    assert ADAPTER.map_event("GROUP_MESSAGE_CREATE", group_payload(content="hi")) is not None


class _Resp:
    def __init__(self, status: int = 200, payload: dict | None = None,
                 text: str = "") -> None:
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


class _FakeHTTP:
    """假 httpx.AsyncClient：按 (method, url) 查表，并把请求记进 calls。"""

    def __init__(self, routes: dict, calls: list) -> None:
        self.routes = routes
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, json))
        val = self.routes.get(("POST", url), _Resp(500, text="no route"))
        # 同一个 URL 允许排队多个响应：验证"先失败后回退"的流程
        if isinstance(val, list):
            return val.pop(0) if val else _Resp(500, text="exhausted")
        return val

    async def put(self, url, content=None, headers=None):
        self.calls.append(("PUT", url, {"size": len(content or b""),
                                        "headers": headers or {}}))
        return self.routes.get(("PUT", url), _Resp(500, text="no route"))


def test_upload_chunked_puts_each_part_to_its_own_url(monkeypatch):
    """分片上传：每片 PUT 到自己的 presigned_url，再合并拿 file_info。

    ★ 2026-11-18 线上 NameError（`part` 未定义）就是这个函数，之前没有测试
      覆盖到，9MB 的文件全部进死信。
    """
    import asyncio

    import httpx

    import kernel.adapters.qq_official as m

    raw = b"A" * 10 + b"B" * 10
    base = f"{m.API}/v2/groups/G"
    calls: list = []
    routes = {
        ("POST", base + "/upload_prepare"): _Resp(200, {
            "upload_id": "UP1", "block_size": "10",
            "parts": [
                {"index": 1, "block_size": "10", "presigned_url": "https://cos.example/1"},
                {"index": 2, "block_size": "10", "presigned_url": "https://cos.example/2"},
            ]}),
        ("PUT", "https://cos.example/1"): _Resp(200),
        ("PUT", "https://cos.example/2"): _Resp(200),
        ("POST", base + "/upload_part_finish"): _Resp(200, {"code": 0}),
        ("POST", base + "/files"): _Resp(200, {"file_info": "FI_9MB"}),
    }
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeHTTP(routes, calls))

    info = asyncio.run(m.QQOfficialAdapter._upload_chunked(
        ADAPTER, "tok", "G", "group", raw, 4, "a.mp3"))
    assert info == "FI_9MB"

    puts = [(u, c["size"]) for k, u, c in calls if k == "PUT"]
    assert puts == [("https://cos.example/1", 10), ("https://cos.example/2", 10)]
    # PUT 预签名 URL 时不能带 Authorization（带了 COS 会拒签名）
    assert all("Authorization" not in c["headers"]
               for k, _, c in calls if k == "PUT")
    finish = [c[2] for c in calls
              if c[0] == "POST" and c[1] == base + "/upload_part_finish"]
    assert [f["part_index"] for f in finish] == [1, 2]


# ── 文本默认走 markdown（2026-11-18 定调） ──────────────────

def _adapter(markdown="auto"):
    a = QQOfficialAdapter("appid", "secret", markdown=markdown)
    a._token = "tok"
    a._token_expire = 9e18          # 别真去换 token
    return a


def _msg_bodies(calls):
    return [c[2] for c in calls if c[0] == "POST" and c[1].endswith("/messages")]


def test_text_defaults_to_markdown(monkeypatch):
    """auto 策略：文本第一次就发 msg_type=2，正文进 markdown.content。"""
    import asyncio

    import httpx

    import kernel.adapters.qq_official as m
    from kernel.event import Message

    a = _adapter()
    url = f"{m.API}/v2/groups/G/messages"
    calls: list = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda *x, **k: _FakeHTTP(
        {("POST", url): _Resp(200, {"id": "m1"})}, calls))

    asyncio.run(a.send("G", Message.from_text("**粗体** https://a.b"), reply_to=None))

    bodies = _msg_bodies(calls)
    assert len(bodies) == 1
    assert bodies[0]["msg_type"] == 2
    assert bodies[0]["markdown"]["content"] == "**粗体** https://a.b"
    assert bodies[0]["content"] == ""
    assert a._md_state["group"] == "on"


def test_markdown_failure_falls_back_and_remembers(monkeypatch):
    """没开 markdown 权限：本条回退纯文本，之后该 scene 直接走纯文本。"""
    import asyncio

    import httpx

    import kernel.adapters.qq_official as m
    from kernel.event import Message

    a = _adapter()
    url = f"{m.API}/v2/groups/G/messages"
    calls: list = []
    routes = {("POST", url): [_Resp(400, text="no markdown"),
                              _Resp(200, {"id": "m2"}),
                              _Resp(200, {"id": "m3"})]}
    monkeypatch.setattr(httpx, "AsyncClient", lambda *x, **k: _FakeHTTP(routes, calls))

    asyncio.run(a.send("G", Message.from_text("**粗体** hi"), reply_to=None))
    bodies = _msg_bodies(calls)
    assert [b["msg_type"] for b in bodies] == [2, 0]      # 先 md 失败 → 纯文本
    assert bodies[1]["content"] == "粗体 hi"               # 回退时削掉标记
    assert a._md_state["group"] == "off"

    # 第二次：不再白跑一次 markdown
    calls.clear()
    asyncio.run(a.send("G", Message.from_text("again"), reply_to=None))
    bodies = _msg_bodies(calls)
    assert [b["msg_type"] for b in bodies] == [0]


def test_markdown_policy_off(monkeypatch):
    """policy=off：一条 markdown 都不发。"""
    import asyncio

    import httpx

    import kernel.adapters.qq_official as m
    from kernel.event import Message

    a = _adapter(markdown="off")
    url = f"{m.API}/v2/groups/G/messages"
    calls: list = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda *x, **k: _FakeHTTP(
        {("POST", url): _Resp(200, {"id": "m3"})}, calls))

    asyncio.run(a.send("G", Message.from_text("hi"), reply_to=None))
    assert [b["msg_type"] for b in _msg_bodies(calls)] == [0]


def test_strip_md_keeps_text_readable():
    """纯文本兜底：只削标记，不毁内容（含标识符里的下划线）。"""
    from kernel.adapters.qq_official import _strip_md

    assert _strip_md("**粗体** 和 `code`") == "粗体 和 code"
    assert _strip_md("# 标题\n正文") == "标题\n正文"
    assert _strip_md("> 引用\n- 列表项") == "引用\n列表项"
    assert _strip_md("![#720px #360px](https://a.b/c.jpg)") == "https://a.b/c.jpg"
    assert _strip_md("看 [文档](https://a.b) 吧") == "看 文档 https://a.b 吧"
    # 表格：去掉分隔行与竖线
    assert _strip_md("| 指标 | 值 |\n|---|---|\n| bw_kills | 42 |") == "指标  值\nbw_kills  42"
    # 标识符里的单个下划线必须留着
    assert _strip_md("arena_kills / bw_wins") == "arena_kills / bw_wins"
