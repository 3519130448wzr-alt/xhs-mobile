# 本机持久 PostgreSQL（无 Docker）

此入口用于当前 Mac 的第一阶段真实数据存储。它使用 Homebrew PostgreSQL 17，
不启动 Homebrew 默认服务，不管理 `.tools/pg-test` 测试集群。
Docker Compose 仍是其他主机可使用的部署路线；两种方式不要同时占用同一数据库。

## 安装与启动

```sh
brew install postgresql@17
.venv/bin/python scripts/local_postgres.py start
.venv/bin/python scripts/local_postgres.py migrate
.venv/bin/python scripts/local_postgres.py status
```

当前脚本明确使用 `/opt/homebrew/opt/postgresql@17/bin`，适用于本机 Apple Silicon
Homebrew 路径。初次启动创建项目 `var/postgres-live/data`；后续启动复用原数据。
集群监听 `127.0.0.1:55432`，不开放 LAN 地址或 Unix socket，认证使用 SCRAM。
启用数据页校验和，保留 PostgreSQL 的同步提交、WAL 与 fsync 持久性设置。
应用角色 `xhs_app` 是独立数据库 `xhs_mobile` 的所有者，不具有超级用户、创建数据库
或创建角色权限。管理员仅用于本机集群初始化、身份检查与管理。

`var/postgres-live/credentials.json` 由程序随机生成，权限为 0600；父目录权限为 0700。
脚本不打印密码或数据库连接串。此目录已被项目 `.gitignore` 的 `var/` 规则排除。
不要重建、清空此目录，也不要将任何真实库地址赋给 `TEST_DATABASE_URL`。

安装 Homebrew formula 会建立 `/opt/homebrew/var/postgresql@17` 默认空集群；
本项目不会启动或使用它。请不要为本项目运行 `brew services start postgresql@17`。

## 运行采集程序

通过包装入口把数据库地址传给子进程，避免把密码复制到命令行或终端历史：

```sh
.venv/bin/python scripts/local_postgres.py xhs -- status
XHS_ADB_SERIAL='127.0.0.1:6100' .venv/bin/python scripts/local_postgres.py xhs -- collect-current --device lab01
```

`xhs` 后参数与普通 `xhs-mobile` 一致；需要使用其他配置时，在子命令之前传
`--config`，且该配置的 `database_url_env` 仍须为 `XHS_DATABASE_URL`。
包装入口先核对数据库身份，再释放管理锁，不会持有管理锁阻挡长任务期间
的状态查询。实验室仍负责启动真实采集，且正式采集需要已验证的真实页面规则。
数据库准备成功并不证明手机页面校准或 30 篇实机验收已完成。

此脚本是本机源项目的部署工具，不是 wheel 中的通用数据库服务。常规 CLI 也可由
受控环境设置 `XHS_DATABASE_URL` 连接此库；不要打印或提交该环境变量。

## 停止、重启与备份

```sh
.venv/bin/python scripts/local_postgres.py stop
.venv/bin/python scripts/local_postgres.py start
.venv/bin/python scripts/local_postgres.py backup
```

停止前先正常暂停采集进程；`stop` 使用 PostgreSQL fast shutdown，保留数据目录。
异常退出后再次 `start` 会让 PostgreSQL 正常执行 WAL 恢复。脚本验证集群归属、
主版本、私有文件权限、回环监听和 SCRAM 配置，拒绝管理不符合这些条件的目录。
脚本不提供 `reset`、`cleanup`、`delete` 或覆盖恢复命令。

`backup` 在 `var/postgres-live/backups/` 产生 custom-format 数据库转储和 SHA-256
清单，先校验 `pg_restore --list` 成功，再发布完成文件。失败的临时转储不记为备份。
**这个命令仅备份数据库，不包含截图和 UI 树。** 完整项目备份应先暂停所有采集，
确认工作进程停止，再执行数据库备份，并另外复制以下内容到可靠的备份介质：

- `var/evidence/`（原始截图、UI 树与哈希清单）；
- 所选数据库 `.dump` 与配套 `.json` 清单；
- `profiles/*.local.toml` 与运行配置、程序版本记录。

私密凭据另行安全保存，不放入共享的研究成果包。数据库备份应由 `pg_restore`
还原到**新建的隔离数据库**演练，并核对迁移版本、记录数量与证据哈希；不能只凭
转储文件存在就认定灾难恢复已验证。不要用 `pg_restore --clean` 指向正在采集的库。
复制运行中的 PostgreSQL `data/` 目录不是本方案的备份方法。

## 自动启动

当前默认采用手动 `start`，数据库进程可独立于 Codex 和启动终端继续运行。
如果后续需要登录 Mac 后自动启动，可为本项目单独配置用户 LaunchAgent，运行
`postgres -D <项目绝对路径>/var/postgres-live/data` 并写入本项目日志。
用户 LaunchAgent 是**登录后启动**，不等同于系统开机前启动。
此轮不会创建自动启动服务，避免与手动管理或 Homebrew 默认集群混淆。

## 版本与验证边界

2026-09-13 核查：本机为 macOS 26.6.2 arm64，Homebrew 提供 PostgreSQL **17.11**
及匹配的 arm64 Tahoe bottle。项目冻结迁移只使用标准 PostgreSQL 类型、索引、
外键和约束，不需要额外数据库扩展。官方建议使用所在主版本的当前次版本：
[PostgreSQL versioning](https://www.postgresql.org/support/versioning/)。

`.tools/pgserver` 中旧 PostgreSQL 16.2 仅保留为此前隔离测试工具，不作为本机真实库。
本机 PostgreSQL 17 的实际启动、迁移、停启后保留与备份检查结果，应以运行报告为准；
不把离线管理脚本测试或旧 16.2 集成测试替代这些验证。

本轮已实测：17.11 成功启动且仅监听 `127.0.0.1:55432`；HBA 只有一条回环 SCRAM
规则且无解析错误；迁移至 `0001`；停止再启动后 PostgreSQL 集群标识及迁移均保留；
数据库转储已通过 `pg_restore --list` 校验；独立包装入口 `xhs -- status` 返回空任务列表。
20 项管理边界离线测试及该脚本 Ruff 检查通过。五张业务表当前均为零行，未写入测试
任务或虚假真实成果。完整还原到隔离数据库尚未演练。
本轮报告保存在 `var/diagnostics/2026-09-13-postgres17-live-readiness.json`。
