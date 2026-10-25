"""Trusted optional RO-cache advice child; no file contents or global cache control."""
import json
import math
import os
import signal
import stat
import sys
import time

ROOTS = ("/opt/venv", "/models/paddle", "/models/layout", "/models/ollama")
MAX_FILES, MAX_BYTES = 100000, 32 * 1024**3


class OwnershipUnknown(ValueError):
    pass


def identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid,
        value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def require(value):
    if not value:
        raise OwnershipUnknown("CACHE_OWNERSHIP_UNVERIFIED")


def validate_file(value, device):
    require(stat.S_ISREG(value.st_mode) and value.st_uid == 65532 and value.st_dev == device and value.st_nlink == 1)


def advise(seconds, *, clock=time.monotonic):
    require(os.getuid() == os.getgid() == 65532)
    deadline = clock() + seconds
    counts = {"advised_files": 0, "bytes_in_scope": 0, "symlinks_skipped": 0, "advice_os_errors": 0}
    def check():
        if clock() >= deadline:
            raise TimeoutError("CACHE_ADVICE_TIMEOUT")
    for root_path in ROOTS:
        check()
        root = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            before_root = os.fstat(root)
            require(stat.S_ISDIR(before_root.st_mode) and before_root.st_uid == 65532
                and os.fstatvfs(root).f_flag & os.ST_RDONLY)
            for _parent, directories, files, directory in os.fwalk(".", follow_symlinks=False, dir_fd=root):
                check()
                before_directory = os.fstat(directory)
                require(before_directory.st_uid == 65532 and before_directory.st_dev == before_root.st_dev)
                kept = []
                for name in directories:
                    check()
                    entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if stat.S_ISLNK(entry.st_mode):
                        counts["symlinks_skipped"] += 1
                    else:
                        require(stat.S_ISDIR(entry.st_mode) and entry.st_uid == 65532 and entry.st_dev == before_root.st_dev)
                        kept.append(name)
                directories[:] = kept
                for name in files:
                    check()
                    entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if stat.S_ISLNK(entry.st_mode):
                        counts["symlinks_skipped"] += 1
                        continue
                    validate_file(entry, before_root.st_dev)
                    counts["advised_files"] += 1; counts["bytes_in_scope"] += entry.st_size
                    require(counts["advised_files"] <= MAX_FILES and counts["bytes_in_scope"] <= MAX_BYTES)
                    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                    try:
                        held = os.fstat(descriptor)
                        require(identity(held) == identity(entry))
                        try:
                            os.posix_fadvise(descriptor, 0, 0, os.POSIX_FADV_DONTNEED)
                        except OSError:
                            counts["advice_os_errors"] += 1
                        require(identity(held) == identity(os.fstat(descriptor))
                            == identity(os.stat(name, dir_fd=directory, follow_symlinks=False)))
                    finally:
                        os.close(descriptor)
                require(identity(before_directory) == identity(os.fstat(directory)))
            require(identity(before_root) == identity(os.fstat(root))
                == identity(os.stat(root_path, follow_symlinks=False)))
        finally:
            os.close(root)
    return {"status": "ADVICE_ACCEPTED" if not counts["advice_os_errors"] else "PARTIAL_ADVICE_OS_ERRORS", **counts,
        "data_file_content_bytes_read": 0, "filesystem_bytes_modified": 0,
        "reclaim_or_cold_state_guaranteed": False}


def main():
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (128 * 1024**2, 128 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (4, 4))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    seconds = float(sys.argv[1])
    require(math.isfinite(seconds) and 0 < seconds <= 4)
    def timeout(_signum, _frame): raise TimeoutError("CACHE_ADVICE_TIMEOUT")
    signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        result = advise(seconds)
    except TimeoutError:
        result = {"status": "ADVICE_TIMEOUT", "reclaim_or_cold_state_guaranteed": False}
    except (OwnershipUnknown, OSError):
        result = {"status": "OWNERSHIP_UNVERIFIED", "error_code": "CACHE_OWNERSHIP_UNVERIFIED"}
    except (ValueError, TypeError, KeyError, MemoryError):
        result = {"status": "ADVICE_UNAVAILABLE", "reclaim_or_cold_state_guaranteed": False}
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    print(json.dumps(result, allow_nan=False), flush=True)


if __name__ == "__main__": main()
