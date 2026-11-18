"""分发层：插件返回 Message（图片）时应原样发出去，不强制转成文字。"""
import asyncio

from kernel.bus.command import CommandRegistry
from kernel.bus.dispatch import Dispatcher
from kernel.event import Event, Message, Segment
from kernel.services.registry import ServiceRegistry


class Recorder:
    def __init__(self):
        self.sent = []

    async def send(self, target, message, *, reply_to=None):
        self.sent.append((target, message, reply_to))
        return {}


def test_text_result_is_wrapped():
    async def main():
        cmds, svcs = CommandRegistry(), ServiceRegistry()
        rec = Recorder()
        svcs.provide("adapter", rec, owner="test")
        cmds.register("hi", lambda e, a: "hello", owner="p")
        d = Dispatcher(cmds, svcs)
        await d.handle(Event(id="1", platform="mock", kind="message.group",
                             group_id="g", message=Message.from_text("/hi")))
        return rec

    rec = asyncio.run(main())
    assert rec.sent[0][1].text == "hello"


def test_message_result_is_passed_through():
    async def main():
        cmds, svcs = CommandRegistry(), ServiceRegistry()
        rec = Recorder()
        svcs.provide("adapter", rec, owner="test")
        img = Message(segments=[Segment("image", {"path": "/tmp/a.png"})])
        cmds.register("pic", lambda e, a: img, owner="p")
        d = Dispatcher(cmds, svcs)
        await d.handle(Event(id="2", platform="mock", kind="message.group",
                             group_id="g", message=Message.from_text("/pic")))
        return rec

    rec = asyncio.run(main())
    sent_msg = rec.sent[0][1]
    assert sent_msg.only_image is True
    assert sent_msg.segments[0].data["path"] == "/tmp/a.png"


def test_no_result_sends_nothing():
    async def main():
        cmds, svcs = CommandRegistry(), ServiceRegistry()
        rec = Recorder()
        svcs.provide("adapter", rec, owner="test")
        cmds.register("noop", lambda e, a: None, owner="p")
        d = Dispatcher(cmds, svcs)
        await d.handle(Event(id="3", platform="mock", kind="message.group",
                             group_id="g", message=Message.from_text("/noop")))
        return rec

    assert asyncio.run(main()).sent == []


def test_recorder_sees_every_message_even_without_command():
    """登记发言人要在解析指令之前：普通聊天（非指令）也要记下来。

    官方渠道的 openid 只能从消息事件里获得，漏记等于永远配不上管理员。
    """
    seen = []

    async def main():
        cmds, svcs = CommandRegistry(), ServiceRegistry()
        cmds.register("hi", lambda e, a: "hello", owner="p")
        d = Dispatcher(cmds, svcs, recorder=seen.append)
        await d.handle(Event(id="1", platform="mock", kind="message.group",
                             group_id="g1", user_id="u1",
                             message=Message.from_text("随便说句话")))
        await d.handle(Event(id="2", platform="mock", kind="message.group",
                             group_id="g1", user_id="u1",
                             message=Message.from_text("/hi")))

    asyncio.run(main())
    assert [(e.user_id, e.group_id) for e in seen] == [("u1", "g1"), ("u1", "g1")]


def test_recorder_failure_does_not_block_command():
    """登记失败不能影响指令本身（日志类逻辑不许拖垮主流程）。"""

    async def boom(_e):
        raise RuntimeError("库挂了")

    async def main():
        cmds, svcs = CommandRegistry(), ServiceRegistry()
        cmds.register("hi", lambda e, a: "hello", owner="p")
        d = Dispatcher(cmds, svcs, recorder=boom)
        return await d.handle(Event(id="1", platform="mock", kind="message.group",
                                    group_id="g", message=Message.from_text("/hi")))

    assert asyncio.run(main()) is True
