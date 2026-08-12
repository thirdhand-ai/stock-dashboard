"""File-based lock preventing overlapping automation runs.

Uses a plain OS-level advisory lock (fcntl.flock) rather than a database
row or a sentinel file with a manually-managed staleness timeout - the OS
releases the lock the instant the holding process exits, cleanly or via
crash, so there is no stale-lock cleanup logic to get wrong.

POSIX-only (macOS/Linux) - consistent with the rest of this project's
deployment assumptions (nohup-based local dev server, etc.); Windows is not
a target environment here.
"""
import contextlib
import fcntl
import os


class LockHeldError(Exception):
    """Another automation run is already in progress."""


@contextlib.contextmanager
def acquire_run_lock(lock_path: str):
    lock_dir = os.path.dirname(lock_path)
    if lock_dir:
        os.makedirs(lock_dir, exist_ok=True)

    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        raise LockHeldError(f"another automation run already holds the lock at {lock_path}")

    try:
        fd.write(str(os.getpid()))
        fd.flush()
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
