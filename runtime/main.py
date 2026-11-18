"""入口：装配内核、加载插件、跑通一条消息。

用法：
    python -m runtime.main --demo       # 发一条假消息，走完 指令→回复，然后退出
    python -m runtime.main              # 常驻
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import tomllib
from pathlib import Path

from kernel.adapters.base import MockAdapter
from kernel.adapters.napcat import NapCatAdapter
from kernel.adapters.qq_official import QQOfficialAdapter
from kernel.adapters import router
from kernel.bus.command import CommandRegistry
from kernel.bus.dispatch import Dispatcher
from kernel.bus.eventbus import EventBus
from kernel.config.loader import load_config, project_root
from kernel.event import Event, Message
from kernel.modules import ModuleRegistry
from kernel.obs.buffer import RingBuffer
from kernel.obs.logger import get_logger, set_trace, setup_logging
from kernel.platform_probe import probe
from kernel.plugins.runtime import PluginRuntime
from kernel.queues import InboundPump, OutboundSender
from kernel.render.service import RenderService
from kernel.scheduler.runner import Scheduler
from kernel.services.registry import ServiceRegistry
from kernel.storage.db import JobLedger, Store
from kernel.web.host import WebHost


class Kernel:
    def __init__(self, root: Path, *, profile: str = "full") -> None:
        self.root = root
        self.cfg = load_config(root)
        self.cfg["app"]["profile"] = profile
        self.env = probe()
        obs = self.cfg["obs"]
        setup_logging(self.cfg["app"]["log_level"], root / obs["log_dir"],
                      bool(obs["log_json"]),
                      console_format=str(obs.get("console_format", "text")),
                      quiet=tuple(obs.get("quiet_loggers") or ()),
                      quiet_level=str(obs.get("quiet_level", "WARNING")))
        self.log = get_logger("runtime")
        self.log_dir = root / self.cfg["obs"]["log_dir"]

        data_dir = root / self.cfg["app"]["data_dir"]
        self.store = Store(data_dir / "db" / "main.db")
        self.ledger = JobLedger(self.store)
        self.bus = EventBus(default_timeout=float(self.cfg["runtime"]["handler_timeout_s"]))
        c_cfg = self.cfg.get("commands", {})
        self.commands = CommandRegistry(
            prefixes=c_cfg.get("prefixes"),
            case_sensitive=bool(c_cfg.get("case_sensitive", False)))
        self.services = ServiceRegistry()
        self.scheduler = Scheduler(self.ledger, tick=float(self.cfg["scheduler"]["tick_s"]),
                                   lease_seconds=float(self.cfg["scheduler"]["lease_seconds"]))

        self.pump = InboundPump(self.bus,
                                maxsize=int(self.cfg["adapters"]["inbound_queue_size"]))
        self.adapter = self._build_adapter()
        self.outbound = OutboundSender(
            self.adapter,
            rate_per_min=int(self.cfg["adapters"]["outbound_rate_per_min"]),
            retry_max=int(self.cfg["adapters"]["retry_max"]))
        self.services.provide("adapter", self.outbound, owner="kernel")
        from kernel.obs.metrics import PluginMetrics
        self.metrics = PluginMetrics(
            trip_after=int(self.cfg["runtime"].get("breaker_failures", 5)),
            cooldown_s=float(self.cfg["runtime"].get("breaker_cooldown_s", 60)))
        self.services.provide("metrics", self.metrics, owner="kernel")

        self.modules = ModuleRegistry()
        self.log_buffer = RingBuffer(int(self.cfg["web"].get("log_buffer_size", 2000)))
        self.runtime = PluginRuntime(bus=self.bus, commands=self.commands,
                                     services=self.services, scheduler=self.scheduler,
                                     plugins_dir=root / self.cfg["plugins"]["dir"],
                                     config=self._plugin_configs(root),
                                     db=self.store, adapter=self.outbound,
                                     root_cfg=self._root_cfg(), root=root)
        self.dispatcher = Dispatcher(
            self.commands, self.services,
            slow_ms=float(self.cfg["runtime"].get("slow_handler_ms", 2000)),
            metrics=self.metrics,
            admins={str(a) for a in (self.cfg["app"].get("admins") or [])},
            admins_provider=self._current_admins,
            recorder=self._record_user)
        self.bus.subscribe("message", self.dispatcher.handle, priority=1000, owner="kernel")

        self.web = None
        if self.cfg["web"].get("enabled"):
            self.web = WebHost(host=str(self.cfg["web"].get("host", "127.0.0.1")),
                               port=int(self.cfg["web"].get("port", 59310)),
                               token=str(self.cfg["web"].get("token", "")),
                               health_fn=self._health)
            self.services.provide("web", self.web, owner="kernel")
            self.modules.register("web", self.web, order=50)

        self.render = None
        if self.cfg["render"].get("enabled", True):
            r_cfg = self.cfg["render"]
            self.render = RenderService(
                out_dir=root / str(r_cfg.get("out_dir", "data/store/cards")),
                chromium_path=str(r_cfg.get("chromium_path") or self.env.chromium_path),
                pool_size=int(r_cfg.get("pool_size", 1)),
                timeout_ms=int(r_cfg.get("timeout_ms", 15000)),
                cache_ttl_s=int(r_cfg.get("cache_ttl_s", 600)),
                cache_max=int(r_cfg.get("cache_max_items", 500)),
                device_scale=int(r_cfg.get("device_scale", 2)))
            self.services.provide("render", self.render, owner="kernel")
            self.modules.register("render", self.render, order=150)

        # 面板等插件通过服务名拿内核门面，不直接 import 内核内部结构
        self.services.provide("kernel", self, owner="kernel")
        self.services.provide("queue", self.pump, owner="kernel")
        from kernel.storage.kv import KV
        self.kv = KV(Path(self.cfg["app"].get("data_dir", "data")) / "kv")
        self.services.provide("kv", self.kv, owner="kernel")
        self._sampler: asyncio.Task | None = None
        self._last_gc: float = 0.0
        self._cfg_mtime: float = 0.0
        self._plugin_cfg_mtime: dict[str, float] = {}
        self._admins_stale = False

    # ── 装配 ──────────────────────────────────────────────
    def _check_app_id(self) -> None:
        """换了机器人（AppID 变了）就清掉旧 openid 状态。

        openid 按 AppID 分配，test1 → 正式 bot 之后：
          - chat_admins / chat_switch / seen_users 全是废数据
          - config/app.toml 里的 admins 也失效，会导致"谁都不能认领"
        这里自动清库并标记 admins 过期，让新人能立刻认领；
        配置文件本身不动（改配置属于运维操作，程序不擅自写）。
        """
        cur = str(getattr(self.adapter, "app_id", "") or "")
        if not cur:
            return
        row = None
        try:
            rows = self.store.query("SELECT v FROM meta WHERE k = 'app_id'")
            row = rows[0] if rows else None
        except Exception:
            pass
        old = str(row["v"]) if row else ""
        if old and old != cur:
            self.log.warning("检测到 AppID 变更，清理旧机器人的 openid 状态",
                             extra={"fields": {"from": old, "to": cur}})
            for t in ("chat_admins", "chat_switch", "seen_users"):
                try:
                    self.store.execute(f"DELETE FROM {t}")
                except Exception:
                    self.log.warning("清理表失败", exc_info=True,
                                     extra={"fields": {"table": t}})
            # 内存里直接清空：插件层（ctx.identity）读的也是这份配置，
            # 只标记 Kernel 自己的话，/~llm 的认领仍然会被旧 openid 挡住
            self.cfg["app"]["admins"] = []
            self._admins_stale = True
            self.log.warning("config/app.toml 的 admins 是旧机器人的，已忽略；"
                             "请清空该列表。在此之前第一个发 /~llm 的人会被认领为管理员")
        try:
            self.store.execute(
                "INSERT INTO meta(k, v) VALUES('app_id', ?)"
                " ON CONFLICT(k) DO UPDATE SET v = excluded.v", (cur,))
        except Exception:
            self.log.warning("写 app_id 失败", exc_info=True)

    def _record_user(self, event) -> None:
        """登记发言人：openid + 昵称 + 会话，供配置管理员/排查用。

        官方渠道没有"查 openid"的接口，只能这样攒。写失败不影响指令执行。
        """
        import time as _t
        uid = str(event.user_id or "")
        cid = str(event.group_id or event.user_id or "")
        if not uid or not cid:
            return
        now = int(_t.time())
        kind = "group" if event.kind == "message.group" else "private"
        self.store.execute(
            "INSERT INTO seen_users(user_id, chat_id, chat_kind, name,"
            " first_seen, last_seen, msg_count)"
            " VALUES (?, ?, ?, ?, ?, ?, 1)"
            " ON CONFLICT(user_id, chat_id) DO UPDATE SET"
            "   last_seen = excluded.last_seen,"
            "   msg_count = msg_count + 1,"
            "   name = CASE WHEN excluded.name <> '' THEN excluded.name ELSE name END",
            (uid, cid, kind, str(event.sender_name or ""), now, now))

    def _current_admins(self) -> set[str]:
        """配置里的 admins + 运行时认领的（每次现查，认领后立即生效）。

        `_admins_stale` 表示配置里的列表属于上一个机器人，忽略它们，
        否则换 bot 之后没人能认领。
        """
        out: set[str] = set()
        if not self._admins_stale:
            out |= {str(a) for a in (self.cfg["app"].get("admins") or [])}
        try:
            for r in self.store.query("SELECT user_id FROM chat_admins"):
                out.add(str(r["user_id"]))
        except Exception:
            pass
        return out

    def _health(self) -> dict:
        """健康摘要（§9）：适配器、队列深度、渲染池、插件状态与指标。"""
        def _safe(obj, method: str) -> dict:
            try:
                return getattr(obj, method)() or {}
            except Exception:
                return {}

        return {
            "profile": self.cfg["app"]["profile"],
            "adapter": getattr(self.adapter, "state", "running"),
            "app_id": str(getattr(self.adapter, "app_id", "")),
            "adapter_events": getattr(self.adapter, "event_stats", {}),
            "modules": self.modules.snapshot(),
            "plugins": self.runtime.snapshot()["loaded"],
            "failed_plugins": self.runtime.failed,
            "plugin_health": self.runtime.health,
            "queue": _safe(self.pump, "stats"),
            "render": _safe(self.render, "stats"),
            "metrics": _safe(self.metrics, "summary"),
        }

    async def _sample_loop(self) -> None:
        """每分钟把指标快照落一行，供面板画曲线。只保留 24 小时。"""
        import time as _t
        while True:
            try:
                row = self.metrics.sample()
                now = _t.strftime("%Y-%m-%d %H:%M:%S")
                self.store.execute(
                    "INSERT INTO metric_samples(ts, handled, failed, timeout, plugins, tripped)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (now, row["handled"], row["failed"], row["timeout"],
                     row["plugins"], row["tripped"]))
                cutoff = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(_t.time() - 86400))
                self.store.execute("DELETE FROM metric_samples WHERE ts < ?", (cutoff,))
                # 每小时顺带清一次 store/ 的过期文件（§8.5：30 天 TTL）
                if self._last_gc + 3600 < _t.time():
                    self._last_gc = _t.time()
                    self._clean_store()
                await self.runtime.check_health()
                self._watch_config()
                await self._watch_plugin_configs()
            except Exception:
                log.error("指标采样失败", exc_info=True)
            await asyncio.sleep(60)

    def _build_adapter(self):
        """按 `[adapters] enabled` 建适配器。多个平台时套一层 RouterAdapter。

        单平台保持原来的行为（直接把适配器返回给内核），多平台才走路由，
        免得给只跑官方的部署平白多一层间接。
        """
        import os

        for k, v in self._load_secrets(self.root).items():
            os.environ.setdefault(k, v)

        enabled = self.cfg["adapters"]["enabled"] or ["mock"]
        if isinstance(enabled, str):
            enabled = [x.strip() for x in enabled.split(",") if x.strip()]

        built: dict = {}
        for name in enabled:
            adapter = self._build_one(name, os.environ)
            if adapter is not None:
                built[name] = adapter
        if not built:
            self.log.warning("没有可用适配器，退回 mock")
            return MockAdapter(self.bus)
        if len(built) > 1:
            self.log.info("多适配器模式", extra={"fields": {"names": sorted(built)}})
        return router.build(built)

    def _build_one(self, name: str, env):
        if name == "qq_official":
            cfg = self.cfg["adapters"].get("qq_official", {})
            app_id = env.get("QQ_APPID", "")
            app_secret = env.get("QQ_SECRET", "")
            if not app_id or not app_secret:
                raise RuntimeError("qq_official 需要 QQ_APPID / QQ_SECRET")
            self.log.info("使用官方 QQ 适配器", extra={"fields": {"mode": cfg.get("mode")}})
            return QQOfficialAdapter(app_id, app_secret,
                                     on_event=self.pump.submit,
                                     sandbox=bool(cfg.get("sandbox", False)),
                                     heartbeat_margin_s=float(cfg.get("heartbeat_margin_s", 5)),
                                     markdown=str(cfg.get("markdown", "auto")))
        if name == "napcat":
            cfg = self.cfg["adapters"].get("napcat", {})
            self.log.info("使用 NapCat 适配器", extra={"fields": {"url": cfg.get("ws_url")}})
            return NapCatAdapter(cfg.get("ws_url", "ws://127.0.0.1:3001"),
                                 env.get("NAPCAT_TOKEN", ""),
                                 on_event=self.pump.submit,
                                 reconnect_initial_ms=int(cfg.get("reconnect_initial_ms", 1000)),
                                 reconnect_max_ms=int(cfg.get("reconnect_max_ms", 30000)))
        if name == "mock":
            self.log.info("使用 mock 适配器")
            return MockAdapter(self.bus)
        self.log.warning("未知适配器，已跳过", extra={"fields": {"name": name}})
        return None

    @staticmethod
    def _load_secrets(root: Path) -> dict:
        f = root / "config" / "secrets.env"
        out: dict = {}
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
        return out

    def _root_cfg(self) -> dict:
        """全局配置 + 文案表，供插件的 identity / i18n 使用。"""
        import tomllib

        cfg = dict(self.cfg)
        lang = self.root / "config" / "lang.toml"
        if lang.exists():
            try:
                with open(lang, "rb") as fh:
                    cfg["_lang"] = tomllib.load(fh)
            except Exception:
                self.log.warning("lang.toml 解析失败", exc_info=True)
        return cfg

    def _plugin_configs(self, root: Path) -> dict:
        out: dict = {}
        d = root / "config" / "plugins"
        if d.exists():
            for f in sorted(d.glob("*.toml")):
                with open(f, "rb") as fh:
                    out[f.stem] = tomllib.load(fh)
        return out

    # ── 生命周期 ──────────────────────────────────────────
    async def boot(self) -> dict:
        self.store.execute(
            "CREATE TABLE IF NOT EXISTS metric_samples ("
            "  ts TEXT PRIMARY KEY, handled INTEGER, failed INTEGER,"
            "  timeout INTEGER, plugins INTEGER, tripped INTEGER)")
        # 元信息（appid 等），用于检测"换了机器人"
        self.store.execute(
            "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        # 见过的用户：官方 openid 只能从事件里拿，落库后才能离线查
        self.store.execute(
            "CREATE TABLE IF NOT EXISTS seen_users ("
            "  user_id TEXT, chat_id TEXT, chat_kind TEXT, name TEXT,"
            "  first_seen INTEGER, last_seen INTEGER, msg_count INTEGER DEFAULT 1,"
            "  PRIMARY KEY (user_id, chat_id))")
        self._sampler = asyncio.create_task(self._sample_loop())
        self._ensure_layout()
        self._clean_tmp()
        self._check_app_id()
        self.log.info("平台探测", extra={"fields": self.env.as_dict()})
        await self.adapter.start()
        await self.pump.start()
        await self.outbound.start()

        self.modules.register("adapter", self.adapter, order=200)
        self.modules.register("scheduler", self.scheduler, order=100)
        self.modules.set_state("adapter", "running")
        self.modules.set_state("scheduler", "running")

        # 先加载插件（面板在这里注册路由），再启动 HTTP 宿主
        disabled = set(self.cfg.get("plugins", {}).get("disabled") or [])
        manifests = self.runtime.discover()
        for pid in disabled:
            manifests.pop(pid, None)
            self.log.info("插件已按配置禁用", extra={"fields": {"plugin": pid}})
        loaded = [pid for pid in manifests if self.runtime.load(manifests[pid])]

        if self.web is not None:
            import logging
            logging.getLogger().addHandler(self.log_buffer)
            await self.web.start()
            self.modules.set_state("web", "running")

        if self.render is not None:
            try:
                await self.render.start()
                self.modules.set_state("render", "running")
            except Exception:
                self.log.error("渲染服务启动失败", exc_info=True)
                self.modules.set_state("render", "error")

        started = await self.runtime.start_all()
        await self.scheduler.start()

        snap = self.runtime.snapshot()
        self.log.info("内核已启动", extra={"fields": {
            "profile": self.cfg["app"]["profile"], "loaded": loaded,
            "started": started, **snap}})
        return snap

    def _clean_store(self) -> None:
        """清理 store/ 下超过 30 天且不是 tmp 的文件（§8.5）。archive/ 不动。"""
        import time as _t
        store = Path(self.cfg["app"].get("data_dir", "data")) / "store"
        if not store.is_dir():
            return
        cutoff = _t.time() - 30 * 86400
        removed = 0
        for f in store.rglob("*"):
            if not f.is_file() or f.name == "tmp" or "tmp" in f.parts:
                continue
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                pass
        if removed:
            self.log.info("已清理过期缓存", extra={"fields": {"dir": str(store), "files": removed}})

    def _watch_config(self) -> None:
        """§8.6 热更新：app.toml 变了就重载（60s 轮询，不引 watchdog）。

        校验失败（TOML 语法错/字段非法）时 reload_config 内部已保留旧配置，
        这里只负责发现变化并应用"能立即生效"的副作用（日志级别）。
        """
        f = self.root / "config" / "app.toml"
        if not f.is_file():
            return
        try:
            mtime = f.stat().st_mtime
        except OSError:
            return
        if self._cfg_mtime == 0:
            self._cfg_mtime = mtime
            return
        if mtime == self._cfg_mtime:
            return
        self._cfg_mtime = mtime

        old_level = str(self.cfg["app"]["log_level"])
        res = self.reload_config()
        if not res.get("ok"):
            self.log.warning("配置校验失败，继续使用旧配置",
                             extra={"fields": {"error": res.get("error")}})
            return
        new_level = str(self.cfg["app"]["log_level"])
        if new_level != old_level:
            from kernel.obs.logger import setup_logging
            obs = self.cfg["obs"]
            setup_logging(new_level, self.root / obs["log_dir"],
                          bool(obs["log_json"]),
                          console_format=str(obs.get("console_format", "text")),
                          quiet=tuple(obs.get("quiet_loggers") or ()),
                          quiet_level=str(obs.get("quiet_level", "WARNING")))
            self.log.info("日志级别已热更新", extra={"fields": {
                "from": old_level, "to": new_level}})

    async def _watch_plugin_configs(self) -> None:
        """§8.6：config/plugins/<id>.toml 变了就重载该插件。

        解析失败保留旧配置（不半生效）；插件没加载就把配置缓存下来。
        """
        cfg_dir = self.root / "config" / "plugins"
        if not cfg_dir.is_dir():
            return
        for f in cfg_dir.glob("*.toml"):
            pid = f.stem
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            prev = self._plugin_cfg_mtime.get(pid)
            self._plugin_cfg_mtime[pid] = mtime
            if prev is None or prev == mtime:
                continue
            try:
                import tomllib
                with open(f, "rb") as fh:
                    cfg = tomllib.load(fh)
            except Exception:
                self.log.error("插件配置解析失败，保留旧配置", exc_info=True,
                               extra={"fields": {"plugin": pid}})
                continue
            self.runtime.config[pid] = cfg
            if pid in self.runtime.loaded:
                ok = await self.runtime.reload(pid)
                self.log.info("插件配置已热更新", extra={"fields": {
                    "plugin": pid, "reloaded": ok}})
            else:
                self.log.info("插件未加载，配置已缓存",
                              extra={"fields": {"plugin": pid}})

    def reload_config(self) -> dict:
        """重载 app.toml（§8.6 的最小可用版：手动触发，不做文件监听）。"""
        from kernel.config.loader import load_config
        try:
            self.cfg = load_config(self.root)
            self.log.info("配置已重载", extra={"fields": {
                "profile": self.cfg["app"]["profile"],
                "log_level": self.cfg["app"]["log_level"]}})
            return {"ok": True}
        except Exception as e:
            self.log.error("配置重载失败，保留旧配置", exc_info=True)
            return {"ok": False, "error": str(e)}

    def _ensure_layout(self) -> None:
        """按 §8.4 建标准目录，缺目录时插件各写各的才是乱根源。"""
        data = Path(self.cfg["app"].get("data_dir", "data"))
        for sub in ("db", "kv", "store/cards", "store/images", "store/tmp",
                    "archive/stats_images"):
            try:
                (data / sub).mkdir(parents=True, exist_ok=True)
            except OSError:
                self.log.warning("目录创建失败", exc_info=True,
                                 extra={"fields": {"dir": str(data / sub)}})

    def _clean_tmp(self) -> None:
        """启动清空 data/store/tmp（§8.5）：那里只放临时图，不该越用越大。"""
        tmp = Path(self.cfg["app"].get("data_dir", "data")) / "store" / "tmp"
        if not tmp.is_dir():
            return
        removed = 0
        for f in tmp.glob("*"):
            try:
                if f.is_file():
                    f.unlink()
                    removed += 1
            except OSError:
                pass
        if removed:
            self.log.info("已清空临时目录", extra={"fields": {"dir": str(tmp), "files": removed}})

    async def shutdown(self) -> None:
        if self._sampler is not None:
            self._sampler.cancel()
            self._sampler = None
        if self.render is not None:
            await self.render.stop()
        if self.web:
            await self.web.stop()
        await self.outbound.stop()
        await self.pump.stop()
        await self.scheduler.stop()
        for pid in list(self.runtime.loaded):
            await self.runtime.unload(pid)
        await self.adapter.stop()
        self.log.info("内核已停止")


async def demo() -> int:
    root = project_root()
    k = Kernel(root)
    await k.boot()

    set_trace("demo0001")
    event = Event(id="demo-1", platform="mock", kind="message.group",
                  group_id="10001", user_id="20002",
                  message=Message.from_text("/~echo 你好，幻梦 3.0"))
    k.pump.submit(event)
    await asyncio.sleep(0.4)

    print("── 适配器发出 ──")
    for rec in k.adapter.sent:
        print(json.dumps(rec, ensure_ascii=False))

    k.scheduler.register("demo.job", lambda p: None)
    k.ledger.enqueue("demo.job", {"hello": 1})
    await k.scheduler.run_once()
    print("── 账本 ──", json.dumps(k.ledger.stats(), ensure_ascii=False))

    await k.shutdown()
    return 0 if k.adapter.sent else 1


async def serve() -> int:
    root = project_root()
    k = Kernel(root)
    await k.boot()
    stop = asyncio.Event()

    def _signal(*_a):
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _signal)
        except (ValueError, OSError):
            pass

    await stop.wait()
    await k.shutdown()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="nox")
    ap.add_argument("--demo", action="store_true", help="跑一条假消息后退出")
    ap.add_argument("--profile", default="full", choices=["full", "collector"])
    args = ap.parse_args()
    try:
        return asyncio.run(demo() if args.demo else serve())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
