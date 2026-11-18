"""CQ 码解析与 NapCat 上报映射（纯函数，不联网）。"""
from kernel.adapters.cq import parse_cq, to_onebot_message
from kernel.adapters.napcat import NapCatAdapter
from kernel.event import Message, Segment


def test_parse_text_only():
    segs = parse_cq("你好")
    assert len(segs) == 1 and segs[0].type == "text" and segs[0].data["text"] == "你好"


def test_parse_at_and_text():
    segs = parse_cq("[CQ:at,qq=12345] 早")
    assert [s.type for s in segs] == ["at", "text"]
    assert segs[0].data["qq"] == "12345"
    assert segs[1].data["text"] == " 早"          # 空格属于正文，由 normalize 处理


def test_parse_image_and_reply():
    segs = parse_cq("[CQ:reply,id=99][CQ:image,file=abc.jpg,url=http://x/y.jpg]")
    assert segs[0].type == "reply" and segs[0].data["id"] == "99"
    assert segs[1].type == "image" and segs[1].data["file"] == "abc.jpg"


def test_unescape_brackets():
    segs = parse_cq("&#91;不是CQ&#93;")
    assert segs[0].data["text"] == "[不是CQ]"


def test_to_onebot_roundtrip():
    msg = Message(segments=[Segment("text", {"text": "hi"}),
                            Segment("at", {"qq": "1"}),
                            Segment("image", {"file": "a.png"})])
    out = to_onebot_message(msg.segments)
    assert [x["type"] for x in out] == ["text", "at", "image"]


def test_napcat_map_group_with_mention():
    a = NapCatAdapter("ws://127.0.0.1:3001")
    a.self_id = 999
    e = a.map_event({
        "post_type": "message", "message_type": "group", "self_id": 999,
        "group_id": 111, "user_id": 222, "message_id": 333,
        "message": [{"type": "at", "data": {"qq": "999"}},
                    {"type": "text", "data": {"text": " hi"}}],
        "time": 1794665400,
    })
    assert e.kind == "message.group"
    assert e.group_id == "111" and e.user_id == "222"
    assert e.mentions_me is True
    assert e.text == " hi"


def test_napcat_map_without_mention():
    a = NapCatAdapter("ws://127.0.0.1:3001")
    a.self_id = 999
    e = a.map_event({
        "post_type": "message", "message_type": "group", "self_id": 999,
        "group_id": 1, "user_id": 2, "message_id": 3,
        "message": [{"type": "text", "data": {"text": "普通聊天"}}],
    })
    assert e.mentions_me is False


def test_napcat_meta_event_sets_self_id():
    a = NapCatAdapter("ws://127.0.0.1:3001")
    assert a.map_event({"post_type": "meta_event", "meta_event_type": "lifecycle",
                        "self_id": 123}) is None
    assert a.self_id == 123


def test_napcat_private_message():
    a = NapCatAdapter("ws://127.0.0.1:3001")
    e = a.map_event({"post_type": "message", "message_type": "private",
                     "user_id": 7, "message_id": 8,
                     "message": [{"type": "text", "data": {"text": "hi"}}]})
    assert e.kind == "message.private" and e.group_id is None
