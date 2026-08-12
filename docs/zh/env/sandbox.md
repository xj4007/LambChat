# 沙箱配置

用于安全代码执行的沙箱设置。支持 Daytona、E2B、CubeSandbox 和本地 Docker Engine。

## 通用设置

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `ENABLE_SANDBOX` | `false` | 启用沙箱执行。 |
| `SANDBOX_PLATFORM` | `daytona` | 沙箱平台：`daytona`、`e2b`、`cubesandbox` 或 `docker`。修改后需要重启服务。 |
| `SANDBOX_GREP_TIMEOUT` | `30` | 沙箱 grep 命令超时时间（秒）。 |

## 平台与工作目录语义

| 平台 | 运行时 | 绑定与工作目录 |
|------|--------|----------------|
| `daytona` | Daytona API | 每个用户一个绑定，在对话和会话之间复用。 |
| `e2b` | E2B API | 每个用户一个绑定，在对话和会话之间复用。 |
| `cubesandbox` | CubeSandbox API | 每个用户一个绑定，在对话和会话之间复用。 |
| `docker` | 本地 Docker Engine | 每个用户一个非 root 容器；每个会话在该容器内使用独立目录。 |

所有平台都按用户复用绑定，并按会话隔离工作目录。Docker 对外稳定的 `/workspace/...` 会映射到容器内 `/tmp/lambchat-workspace` 下当前会话的目录。正常关闭服务只停止 Docker 容器；空闲超时后清理任务会删除容器及其临时文件。删除容器会永久丢失可写层，不使用 named volume 或宿主机 bind mount。

## Daytona

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `DAYTONA_API_KEY` | _(空)_ | 是 | Daytona API 密钥。 |
| `DAYTONA_SERVER_URL` | _(空)_ | 否 | Daytona 服务器 URL。 |
| `DAYTONA_TIMEOUT` | `180` | 否 | 命令超时时间（秒）。 |
| `DAYTONA_IMAGE` | _(空)_ | 否 | 使用的沙箱镜像/快照 ID。 |
| `DAYTONA_AUTO_STOP_INTERVAL` | `5` | 否 | 自动停止间隔（分钟）。 |
| `DAYTONA_AUTO_ARCHIVE_INTERVAL` | `5` | 否 | 自动归档间隔（分钟）。 |
| `DAYTONA_AUTO_DELETE_INTERVAL` | `1440` | 否 | 归档后自动删除间隔（分钟）。 |

## E2B

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `E2B_API_KEY` | _(空)_ | 是 | E2B API 密钥。 |
| `E2B_TEMPLATE` | `base` | 否 | 沙箱模板名称。 |
| `E2B_TIMEOUT` | `3600` | 否 | 沙箱超时时间（秒）。 |
| `E2B_AUTO_PAUSE` | `true` | 否 | 超时时暂停沙箱而非终止。 |
| `E2B_AUTO_RESUME` | `true` | 否 | 下次活动时自动恢复暂停的沙箱。 |

## CubeSandbox

CubeSandbox 使用原生 Python SDK。LambChat 按用户保存绑定，并在多个会话之间复用同一沙箱。

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `CUBE_API_URL` | `http://127.0.0.1:3000` | CubeSandbox API 地址；本地开发常用 `http://127.0.0.1:13000`。 |
| `CUBE_TEMPLATE` | _(空)_ | 创建沙箱使用的模板 ID。 |
| `CUBE_TIMEOUT` | `3600` | 命令超时时间（秒）。 |
| `CUBE_PROXY_NODE_IP` | _(空)_ | SDK 访问数据平面服务使用的代理节点 IP。 |
| `CUBE_PROXY_PORT_HTTP` | `80` | SDK 使用的 HTTP 代理端口。 |
| `CUBE_SANDBOX_DOMAIN` | `cube.app` | CubeSandbox 代理路由使用的域名后缀。 |
| `CUBE_REQUEST_TIMEOUT` | `120` | SDK 请求超时时间（秒）。 |
| `CUBE_AUTO_PAUSE` | `true` | 在运行时支持时，超时后请求暂停而不是终止。 |
| `CUBE_AUTO_RESUME` | `true` | 在运行时支持时，活动时自动恢复。 |

## 本地 Docker Engine

Docker 是显式启用的本地后端。普通部署不应接触 Docker daemon。运行 LambChat 的进程必须能访问 Linux Docker Engine，并提供 memory/swap、CPU、PID cgroup 限制和默认 seccomp；启动探针缺失这些隔离能力时会拒绝创建容器。

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `DOCKER_SANDBOX_NAMESPACE` | `default` | daemon 上隔离不同 LambChat 部署的命名空间；每个部署必须唯一。 |
| `DOCKER_SANDBOX_IMAGE` | `python:3.12-slim-bookworm` | 本地不存在时自动拉取；生产环境应使用不可变 digest。 |
| `DOCKER_SANDBOX_TIMEOUT` | `180` | 单命令最大执行秒数。 |
| `DOCKER_SANDBOX_IDLE_TIMEOUT` | `1800` | 空闲多少秒后删除容器和临时文件。 |
| `DOCKER_SANDBOX_CLEANUP_INTERVAL` | `60` | 清理扫描间隔（秒）。 |
| `DOCKER_SANDBOX_MAX_CONTAINERS` | `20` | 当前命名空间的受管容器上限。 |
| `DOCKER_SANDBOX_MEMORY_LIMIT_MB` | `1024` | 每容器硬内存和 swap 上限（MB）。 |
| `DOCKER_SANDBOX_CPU_LIMIT` | `1.0` | CPU 核数，转换为 Docker `nano_cpus`。 |
| `DOCKER_SANDBOX_PIDS_LIMIT` | `256` | 每容器 PID 上限。 |
| `DOCKER_SANDBOX_NETWORK_MODE` | `bridge` | `bridge` 为每个容器创建独立 user-defined bridge；`none` 完全禁用网络。 |
| `DOCKER_SANDBOX_MAX_OUTPUT_BYTES` | `10485760` | 单命令直接捕获输出上限（字节）。 |

容器固定以 UID/GID `65534:65534` 运行，删除全部 capabilities，启用 `no-new-privileges`，不开放端口、不挂载设备/目录/volume、不使用 host PID/IPC，也不传入 Docker socket。`bridge` 容器只连接自己的非 internal 网络，不加入 default bridge、Compose 网络或 host 网络；停止/启动会保留可写层和网络，删除会丢弃临时文件。

Docker socket 权限等价于主机 root。只有受信任的单机管理员才应启用 Compose override；不要为了绕过权限改用未认证的明文 TCP daemon。硬崩溃后请按旧 namespace 和 `io.lambchat.sandbox.managed=true` 标签手工清理遗留资源。`MAX_CONTAINERS × MEMORY_LIMIT_MB` 是宿主机最坏内存上界，Docker data-root 必须监控并配置磁盘告警。

## 示例

### Daytona（自托管）

```bash
ENABLE_SANDBOX=true
SANDBOX_PLATFORM=daytona
DAYTONA_API_KEY=your_daytona_api_key
DAYTONA_SERVER_URL=https://daytona.example.com
DAYTONA_TIMEOUT=180
```

### E2B（云服务）

```bash
ENABLE_SANDBOX=true
SANDBOX_PLATFORM=e2b
E2B_API_KEY=your_e2b_api_key
E2B_TEMPLATE=base
E2B_TIMEOUT=3600
```

### CubeSandbox（本地开发）

```bash
ENABLE_SANDBOX=true
SANDBOX_PLATFORM=cubesandbox
CUBE_API_URL=http://127.0.0.1:13000
CUBE_TEMPLATE=tpl-your-template-id
CUBE_PROXY_NODE_IP=127.0.0.1
CUBE_PROXY_PORT_HTTP=11080
CUBE_SANDBOX_DOMAIN=cube.app
CUBE_TIMEOUT=3600
CUBE_REQUEST_TIMEOUT=120
```

### Docker（本地 Engine）

```bash
ENABLE_SANDBOX=true
SANDBOX_PLATFORM=docker
DOCKER_SANDBOX_NAMESPACE=my-lambchat
DOCKER_SANDBOX_IMAGE=python:3.12-slim-bookworm
DOCKER_SANDBOX_NETWORK_MODE=bridge
```

切换平台或 namespace 是重启后的 clean cutover；同一 daemon 上的 namespace 不得被多个 LambChat 副本共享。需要严格无网时设置 `DOCKER_SANDBOX_NETWORK_MODE=none`。

::: info
`DAYTONA_AUTO_*_INTERVAL` 设置控制 Daytona 沙箱生命周期。Docker 则由 `DOCKER_SANDBOX_IDLE_TIMEOUT` 和 `DOCKER_SANDBOX_CLEANUP_INTERVAL` 控制空闲删除。
:::
