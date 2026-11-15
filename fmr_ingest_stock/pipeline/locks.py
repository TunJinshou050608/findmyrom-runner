"""Non-blocking POSIX file locks for collector and publisher isolation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import time


class LockBusyError(RuntimeError):
    """Raised immediately when another process already holds an exclusive pipeline lock."""

    def __init__(self, lock_path: Path) -> None:
        super().__init__(f"Lock is already held by another process: {lock_path}")
        self.lock_path = lock_path


@contextmanager
def acquire_file_lock(lock_path: str | Path) -> Iterator[Path]:
    """Acquire a non-blocking exclusive POSIX flock, raising LockBusyError if contended."""
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (BlockingIOError, OSError) as exc:
            err_no = getattr(exc, "errno", None)
            if isinstance(exc, BlockingIOError) or err_no in (
                errno.EACCES,
                errno.EAGAIN,
                errno.EWOULDBLOCK,
            ):
                raise LockBusyError(path) from exc
            raise

        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"pid={os.getpid()} acquired_at={int(time.time())}\n".encode("ascii"))
        yield path
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


__all__ = ["LockBusyError", "acquire_file_lock"]
