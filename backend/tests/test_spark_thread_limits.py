"""Pinned ABI and queue safety, without requiring a JVM for unit checks."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from trackvance.spark_thread_limits import (
    TARGETS,
    SparkThreadLimitError,
    bound_local_driver_pools,
    cgroup_pid_limit,
    needs_local_thread_bound,
)


class JavaClass:
    def __init__(self, instance):
        self.instance = instance

    def getName(self):
        return self.instance.name

    def getDeclaredField(self, name):
        instance = self.instance
        if name not in instance.fields:
            raise ValueError("Missing declared field")
        return SimpleNamespace(setAccessible=lambda _: None, get=lambda _: instance.fields[name])


class JavaObject:
    def __init__(self, name, **fields):
        self.name, self.fields = name, fields

    def getClass(self):
        return JavaClass(self)


class JavaQueue(JavaObject):
    def __init__(self):
        super().__init__("java.util.concurrent.LinkedBlockingQueue")
        self.pending = ["existing_callback"]

    def equals(self, other):
        return self is other


class JavaPool(JavaObject):
    def __init__(self):
        super().__init__("java.util.concurrent.ThreadPoolExecutor")
        self.core, self.maximum, self.queue = 100, 100, JavaQueue()
        self.calls = []

    def getQueue(self):
        return self.queue

    def allowsCoreThreadTimeOut(self):
        return True

    def isShutdown(self):
        return False

    def getCorePoolSize(self):
        return self.core

    def getMaximumPoolSize(self):
        return self.maximum

    def getLargestPoolSize(self):
        return 0

    def setCorePoolSize(self, value):
        self.core = value
        self.calls.append(("core", value))

    def setMaximumPoolSize(self, value):
        if value < self.core:
            raise ValueError("Java maximum cannot be below core")
        self.maximum = value
        self.calls.append(("maximum", value))


class EndpointSet:
    def __init__(self, values):
        self.values = values

    def keySet(self):
        return self

    def iterator(self):
        iterator = iter(self.values)
        cursor = [next(iterator, None)]

        def following():
            value = cursor[0]
            cursor[0] = next(iterator, None)
            return value

        return SimpleNamespace(hasNext=lambda: cursor[0] is not None, next=following)


def session_fixture():
    pools = [JavaPool(), JavaPool()]
    endpoints = [JavaObject(name, **{field: pool}) for (name, field), pool in zip(TARGETS.items(), pools, strict=True)]
    refs = EndpointSet(endpoints)
    dispatcher = JavaObject("org.apache.spark.rpc.netty.Dispatcher", endpointRefs=refs)
    rpc = JavaObject("org.apache.spark.rpc.netty.NettyRpcEnv", dispatcher=dispatcher)
    context = SimpleNamespace(master="local[1]", _jsc=SimpleNamespace(
        sc=lambda: SimpleNamespace(env=lambda: SimpleNamespace(rpcEnv=lambda: rpc))))
    return SimpleNamespace(version="4.0.3", sparkContext=context), pools, refs


class Checks(unittest.TestCase):
    def test_only_actual_small_finite_local_cgroups_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pids.max"
            for raw, expected in (("128\n", 128), ("512", 512), ("max", None), ("invalid", None)):
                path.write_text(raw, encoding="ascii")
                self.assertEqual(cgroup_pid_limit((path, Path(directory) / "absent")), expected)
        self.assertTrue(needs_local_thread_bound("local[1]", 128))
        self.assertTrue(needs_local_thread_bound("local[2]", 511))
        for master, limit in (("local[1]", 512), ("local[1]", None), ("spark://isolated:7077", 128)):
            self.assertFalse(needs_local_thread_bound(master, limit))

    def test_both_pools_keep_existing_queue_and_callbacks(self):
        session, pools, _ = session_fixture()
        queues = [pool.queue for pool in pools]
        result = bound_local_driver_pools(session)
        self.assertEqual(set(result), {"askThreadPool", "asyncThreadPool"})
        for index, pool in enumerate(pools):
            self.assertEqual(pool.calls, [("core", 4), ("maximum", 4)])
            self.assertIs(pool.queue, queues[index])
            self.assertEqual(pool.queue.pending, ["existing_callback"])
        self.assertTrue(all(value["original_core_threads"] == 100 and value["maximum_threads"] == 4 for value in result.values()))

    def test_missing_duplicate_wrong_queue_and_version_reject_before_mutation(self):
        for kind in ("missing", "duplicate", "queue", "pool", "version", "standalone"):
            with self.subTest(kind=kind):
                session, pools, refs = session_fixture()
                if kind == "missing":
                    refs.values.pop()
                elif kind == "duplicate":
                    refs.values.append(refs.values[0])
                elif kind == "queue":
                    pools[1].queue.name = "java.util.concurrent.SynchronousQueue"
                elif kind == "pool":
                    pools[1].maximum = 200
                elif kind == "version":
                    session.version = "4.1.0"
                else:
                    session.sparkContext.master = "spark://isolated:7077"
                with self.assertRaises(SparkThreadLimitError):
                    bound_local_driver_pools(session)
                self.assertTrue(all(not pool.calls for pool in pools))

    def test_queue_substitution_during_adjustment_fails_closed(self):
        session, pools, _ = session_fixture()
        original = pools[0].setMaximumPoolSize

        def substitute(value):
            original(value)
            pools[0].queue = JavaQueue()

        pools[0].setMaximumPoolSize = substitute
        with self.assertRaises(SparkThreadLimitError):
            bound_local_driver_pools(session)


if __name__ == "__main__":
    unittest.main()
