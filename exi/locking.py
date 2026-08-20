"""Cross-platform advisory file locking (standard library only).

Every mutation in this project serializes on a small ``*.lock`` sidecar file so
two concurrent ``exi`` / hook processes cannot interleave a read-modify-write
and lose an update. Historically that used ``fcntl.flock`` imported at module
top, which raises ``ImportError`` the instant any of those modules is merely
*imported* on Windows — before a single lock is taken. This module isolates the
platform dependency behind one small abstraction so the rest of the codebase
never imports ``fcntl`` (or ``msvcrt``) directly and imports cleanly everywhere.

Backends:

* POSIX  -> ``fcntl.flock(fd, LOCK_EX)`` / ``LOCK_UN`` (whole-file advisory).
* Windows -> ``msvcrt.locking(fd, LK_NBLCK, 1)`` over a single byte at offset 0,
  wrapped in a bounded retry/backoff loop so a contended lock blocks instead of
  raising ``EDEADLOCK`` immediately (``LK_LCK`` only retries ~10 times then
  raises, and cannot be interrupted; a manual non-blocking loop is friendlier).

The public surface is intentionally tiny:

* ``file_lock(path)`` — a context manager that creates ``path`` if needed,
  takes an exclusive lock for the duration of the ``with`` block, and always
  releases the lock and closes the handle on exit (so the suite can run with
  ``-W error::ResourceWarning`` without leaking file objects).
* ``PosixLockBackend`` / ``WindowsLockBackend`` — the two acquire/release
  implementations, each accepting an injectable ``os`` module (POSIX) or
  ``msvcrt`` module (Windows) so the Windows code path can be unit-tested with a
  fake backend on a POSIX CI runner, where the real ``msvcrt`` does not exist.
"""
from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

# The single byte, at offset 0, that the Windows backend locks. msvcrt locks a
# *region* of the file measured from the current seek position; locking one
# byte at the start is the well-worn idiom for a whole-file mutex and works even
# when the file is empty (a region may extend at/over EOF).
_WINDOWS_LOCK_BYTES = 1

# Bounded backoff for the Windows non-blocking lock loop.
_WIN_RETRY_SLEEP = 0.02
_WIN_MAX_WAIT_SECONDS = 30.0

IS_WINDOWS = os.name == "nt"


class PosixLockBackend:
    """Whole-file advisory lock via ``fcntl.flock``."""

    def __init__(self, fcntl_mod=None, sleep=time.sleep):
        if fcntl_mod is None:
            import fcntl as fcntl_mod  # imported lazily; never at Windows import time
        self._fcntl = fcntl_mod
        self._sleep = sleep

    def acquire(self, fileobj) -> None:
        self._fcntl.flock(fileobj.fileno(), self._fcntl.LOCK_EX)

    def release(self, fileobj) -> None:
        self._fcntl.flock(fileobj.fileno(), self._fcntl.LOCK_UN)


class WindowsLockBackend:
    """Single-byte exclusive lock via ``msvcrt.locking`` with a retry loop.

    ``msvcrt_mod`` is injectable so the acquire/release logic can be exercised
    on a POSIX runner with a fake module that records calls (and can simulate a
    contended lock by raising ``OSError`` a few times before succeeding).
    """

    def __init__(self, msvcrt_mod=None, sleep=time.sleep, max_wait=_WIN_MAX_WAIT_SECONDS,
                 monotonic=time.monotonic):
        if msvcrt_mod is None:
            import msvcrt as msvcrt_mod  # only reachable on real Windows
        self._msvcrt = msvcrt_mod
        self._sleep = sleep
        self._max_wait = max_wait
        self._monotonic = monotonic

    def acquire(self, fileobj) -> None:
        fileobj.seek(0)
        deadline = self._monotonic() + self._max_wait
        while True:
            try:
                self._msvcrt.locking(fileobj.fileno(), self._msvcrt.LK_NBLCK, _WINDOWS_LOCK_BYTES)
                return
            except OSError:
                if self._monotonic() >= deadline:
                    raise
                self._sleep(_WIN_RETRY_SLEEP)

    def release(self, fileobj) -> None:
        # Unlock the exact same region that was locked (offset 0, one byte).
        fileobj.seek(0)
        self._msvcrt.locking(fileobj.fileno(), self._msvcrt.LK_UNLCK, _WINDOWS_LOCK_BYTES)


def _default_backend():
    return WindowsLockBackend() if IS_WINDOWS else PosixLockBackend()


# Module-level default, resolved once. Callers that want the fake-Windows path in
# a test construct their own backend and use `file_lock_with(...)`.
_BACKEND = None


def _backend():
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = _default_backend()
    return _BACKEND


@contextlib.contextmanager
def file_lock(path):
    """Exclusive advisory lock over ``path`` for the ``with`` block.

    Creates the parent directory and the lock file if missing, yields the open
    handle, and guarantees the lock is released and the handle closed on exit —
    even if the body raises. Opened in ``a+`` (never ``w``) so the lock file is
    not truncated out from under a concurrent holder on Windows and there is a
    stable byte to lock.
    """
    with file_lock_with(_backend(), path) as f:
        yield f


@contextlib.contextmanager
def file_lock_with(backend, path):
    """Like :func:`file_lock` but against an explicit backend (for tests)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        backend.acquire(f)
        try:
            yield f
        finally:
            backend.release(f)
    finally:
        f.close()
