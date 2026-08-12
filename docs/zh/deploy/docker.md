# Docker 部署

Docker Compose 适合在单台主机上运行单个应用副本。多副本或分布式生产部署请使用 Kubernetes 或其他编排系统，并配置共享 MongoDB、Redis 和 S3 兼容对象存储。

## 快速开始

```bash
# 克隆仓库
git clone https://github.com/Yanyutin753/LambChat.git
cd LambChat

# Compose 会从项目目录 deploy/ 读取环境文件
cp deploy/.env.example deploy/.env
# 编辑 deploy/.env；JWT_SECRET_KEY 和 MCP_ENCRYPTION_SALT 必须配置

# 构建镜像、初始化可写目录权限并启动全部服务
bash deploy/deploy.sh
```

部署脚本会切换到 `deploy/` 目录，以非 root 应用用户修复挂载目录权限，然后启动服务。不要绕过部署脚本直接启动 Compose，否则可能跳过权限初始化。

## 架构

Docker Compose 为单节点服务栈启动三个服务：

| 服务 | 镜像 | 主机端口 | 说明 |
|------|------|----------|------|
| `lambchat` | 自定义构建：`lambchat:current` | `8000` | LambChat 应用（FastAPI + 静态前端） |
| `mongodb` | `mongo:8.2.5` | `127.0.0.1:27017` | MongoDB 数据库，仅绑定主机回环地址 |
| `redis` | `redis:alpine` | `127.0.0.1:6379` | Redis 缓存和发布/订阅，仅绑定主机回环地址 |

Compose 容器名称分别为 `lambchat`、`lambchat-mongodb` 和 `lambchat-redis`。

## 配置

### 环境变量

将 `deploy/.env.example` 复制为 `deploy/.env`，并在该文件中配置：

```dotenv
# 必需的稳定密钥。每个值只生成一次，并在升级时保持不变。
JWT_SECRET_KEY=your-stable-secret-key
MCP_ENCRYPTION_SALT=your-stable-encryption-salt

# 可选：E2B Sandbox 配置
E2B_API_KEY=
E2B_TEMPLATE=base
```

`deploy/docker-compose.yml` 要求必须提供 `JWT_SECRET_KEY` 和 `MCP_ENCRYPTION_SALT`；Compose 不会自动生成这两个值。升级时不要重新生成，否则已有登录会话和已加密的 MCP 配置可能失效。建议使用 `openssl rand -hex 32` 生成强随机值，并只生成一次。

应用在容器内部通过 `mongodb://mongodb:27017` 和 `redis://redis:6379/0` 连接 Compose 服务。不要在应用容器内将这些主机名改成 `localhost`。

::: tip
LLM 模型通过部署后的 **模型配置 UI** 添加，无需在环境变量中配置。详见[模型配置](/zh/env/llm)。
:::

完整参考见[环境变量](/zh/env/app)。

## 显式启用本地 Docker 沙箱

基础 Compose 文件默认不包含 Docker socket。只有受信任管理员才应启用本地 Docker 沙箱，因为访问 `/var/run/docker.sock` 等价于获得主机 Docker Engine 的 root 级控制能力。

### 宿主机源码运行

Python Docker SDK 使用部署进程环境中的 `DOCKER_HOST` 以及 TLS/证书变量连接 daemon：

```python
import docker

client = docker.from_env()
assert client.ping()
```

本地 Linux Docker Engine 和 Docker Desktop 均可使用，只要该进程能访问 daemon。`tcp://` 或 `http://` endpoint 在 `DOCKER_TLS_VERIFY` 为空时会被拒绝。不要暴露未认证的 2375 端口。上述传输变量不会保存到数据库设置，也不会传给子容器。

### Compose opt-in override

在 Linux 宿主机上显式合并 override，普通部署保持不变：

```bash
cd deploy
export DOCKER_GID="$(stat -c '%g' /var/run/docker.sock)"
export COMPOSE_FILE=docker-compose.yml:docker-compose.docker-sandbox.yml
docker compose config
bash ./deploy.sh
```

override 只修改 `lambchat` 服务，设置 `DOCKER_HOST=unix:///var/run/docker.sock`、挂载 socket、强制要求 `DOCKER_GID`，并保留非 root 的 `app` 用户。socket 不会传给任何沙箱子容器。不可信多租户部署不要启用该 override。

Docker daemon 启动探针必须报告 Linux `OSType`，并启用 memory/swap 限制、CPU CFS quota、PID 限制和 `seccomp` 安全选项。LambChat 缺少任一能力时拒绝创建，不假装资源限制已生效。`DOCKER_SANDBOX_MEMORY_LIMIT_MB` 是每容器 hard limit；Docker 没有跨 storage driver 通用的可写层配额，必须监控 data-root 并配置宿主机磁盘告警。

### Namespace 与生命周期

同一 daemon 上的 `DOCKER_SANDBOX_NAMESPACE` 必须只属于一个 LambChat 部署。第二个实例要使用不同 namespace；不支持同 namespace 多副本，因为关闭和 janitor 会管理该 namespace 下的全部受管容器。切换平台或 namespace 需要重启并执行 clean cutover。硬崩溃后按旧 namespace 以及 `io.lambchat.sandbox.managed=true`、`io.lambchat.sandbox.platform=docker` 标签手工检查和清理遗留资源。

每个用户跨会话复用一个非 root 容器，每个会话在 `/tmp/lambchat-workspace` 下使用独立目录；既有 lazy backend 将公开 `/workspace/...` 映射到实际目录。正常关闭只停止容器并保留可写层；janitor 在 `DOCKER_SANDBOX_IDLE_TIMEOUT` 后删除空闲容器，临时文件永久丢失。子容器不使用 named volume 或宿主机目录挂载。

`bridge` 为每个容器创建一个 `internal=false`、`attachable=false` 的 user-defined bridge。容器不会加入 default bridge、Compose 网络或 host 网络，也不发布端口。需要严格无网时设置 `DOCKER_SANDBOX_NETWORK_MODE=none`。宿主机最坏内存按 `DOCKER_SANDBOX_MAX_CONTAINERS × DOCKER_SANDBOX_MEMORY_LIMIT_MB` 规划，并为 LambChat、MongoDB、Redis 和宿主机预留余量。

默认镜像只是开箱方便值，不是生产 pin；生产环境优先使用不可变 digest。自定义镜像必须提供 `/bin/sh`、`python3 -m pip`、GNU `timeout`、`sleep`、`mkdir`、`stat`、`find`、`grep`、`head`、`tail`、`cat`、`wc`、`tr`、`rm`，并允许 UID/GID `65534:65534` 写入 `/tmp/lambchat-workspace`。命令契约以该 UID 执行 `sh -lc`，rootfs 可写但无 capabilities 且启用 `no-new-privileges`；系统依赖应预构建进镜像，运行时不得授予 root 或执行 apt。

### 反向代理

生产环境建议使用反向代理（nginx、Traefik、Caddy）并配置 SSL：

**nginx 示例：**

```nginx
server {
    listen 443 ssl http2;
    server_name lambchat.example.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    client_max_body_size 100M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE 支持
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400s;
    }
}
```

如果应用需要公开访问的基础 URL，请在 `deploy/docker-compose.yml` 的 `lambchat.environment` 中加入 `- APP_BASE_URL=${APP_BASE_URL:-}`，在 `deploy/.env` 中设置 `APP_BASE_URL=https://lambchat.example.com`，然后执行 `bash deploy/deploy.sh`。

## 管理服务栈

在 Compose 项目目录中执行命令，确保从 `deploy/` 加载 `.env`：

```bash
cd deploy

# 构建镜像、修复非 root 挂载目录权限并启动服务
bash ./deploy.sh

# 查看状态和日志
docker compose ps
docker compose logs -f --tail=200 lambchat

# 重启应用（保留数据）
docker compose restart lambchat

# 停止服务；命名卷会保留
docker compose down

# 代码或 Dockerfile 变更后重新构建并发布
bash ./deploy.sh
```

不要绕过 `bash ./deploy.sh`：部署脚本会在非 root 应用启动前，初始化 `/app/data`、`/app/workspace` 和 `/app/uploads` 的所有权。

## 数据持久化

Compose 文件使用以下命名卷：

- `mongodb-data` — MongoDB 数据
- `redis-data` — Redis 应用数据
- `lamb-data` — 挂载到 `/app/data` 的应用数据

此外还使用相对于 `deploy/` 的绑定挂载：

- `deploy/uploads` — 挂载到 `/app/uploads` 的上传文件
- `deploy/workspace` — 挂载到 `/app/workspace` 的工作区文件

命名卷在容器重启和重建时保持不变。由于 `lambchat` 容器以非 root 的 `app` 用户运行，部署脚本会修复三个应用挂载目录的所有权。

使用本地 `uploads` 绑定挂载时，不要把 `lambchat` Compose 服务扩展为多个副本。多副本部署需要共享对象存储（`S3_ENABLED=true`），并使用不会冲突的固定容器名和主机端口的负载均衡服务。

`docker compose down -v` 会删除命名卷及其中的数据。只有完成备份后才可执行。
