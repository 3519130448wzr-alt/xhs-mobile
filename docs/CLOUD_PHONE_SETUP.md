# 云手机接入与页面校准

第一阶段需要一台实际运行 Android 的云手机，目标固定为小红书 App。不绑定云手机供应商；浏览器移动模拟、自建展示站或其他平台不能替代本阶段验证。

项目曾以 Android 12、ADB、`uiautomator2`、720 × 1280 屏幕及小红书 **9.46.0**（包名 `com.xingin.xhs`）完成基础接入与页面校准。新设备仍需逐项验证连接、截图、UI 树、中文输入、页面可读性和采集流程。公开仓库不包含真实账号、连接地址或页面证据；验收边界见 [第一阶段验收](ACCEPTANCE.md)。

## 1. 设备采购／试用前确认

| 要求 | 必须确认的内容 | 不满足时的影响 |
| --- | --- | --- |
| 实际 Android 系统 | 能运行小红书正式 App，能记录 Android 版本、App 版本、屏幕尺寸与方向。 | 无法进行本阶段手机 App 验证。 |
| ADB 接入 | 供应商提供控制主机可访问的 ADB 连接方式和明确 serial；能执行安装、设备状态和截图等正常操作。 | 仅有远程观看画面的控制台不足以支持当前驱动。 |
| 连接维持 | 能保持单设备会话，并在短时断开后使用同一设备标识重新连接。 | 无法可靠恢复任务，需要先解决供应商连接问题。 |
| 自动化服务 | 允许安装与运行 `uiautomator2` 所需辅助组件／服务，权限与安装方式符合供应商环境。 | ADB 可连并不等于 UI 自动化可用。 |
| 页面读取 | UI 树可导出，截图不是黑屏；能读取当前小红书页面的实际控件与文字。 | 需要记录证据诊断；不能猜控件 ID 或宣称字段已可读。 |
| 中文输入 | 自动化输入中文关键词后，页面真实显示内容与输入字符串一致。 | 无法验收中文关键词搜索。 |
| 人工初始化 | 实验室可正常安装、登录、授权系统权限并处理必要的人工验证。 | 程序需等待人工处理，不能自行补造账号或绕过验证。 |

设备是否满足当前驱动，必须通过试用或实测确定。不能只依据“支持 Android”或“支持 ADB”的广告判断可用；本项目不要求通过 Root、身份伪造或修改目标 App 来补足能力。

## 2. 控制主机与配置

- 首次设备诊断只需 Python 3.12／3.13、项目依赖和 Android Platform Tools。PostgreSQL 在开始 `collect-current`／`run` 前准备，用于持久化任务与记录。
- 通过供应商正常连接方式建立 ADB；如需隧道或专用客户端，先由实验室完成连接，不在代码中硬编码某家供应商。
- 在本地配置中绑定逻辑设备名（例如 `lab01`）与真实 serial、会话标识、App 配置、页面规则和状态目录；配置格式以项目示例为准。
- 当前示例通过 `XHS_DATABASE_URL` 读取 PostgreSQL 地址，通过 `XHS_ADB_SERIAL` 读取真实 serial；`app_package` 必须填写实机确认的小红书包名，不能猜测。完成后的真实页面规则由 `profile_path` 指向。
- 同一控制主机的所有运行命令必须使用同一状态目录，以使本机互斥生效。两台主机各自的状态目录不能保证设备独占，第一阶段不允许这样并行控制。
- 使用本地配置或环境变量保存数据库与供应商凭证；不要提交真实配置、会话密钥或包含凭证的日志。

查看连接情况的标准命令示例：

```sh
adb devices -l
adb -s ACTUAL_SERIAL get-state
```

将 `ACTUAL_SERIAL` 替换为实际设备 serial。即使只连接了一台设备，也应显式指定 serial。网络连接参数来自供应商，不能把示例地址当作实际接入地址。

当前 App 0.3.0 可通过[本机自动连接](AUTO_CONNECTION.md)处理换网：系统路线失败后，尝试可用物理接口；用专用前缀列表动态授权单个公网 IPv4 `/32`，容量 2 用于验证时暂存新旧地址。首次授权完成前保留原接入配置。下面的固定接口命令用于维护和回退。

macOS 上如果 VPN 持续改写默认出口，而安全组只允许可信网络接口的单一公网 IP，可用项目内的 TCP relay 只让 ADB 经指定接口发出，其他流量不受影响：

```sh
.venv/bin/python scripts/adb_interface_relay.py \
  --interface ACTUAL_INTERFACE \
  --remote-host ACTUAL_ADB_HOST \
  --remote-port ACTUAL_ADB_PORT \
  --listen-host 127.0.0.1 \
  --listen-port 6100
adb connect 127.0.0.1:6100
adb -s 127.0.0.1:6100 get-state
```

relay 必须只监听回环地址；供应商端的 ADB 入站仍应限制到控制主机的单一公网 IP，不得因 VPN 出口变化扩大为开放网段。当前设备的本地连接参数只保存在被 Git 忽略的 `var/connection/`，不提交公网端点或账号信息。

如需明确使用系统当前路由，可设置 `--interface system`；此模式不绑定物理接口。指定 `en0` 等接口时仍必须通过该接口连接，不会失败后自动切换。两种模式都强制使用 IPv4 loopback 监听。自动连接管理器通过独立路线文件选择这些模式，固定接口 relay 本身不会擅自切换。改变出口后必须验证 `adb -s ACTUAL_SERIAL get-state` 返回 `device`，仅 TCP 连接成功不算接入通过；握手失败时恢复原配置，再检查供应商接入状态。

转发器采用每方向有界缓存和非阻塞写入；一侧发送受阻时，反向回复仍可处理，EOF 会在缓存发完后传播。2026-09-13 的大文件 ADB 安装曾停滞，随后通过云手机系统下载工具从官方源下载并安装成功；这不等于大文件 ADB 上传已经验收通过。安装发生超时后，应先核对安装包和设备状态，不反复盲目重试。

## 3. 实验室初始化

1. 正常安装并打开小红书；按需要由实验室完成登录、系统权限和验证。
2. 固定屏幕方向、显示尺寸、字体与 App 语言，记录 App 和 Android 版本。校准后的这些设置变更都可能要求重新校准。
3. 人工验证能够搜索中文关键词、进入一篇图文笔记、展开正文并返回结果列表。
4. 确认页面显示的是实际内容，且截图和 UI 树能与肉眼看到的页面对应。
5. 在没有其他程序操作该设备的情况下开始诊断。

```sh
.venv/bin/xhs-mobile --config config.local.toml doctor --device lab01
.venv/bin/xhs-mobile --config config.local.toml snapshot --device lab01 --label home
```

数据库迁移使用实际配置指向的项目数据库。`doctor` 成功只是设备接入条件通过，不能替代搜索、正文完整性或 30 篇真实数据验收。

`doctor` 的自动化探测可能安装或启动 `uiautomator2` 辅助服务；它不登录、不代填权限。`app_package` 首次可留空，此时报告会列出候选安装包并以 `needs_package_configuration=true` 提醒继续配置，退出码 2 不代表 ADB 必然失败。

确认包名后，由实验室先人工聚焦一个可以安全替换内容的搜索输入框，再运行中文输入检查：

```sh
.venv/bin/xhs-mobile --config config.local.toml doctor --device lab01 --check-input
```

该命令替换输入框内容并精确回读，但不会按回车或提交搜索。输入后仍需人工核对手机画面。

## 4. 真实页面校准

页面规则必须来自该设备当前 App 版本的真实截图和 UI 树，不能使用猜测的资源 ID、未经验证的网络示例或 synthetic 夹具直接开始真实采集。

实验室分别打开以下正常页面并保存快照：

- 首页／搜索入口。
- 已聚焦的搜索框和中文输入后的状态。
- 搜索结果页，包含图文卡片和可识别的详情入口。
- 图文详情页：标题、作者、正文；需要展开或滚动时保存相应状态。
- 详情返回后的结果页。

用不同标签保存当前页面：

```sh
.venv/bin/xhs-mobile --config config.local.toml snapshot --device lab01 --label search-focused
.venv/bin/xhs-mobile --config config.local.toml snapshot --device lab01 --label search-with-chinese
.venv/bin/xhs-mobile --config config.local.toml snapshot --device lab01 --label results
.venv/bin/xhs-mobile --config config.local.toml snapshot --device lab01 --label detail-before-expand
.venv/bin/xhs-mobile --config config.local.toml snapshot --device lab01 --label detail-after-expand
.venv/bin/xhs-mobile --config config.local.toml snapshot --device lab01 --label results-after-back
```

每次快照前由实验室确保当前页面确实对应标签。根据这些证据校准页面分类、搜索输入、卡片定位、详情字段和正文完整性规则，再验证规则及样本：

```sh
uv run xhs-mobile profile-check --profile ACTUAL_PROFILE_PATH
uv run xhs-mobile profile-check --profile ACTUAL_PROFILE_PATH --sample ACTUAL_SNAPSHOT_DIRECTORY
```

`profiles/template.toml` 故意保留为 `verified = false` 的未校准模板。填写时需要实际包名、App 版本、参考分辨率和校准证据路径；页面与动作规则从实际 UI 树确定。正文完整性需要明确的正面证据，不能仅因没有“展开”按钮就认定完整；未找到标题也不能直接标成页面没有标题。只有完成实机校准后才能设置 `verified = true`，真实采集不能使用 `synthetic` 规则。

尚未收齐全部页面时，使用草稿检查逐页验证，不需要提前修改 `verified`：

```sh
uv run xhs-mobile profile-check --draft --profile ACTUAL_PROFILE_PATH --sample ACTUAL_SNAPSHOT_DIRECTORY
```

草稿检查读取并核验本地证据，报告已有页面和动作规则的命中数、可用边界，以及尚缺的生产配置。已填写的包名、版本和分辨率必须与快照相符；未填写项会明确标记为未检查。它不会连接设备、访问数据库或把草稿升级为已校准规则，`run` 和 `collect-current` 仍拒绝草稿。草稿检查通过只表示本次检查无错误，不表示允许开始采集。

校准通过不代表所有页面都能读取。小红书版本升级、布局变化、不同类型笔记和系统显示设置变化后，需要重新验证实际样本。未知页面应保存证据并停止或等待校准，不继续盲点。

中文输入需要真实检查输入框显示值和实际搜索结果页面；仅检查函数没有报错不足以证明成功。采集读取正文文本，不把图片内容识别当作正文补全。

需要本地 OCR 时，仅配置页面实际文本所在的区域，并单独准备本地 OCR 程序及对应语言数据；它是可选能力，不是云端模型调用。保留识别区域与置信度，不对笔记配图做 OCR，也不使用 OCR 输出充当可信笔记 ID。

UI 树与截图是顺序采集，不保证来自同一渲染帧。应在页面稳定时保存；若两者内容不一致，重新采样并核对，不能把错位证据用于验收。

## 5. 首次运行顺序

1. `doctor` 检查连接、实际包名与自动化能力，`doctor --check-input` 验证中文输入，`snapshot` 留存真实页面。
2. 完成页面规则校准，用 `profile-check` 验证配置及样本。
3. 准备 PostgreSQL，设置 `XHS_DATABASE_URL` 并运行 `db upgrade`。
4. 人工打开一篇图文详情，运行 `collect-current --device lab01`；核对正文是否完整、作者是否正确、证据是否对应。
5. 运行一个关键词的小任务，核对搜索、进入详情、返回、继续与保存的完整闭环。
6. 按 [验收规程](ACCEPTANCE.md) 执行 3 个关键词任务、人工审阅、恢复验证与导出。

程序可以脱离 Agent 运行，但登录、验证码及无法判断的页面仍可能需要实验室人工处理。等待人工处理是一种明确任务状态，不能由后台无限重试代替。

## 6. 接入记录

记录供应商及连接方式、逻辑设备 ID、实际 serial、Android／小红书版本、自动化服务状态、屏幕设置、中文输入结果、页面规则版本、快照位置、已知缺失字段和未通过项目。记录连接事实与证据，不保存账号密码或供应商访问密钥。
