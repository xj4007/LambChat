# Docker Deployment

Use Docker Compose for a single application replica on one host. For multi-replica or distributed production deployments, use Kubernetes or another orchestrator with shared MongoDB, Redis, and S3-compatible object storage.

## Quick Start

```bash
# Clone the repository
git clone https://github.com/Yanyutin753/LambChat.git
cd LambChat

# Compose reads this file from its project directory: deploy/
cp deploy/.env.example deploy/.env
# Edit deploy/.env. JWT_SECRET_KEY and MCP_ENCRYPTION_SALT are required.

# Build the image, initialize writable mounts, and start all services.
bash deploy/deploy.sh
```

The deployment script runs from `deploy/`, prepares ownership for the non-root application container, and then starts the stack. Use it instead of bypassing the deployment script with direct Compose startup commands.

## Architecture

Docker Compose starts three services for a single-node stack:

| Service | Image | Host port | Description |
|---------|-------|-----------|-------------|
| `lambchat` | Custom build: `lambchat:current` | `8000` | LambChat application (FastAPI + static frontend) |
| `mongodb` | `mongo:8.2.5` | `127.0.0.1:27017` | MongoDB database; host-only binding |
| `redis` | `redis:alpine` | `127.0.0.1:6379` | Redis cache and pub/sub; host-only binding |

The Compose containers are named `lambchat`, `lambchat-mongodb`, and `lambchat-redis`.

## Configuration

### Environment Variables

Copy `deploy/.env.example` to `deploy/.env` and configure the file there:

```dotenv
# Required stable secrets. Generate each value once and keep it across upgrades.
JWT_SECRET_KEY=your-stable-secret-key
MCP_ENCRYPTION_SALT=your-stable-encryption-salt

# Optional E2B sandbox configuration
E2B_API_KEY=
E2B_TEMPLATE=base
```

`JWT_SECRET_KEY` and `MCP_ENCRYPTION_SALT` are required by `deploy/docker-compose.yml`; Compose does not auto-generate them. Do not regenerate them during an upgrade, or existing sessions and encrypted MCP configuration may stop working. Generate strong values once, for example with `openssl rand -hex 32`.

The application connects to the Compose services internally through `mongodb://mongodb:27017` and `redis://redis:6379/0`. Do not replace these container hostnames with `localhost` inside the application container.

::: tip
LLM models are configured through the **Model Config UI** after deployment — no environment variables needed. See [LLM Configuration](/en/env/llm) for details.
:::

See [Environment Variables](/en/env/app) for the complete application reference.

## Opt-in Local Docker Sandboxes

The normal Compose file is deliberately socket-free. Only a trusted administrator should enable local Docker sandboxes because access to `/var/run/docker.sock` is equivalent to host-root control of the Docker Engine.

### Run from the host or from source

The Python Docker SDK uses the deployment process environment (`DOCKER_HOST`, and TLS/certificate variables when a secured endpoint is configured):

```python
import docker

client = docker.from_env()
assert client.ping()
```

This works with a local Linux Docker Engine and with Docker Desktop when the daemon is reachable from the process. `tcp://` and `http://` endpoints are rejected unless `DOCKER_TLS_VERIFY` is non-empty. Do not expose an unauthenticated port 2375. These transport variables are never saved in settings or passed to child containers.

### Compose opt-in override

Keep the default deployment unchanged and opt in explicitly on a Linux host:

```bash
cd deploy
export DOCKER_GID="$(stat -c '%g' /var/run/docker.sock)"
export COMPOSE_FILE=docker-compose.yml:docker-compose.docker-sandbox.yml
docker compose config
bash ./deploy.sh
```

The override affects only `lambchat`, sets `DOCKER_HOST=unix:///var/run/docker.sock`, adds the socket bind, requires `DOCKER_GID`, and keeps the application user `app` non-root. The socket is not passed to any sandbox child container. Do not enable this override for an untrusted multi-tenant deployment.

The Docker daemon must report Linux `OSType`, enabled memory and swap limits, CPU CFS quota, PID limits, and a `seccomp` security option. LambChat probes these capabilities before admission and fails closed instead of pretending resource limits are active. `DOCKER_SANDBOX_MEMORY_LIMIT_MB` is a hard per-container limit; Docker has no portable writable-layer quota, so monitor the daemon data-root and configure host disk alerts.

### Namespace and lifecycle operations

`DOCKER_SANDBOX_NAMESPACE` is unique to one LambChat deployment on a daemon. A second instance must use a different namespace; one namespace is not supported by multiple replicas because shutdown and janitor operations manage every matching container. Changing platform or namespace is a restart-required clean cutover. After a hard crash, manually inspect and remove old resources using the old namespace plus `io.lambchat.sandbox.managed=true` and `io.lambchat.sandbox.platform=docker` labels.

Each user reuses one non-root container across sessions. Each session gets a separate directory under `/tmp/lambchat-workspace`; the public `/workspace/...` path is mapped by the existing lazy backend. Normal shutdown stops the container and keeps its writable layer. The janitor removes idle containers after `DOCKER_SANDBOX_IDLE_TIMEOUT`, permanently deleting temporary files. There are no named volumes or host mounts for child sandboxes.

`bridge` creates one non-internal, non-attachable user-defined bridge for each container. The container never joins the default bridge, a Compose network, or host networking, and no port is published. Set `DOCKER_SANDBOX_NETWORK_MODE=none` when the sandbox must have no network. Plan the worst-case memory envelope as `DOCKER_SANDBOX_MAX_CONTAINERS × DOCKER_SANDBOX_MEMORY_LIMIT_MB`, leaving capacity for LambChat, MongoDB, Redis, and the host.

The default image is a convenience value, not a production pin. Prefer an immutable digest. A custom image must provide `/bin/sh`, `python3 -m pip`, GNU `timeout`, `sleep`, `mkdir`, `stat`, `find`, `grep`, `head`, `tail`, `cat`, `wc`, `tr`, and `rm`, and must allow UID/GID `65534:65534` to write `/tmp/lambchat-workspace`. The command contract runs `sh -lc` as that UID with a writable rootfs, no capabilities, and `no-new-privileges`; system packages must be built into the image rather than installed with root or `apt` at runtime.

### Reverse Proxy

For production, use a reverse proxy (nginx, Traefik, Caddy) with SSL:

**nginx example:**

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

        # SSE support
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400s;
    }
}
```

If the application needs a public base URL, add `- APP_BASE_URL=${APP_BASE_URL:-}` to the `lambchat.environment` section in `deploy/docker-compose.yml`, set `APP_BASE_URL=https://lambchat.example.com` in `deploy/.env`, and run `bash deploy/deploy.sh`.

## Managing the Stack

Run stack commands from the Compose project directory so `.env` is loaded from `deploy/`:

```bash
cd deploy

# Build, fix non-root mount ownership, and start services
bash ./deploy.sh

# View status and logs
docker compose ps
docker compose logs -f --tail=200 lambchat

# Restart the application while preserving data
docker compose restart lambchat

# Stop services; named volumes are retained
docker compose down

# Rebuild after code or Dockerfile changes
bash ./deploy.sh
```

Do not bypass `bash ./deploy.sh`: the script initializes ownership for `/app/data`, `/app/workspace`, and `/app/uploads` before the non-root application starts.

## Data Persistence

The Compose file uses these named volumes:

- `mongodb-data` — MongoDB data
- `redis-data` — application Redis data
- `lamb-data` — application data mounted at `/app/data`

It also uses bind mounts relative to `deploy/`:

- `deploy/uploads` — uploaded files mounted at `/app/uploads`
- `deploy/workspace` — workspace files mounted at `/app/workspace`

Named volumes persist across container restarts and recreations. The deployment script fixes ownership of all three application mounts because the `lambchat` container runs as the non-root `app` user.

Do not scale the `lambchat` Compose service to multiple replicas while using the local `uploads` bind mount. Multiple replicas need shared object storage (`S3_ENABLED=true`) and a load-balanced service without fixed container names or host port conflicts.

`docker compose down -v` removes the named volumes and their data. Use it only after making a backup.
