# Nox

微内核 + 纯插件的 QQ/KOOK/Discord 机器人。

- 许可证：AGPL-3.0-or-later（见 `LICENSE`）
- Python：>= 3.12
- 设计文档：`docs/幻梦3.0-内核设计v1.md`（在 WorkBuddy 工作区）

## 当前状态

官方 QQ 渠道已接入并在线运行，13 个插件 / 25 条指令 / 11 个服务，
`python -m pytest -q` 203 通过。

平台硬约束（踩过的坑，源码注释里有细节）：

- 被动回复必须带 `msg_id` 与 `msg_seq`（同一 `msg_id` 递增），否则接口拒绝
- 官方群只有**被 @** 才下发；`content` 前可能有 0～2 个空格
- 官方渠道**没有 CQ 码**，也没有「查 openid」接口，身份只能从事件里取
- 文本默认发 `msg_type=2`（QQ markdown）；没有该权限的群按 scene 记住后回退纯文本
- 超过 5MB 的文件必须分片上传（`upload_prepare` → 逐片 PUT → `files` 合并）

## 快速开始

```bash
python -m pip install -e ".[adapters,web,dev]"
python -m pytest -q                 # 73 个测试
python -m runtime.main --demo       # 跑一条假消息，走完 指令 → 回复
python -m kernel.obs.analyze --since 2h   # 控制台看日志分析报表
python scripts/render_demo.py             # 渲染一张自检卡片（需系统 Chromium）
```

## 仓库结构

```
.
├── kernel/        内核（总线、插件运行时、服务、存储、调度、平台探测）
├── runtime/       进程入口与装配
├── plugins/       业务插件（echo / ping / panel）
├── config/
│   ├── app.toml       全局配置：前缀、超时、队列、日志、调度、渲染、LLM、Web
│   └── plugins/       每插件一份 <id>.toml（M1 起）
├── tests/
├── docs/          设计文档
├── v2/            旧版 2.x 源码快照（只读参考，不再部署）
├── data/          运行时数据（gitignore）
└── logs/          日志（gitignore）
```

## 面板

`plugins/panel` 是可选插件，提供 WebUI（默认 `http://127.0.0.1:59310/`）：
概览、日志（内存/文件、级别/模块/关键字过滤、分析报表）、插件热重载、模块单点重启、任务账本。
内核只提供 HTTP 宿主与 `/healthz`，卸掉面板内核照常运行。

## 配置可调项

`config/app.toml` 覆盖全部可调行为，改完重启即可，不需要碰代码：

| 段 | 内容 |
|---|---|
| `[app]` | 名称、profile（full/collector）、时区、日志级别、数据目录 |
| `[runtime]` | 关闭超时、线程数、处理器超时、慢日志阈值 |
| `[commands]` | **指令前缀列表**、是否大小写敏感、是否剥前导空格 |
| `[adapters]` | 启用哪些平台、队列长度、出站限速、各平台连接参数 |
| `[scheduler]` | 轮询间隔、租约、重试上限、退避、数据保留 |
| `[render]` / `[llm]` / `[web]` | 渲染池、模型默认值、面板端口（M2/M3 生效） |
| `[obs]` | 日志目录、JSON 格式、轮转天数、采样率 |
| `[plugins]` | 插件目录、禁用列表 |

## 分层约束

- `kernel/**` 不得 import `plugins`
- `plugins/a` 不得 import `plugins/b`，只能通过 `ctx.require()` 取服务
- 插件卸载必须回收全部注册（有测试守着：reload 100 次注册表不增长）

## 里程碑

| 阶段 | 内容 |
|---|---|
| M0 ✅ | 骨架 + 任务账本 + mock 适配器 |
| M1 ✅ | 队列与背压 / 官方 QQ 适配器 / NapCat 适配 / 模块级重启 / 样板插件 / Linux 验证 |
| M2 ✅ | 渲染服务 + WDSJ 卡片 + 官方 QQ 适配（分片上传、markdown 默认） |
| M3 ✅ | llm 插件 + panel 插件 |
| M4 🚧 | 其余插件迁移 + 灰度切换（wdsj 定时采集已补） |
