"""A cross-process mutex around Hyperliquid API access.

poller.py and cohort.py are separate processes that share one rate limit:
/info is throttled by request *weight* per IP (~1200/min), and each script
budgets its delays assuming it is the only caller. Run them together and the
combined weight exceeds the ceiling, which shows up as 429s in whichever one
happens to ask next - usually the poller's fill loop, since it is the
heaviest.

They cannot simply be scheduled apart. The cohort refresh runs 30-50 minutes
and the poll cycle is ~10 and growing with the panel, so any fixed offset
eventually collides. This lock makes the two serialize on contact instead.

Staleness is judged on the lock file's mtime, NOT on whether the recorded pid
is alive. os.kill(pid, 0) is the usual liveness probe, but on Windows it does
not probe - it routes to TerminateProcess and would kill the very job we are
checking on. The pid in the file is therefore diagnostic only.
"""

import errno
import os
import time

from .config import BASE_DIR

LOCK_PATH = os.path.join(BASE_DIR, ".api.lock")

# Longer than any legitimate holder: a 600-candidate cohort run is ~50 min and
# the scheduled task gives up at PT2H. A lock older than this means the holder
# was killed (ctrl-c, reboot, Task Scheduler hard-terminate) without cleaning
# up, so it is safe to break.
STALE_SECONDS = 2 * 60 * 60


class Busy(Exception):
    """Another process holds the lock."""


def _read() -> str:
    try:
        with open(LOCK_PATH) as f:
            return f.read().strip()
    except OSError:
        return "unknown"


def _break_if_stale() -> bool:
    """Remove the lock if its holder is long gone. True if it was broken."""
    try:
        age = time.time() - os.path.getmtime(LOCK_PATH)
    except OSError:
        return False  # vanished between calls - the next O_EXCL settles it
    if age < STALE_SECONDS:
        return False
    try:
        os.unlink(LOCK_PATH)
    except OSError:
        return False
    return True


def acquire(owner: str) -> None:
    """Take the lock or raise Busy. O_CREAT|O_EXCL is the atomic part."""
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
        if not _break_if_stale():
            raise Busy(_read())
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w") as f:
        f.write(f"{owner} pid={os.getpid()} since={time.strftime('%Y-%m-%d %H:%M:%S')}")


def release() -> None:
    try:
        os.unlink(LOCK_PATH)
    except OSError:
        pass


class held:
    """Context manager. `wait` seconds of patience, 0 to fail immediately.

    Used two ways on purpose: the poller passes wait=0 and skips the cycle
    (a missed cycle costs nothing - fills are watermark-driven and catch up,
    and the page already treats unobserved hours as null rather than zero),
    while the daily cohort refresh waits out a poll cycle rather than
    forfeiting its run until tomorrow.
    """

    def __init__(self, owner: str, wait: float = 0, poll: float = 15.0):
        self.owner, self.wait, self.poll = owner, wait, poll

    def __enter__(self):
        deadline = time.time() + self.wait
        while True:
            try:
                acquire(self.owner)
                return self
            except Busy:
                if time.time() >= deadline:
                    raise
                time.sleep(self.poll)

    def __exit__(self, *exc):
        release()
        return False
