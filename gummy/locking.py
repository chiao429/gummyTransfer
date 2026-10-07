"""Process locks shared by local CLI, scheduler and the durable task worker."""

import hashlib
import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url

from gummy.errors import RunConflict


@contextmanager
def process_lock(db, purpose="collection"):
    if purpose not in {"collection", "admission", "task-worker", "scheduler", "schedule-control"}:
        raise ValueError("Unknown lock purpose")
    parsed = make_url(str(db.engine.url))
    if parsed.drivername == "sqlite":
        path = Path(parsed.database).resolve().with_suffix("." + purpose + ".lock")
        if path.is_symlink():
            raise RunConflict("Unsafe local task lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            stat = os.fstat(fd)
            if stat.st_nlink != 1 or (hasattr(os, "getuid") and stat.st_uid != os.getuid()):
                raise RunConflict("Unsafe local task lock")
            try:
                if os.name == "nt":
                    import msvcrt

                    if stat.st_size == 0:
                        os.write(fd, b"0")
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RunConflict("Another task is currently using this resource") from None
            yield
        finally:
            os.close(fd)
    else:
        key = int.from_bytes(hashlib.sha256(purpose.encode()).digest()[:8], "big", signed=True)
        with db.engine.connect() as conn:
            if not conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar():
                raise RunConflict("Another task is currently using this resource")
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
