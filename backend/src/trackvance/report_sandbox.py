"""Standalone DuckDB executor. No application imports, database, secrets or network.

Run only through report_executor. Landlock ABI 3 denies filesystem access by
default and libseccomp denies network/process escape syscalls. Both fail closed.
The output channel is a bounded NDJSON pipe with exact, tagged scalar values.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import json
import os
import resource
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import sqlglot
from sqlglot import exp

duckdb = None
# This script is Linux-only; Windows typeshed omits the rusage API.
linux_resource = cast(Any, resource)


class SandboxError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message


def emit(kind: str, **fields):
    sys.stdout.write(json.dumps({"kind": kind, **fields}, ensure_ascii=False,
                               separators=(",", ":"), allow_nan=False) + "\n")
    sys.stdout.flush()


def quote(value):
    return '"' + value.replace('"', '""') + '"'


def parameter(value):
    if isinstance(value, dict):
        kind, raw = value["type"], value["value"]
        if kind == "DECIMAL":
            return Decimal(raw)
        if kind == "DATE":
            return date.fromisoformat(raw)
        if kind == "TIMESTAMP":
            return datetime.fromisoformat(raw)
        raise SandboxError("REPORT_PARAMETER_TYPE", "Tipo de parámetro no admitido.")
    return value


def bind_parameters(sql, params):
    names = {n.name for n in sqlglot.parse_one(sql, read="duckdb").find_all(exp.Placeholder)}
    return {k: v for k, v in params.items() if k in names}


def filter_literal(sql, params):
    def replace(node):
        if isinstance(node, exp.Placeholder):
            value = params[node.name]
            if isinstance(value, Decimal):
                exponent = value.as_tuple().exponent
                if not isinstance(exponent, int):
                    raise SandboxError("REPORT_PARAMETER_TYPE", "El decimal debe ser finito.")
                return exp.Cast(this=exp.Literal.string(str(value)), to=exp.DataType.build(f"DECIMAL(38,{max(0, -exponent)})"))
            if isinstance(value, datetime):
                return exp.Cast(this=exp.Literal.string(value.isoformat()), to=exp.DataType.build("TIMESTAMPTZ" if value.tzinfo else "TIMESTAMP"))
            if isinstance(value, date):
                return exp.Cast(this=exp.Literal.string(value.isoformat()), to=exp.DataType.build("DATE"))
            return exp.convert(value)
        return node
    return sqlglot.parse_one(sql, read="duckdb").transform(replace).sql(dialect="duckdb")


def tagged(value):
    if isinstance(value, Decimal):
        return {"type": "DECIMAL", "value": str(value)}
    if isinstance(value, datetime):
        return {"type": "TIMESTAMP", "value": value.isoformat()}
    if isinstance(value, date):
        return {"type": "DATE", "value": value.isoformat()}
    if isinstance(value, float):
        # Floating arithmetic is unsupported for exact logical source types.
        raise SandboxError("REPORT_FLOAT_RESULT", "El resultado contiene float; selecciona una expresión exacta.")
    if isinstance(value, str) and len(value.encode()) > 65536:
        raise SandboxError("REPORT_CELL_LIMIT", "Una celda supera 64 KiB.")
    if not isinstance(value, (str, int, bool, type(None))):
        raise SandboxError("REPORT_RESULT_TYPE", "Tipo de resultado no compatible con el contrato canónico.")
    return value


def _landlock(read_files: list[str], writable_dir: str | None):
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 3:
        raise SandboxError("REPORT_SANDBOX_UNAVAILABLE", "Landlock ABI 3 es obligatorio.")
    # ABI 3 supports filesystem bits 0..14 (including REFER and TRUNCATE).
    handled = (1 << 15) - 1
    rules = ctypes.c_uint64(handled)
    descriptor = libc.syscall(444, ctypes.byref(rules), ctypes.sizeof(rules), 0)
    if descriptor < 0:
        raise SandboxError("REPORT_SANDBOX_UNAVAILABLE", "No se pudo crear la frontera Landlock.")

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int)]

    def allow(path: str, mask: int):
        fd = os.open(path, 0x200000 | 0x80000)  # Linux O_PATH | O_CLOEXEC; constants absent on Windows stubs.
        try:
            attr = PathRule(mask, fd)
            if libc.syscall(445, descriptor, 1, ctypes.byref(attr), 0) != 0:
                raise SandboxError("REPORT_SANDBOX_UNAVAILABLE", "No se pudo limitar una fuente verificada.")
        finally:
            os.close(fd)
    try:
        # Only public runtime directories. No /app, /data, /home, /etc, /proc
        # directory grants; source paths and loader cache are exact file grants.
        for location in ("/usr/lib", "/usr/local/lib", "/lib", "/lib64",
                         str(Path(sys.prefix) / "lib"), str(Path(sys.base_prefix) / "lib")):
            if Path(location).is_dir():
                allow(location, 1 | 4 | 8)
        for location in ("/etc/ld.so.cache", "/usr/share/zoneinfo/UTC", "/proc/self/cgroup",
                         "/proc/stat", "/sys/devices/system/cpu/online",
                         "/proc/sys/vm/overcommit_memory", "/sys/fs/cgroup/cpu.max",
                         "/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "/sys/fs/cgroup/cpu/cpu.cfs_period_us",
                         "/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.peak",
                         "/sys/fs/cgroup/memory/memory.limit_in_bytes",
                         "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
            if Path(location).is_file():
                allow(location, 4)
        for path in read_files:
            allow(path, 4)
        if writable_dir:
            allow(writable_dir, handled & ~1)
        if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.syscall(446, descriptor, 0) != 0:
            raise SandboxError("REPORT_SANDBOX_UNAVAILABLE", "No se pudo aplicar la frontera Landlock.")
    finally:
        os.close(descriptor)


def _seccomp():
    name = ctypes.util.find_library("seccomp")
    if not name:
        raise SandboxError("REPORT_SANDBOX_UNAVAILABLE", "libseccomp es obligatorio.")
    lib = ctypes.CDLL(name, use_errno=True)
    lib.seccomp_init.argtypes, lib.seccomp_init.restype = [ctypes.c_uint32], ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes, lib.seccomp_syscall_resolve_name.restype = [ctypes.c_char_p], ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes, lib.seccomp_release.argtypes = [ctypes.c_void_p], [ctypes.c_void_p]
    context = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise SandboxError("REPORT_SANDBOX_UNAVAILABLE", "No se pudo crear la frontera seccomp.")
    deny = 0x00050000 | errno.EPERM
    try:
        for syscall in ("socket", "socketpair", "connect", "bind", "listen", "accept", "accept4",
                        "sendto", "sendmsg", "sendmmsg", "recvfrom", "recvmsg", "recvmmsg",
                        "execve", "execveat", "fork", "vfork", "clone3", "ptrace",
                        "process_vm_readv", "process_vm_writev", "mount", "umount2", "unshare",
                        "setns", "open_by_handle_at", "name_to_handle_at", "bpf", "keyctl",
                        "io_uring_setup", "io_uring_enter", "io_uring_register", "userfaultfd"):
            number = lib.seccomp_syscall_resolve_name(syscall.encode())
            # clone3's pointer argument cannot be filtered for CLONE_THREAD.
            # Keep it unavailable, but return ENOSYS so modern glibc falls back
            # to clone, whose flags are checked below. EPERM aborts pthread
            # creation on bare-host glibc even though safe threads are allowed.
            action = (0x00050000 | errno.ENOSYS) if syscall == "clone3" else deny
            if number >= 0 and lib.seccomp_rule_add(context, action, number, 0) != 0:
                raise SandboxError("REPORT_SANDBOX_UNAVAILABLE", "No se pudo denegar un syscall.")

        class Argument(ctypes.Structure):
            _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                        ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]
        lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                               ctypes.c_uint, ctypes.POINTER(Argument)]
        # Allow thread creation, deny clone when CLONE_THREAD is absent.
        constraint = Argument(0, 7, 0x10000, 0)  # SCMP_CMP_MASKED_EQ
        clone = lib.seccomp_syscall_resolve_name(b"clone")
        if clone >= 0 and lib.seccomp_rule_add_array(context, deny, clone, 1, ctypes.byref(constraint)) != 0:
            raise SandboxError("REPORT_SANDBOX_UNAVAILABLE", "No se pudo limitar creación de procesos.")
        if lib.seccomp_load(context) != 0:
            raise SandboxError("REPORT_SANDBOX_UNAVAILABLE", "No se pudo aplicar la frontera seccomp.")
    finally:
        lib.seccomp_release(context)


def sandbox(sources: list[dict], limits: dict, staging: str | None):
    if sys.platform != "linux":
        raise SandboxError("REPORT_SANDBOX_UNAVAILABLE", "Reportes requiere el ejecutor Linux aislado.")
    # ABI 3 confines the calling thread and future children, rather than TSYNC.
    # Never continue if an import already created a thread outside the domain.
    if len(list(Path("/proc/self/task").iterdir())) != 1:
        raise SandboxError("REPORT_SANDBOX_THREADS", "El ejecutor debe entrar a la frontera antes de crear threads.")
    resource.setrlimit(resource.RLIMIT_AS, (limits["process_memory_bytes"], limits["process_memory_bytes"]))
    resource.setrlimit(resource.RLIMIT_CPU, (limits["timeout_seconds"], limits["timeout_seconds"] + 1))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(4096, 128 + sum(len(s["paths"]) for s in sources)),) * 2)
    # Resolve libseccomp before Landlock closes ctypes.util's discovery subprocess.
    libname = ctypes.util.find_library("seccomp")
    if not libname:
        raise SandboxError("REPORT_SANDBOX_UNAVAILABLE", "libseccomp no está disponible.")
    ctypes.util.find_library = lambda name: libname if name == "seccomp" else None
    _landlock([p for s in sources for p in s["paths"]], staging)
    _seccomp()


def typed_view(connection, source, params):
    alias, paths, schema = source["alias"], source["paths"], source["schema"]
    raw = "__tv_raw_" + alias
    # File paths originate exclusively from the trusted coordinator. User SQL
    # sees only typed views and has no ability to supply read_parquet arguments.
    connection.read_parquet(paths, union_by_name=False).create_view(raw)
    positions = "row_number() OVER ()::BIGINT AS __tv_source_pos"
    projections = []
    for item in schema:
        name, logical = item["name"], item.get("logical_type", "STRING")
        ref = quote(name)
        if logical == "STRING":
            cast = f"CAST({ref} AS VARCHAR)"
        elif logical in {"INTEGER", "INT64"}:
            bad = connection.execute(f"SELECT count(*) FROM {quote(raw)} WHERE {ref} IS NOT NULL AND NOT regexp_full_match(CAST({ref} AS VARCHAR),'[+-]?[0-9]+')").fetchone()[0]
            if bad:
                raise SandboxError("REPORT_SOURCE_TYPE", "Una columna INTEGER contiene valores incompatibles.")
            cast = f"CAST({ref} AS BIGINT)"
        elif logical == "DECIMAL":
            bad = connection.execute(f"SELECT count(*) FROM {quote(raw)} WHERE {ref} IS NOT NULL AND NOT regexp_full_match(CAST({ref} AS VARCHAR),'[+-]?[0-9]+(\\.[0-9]+)?')").fetchone()[0]
            if bad:
                raise SandboxError("REPORT_SOURCE_TYPE", "Una columna DECIMAL contiene valores incompatibles.")
            integers, scale = connection.execute(f"SELECT COALESCE(MAX(length(split_part(ltrim(CAST({ref} AS VARCHAR),'+-'),'.',1))),1), COALESCE(MAX(length(split_part(CAST({ref} AS VARCHAR),'.',2))),0) FROM {quote(raw)}").fetchone()
            precision = int(integers) + int(scale)
            if precision > 38:
                raise SandboxError("REPORT_DECIMAL_PRECISION", "La precisión DECIMAL excede 38; no se perderá información.")
            cast = f"CAST({ref} AS DECIMAL({max(1, precision)},{int(scale)}))"
        elif logical == "DATE":
            bad = connection.execute(f"SELECT count(*) FROM {quote(raw)} WHERE {ref} IS NOT NULL AND NOT regexp_full_match(CAST({ref} AS VARCHAR),'[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}')").fetchone()[0]
            if bad:
                raise SandboxError("REPORT_SOURCE_TYPE", "DATE requiere representación canónica ISO; una conversión ambigua no es admisible.")
            cast = f"CAST({ref} AS DATE)"
        elif logical == "TIMESTAMP":
            aware, naive = connection.execute(f"SELECT count(*) FILTER(WHERE regexp_matches(CAST({ref} AS VARCHAR),'(Z|[+-][0-9]{{2}}:[0-9]{{2}})$')), count(*) FILTER(WHERE {ref} IS NOT NULL AND NOT regexp_matches(CAST({ref} AS VARCHAR),'(Z|[+-][0-9]{{2}}:[0-9]{{2}})$')) FROM {quote(raw)}").fetchone()
            if aware and naive:
                raise SandboxError("REPORT_TIMESTAMP_ZONE", "No se permite mezclar timestamps con zona y sin zona.")
            cast = f"CAST({ref} AS {'TIMESTAMPTZ' if aware else 'TIMESTAMP'})"
        elif logical == "BOOLEAN":
            bad = connection.execute(f"SELECT count(*) FROM {quote(raw)} WHERE {ref} IS NOT NULL AND lower(CAST({ref} AS VARCHAR)) NOT IN ('true','false')").fetchone()[0]
            if bad:
                raise SandboxError("REPORT_SOURCE_TYPE", "BOOLEAN requiere true o false.")
            cast = f"CAST({ref} AS BOOLEAN)"
        else:
            raise SandboxError("REPORT_SOURCE_TYPE", "Tipo lógico no admitido por el motor inicial.")
        projections.append(cast + " AS " + ref)
    # Positions are assigned before filters over the immutable part order.
    typed = "__tv_typed_" + alias
    connection.execute(f"CREATE VIEW {quote(typed)} AS SELECT {','.join(projections)},{positions} FROM {quote(raw)}")
    connection.execute(f"CREATE VIEW {quote(alias)} AS SELECT * FROM {quote(typed)}")
    # Validate strict casts over every source cell; projections cannot hide an
    # incompatible unused value by causing the optimizer to skip its cast.
    for item in schema:
        connection.execute(f"SELECT count({quote(item['name'])}) FROM {quote(alias)}").fetchone()
    if source.get("filter"):
        condition = filter_literal(source["filter"], params)
        connection.execute(f"CREATE OR REPLACE VIEW {quote(alias)} AS SELECT * FROM {quote(typed)} AS {quote(alias)} WHERE {condition}")


def cardinality(connection, plan, params, limits):
    results = []
    for join in plan["joins"]:
        pairs, right = join["keys"], join["right_alias"]
        left_keys = [f"{quote(p['left_alias'])}.{quote(p['left_column'])}" for p in pairs]
        right_keys = [f"{quote(right)}.{quote(p['right_column'])}" for p in pairs]
        left_select = ",".join(f"{k} AS k{i}" for i, k in enumerate(left_keys))
        right_select = ",".join(f"{k} AS k{i}" for i, k in enumerate(right_keys))
        nonnull_l = " AND ".join(k + " IS NOT NULL" for k in left_keys)
        nonnull_r = " AND ".join(k + " IS NOT NULL" for k in right_keys)
        keys = ",".join(f"k{i}" for i in range(len(pairs)))
        equality = " AND ".join(f"l.k{i}=r.k{i}" for i in range(len(pairs)))
        sql = f"WITH l AS (SELECT {left_select},count(*) AS n {join['left_from']} WHERE {nonnull_l} GROUP BY {','.join(left_keys)}), r AS (SELECT {right_select},count(*) AS n FROM {quote(right)} WHERE {nonnull_r} GROUP BY {','.join(right_keys)}) SELECT COALESCE(MAX(l.n),0),COALESCE(MAX(r.n),0),COALESCE(SUM(l.n*r.n),0),COALESCE(SUM(l.n),0),COALESCE(SUM(r.n),0),COALESCE(count(*) FILTER(WHERE l.n>1 AND r.n>1),0) FROM l JOIN r ON {equality}"
        del keys
        ml, mr, matched, matched_left, matched_right, nm_keys = connection.execute(sql).fetchone()
        left_total = connection.execute(f"SELECT count(*) {join['left_from']}").fetchone()[0]
        right_total = connection.execute(f"SELECT count(*) FROM {quote(right)}").fetchone()[0]
        observed = "N:M" if nm_keys else "1:N" if mr > 1 else "N:1" if ml > 1 else "1:1"
        if nm_keys and not join["allow_many_to_many"]:
            raise SandboxError("REPORT_MANY_TO_MANY", "La población participante contiene N:M sin autorización explícita.")
        count = int(matched)
        if join["type"] in {"LEFT", "FULL"}:
            count += int(left_total - matched_left)
        if join["type"] in {"RIGHT", "FULL"}:
            count += int(right_total - matched_right)
        expansion = count / max(1, left_total, right_total)
        if count > limits["max_join_rows"] or expansion > limits["max_join_expansion"]:
            raise SandboxError("REPORT_JOIN_EXPANSION", "El cruce supera el límite de filas o expansión verificado.")
        results.append({"right_alias": right, "declared": join["expected_cardinality"],
                        "observed": observed, "many_to_many_keys": int(nm_keys),
                        "joined_rows": count, "left_rows": int(left_total), "right_rows": int(right_total),
                        "expansion": expansion, "discrepancy": observed != join["expected_cardinality"]})
    return results


def run(payload):
    global duckdb
    sources, limits, plan = payload["sources"], payload["limits"], payload["plan"]
    staging = payload.get("staging")
    # Kill the executor if its coordinator dies, including while DuckDB is
    # computing and before the first pipe write notices consumer disappearance.
    libc = ctypes.CDLL(None, use_errno=True)
    parent = os.getppid()
    if libc.prctl(1, 9, 0, 0, 0) != 0 or os.getppid() != parent:
        raise SandboxError("REPORT_PARENT_LOST", "El coordinador de la consulta ya no está disponible.")
    sandbox(sources, limits, staging)
    if payload.get("probe"):
        import socket
        import threading
        checks = {}
        for key, action in {
            "network_denied": lambda: socket.socket(),
            "outside_read_denied": lambda: Path(payload["probe"]["forbidden"]).read_bytes(),
            "outside_write_denied": lambda: Path(payload["probe"]["write_target"]).write_bytes(b"forbidden"),
        }.items():
            try:
                action()
                checks[key] = False
            except OSError:
                checks[key] = True
        checks["sensitive_environment_absent"] = not any("PASSWORD" in k or "SECRET" in k or k == "DATABASE_URL" for k in os.environ)
        inherited = []
        def inherited_policy():
            try:
                Path(payload["probe"]["forbidden"]).read_bytes()
                inherited.append(False)
            except OSError:
                inherited.append(True)
        thread = threading.Thread(target=inherited_policy)
        thread.start()
        thread.join(timeout=2)
        checks["confined_threads_available"] = inherited == [True] and not thread.is_alive()
        libc = ctypes.CDLL(None, use_errno=True)
        checks["clone3_unavailable"] = libc.syscall(435, 0, 0) == -1 and ctypes.get_errno() == errno.ENOSYS
        try:
            child = cast(Any, os).fork()
        except OSError as exc:
            checks["process_creation_denied"] = exc.errno == errno.EPERM
        else:
            if child == 0:
                os._exit(0)
            os.waitpid(child, 0)
            checks["process_creation_denied"] = False
        emit("probe", checks=checks)
        return
    # DuckDB's module import may create its default connection's scheduler
    # threads. Import it only after both OS policies, so every thread inherits
    # the Landlock domain even on kernels without Landlock TSYNC.
    import duckdb as engine
    duckdb = engine
    params = {key: parameter(value) for key, value in plan["parameters"].items()}
    connection = duckdb.connect(":memory:", config={
        "threads": limits["threads"], "memory_limit": f"{limits['memory_bytes']}B",
        "temp_directory": staging or "", "max_temp_directory_size": f"{limits['temp_bytes']}B" if staging else "0B",
        "autoload_known_extensions": False, "autoinstall_known_extensions": False,
        "allow_unsigned_extensions": False, "enable_external_access": True,
        "preserve_insertion_order": True,
    })
    started = time.monotonic()
    initial_usage = linux_resource.getrusage(linux_resource.RUSAGE_SELF)
    try:
        connection.execute("SET allowed_paths = ?", [[path for source in sources for path in source["paths"]]])
        if staging:
            connection.execute("SET allowed_directories = ?", [[staging]])
        for source in sources:
            typed_view(connection, source, params)
        # Engine access settings complement OS confinement. No extension load,
        # catalog/file/table functions reach user SQL through the AST policy.
        connection.execute("SET enable_external_access=false")
        connection.execute("SET lock_configuration=true")
        checks = cardinality(connection, plan, params, limits)
        emit("cardinality", items=checks)
        cursor = connection.execute(plan["sql"], bind_parameters(plan["sql"], params))
        columns = [{"name": c[0], "type": str(c[1])} for c in cursor.description]
        emit("schema", columns=columns)
        rows = bytes_out = 0
        while True:
            batch = cursor.fetchmany(min(limits["batch_rows"], 10 if payload["profile"] == "PREVIEW" else limits["batch_rows"]))
            if not batch:
                break
            encoded = [[tagged(cell) for cell in row] for row in batch]
            size = len(json.dumps(encoded, ensure_ascii=False, separators=(",", ":")).encode())
            if size > limits["batch_bytes"]:
                raise SandboxError("REPORT_BATCH_LIMIT", "Un lote excede el presupuesto de memoria del canal.")
            rows += len(batch)
            bytes_out += size
            if rows > limits["max_rows"] or bytes_out > limits["max_bytes"]:
                raise SandboxError("REPORT_RESULT_LIMIT", "El resultado excede el límite; no se truncó para declarar éxito.")
            emit("batch", rows=encoded)
            if payload["profile"] == "PREVIEW":
                break
        usage = linux_resource.getrusage(linux_resource.RUSAGE_SELF)
        observed = {}
        for path, metric in (("/sys/fs/cgroup/memory.current", "cgroup_memory_current_bytes"),
                             ("/sys/fs/cgroup/memory.peak", "cgroup_memory_lifetime_peak_bytes")):
            try:
                observed[metric] = int(Path(path).read_text().strip())
            except (OSError, ValueError):
                pass  # Optional counter; never report an unavailable measure as zero.
        emit("complete", rows=rows, bytes=bytes_out, elapsed_seconds=time.monotonic() - started,
             max_rss_bytes=usage.ru_maxrss * 1024,
             cpu_user_seconds=usage.ru_utime - initial_usage.ru_utime,
             cpu_system_seconds=usage.ru_stime - initial_usage.ru_stime,
             sample=payload["profile"] == "PREVIEW", **observed)
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        line = sys.stdin.buffer.readline(2 * 1024 * 1024 + 1)
        if len(line) > 2 * 1024 * 1024:
            raise SandboxError("REPORT_CONTEXT_LIMIT", "Contexto técnico demasiado grande.")
        run(json.loads(line))
    except SandboxError as exc:
        emit("error", code=exc.code, message=exc.message)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - standalone trust boundary emits sanitized codes only.
        if duckdb is not None and isinstance(exc, duckdb.OutOfMemoryException):
            emit("error", code="REPORT_MEMORY_LIMIT", message="La consulta excedió memoria sin spill autorizado.")
        elif duckdb is not None and isinstance(exc, duckdb.Error):
            emit("error", code="REPORT_QUERY_FAILED", message="La consulta falló por tipo, precisión o semántica SQL; revisa el diagnóstico.")
        else:
            emit("error", code="REPORT_EXECUTOR_FAILED", message="El ejecutor aislado falló sin publicar resultados parciales.")
        sys.exit(1)
