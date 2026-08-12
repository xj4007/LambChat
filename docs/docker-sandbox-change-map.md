# Docker Sandbox 变更地图

> 用途：后续合并其他分支时，先阅读本文件，再按“冲突处理约定”合并 Docker Sandbox 相关改动。本文只描述本分支新增/修改的 Docker Sandbox 合约，不替代各文件中的实现注释和测试。
>
> 分支：`sanyun-main`
> 功能状态：已在 Linux Docker Engine 服务器完成真实容器 smoke 验证。
> 当前实现版本：`DOCKER_SANDBOX_CONTRACT_VERSION = "1"`。

## 1. 功能摘要

LambChat 新增第四种 Sandbox 平台字面量：`docker`。启用后，应用通过 Docker Python SDK 调用宿主机 Docker Engine，为每个用户创建一个非 root 容器，并在同一容器内为每个会话使用独立工作目录。

主要行为：

- 设置链路支持 `SANDBOX_PLATFORM=docker` 和 11 个 `DOCKER_SANDBOX_*` 参数。
- Search Agent 继续懒初始化；Team Agent 等既有 Agent 路径继续使用平台无关的 Sandbox/CompositeBackend 接口。
- 同一用户跨会话复用同一个 Sandbox 容器；会话目录相互隔离。
- 默认 30 分钟空闲回收；默认 namespace 容量上限 20 个容器。
- 命令执行具有超时、输出上限、取消后恢复和 stop/kill 兜底。
- `bridge` 模式为每个子容器创建独立 user-defined bridge；`none` 模式完全禁用子容器网络。
- 子容器固定 `65534:65534`，丢弃全部 capabilities，启用 `no-new-privileges`，不挂载宿主机路径、不发布端口、不使用 privileged/host network。
- 普通 Compose 默认不接触 Docker daemon；只有显式使用 `docker-compose.docker-sandbox.yml` 覆盖文件时才挂载 Docker Unix socket。
- Docker socket 权限等价于主机 root，只适用于受信任管理员控制的单机部署。

## 2. 设置与配置合约

### 设置元数据和校验

| 设置 | 默认值 | 约束/用途 |
|---|---:|---|
| `DOCKER_SANDBOX_NAMESPACE` | `default` | `[a-z0-9][a-z0-9_.-]{0,31}`；同一 daemon 上不同部署必须唯一；修改需重启 |
| `DOCKER_SANDBOX_IMAGE` | `python:3.12-slim-bookworm` | 非空且无首尾空白；生产建议使用不可变 digest |
| `DOCKER_SANDBOX_TIMEOUT` | `180` | 单命令超时秒数，`1..86400` |
| `DOCKER_SANDBOX_IDLE_TIMEOUT` | `1800` | 空闲容器删除秒数，`60..604800` |
| `DOCKER_SANDBOX_CLEANUP_INTERVAL` | `60` | janitor 扫描间隔，`10..3600` |
| `DOCKER_SANDBOX_MAX_CONTAINERS` | `20` | namespace 容器容量，`1..100` |
| `DOCKER_SANDBOX_MEMORY_LIMIT_MB` | `1024` | 每容器硬内存限制，`128..32768` MB |
| `DOCKER_SANDBOX_CPU_LIMIT` | `1.0` | CPU 核数，`0.1..64` |
| `DOCKER_SANDBOX_PIDS_LIMIT` | `256` | 每容器 PID 上限，`16..4096` |
| `DOCKER_SANDBOX_NETWORK_MODE` | `bridge` | 仅允许 `bridge` 或 `none` |
| `DOCKER_SANDBOX_MAX_OUTPUT_BYTES` | `10485760` | 单命令捕获上限，`1 MiB..100 MiB` |

`SANDBOX_PLATFORM` 选项顺序固定为：

```text
["daytona", "e2b", "cubesandbox", "docker"]
```

`SANDBOX_PLATFORM` 与 `DOCKER_SANDBOX_NAMESPACE` 属于重启必需设置。数据库设置优先于 `.env`，因此服务器 `.env` 中的 namespace 可能被数据库中旧值覆盖；排查实际运行值时应同时查看容器环境、应用日志和 Docker 标签。

共享校验唯一入口：

```text
src/kernel/config/docker_sandbox.py
```

不要在 Settings 定义、SettingsStorage、运行时配置映射或 Docker adapter 中复制第二套范围或隐式 trim/clamp 逻辑。

## 3. 代码文件地图

### 配置与管理界面

| 文件 | 责任 | 合并注意 |
|---|---|---|
| `src/kernel/config/docker_sandbox.py` | Docker 默认值、选项、范围、纯校验函数、契约版本 | 与任何新 Sandbox 设置合并时保留这里作为唯一 validator 来源 |
| `src/kernel/config/_definitions_sandbox.py` | `SANDBOX_PLATFORM` 的 `docker` 选项及 Docker 11 项管理元数据 | 保留 `depends_on: SANDBOX_PLATFORM=docker` 和 `subcategory: docker` |
| `src/kernel/config/base.py` | `Settings` 字段、Pydantic Docker 校验、最终配置防御性校验 | 不要让平台切换绕过重启要求 |
| `src/kernel/config/constants.py` | `RESTART_REQUIRED_SETTINGS` 中的平台/namespace 设置 | 平台在 manager 构造时固定，不能热切换当前 manager |
| `src/kernel/config/__init__.py` | 导出 Docker 配置常量和校验函数 | 新增导出时保持现有 config API |
| `src/infra/settings/storage.py` | 数据库存储前调用共享 Docker validator | 先校验再持久化 |
| `frontend/src/components/panels/SettingsPanel.tsx` | `docker` subcategory 标签、restart-required 提示 | 复用通用 Settings 链路，不新增 Docker 专用控件 |
| `frontend/src/i18n/locales/{en,zh,ja,ko,ru}.json` | Docker 字段描述、Docker subcategory、重启提示 | 五个 locale 的 key 集合必须保持一致 |

### Docker Engine 边界和执行后端

| 文件 | 责任 | 合并注意 |
|---|---|---|
| `src/infra/sandbox/_docker_adapter.py` | Docker SDK 唯一边界：client、daemon 探针、镜像、网络、容器、标签、生命周期、operation state、回收 | 不要在其他模块直接导入 Docker SDK；不放宽 transport 或容器安全参数 |
| `src/infra/backend/docker.py` | DeepAgents `BaseSandbox`：execute/aexecute、grep、tar 文件上传/下载、输出限制、超时恢复 | 保持构造签名和 `/tmp/lambchat-workspace` 实际路径语义；public `/workspace` 映射由既有 lazy backend 处理 |
| `src/infra/backend/protocol_compat.py` | DeepAgents v0.7 文件响应兼容类型和扩展错误字面量 | Docker 文件传输错误必须使用现有协议兼容层 |
| `src/infra/sandbox/base.py` | `DockerSandboxConfig`、Settings 到 Docker config 映射、SandboxFactory Docker dispatch、registry close callback | registry 使用 `_SandboxRegistration`，不要恢复旧 tuple 结构 |

### Manager 集成

| 文件 | 责任 | 合并注意 |
|---|---|---|
| `src/infra/sandbox/_docker_helpers.py` | `_DockerMixin`：每用户 cache/binding 恢复、创建、session workdir、env sync、stop、cleanup 辅助 | 依赖 `SessionSandboxManager` 的 `_bindings`、`_cache`、locks 和工作目录方法 |
| `src/infra/sandbox/session_manager.py` | 固定平台 dispatch、Docker adapter 初始化、janitor loop、stale/orphan cleanup、close_all clean cutover | namespace 在 manager 构造时固定；Docker binding 使用 `sandboxes.docker` nested slot |
| `src/infra/sandbox/__init__.py` | 导出 Docker config、Docker factory mapping、manager API | 保留既有平台导出 |
| `src/api/main.py` | settings 初始化后启动 Docker janitor；失败只告警，不拖垮 API startup | 仅 `ENABLE_SANDBOX=true` 且平台为 docker 时启动 |

### 部署与文档

| 文件 | 责任 | 合并注意 |
|---|---|---|
| `deploy/docker-compose.yml` | 基础 Compose：传递 Docker 设置但默认 socket-free | 任何普通部署改动不得把 `/var/run/docker.sock` 放入基础文件 |
| `deploy/docker-compose.docker-sandbox.yml` | opt-in override：socket、`DOCKER_HOST`、`DOCKER_GID`、非 root app | 只合并 `lambchat`；不要扩展给 MongoDB/Redis 或子容器 |
| `deploy/.env.example`、`.env.example` | Docker 设置示例和 opt-in 说明 | 不写真实 credentials；保留 socket-free 默认 |
| `deploy/deploy.sh` | 构建、目录准备、权限修复、Compose 启动 | 保留 `/app/data`、`/app/workspace`、`/app/uploads` 的 root chown 步骤 |
| `Dockerfile` | 运行镜像包含 Docker Python SDK，不安装 Docker CLI | `uv.lock` 和 `pyproject.toml` 必须一起合并 |
| `docs/en/env/sandbox.md`、`docs/zh/env/sandbox.md` | Sandbox 平台、目录、网络、清理和前置条件 | 明确 socket 等价 root、namespace 唯一、空闲删除丢文件 |
| `docs/en/deploy/docker.md`、`docs/zh/deploy/docker.md` | Linux Docker Engine/Compose opt-in 运维说明 | 普通部署和 Docker override 分开描述 |
| `README.md`、`docs/{en,zh}/getting-started.md`、`docs/{en,zh}/index.md` | 支持平台总览 | 只合并 Docker 平台描述，不覆盖其他分支的新文案 |

## 4. 容器与标签合约

容器名称：

```text
lambchat-sbx-<namespace>-<owner_hash[:12]>-<uuid[:12]>
```

固定 labels：

```text
io.lambchat.sandbox.managed=true
io.lambchat.sandbox.platform=docker
io.lambchat.sandbox.namespace=<namespace>
io.lambchat.sandbox.owner_hash=<sha256>
io.lambchat.sandbox.config_hash=<container_config_hash>
io.lambchat.sandbox.container_token=<uuid>
io.lambchat.sandbox.created_at=<UTC ISO-8601>
```

创建期安全契约包括：非 root UID、capabilities、security option、资源限制、网络模式、工作目录、固定环境变量和 keepalive command。若这些创建期约束变化，必须递增 `DOCKER_SANDBOX_CONTRACT_VERSION`，使旧容器不再兼容并触发重建路径。

## 5. 部署使用方式

普通部署：

```bash
docker compose -f docker-compose.yml config
```

预期：不包含 `/var/run/docker.sock`。

Docker Sandbox 部署：

```bash
export DOCKER_GID="$(stat -c '%g' /var/run/docker.sock)"
export COMPOSE_FILE=docker-compose.yml:docker-compose.docker-sandbox.yml
docker compose config
docker compose up -d --no-build
```

override 会给 LambChat 进程：

```text
DOCKER_HOST=unix:///var/run/docker.sock
/var/run/docker.sock:/var/run/docker.sock
group_add=<Docker socket group>
user=app
```

这等价于向应用进程授予 Docker daemon 的主机级控制能力，只能用于受信任的管理员测试环境。

## 6. 合并冲突处理约定

1. **先保留功能合约，再合并文案。** 如果 `base.py`、`session_manager.py`、Compose 或设置定义发生冲突，先按本文第 2、4 节恢复 Docker Sandbox 合约，再合并其他分支的业务改动。
2. **配置冲突：** `docker_sandbox.py` 是默认值/范围唯一来源；`_definitions_sandbox.py` 只负责 UI 元数据；`SettingsStorage` 只调用共享 validator。不要把 Docker 字段改回分散常量。
3. **平台 dispatch 冲突：** `SessionSandboxManager._platform` 必须在构造时固定；Docker 分支不能落入 Daytona 默认分支；未知平台必须显式报错。
4. **生命周期冲突：** `DockerSandboxAdapter` 必须保留 operation state gate。回收/恢复期间的新命令不能抢跑；active operation 不能被 janitor 删除。
5. **安全冲突：** 不接受把 Docker socket 放入基础 Compose、改为明文 `tcp://2375`、添加 `privileged`、host network、宿主路径 mount、端口发布或 root 子容器。
6. **路径冲突：** `/workspace/...` 是应用侧 public path；Docker backend 实际使用 `/tmp/lambchat-workspace/...`。不要为了另一分支的路径假设给子容器增加宿主挂载。
7. **测试冲突：** 保留 Docker 配置、adapter、backend、Compose 安全和 manager 测试；真实 Docker 测试必须显式设置 `RUN_DOCKER_SANDBOX_INTEGRATION=1`，并在 finally 中清理带测试 namespace 的容器和网络。
8. **锁文件冲突：** `pyproject.toml` 的 Docker SDK 依赖必须与 `uv.lock` 同步；不要只合并其中一个文件。
9. **前端 locale 冲突：** 五个 locale 必须同时拥有相同 Docker keys；SettingsPanel 继续走通用 options/depends_on 机制。
10. **服务器配置冲突：** 服务器 `/opt/lambchat/deploy/.env` 不属于 Git 提交；更新部署时不得上传覆盖该文件。数据库设置优先于 `.env`，实际值以运行容器环境和 Docker labels 为准。

## 7. 验证入口

后端 Docker 聚焦测试：

```bash
uv run pytest \
  tests/kernel/config/test_sandbox_setting_definitions.py \
  tests/infra/backend/test_docker_backend.py \
  tests/infra/test_docker_sandbox_adapter.py \
  tests/infra/settings/test_settings_storage.py \
  tests/infra/test_session_sandbox_manager.py \
  tests/infra/test_sandbox_factory.py \
  tests/agents/test_search_agent_lazy_sandbox.py \
  tests/deploy/test_docker_compose_sandbox_env.py -v
```

真实 Engine 集成测试：

```bash
RUN_DOCKER_SANDBOX_INTEGRATION=1 uv run pytest \
  tests/infra/backend/test_docker_sandbox_integration.py -v
```

本次服务器真实验证已覆盖容器创建、命令执行、文件传输、会话隔离、取消恢复、stop/start、容器安全属性和清理。
