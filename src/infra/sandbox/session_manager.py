"""
User-Sandbox 绑定管理器

管理 User 与 Sandbox 的绑定关系，支持 Daytona、E2B、CubeSandbox 和 Docker 平台。
- 沙箱绑定关系存储在 MongoDB user_sandbox_bindings 集合中
- 每个用户对应一个沙箱，跨 session 共享
- Daytona/E2B/CubeSandbox 使用各自生命周期；Docker 空闲容器由 janitor 删除
- 使用 deepagents.CompositeBackend 组合 Sandbox 和 Skills Store

平台特定的生命周期逻辑分别放在 _daytona_helpers、_e2b_helpers、
_cubesandbox_helpers 和 _docker_helpers 模块中，通过 mixin 组合到本类。
"""

import asyncio
import contextlib
import re
import shlex
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional, cast

if TYPE_CHECKING:
    from cubesandbox import Sandbox as CubeSandbox
    from daytona import Daytona
    from e2b import Sandbox as E2BSandbox

from deepagents.backends import CompositeBackend
from deepagents.backends.protocol import SandboxBackendProtocol

from src.infra.async_utils import run_blocking_io
from src.infra.backend.daytona import DaytonaBackend
from src.infra.backend.skills_store import create_skills_backend
from src.infra.envvar.sync import sync_sandbox_env_vars
from src.infra.logging import get_logger
from src.infra.utils.datetime import utc_now_iso
from src.kernel.config import settings

from ._adapters import (
    _MAX_CACHE_ENTRIES,
    _MAX_LOCKS,
    _MAX_READY_WORK_DIRS,
    BINDING_COLLECTION,
    DEFAULT_DAYTONA_TIMEOUT,
    READY_STATES,
    RESUMABLE_STATES,
    TRANSITIONAL_STATES,
    UNAVAILABLE_STATES,
    CubeSandboxAdapter,
    E2BSandboxAdapter,
)
from ._cubesandbox_helpers import _CubeSandboxMixin
from ._daytona_helpers import _DaytonaMixin
from ._docker_adapter import CREATED_AT_LABEL, DockerSandboxAdapter
from ._docker_helpers import _DockerMixin
from ._e2b_helpers import _E2BMixin
from .base import get_docker_sandbox_config_from_settings

logger = get_logger(__name__)

# Re-export for backward compatibility (tests access sandbox_module.BINDING_COLLECTION)
__all__ = [
    "SessionSandboxManager",
    "close_session_sandbox_manager",
    "get_session_sandbox_manager",
]


class SessionSandboxManager(_DockerMixin, _DaytonaMixin, _E2BMixin, _CubeSandboxMixin):
    """管理 User 与 Sandbox 的绑定关系（每个用户一个沙箱，跨 session 共享）"""

    _index_task: asyncio.Task[None] | None = None
    _index_ensured = False

    def __init__(self):
        self._platform = settings.SANDBOX_PLATFORM.lower()
        if self._platform not in {"daytona", "e2b", "cubesandbox", "docker"}:
            raise ValueError(f"Unsupported sandbox platform: {self._platform}")
        self._daytona_client: Optional["Daytona"] = None
        self._e2b_adapter: Optional[E2BSandboxAdapter] = None
        self._cube_adapter: Optional[CubeSandboxAdapter] = None
        self._docker_adapter: Optional[DockerSandboxAdapter] = None
        self._docker_namespace = getattr(settings, "DOCKER_SANDBOX_NAMESPACE", "default")
        self._docker_cleanup_task: asyncio.Task[None] | None = None
        self._collection: Any = None
        self._cache: OrderedDict[str, tuple[str, CompositeBackend, object | None]] = OrderedDict()
        self._ready_work_dirs: OrderedDict[str, None] = OrderedDict()
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._locks_mutex = threading.Lock()
        if self._platform == "docker":
            docker_config = get_docker_sandbox_config_from_settings()
            self._docker_namespace = docker_config.namespace
            self._docker_adapter = DockerSandboxAdapter(docker_config)
        elif self._platform == "e2b":
            self._e2b_adapter = E2BSandboxAdapter(
                api_key=settings.E2B_API_KEY,
                template=settings.E2B_TEMPLATE,
                timeout=settings.E2B_TIMEOUT,
                auto_pause=getattr(settings, "E2B_AUTO_PAUSE", True),
                auto_resume=getattr(settings, "E2B_AUTO_RESUME", True),
            )
        elif self._platform == "cubesandbox":
            self._cube_adapter = CubeSandboxAdapter(
                api_url=settings.CUBE_API_URL,
                template=settings.CUBE_TEMPLATE,
                proxy_node_ip=settings.CUBE_PROXY_NODE_IP,
                proxy_port_http=settings.CUBE_PROXY_PORT_HTTP,
                sandbox_domain=settings.CUBE_SANDBOX_DOMAIN,
                timeout=settings.CUBE_TIMEOUT,
                request_timeout=settings.CUBE_REQUEST_TIMEOUT,
                auto_pause=getattr(settings, "CUBE_AUTO_PAUSE", True),
                auto_resume=getattr(settings, "CUBE_AUTO_RESUME", True),
            )

    # ── Infrastructure / state plumbing ─────────────────────────────

    @property
    def _bindings(self):
        """延迟加载 MongoDB 集合"""
        if self._collection is None:
            from src.infra.storage.mongodb import get_mongo_client

            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._collection = db[BINDING_COLLECTION]
            self._schedule_index()
        assert self._collection is not None
        return self._collection

    def _schedule_index(self) -> None:
        cls = type(self)
        if cls._index_ensured:
            return
        task = cls._index_task
        if task is not None and not task.done():
            return
        try:
            task = asyncio.create_task(self._ensure_index())
        except RuntimeError:
            return
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        cls._index_task = task
        cls._index_ensured = True

    async def _ensure_index(self):
        """异步创建索引"""
        try:
            await self._collection.create_index(
                "user_id",
                unique=True,
                name="user_id_unique_idx",
                background=True,
            )
        except Exception as e:
            logger.warning(f"Failed to create index on {BINDING_COLLECTION}: {e}")

    def _get_user_lock(self, user_id: str) -> asyncio.Lock:
        """获取用户级锁（线程安全，LRU 淘汰）"""
        with self._locks_mutex:
            if user_id in self._locks:
                # 移到末尾表示最近使用
                self._locks.move_to_end(user_id)
            else:
                # 超出上限时淘汰最久未使用的锁
                while len(self._locks) >= _MAX_LOCKS:
                    evicted = False
                    for existing_user_id, existing_lock in list(self._locks.items()):
                        if existing_lock.locked():
                            continue
                        self._locks.pop(existing_user_id, None)
                        evicted = True
                        break
                    # 如果所有锁都在使用中，宁可临时超出上限也不要破坏互斥语义
                    if not evicted:
                        break
                self._locks[user_id] = asyncio.Lock()
            return self._locks[user_id]

    def _binding_platform(self) -> str:
        """Return the platform fixed when this manager process was constructed."""
        return self._platform

    async def _get_binding(self, user_id: str) -> Optional[dict]:
        """从 MongoDB 获取当前平台的用户沙箱绑定"""
        doc = await self._bindings.find_one({"user_id": user_id})
        if not doc:
            return None

        platform = self._binding_platform()
        platform_binding = (doc.get("sandboxes") or {}).get(platform)
        if platform_binding:
            scoped_doc = dict(doc)
            scoped_doc.update(platform_binding)
            scoped_doc["sandbox_platform"] = platform
            return scoped_doc

        # Backward compatibility for records written before platform-scoped
        # bindings existed. Once saved again, the platform slot is populated.
        legacy_platform = doc.get("sandbox_platform")
        if legacy_platform is None or legacy_platform == platform:
            return doc
        return None

    def _evict_if_needed(self) -> None:
        """淘汰最久未使用的缓存条目（LRU），防止内存泄漏。

        仅移除内存引用，不停止沙箱（平台有自己的 auto-stop/auto-archive 生命周期）。
        下次访问会从 MongoDB binding 重新创建。
        """
        while len(self._cache) > _MAX_CACHE_ENTRIES:
            evicted_user_id, (sandbox_id, _, _) = self._cache.popitem(last=False)
            logger.info(
                f"[SessionSandboxManager] Evicted LRU cache entry: "
                f"user={evicted_user_id}, sandbox={sandbox_id}"
            )

    # ── Work-directory management ───────────────────────────────────

    def _session_work_dir(self, base_work_dir: str, session_id: str) -> str:
        """Return a stable, shell-safe workspace directory for a session."""
        safe_session_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", session_id).strip(".-")
        if not safe_session_id:
            safe_session_id = "session"
        return f"{base_work_dir.rstrip('/')}/sessions/{safe_session_id[:80]}"

    async def _ensure_work_dir(self, backend: CompositeBackend, work_dir: str) -> None:
        sandbox_backend = cast(SandboxBackendProtocol, backend.default)
        sandbox_id = str(getattr(sandbox_backend, "id", "unknown"))
        cache_key = f"{sandbox_id}:{work_dir}"
        if cache_key in self._ready_work_dirs:
            self._ready_work_dirs.move_to_end(cache_key)
            return

        result = await sandbox_backend.aexecute(f"mkdir -p {shlex.quote(work_dir)}")
        if getattr(result, "exit_code", 0) != 0:
            raise RuntimeError(f"Failed to create session work_dir {work_dir}: {result.output}")
        self._ready_work_dirs[cache_key] = None
        while len(self._ready_work_dirs) > _MAX_READY_WORK_DIRS:
            self._ready_work_dirs.popitem(last=False)

    # ── Backend scoping ──────────────────────────────────────────────

    def _scope_daytona_backend(
        self,
        backend: CompositeBackend,
        user_id: str,
        work_dir: str,
    ) -> CompositeBackend:
        default = backend.default
        if not isinstance(default, DaytonaBackend):
            return backend
        daytona_backend = DaytonaBackend(
            sandbox=default._sandbox,
            timeout=default._timeout,
            env_vars=default.env_vars,
            work_dir=work_dir,
        )
        return CompositeBackend(
            default=daytona_backend,
            routes={"/skills/": create_skills_backend(user_id=user_id)},
        )

    def _scope_e2b_backend(
        self,
        provider_obj: object,
        user_id: str,
        work_dir: str,
    ) -> CompositeBackend:
        from src.infra.backend.e2b import E2BBackend

        return CompositeBackend(
            default=E2BBackend(sandbox=cast("E2BSandbox", provider_obj), work_dir=work_dir),
            routes={"/skills/": create_skills_backend(user_id=user_id)},
        )

    def _scope_cube_backend(
        self,
        provider_obj: object,
        user_id: str,
        work_dir: str,
    ) -> CompositeBackend:
        from src.infra.backend.cubesandbox import CubeSandboxBackend

        return CompositeBackend(
            default=CubeSandboxBackend(
                sandbox=cast("CubeSandbox", provider_obj),
                work_dir=work_dir,
            ),
            routes={"/skills/": create_skills_backend(user_id=user_id)},
        )

    # ── Binding persistence ─────────────────────────────────────────

    async def _save_binding(
        self,
        user_id: str,
        sandbox_id: str,
        state: str,
        is_new: bool = False,
    ) -> None:
        """保存/更新用户的沙箱绑定"""
        now = utc_now_iso()
        platform = self._binding_platform()
        update = {
            "$set": {
                "sandbox_platform": platform,
                "sandbox_id": sandbox_id,
                "sandbox_state": state,
                "sandbox_last_used_at": now,
                f"sandboxes.{platform}.sandbox_id": sandbox_id,
                f"sandboxes.{platform}.sandbox_state": state,
                f"sandboxes.{platform}.sandbox_last_used_at": now,
                f"sandboxes.{platform}.sandbox_platform": platform,
            },
        }
        # 仅在首次创建时设置 sandbox_created_at
        if is_new:
            update["$set"]["sandbox_created_at"] = now
            update["$set"][f"sandboxes.{platform}.sandbox_created_at"] = now
        else:
            update["$setOnInsert"] = {
                "sandbox_created_at": now,
                f"sandboxes.{platform}.sandbox_created_at": now,
            }

        await self._bindings.update_one(
            {"user_id": user_id},
            update,
            upsert=True,
        )

    def start_background_tasks(self) -> None:
        """Start the Docker janitor once; no-op for remote sandbox platforms."""

        if self._platform != "docker" or self._docker_adapter is None:
            return
        task = self._docker_cleanup_task
        if task is not None and not task.done():
            return
        try:
            task = asyncio.create_task(self._docker_cleanup_loop())
        except RuntimeError:
            return
        self._docker_cleanup_task = task

        def _consume(done_task: asyncio.Task[None]) -> None:
            if not done_task.cancelled():
                with contextlib.suppress(Exception):
                    done_task.exception()

        task.add_done_callback(_consume)

    async def _docker_cleanup_loop(self) -> None:
        while True:
            try:
                await self._cleanup_stale_docker_sandboxes()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Docker sandbox cleanup round failed: %s", exc, exc_info=True)
            try:
                config = get_docker_sandbox_config_from_settings(self._docker_namespace)
                interval = config.cleanup_interval
            except Exception as exc:
                logger.warning("Docker cleanup interval refresh failed: %s", exc)
                interval = 60
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return

    @staticmethod
    def _docker_parse_activity(value: object) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (
            parsed.replace(tzinfo=timezone.utc)
            if parsed.tzinfo is None
            else parsed.astimezone(timezone.utc)
        )

    async def _docker_binding_documents(self, container_ids: list[str]) -> list[dict[str, Any]]:
        if not container_ids:
            return []
        cursor = self._bindings.find(
            {"sandboxes.docker.container_id": {"$in": container_ids}},
            {
                "user_id": 1,
                "sandbox_id": 1,
                "sandbox_platform": 1,
                "sandboxes.docker": 1,
            },
        )
        return await cursor.to_list(length=len(container_ids) + 1)

    async def _mark_docker_deleted(
        self,
        document: dict[str, Any] | None,
        container_id: str,
    ) -> None:
        if document is None:
            return
        nested = (document.get("sandboxes") or {}).get("docker") or {}
        query: dict[str, Any] = {
            "user_id": document.get("user_id"),
            "sandboxes.docker.container_id": container_id,
        }
        update: dict[str, Any] = {"$set": {"sandboxes.docker.sandbox_state": "deleted"}}
        if (
            document.get("sandbox_platform") == "docker"
            and document.get("sandbox_id") == container_id
        ):
            update["$set"].update(
                {
                    "sandbox_state": "deleted",
                    "sandbox_last_used_at": utc_now_iso(),
                }
            )
        if nested.get("container_id") != container_id:
            return
        await self._bindings.update_one(query, update)

    async def _mark_docker_stopped(
        self,
        document: dict[str, Any] | None,
        container_id: str,
    ) -> None:
        if document is None:
            return
        nested = (document.get("sandboxes") or {}).get("docker") or {}
        if nested.get("container_id") != container_id:
            return
        update: dict[str, Any] = {"$set": {"sandboxes.docker.sandbox_state": "stopped"}}
        if (
            document.get("sandbox_platform") == "docker"
            and document.get("sandbox_id") == container_id
        ):
            update["$set"].update(
                {
                    "sandbox_state": "stopped",
                    "sandbox_last_used_at": utc_now_iso(),
                }
            )
        await self._bindings.update_one(
            {
                "user_id": document.get("user_id"),
                "sandboxes.docker.container_id": container_id,
            },
            update,
        )

    async def _cleanup_stale_docker_sandboxes(
        self,
        now: datetime | None = None,
    ) -> int:
        if self._platform != "docker" or self._docker_adapter is None:
            return 0
        config = get_docker_sandbox_config_from_settings(self._docker_namespace)
        self._docker_adapter.refresh_config(config)
        now_utc = now or datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        else:
            now_utc = now_utc.astimezone(timezone.utc)
        cutoff = now_utc - timedelta(seconds=config.idle_timeout)
        containers = await run_blocking_io(self._docker_adapter.list_sandboxes)
        ids = [self._docker_adapter.get_sandbox_id(container) for container in containers]
        documents = await self._docker_binding_documents(ids)
        by_id: dict[str, dict[str, Any]] = {}
        for document in documents:
            nested = (document.get("sandboxes") or {}).get("docker") or {}
            container_id = nested.get("container_id") or nested.get("sandbox_id")
            if container_id:
                by_id[str(container_id)] = document

        removed = 0
        for container in containers:
            container_id = self._docker_adapter.get_sandbox_id(container)
            try:
                details = self._docker_adapter.info(container)
            except Exception as exc:
                logger.warning(
                    "Docker stale container inspection failed for %s: %s", container_id, exc
                )
                continue
            labels = details.get("labels") or {}
            observed: list[datetime] = []
            for value in (
                labels.get(CREATED_AT_LABEL),
                details.get("created_at"),
                (by_id.get(container_id, {}).get("sandboxes") or {})
                .get("docker", {})
                .get("sandbox_last_used_at"),
            ):
                parsed = self._docker_parse_activity(value)
                if parsed is not None:
                    observed.append(parsed)
            operation_state = self._docker_adapter.get_operation_state(container_id)
            if operation_state is not None:
                observed.append(operation_state.last_activity)
            latest = max(observed) if observed else datetime.min.replace(tzinfo=timezone.utc)
            if latest >= cutoff:
                continue
            claimed = await run_blocking_io(
                self._docker_adapter.claim_for_removal,
                container_id,
                cutoff,
                latest,
            )
            if not claimed:
                continue
            try:
                if await run_blocking_io(self._docker_adapter.remove_sandbox, container_id):
                    await self._mark_docker_deleted(by_id.get(container_id), container_id)
                    removed += 1
            except Exception as exc:
                logger.warning(
                    "Docker stale container removal failed for %s: %s", container_id, exc
                )
        await run_blocking_io(
            self._docker_adapter.cleanup_orphan_networks,
            now_utc - timedelta(seconds=config.cleanup_interval),
        )
        return removed

    # ── Public API ──────────────────────────────────────────────────

    async def get_or_create(
        self,
        session_id: str,
        user_id: str,
    ) -> tuple[CompositeBackend, str]:
        """
        获取或创建沙箱

        返回 CompositeBackend，组合了 Sandbox 和 Skills Store。
        LLM 可以通过 /skills/ 路径读写用户技能。

        沙箱按用户维度绑定，同一用户的多个 session 共享同一个沙箱。

        流程：
        1. 检查内存缓存（user_id 维度）
        2. 检查 MongoDB 中的 user_sandbox_bindings
        3. 如果存在，查询 Daytona 状态
        4. Stopped/Archived → start() 恢复
        5. 不存在或恢复失败 → 创建新沙箱，覆盖绑定

        Args:
            session_id: 当前会话 ID（仅用于日志追踪，不影响沙箱绑定）
            user_id: 用户 ID（沙箱绑定的实际维度）

        Returns:
            tuple[CompositeBackend, str]: (composite_backend, work_dir)
        """
        if not user_id:
            raise ValueError(
                "user_id is required for sandbox binding. "
                "Anonymous users cannot use sandbox features."
            )

        if self._platform == "e2b":
            return await self._get_or_create_e2b(session_id, user_id)
        if self._platform == "cubesandbox":
            return await self._get_or_create_cubesandbox(session_id, user_id)
        if self._platform == "docker":
            return await self._get_or_create_docker(session_id, user_id)
        if self._platform != "daytona":
            raise ValueError(f"Unsupported sandbox platform: {self._platform}")
        lock = self._get_user_lock(user_id)

        async with lock:
            # 1. 检查内存缓存
            if user_id in self._cache:
                self._cache.move_to_end(user_id)  # LRU: mark as recently used
                sandbox_id, backend, _ = self._cache[user_id]
                logger.debug(
                    f"[SessionSandboxManager] Cache hit: user={user_id}, sandbox={sandbox_id}"
                )
                try:
                    base_work_dir = await self._get_work_dir(sandbox_id)
                    work_dir = self._session_work_dir(base_work_dir, session_id)
                    scoped_backend = self._scope_daytona_backend(backend, user_id, work_dir)
                    await self._ensure_work_dir(scoped_backend, work_dir)
                    await sync_sandbox_env_vars(scoped_backend, user_id)
                    await self._save_binding(user_id, sandbox_id, "running")
                    return scoped_backend, work_dir
                except Exception as e:
                    logger.warning(
                        f"[SessionSandboxManager] Failed to get work_dir from cached sandbox {sandbox_id}: {e}. "
                        "Creating new sandbox."
                    )
                    del self._cache[user_id]

            # 2. 从 MongoDB 获取绑定
            binding = await self._get_binding(user_id)
            metadata_sandbox_id: str | None = binding.get("sandbox_id") if binding else None

            if metadata_sandbox_id:
                sandbox_id = metadata_sandbox_id
                # 3. 查询 Daytona 状态
                state = await self._get_sandbox_state(sandbox_id)
                logger.info(
                    f"[SessionSandboxManager] Found sandbox {sandbox_id} with state={state}"
                )

                # 3.1 如果处于中间状态，等待完成
                if state in TRANSITIONAL_STATES:
                    state = await self._wait_for_final_state(sandbox_id, state)
                    logger.info(
                        f"[SessionSandboxManager] Sandbox {sandbox_id} transitioned to state={state}"
                    )

                if state in RESUMABLE_STATES:
                    # 4. 尝试恢复
                    try:
                        await self._start_sandbox(sandbox_id)
                        backend = await self._create_backend(sandbox_id, user_id=user_id)
                        self._cache[user_id] = (sandbox_id, backend, None)
                        self._evict_if_needed()
                        await self._save_binding(user_id, sandbox_id, "running")
                        base_work_dir = await self._get_work_dir(sandbox_id)
                        work_dir = self._session_work_dir(base_work_dir, session_id)
                        scoped_backend = self._scope_daytona_backend(backend, user_id, work_dir)
                        await self._ensure_work_dir(scoped_backend, work_dir)
                        await sync_sandbox_env_vars(scoped_backend, user_id)
                        return scoped_backend, work_dir
                    except Exception as e:
                        logger.warning(
                            f"[SessionSandboxManager] Failed to resume sandbox {sandbox_id}: {e}. "
                            "Creating new sandbox."
                        )
                        if user_id in self._cache:
                            del self._cache[user_id]

                elif state in READY_STATES:
                    try:
                        backend = await self._create_backend(sandbox_id, user_id=user_id)
                        self._cache[user_id] = (sandbox_id, backend, None)
                        self._evict_if_needed()
                        await self._save_binding(user_id, sandbox_id, "running")
                        base_work_dir = await self._get_work_dir(sandbox_id)
                        work_dir = self._session_work_dir(base_work_dir, session_id)
                        scoped_backend = self._scope_daytona_backend(backend, user_id, work_dir)
                        await self._ensure_work_dir(scoped_backend, work_dir)
                        await sync_sandbox_env_vars(scoped_backend, user_id)
                        return scoped_backend, work_dir
                    except Exception as e:
                        logger.warning(
                            f"[SessionSandboxManager] Failed to get work_dir from sandbox {sandbox_id}: {e}. "
                            "Creating new sandbox."
                        )
                        if user_id in self._cache:
                            del self._cache[user_id]

                elif state in UNAVAILABLE_STATES:
                    logger.info(
                        f"[SessionSandboxManager] Sandbox {sandbox_id} is unavailable (state={state})"
                    )

            # 5. 创建新沙箱并绑定
            return await self._create_and_bind(session_id, user_id)

    async def stop(self, user_id: str) -> bool:
        """
        停止用户的沙箱

        持有用户锁执行，防止与 get_or_create 竞态。

        Args:
            user_id: 用户 ID

        Returns:
            是否成功停止
        """
        if not user_id:
            raise ValueError(
                "user_id is required for sandbox binding. "
                "Anonymous users cannot use sandbox features."
            )

        if self._platform == "e2b":
            return await self._stop_e2b(user_id)
        if self._platform == "cubesandbox":
            return await self._stop_cubesandbox(user_id)
        if self._platform == "docker":
            return await self._stop_docker(user_id)
        if self._platform != "daytona":
            raise ValueError(f"Unsupported sandbox platform: {self._platform}")

        lock = self._get_user_lock(user_id)

        async with lock:
            sandbox_id: str | None = None

            if user_id in self._cache:
                sandbox_id, _, _ = self._cache[user_id]
            else:
                binding = await self._get_binding(user_id)
                sandbox_id = binding.get("sandbox_id") if binding else None

            if not sandbox_id:
                return False

            def _sync_stop():
                client = self._get_daytona_client()
                sandbox = client.get(sandbox_id)
                sandbox.stop(timeout=30)

            try:
                await run_blocking_io(
                    _sync_stop,
                    timeout=DEFAULT_DAYTONA_TIMEOUT,
                )
                # stop 成功后清除缓存，避免下次 get_or_create cache hit 后对 stopped 沙箱操作失败
                self._cache.pop(user_id, None)
                await self._save_binding(user_id, sandbox_id, "stopped")
                logger.info(
                    f"[SessionSandboxManager] Stopped sandbox {sandbox_id} for user {user_id}"
                )
                return True
            except asyncio.TimeoutError:
                logger.error(f"[SessionSandboxManager] Timeout stopping sandbox {sandbox_id}")
                return False
            except Exception as e:
                logger.error(f"[SessionSandboxManager] Failed to stop sandbox {sandbox_id}: {e}")
                return False

    # ── Cache management ────────────────────────────────────────────

    def clear_cache(self, user_id: str) -> None:
        """清除内存缓存（用于测试或强制刷新）"""
        self._cache.pop(user_id, None)

    def get_cached_backend(self, user_id: str):
        """Return the currently cached backend for a user, if one exists."""
        entry = self._cache.get(user_id)
        if entry is None:
            return None
        return entry[1]

    async def _close_docker_resources(self) -> None:
        assert self._docker_adapter is not None
        adapter = self._docker_adapter
        task = self._docker_cleanup_task
        self._docker_cleanup_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        keep_namespace = bool(
            getattr(settings, "ENABLE_SANDBOX", False)
            and settings.SANDBOX_PLATFORM.lower() == "docker"
            and getattr(settings, "DOCKER_SANDBOX_NAMESPACE", self._docker_namespace)
            == self._docker_namespace
        )
        try:
            try:
                containers = await run_blocking_io(adapter.list_sandboxes)
            except Exception as exc:
                logger.warning("Failed to list Docker sandboxes during shutdown: %s", exc)
                containers = []
            try:
                documents = await self._docker_binding_documents(
                    [adapter.get_sandbox_id(container) for container in containers]
                )
            except Exception as exc:
                logger.warning("Failed to load Docker bindings during shutdown: %s", exc)
                documents = []
            by_id: dict[str, dict[str, Any]] = {}
            for document in documents:
                nested = (document.get("sandboxes") or {}).get("docker") or {}
                container_id = nested.get("container_id") or nested.get("sandbox_id")
                if container_id:
                    by_id[str(container_id)] = document

            for container in containers:
                container_id = adapter.get_sandbox_id(container)
                try:
                    if keep_namespace:
                        stopped = await run_blocking_io(adapter.recover_sandbox, container, False)
                        if not stopped:
                            logger.warning(
                                "Failed to stop Docker sandbox %s during shutdown", container_id
                            )
                            continue
                        await self._mark_docker_stopped(by_id.get(container_id), container_id)
                    else:
                        removed = await run_blocking_io(
                            adapter.remove_sandbox,
                            container_id,
                            force=True,
                        )
                        if removed:
                            await self._mark_docker_deleted(by_id.get(container_id), container_id)
                        else:
                            logger.warning(
                                "Failed to remove Docker sandbox %s during cutover", container_id
                            )
                except Exception as exc:
                    logger.warning("Failed to close Docker sandbox %s: %s", container_id, exc)

            if not keep_namespace:
                try:
                    await run_blocking_io(
                        adapter.cleanup_orphan_networks,
                        datetime.max.replace(tzinfo=timezone.utc),
                    )
                except Exception as exc:
                    logger.warning("Failed to clean Docker orphan networks during cutover: %s", exc)
        finally:
            self._cache.clear()
            adapter.close()

    async def close_all(self) -> None:
        """Stop or remove all managed resources according to the cutover policy."""
        if self._platform == "docker" and self._docker_adapter is not None:
            await self._close_docker_resources()
        else:
            entries = list(self._cache.items())
            for user_id, (sandbox_id, _backend, _provider_obj) in entries:
                try:
                    await self.stop(user_id)
                except Exception as exc:
                    logger.warning(
                        "[SessionSandboxManager] Failed to stop sandbox %s during shutdown: %s",
                        sandbox_id,
                        exc,
                    )
            self._cache.clear()
        with self._locks_mutex:
            self._locks.clear()
        task = type(self)._index_task
        type(self)._index_task = None
        type(self)._index_ensured = False
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._collection = None
        logger.info("[SessionSandboxManager] All sandboxes stopped and resources cleaned up")


# Singleton
_session_sandbox_manager: Optional[SessionSandboxManager] = None


def get_session_sandbox_manager() -> SessionSandboxManager:
    """获取 SessionSandboxManager 单例"""
    global _session_sandbox_manager
    if _session_sandbox_manager is None:
        _session_sandbox_manager = SessionSandboxManager()
    return _session_sandbox_manager


async def close_session_sandbox_manager() -> None:
    global _session_sandbox_manager
    manager = _session_sandbox_manager
    _session_sandbox_manager = None
    if manager is not None:
        await manager.close_all()
