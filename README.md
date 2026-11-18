# Nox

[![License: AGPL-3.0-or-later](https://img.shields.io/badge/license-AGPL--3.0--orlater-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

Nox 是一个用 Python 编写的聊天机器人**内核**，采用微内核 + 插件架构：内核负责事件分发、
插件生命周期、存储与调度，业务能力全部以插件形式挂载。

本仓库只包含内核与平台适配器。具体业务插件独立维护，不在开源范围内。

## 特性

- **适配器抽象**：把各平台的事件、消息段与能力差异收敛成统一模型，业务代码不感知平台；
  一个进程可同时接入多个平台，按会话自动路由
- **插件运行时**：清单声明、权限校验、热重载、健康检查、连续失败熔断
- **命令表**：多前缀、别名、冲突检测、四级权限（`owner` / `admin` / `group_admin` / `user`）
- **存储**：SQLite（WAL）+ KV + 原子写文件，插件自建表须带 id 前缀
- **任务账本**：租约领取、崩溃恢复、失败重试，进程重启不丢任务
- **出站队列**：按平台限速、退避重试、失败进死信，不阻塞后续消息
- **渲染服务**：无头 Chromium 把 HTML 模板渲染成图片，带缓存与并发池
- **可观测性**：结构化日志（文本 / JSON）、慢调用告警、指标采样、日志分析报表
- **Web 宿主**：内置 HTTP 服务，提供 `/healthz` 与插件路由挂载点

## 支持的平台

| 平台 | 适配器 | 接入方式 | 富文本 | 按钮 |
| --- | --- | --- | --- | --- |
| QQ 官方机器人 | `qq_official` | WebSocket 长连接 | markdown | 内嵌键盘 |
| NapCat / OneBot v11 | `napcat` | 正向 WebSocket | 无 | 无 |
| 本地调试 | `mock` | 进程内 | 有 | 有 |

## 环境要求

- Python 3.12 及以上
- Chromium（仅渲染功能需要，缺省时渲染服务自动降级）

## 安装

```bash
git clone https://github.com/Trusler258/nox-bot.git
cd nox-bot
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[adapters,web]"
```

可选依赖组：`adapters`（HTTP / WebSocket）、`web`（内建 HTTP 宿主）、`dev`（测试与静态检查）。

## 配置

### 凭据

`config/secrets.env`，已在 `.gitignore` 中排除：

```dotenv
QQ_APPID=...
QQ_SECRET=...
NAPCAT_TOKEN=...
```

### 主配置

`config/app.toml` 覆盖全部可调行为，改完重启生效：

| 配置段 | 内容 |
| --- | --- |
| `[app]` | 名称、运行档位、时区、数据目录、日志级别、管理员列表 |
| `[runtime]` | 关闭超时、线程数、处理器超时、慢调用阈值、熔断参数 |
| `[commands]` | 指令前缀列表、大小写敏感、是否剥离前导空格 |
| `[adapters]` | 启用的平台、入/出站队列长度、出站限速、各平台连接参数 |
| `[scheduler]` | 轮询间隔、租约时长、重试上限、数据保留 |
| `[render]` | 浏览器路径、并发数、缓存容量 |
| `[llm]` | 默认模型与调用参数 |
| `[web]` | 监听地址与端口 |
| `[obs]` | 日志目录、JSON 格式、轮转天数、采样率 |
| `[plugins]` | 插件目录与禁用列表 |

启用多平台只需在 `[adapters] enabled` 里列出多个名字，内核会自动套一层路由：

```toml
[adapters]
enabled = ["qq_official", "napcat"]

[adapters.napcat]
ws_url = "ws://127.0.0.1:3001"
```

## 运行

```bash
python -m runtime.main                  # 常驻
python -m runtime.main --demo           # 用一条假消息跑通 指令 → 回复
python -m kernel.obs.analyze --since 2h # 控制台查看日志分析
```

systemd 单元示例：

```ini
[Unit]
Description=Nox
After=network-online.target

[Service]
WorkingDirectory=/opt/nox
ExecStart=/opt/nox/.venv/bin/python -m runtime.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## 目录结构

```
kernel/     内核：适配器、事件总线、插件运行时、存储、调度、渲染、可观测性、Web 宿主
runtime/    进程入口与装配
config/     配置模板（凭据与运行时数据不入库）
tests/      内核测试
scripts/    开发脚本（渲染自检、协议探测等）
libs/       可复用的独立模块
```

## 插件模型

插件是一个目录，含清单 `manifest.yaml` 与入口模块：

```yaml
id: example                    # ^[a-z][a-z0-9-]{2,31}$
name: 示例插件
version: 1.0.0
api_version: "3.0"             # 主版本必须与内核一致
entry: main:ExamplePlugin      # module:ClassName
permissions:                   # 内核强校验，未声明即拒绝
  - send.message
  - storage.kv
```

```python
class ExamplePlugin:
    def on_load(self, ctx) -> None:
        self.ctx = ctx
        ctx.command("hello", self.cmd_hello, help="打声招呼")

    async def cmd_hello(self, event, args: str) -> str:
        return f"hello {args or event.sender_name}"
```

生命周期：`on_load(ctx)` → `on_start()` → `on_stop()` → `on_unload()`。
卸载或热重载时，插件注册的指令、事件订阅、后台任务与定时任务会被统一回收。

常用上下文接口：

| 接口 | 说明 |
| --- | --- |
| `ctx.command(...)` / `ctx.on(...)` | 注册指令 / 订阅事件 |
| `ctx.conf(key, default)` | 读取 `config/plugins/<id>.toml` |
| `ctx.require(name)` / `ctx.provide(name, impl)` | 取用 / 提供服务 |
| `ctx.db` / `ctx.kv` / `ctx.store` | SQLite / 键值存储 / 临时文件目录 |
| `ctx.background.add(coro)` | 后台任务，随插件卸载自动取消 |
| `ctx.schedule(job, fn)` | 注册任务账本的处理器 |
| `ctx.identity.is_admin(user_id)` | 权限判定 |
| `ctx.message.send(text, chat_id, is_group)` | 主动发送消息 |

## 开发

```bash
python -m pytest -q          # 单元测试
python -m ruff check .       # 静态检查
```

分层约束：

- `kernel/**` 不得反向依赖插件
- 插件之间不得互相 import，只能通过 `ctx.require()` 取服务
- 插件卸载必须回收全部注册（有测试守着：反复重载后注册表不增长）

## 许可证

AGPL-3.0-or-later，详见 [LICENSE](LICENSE)。
