"""Bound Spark 4.0.3 local housekeeping in small, finite PID cgroups.

Spark hardcodes two storage pools to 100 core threads. Its LinkedBlockingQueue
creates core threads before reusing idle threads, even for serial cleanup. Keep
the same queues, Futures and separate executors; lower only their thread counts.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

SPARK_VERSION = "4.0.3"
STORAGE_POOL_THREADS = 4
PID_THRESHOLD = 512
TARGETS = {"org.apache.spark.storage.BlockManagerMasterEndpoint": "askThreadPool",
           "org.apache.spark.storage.BlockManagerStorageEndpoint": "asyncThreadPool"}
PID_FILES = (Path("/sys/fs/cgroup/pids.max"), Path("/sys/fs/cgroup/pids/pids.max"))


class SparkThreadLimitError(RuntimeError):
    """A pinned Spark ABI could not safely enforce its housekeeping bound."""


def cgroup_pid_limit(paths: tuple[Path, ...] = PID_FILES) -> int | None:
    """Observe the mounted cgroup limit; never derive capacity from an env flag."""
    limits = []
    for path in paths:
        try:
            value = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            continue
        if value.isdecimal() and int(value) > 0:
            limits.append(int(value))
    return min(limits) if limits else None


def needs_local_thread_bound(master: str, pid_limit: int | None) -> bool:
    return master.startswith("local[") and pid_limit is not None and 0 < pid_limit < PID_THRESHOLD


def _field(instance: Any, name: str) -> Any:
    declared = instance.getClass().getDeclaredField(name)
    declared.setAccessible(True)
    return declared.get(instance)


def bound_local_driver_pools(session: Any) -> dict[str, dict[str, Any]]:
    """Pin exact local 4.0.3 pools, validating both before changing either one.

This bridge is version-bound because Spark 4.0.3 has no configuration setting
for these pools. Reflection resolves only the two named endpoint objects. Any
ABI mismatch fails closed; the caller must close its newly created session.
"""
    if session.version != SPARK_VERSION or not session.sparkContext.master.startswith("local["):
        raise SparkThreadLimitError("RESOURCE_SPARK_THREAD_BINDING_UNSUPPORTED")
    try:
        rpc = session.sparkContext._jsc.sc().env().rpcEnv()
        if rpc.getClass().getName() != "org.apache.spark.rpc.netty.NettyRpcEnv":
            raise SparkThreadLimitError("RESOURCE_SPARK_THREAD_BINDING_UNSUPPORTED")
        dispatcher = _field(rpc, "dispatcher")
        if dispatcher.getClass().getName() != "org.apache.spark.rpc.netty.Dispatcher":
            raise SparkThreadLimitError("RESOURCE_SPARK_THREAD_BINDING_UNSUPPORTED")
        endpoints = _field(dispatcher, "endpointRefs").keySet().iterator()
        found = {}
        while endpoints.hasNext():
            endpoint = endpoints.next()
            name = endpoint.getClass().getName()
            if name not in TARGETS:
                continue
            if name in found:
                raise SparkThreadLimitError("RESOURCE_SPARK_THREAD_BINDING_UNSUPPORTED")
            pool = _field(endpoint, TARGETS[name])
            if (pool.getClass().getName() != "java.util.concurrent.ThreadPoolExecutor"
                    or pool.getQueue().getClass().getName() != "java.util.concurrent.LinkedBlockingQueue"
                    or not pool.allowsCoreThreadTimeOut() or pool.isShutdown()
                    or (pool.getCorePoolSize(), pool.getMaximumPoolSize()) not in {
                        (100, 100), (STORAGE_POOL_THREADS, STORAGE_POOL_THREADS)}):
                raise SparkThreadLimitError("RESOURCE_SPARK_THREAD_BINDING_UNSUPPORTED")
            found[name] = pool
        if set(found) != set(TARGETS):
            raise SparkThreadLimitError("RESOURCE_SPARK_THREAD_BINDING_UNSUPPORTED")
        result = {}
        for name, pool in found.items():
            original_core, original_maximum = pool.getCorePoolSize(), pool.getMaximumPoolSize()
            queue = pool.getQueue()
            # Java requires reducing core before maximum. Keep the existing
            # queue and separate async execution contexts, including callbacks.
            pool.setCorePoolSize(STORAGE_POOL_THREADS)
            pool.setMaximumPoolSize(STORAGE_POOL_THREADS)
            if (pool.getCorePoolSize() != STORAGE_POOL_THREADS
                    or pool.getMaximumPoolSize() != STORAGE_POOL_THREADS
                    or not queue.equals(pool.getQueue())):
                raise SparkThreadLimitError("RESOURCE_SPARK_THREAD_BINDING_UNSUPPORTED")
            result[TARGETS[name]] = {"original_core_threads": original_core,
                "original_maximum_threads": original_maximum,
                "core_threads": pool.getCorePoolSize(), "maximum_threads": pool.getMaximumPoolSize(),
                "largest_threads": pool.getLargestPoolSize(), "queue_class": queue.getClass().getName()}
        return result
    except SparkThreadLimitError:
        raise
    except Exception:  # noqa: BLE001 -- fail closed on any Py4J/reflection ABI failure, without daemon details.
        raise SparkThreadLimitError("RESOURCE_SPARK_THREAD_BINDING_UNSUPPORTED") from None
