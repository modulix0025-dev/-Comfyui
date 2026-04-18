"""
core.locking — atomic cross-process file lock.

Uses O_CREAT|O_EXCL for race-free acquisition. Honors a wait budget with
polling. Reclaims locks left behind by crashed processes (older than
LOCK_STALE_AGE_SEC). Always releases via try/finally in the caller.
"""

import os
import time

LOCK_FILENAME      = ".packager.lock"
LOCK_POLL_MS       = 50
LOCK_MAX_WAIT_SEC  = 3.0
LOCK_STALE_AGE_SEC = 60.0


def acquire_lock(lock_path):
    """
    Try to create the lock atomically. Returns (fd_or_None, acquired_bool).

    Behavior:
      • O_CREAT|O_EXCL — fails if the lock already exists (atomic).
      • If existing lock is older than LOCK_STALE_AGE_SEC, reclaim it.
      • Poll every LOCK_POLL_MS until LOCK_MAX_WAIT_SEC elapses.
      • On total failure, return (None, False). Caller must then fail safely.
    """
    deadline = time.time() + LOCK_MAX_WAIT_SEC
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, f"pid={os.getpid()} ts={time.time():.3f}".encode())
            except Exception:
                pass
            return fd, True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > LOCK_STALE_AGE_SEC:
                    os.remove(lock_path)
                    continue
            except Exception:
                pass
            if time.time() >= deadline:
                return None, False
            time.sleep(LOCK_POLL_MS / 1000.0)
        except Exception:
            if time.time() >= deadline:
                return None, False
            time.sleep(LOCK_POLL_MS / 1000.0)


def release_lock(fd, lock_path):
    """Always-safe lock release. Never raises."""
    try:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
    finally:
        try:
            if os.path.exists(lock_path):
                os.remove(lock_path)
        except Exception:
            pass
