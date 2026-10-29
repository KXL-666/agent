"""One backend process per database, using an OS lock released on process exit."""
from contextlib import contextmanager
from pathlib import Path
import os


@contextmanager
def backend_lease(database: str):
    path = Path(str(Path(database).resolve()) + ".runtime.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Do not unlink the lock file: unlinking permits two locks on different inodes.
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("该数据库已有后端进程。当前架构仅支持一个 worker，请勿使用 --workers 多进程启动。") from error
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
