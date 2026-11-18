"""指令分发：把事件文本解析成指令，调用处理器，把返回值发回去。

归属内核，不含任何业务判断：解析不到指令就静默放过，交给插件自己的事件处理器。

日志口径对齐 V2（`commands.py:handle_command`）：
  解析 → DEBUG 一行，带 cmd/args/user/owner
  完成 → INFO 一行，带耗时与结果形态（V2 是"无返回值（已自行发送）"这种）
  异常 → ERROR 带完整堆栈（V2 踩过：异常被吞掉，只剩"没反应"）
"""
from __future__ import annotations

import inspect
import time

from kernel.adapters.base import Adapter
from kernel.bus.command import CommandRegistry
from kernel.event import Event, Message
from kernel.obs.logger import get_logger
from kernel.services.registry import ServiceRegistry

log = get_logger("kernel.dispatch")


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


class Dispatcher:
    def __init__(self, commands: CommandRegistry, services: ServiceRegistry,
                 *, slow_ms: float = 2000, metrics=None,
                 admins: set[str] | None = None,
                 admins_provider=None,
                 recorder=None) -> None:
        self.commands = commands
        self.services = services
        self.slow_ms = slow_ms
        self.metrics = metrics
        self.admins = admins or set()
        # 动态管理员来源（认领后要立刻生效，不能只认启动时的快照）
        self._admins_provider = admins_provider
        # 见过的用户登记（openid 只能从消息事件里获得，没有查询接口）
        self._recorder = recorder

    async def handle(self, event: Event) -> bool:
        if event.kind != "message.group" and event.kind != "message.private":
            return False

        # 先登记发言人：官方渠道的 openid 是按 AppID 分配的，只能从事件里拿，
        # 平台也没有"查我 openid"的接口（/members 需内邀白名单，实测 11253）。
        if self._recorder is not None:
            try:
                self._recorder(event)
            except Exception:
                log.warning("登记发言人失败", exc_info=True)

        parsed = self.commands.parse(event.text)
        if not parsed:
            return False
        name, args = parsed
        cmd = self.commands.resolve(name)
        if cmd is None:
            return False

        log.debug("指令解析", extra={"fields": {
            "cmd": name, "args": args, "user": event.user_id,
            "group": event.group_id or "-", "owner": cmd.owner}})

        # 命令权限（§10）：owner/admin/group_admin/user 由内核统一判定。
        # group_admin 目前降级为 admin —— 官方群消息事件不带 member_role
        # （那是频道消息的字段），拿不到"该用户是否本群管理员"。
        if not self._allowed(cmd.perm, event):
            log.warning("权限不足，拒绝执行", extra={"fields": {
                "cmd": name, "perm": cmd.perm, "user": event.user_id,
                "group": event.group_id or "-"}})
            return True

        # 熔断：连续失败超过阈值的插件，冷却期内直接跳过（§9 不自动重启）
        if self.metrics is not None and self.metrics.tripped(cmd.owner):
            log.warning("插件已熔断，跳过本次调用", extra={"fields": {
                "cmd": name, "owner": cmd.owner}})
            return True

        started = time.perf_counter()
        try:
            result = cmd.handler(event, args)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            cost = _ms(started)
            if self.metrics is not None:
                self.metrics.record(cmd.owner, cost, ok=False)
            log.error("指令异常", exc_info=True, extra={"fields": {
                "cmd": name, "owner": cmd.owner, "cost_ms": cost,
                "user": event.user_id, "group": event.group_id or "-"}})
            return True

        cost = _ms(started)
        if self.metrics is not None:
            self.metrics.record(cmd.owner, cost, ok=True)

        # 插件自己发了消息（返回 None），这里只记完成
        if result is None:
            log.info("指令完成", extra={"fields": {
                "cmd": name, "owner": cmd.owner, "cost_ms": cost,
                "result": "插件已自行发送"}})
            self._slow(name, cmd.owner, cost)
            return True

        # 插件可以直接返回 Message（比如单图消息），也可以返回 str
        out = result if isinstance(result, Message) else Message.from_text(str(result))

        try:
            adapter: Adapter = self.services.require("adapter")
        except LookupError:
            log.error("没有可用的适配器，无法回复", extra={"fields": {"cmd": name}})
            return True

        target = event.group_id or event.user_id or ""
        await adapter.send(target, out, reply_to=event.id)

        if out.only_image:
            shape = "image"
        else:
            shape = f"text/{len(out.text)}字"
        log.info("指令完成", extra={"fields": {
            "cmd": name, "owner": cmd.owner, "cost_ms": cost,
            "result": shape, "target": target}})
        self._slow(name, cmd.owner, cost)
        return True

    def _allowed(self, perm: str, event: Event) -> bool:
        if perm in ("", "user"):
            return True
        if perm in ("owner", "admin", "group_admin"):
            admins = (self._admins_provider() if self._admins_provider
                      else self.admins)
            return str(event.user_id or "") in admins
        log.warning("未知权限等级，按拒绝处理", extra={"fields": {"perm": perm}})
        return False

    def _slow(self, name: str, owner: str, cost: int) -> None:
        if self.slow_ms and cost >= self.slow_ms:
            log.warning("指令偏慢", extra={"fields": {
                "cmd": name, "owner": owner, "cost_ms": cost,
                "limit_ms": int(self.slow_ms)}})
