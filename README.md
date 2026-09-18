# xhs-mobile

[![Offline and PostgreSQL checks](https://github.com/3519130448wzr-alt/xhs-mobile/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/3519130448wzr-alt/xhs-mobile/actions/workflows/tests.yml)

面向真实 Android 设备的小红书图文笔记采集工具，提供设备诊断、证据驱动的页面校准、关键词采集、持久化恢复、人工审阅和 CSV/JSONL 导出。正常运行不依赖 Codex 或大模型服务。

> 本项目用于研究和经过授权的自动化场景。使用者需自行确认账号、数据和自动化行为符合适用的法律、平台规则和所在机构的要求。项目不提供验证码破解、身份伪造或绕过明确访问限制的功能。

当前公开版本以小红书 9.46.0、Android 12 和 720 × 1280 页面样本完成过单设备校准。App 版本、布局、语言或显示设置变化后，需要使用自己的真实截图与 UI 树重新校准；仓库不包含账号、云资源凭据、真实采集结果或实验室私有页面证据。

在配置完成的 macOS 主机上，日常入口是桌面 **“小红书采集助手.app”**，也可使用 `xhs-mobile` 命令行。

- **开始采集**：填写一个或多个关键词，每词默认 10 条；也可采集手机当前笔记。
- **任务与结果**：查看进度、暂停／继续、预览原文与证据、导出 CSV 和 JSONL。
- **设备连接**：首次配置自动连接授权，之后打开 App 自动准备连接；遇到手机问题，点击“打开云手机”处理。

关闭窗口后采集继续，点击 Dock 图标可重新打开；选择“退出”（⌘Q）会先安全暂停。App 不会在打开时自动采集或自动继续任务。正常运行不需要 Codex 或大模型密钥。

自动换网可选择开放连接或受限前缀列表模式，见 [本机自动连接](docs/AUTO_CONNECTION.md)。项目能力、真实验收边界和扩容限制见 [技术实现说明](docs/LAB_TECHNICAL_IMPLEMENTATION_REPORT.md) 与 [验收清单](docs/ACCEPTANCE.md)。

详细步骤见 [桌面 App 操作说明](docs/DESKTOP_APP.md)。旧 `启动小红书采集.command` 保留为 [终端维护入口](docs/ONE_CLICK.md)，不要同时用两个入口控制同一手机。

已验收的单关键词实测记录和新增批次功能见 [单机批量采集](docs/BATCH_COLLECTION.md)。

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。完成依赖安装后，可运行 `uv run xhs-mobile --help`。软件测试、页面校准和真实设备验收是三个独立层次；synthetic 测试数据不能计入真实采集成果。

## 环境与启动

需要 Python 3.12、uv、ADB（Android SDK Platform Tools）及 PostgreSQL。下面说明 Docker Compose 路线；也可参考 [本机持久 PostgreSQL](docs/LOCAL_DATABASE.md) 使用本地数据库。

```sh
uv sync --locked
cp -n config.example.toml config.local.toml
cp -n .env.example .env
```

编辑 `.env` 中数据库密码及设备 serial，并编辑 `config.local.toml` 的设备信息；不要提交这些文件。Compose 会读取 `.env`，Python CLI 不自动读取 `.env`，在本机可信 `.env` 填妥后使用：

```sh
set -a
source .env
set +a
docker compose up -d --wait
uv run xhs-mobile db upgrade
uv run xhs-mobile doctor --device lab01
```

## 页面诊断与校准

由实验室正常登录小红书、打开指定页面，然后运行：

```sh
uv run xhs-mobile snapshot --device lab01 --label home
```

分别采集首页、搜索输入、结果列表、图文详情、正文展开前后。原始证据保存到 `var/evidence/`，包含截图、UI 树和哈希清单；它们可能包含账号及个人内容，只在实验室本地保存。需要回传调试时使用另外制作的脱敏副本，保留原始文件用于核验。

将 `profiles/template.toml` 复制为本地校准文件，按真实 XML 填入选择器、版本、分辨率与证据路径，并在 `config.local.toml` 指向该文件。模板中的 `verified=false` 只有校准完成后才能改成 `true`，synthetic 测试规则不能用于生产采集。

逐页开发时可用只读草稿检查，查看已有规则在真实快照中的命中情况；它不操作设备，也不会启用采集：

```sh
uv run xhs-mobile profile-check --draft --profile profiles/calibrated.local.toml --sample var/evidence/实际快照目录
```

补齐规则并完成校准后，再使用正式检查及采集入口：

```sh
uv run xhs-mobile profile-check --profile profiles/calibrated.local.toml
uv run xhs-mobile collect-current --device lab01
uv run xhs-mobile run --device lab01 --keyword '实验室选定的关键词' --limit 10
```

从 0.5.0 起，标题、作者和正文可读即可计入目标，不自动判断正文完整性。仅为找到缺失基础字段或补读已校准时间／话题而执行有限滚动，不拼接多屏正文。时间保留页面原文，编辑时间单独标注；话题必须有平台组件证据，普通 `#文字` 不推断为标签。详见 [新版规则与历史迁移](docs/QUALITY_AND_HISTORY_MIGRATION.md)。

## 状态、恢复与导出

```sh
uv run xhs-mobile status
uv run xhs-mobile pause --task TASK_ID
uv run xhs-mobile resume --task TASK_ID
uv run xhs-mobile export --format jsonl --output var/notes.jsonl
uv run xhs-mobile export --format csv --output var/notes.csv
```

`run` 在前台执行；需要查看状态时使用另一个终端。SIGINT/SIGTERM 在安全边界暂停。强制退出后可重新 `resume`，会重新搜索并按笔记身份核对进度，不把卡片序号当成稳定地址。等待冷却也不清空预算。读取失败不再使用终身额度：连续 10 次失败或实际页面连续 10 次无进展时暂停，需明确继续；成功读取和实际前进分别清除对应连续计数。

`--limit` 是基础字段可读观察记录目标，不代表人工已确认的不同笔记数量。无可靠笔记 ID 的记录保留独立观察，人工核验后才可计入最终 30 篇验收。

人工核对后需要补采时，可以用 `resume --task TASK_ID --limit 20` 提高目标；详情、滚动、重试与冷却预算不会清零。需要人工处理的会话，先正常解决提示，再 `resume --task TASK_ID --acknowledge`；程序仍会检查当前页面，确认恢复后才继续。

## 开发验证

```sh
uv run ruff check .
uv run pytest
```

普通测试使用明确标为 synthetic 的离线页面和临时 SQLite 数据库测试事务行为，**生产仍只支持 PostgreSQL**。实际 PostgreSQL 集成测试通过 `TEST_DATABASE_URL` 指向可清空的专用测试数据库后运行：

```sh
uv run pytest -m postgres
```

更多说明：[云手机接入](docs/CLOUD_PHONE_SETUP.md)、[验收清单](docs/ACCEPTANCE.md)、[架构与扩容](docs/ARCHITECTURE.md)。

## 开源许可证

代码以 [MIT License](LICENSE) 发布。真实采集数据、页面证据、账号信息、云资源配置和第三方内容不属于本仓库的开源交付物。
