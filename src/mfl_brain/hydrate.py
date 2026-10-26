"""On-demand hydration for macOS FileProvider placeholders (OneDrive/iCloud).

Files On-Demand placeholders report a size but have st_blocks == 0 (dataless).
Reading one triggers the provider to download the content; reads attempted
before the download completes fail with EAGAIN/EDEADLK (errno 11/35, both
surface as "Resource deadlock avoided"). The correct strategy is to stream
the file with patient retries until it materialises.
"""

from __future__ import annotations

import errno
import os
import time
from pathlib import Path

# macOS: EAGAIN=35, EDEADLK=11 — both render as "Resource deadlock avoided"
TRANSIENT_ERRNOS = {errno.EAGAIN, errno.EDEADLK, 11, 35, 36}

CHUNK = 1024 * 1024  # 1 MB streaming read granularity


def is_dataless(path: Path | str) -> bool:
    """True if the file is a FileProvider placeholder (metadata only)."""
    st = os.stat(path)
    return st.st_blocks == 0 and st.st_size > 0


def hydration_timeout_for(size_bytes: int) -> float:
    """Generous, size-aware budget: ~3s/MB, floor 30s, cap 15 min."""
    return min(900.0, max(30.0, 3.0 * size_bytes / 1e6))


def hydrate(path: Path | str, on_progress=None) -> bool:
    """Stream-read the file to force the provider download.

    Returns True once the content is materialised (readable end to end).
    Retries transient EAGAIN/EDEADLK errors with growing waits until the
    size-aware timeout budget is exhausted.
    """
    path = Path(path)
    st = os.stat(path)
    if st.st_blocks > 0:
        return True  # already local
    deadline = time.time() + hydration_timeout_for(st.st_size)
    wait = 1.0
    while time.time() < deadline:
        try:
            with open(path, "rb") as f:
                while f.read(CHUNK):  # streaming download
                    pass
            return True
        except OSError as exc:
            if exc.errno in TRANSIENT_ERRNOS:
                if on_progress:
                    on_progress(path, wait)
                time.sleep(wait)
                wait = min(wait * 1.6, 20.0)
                continue
            raise
    return False


def ensure_hydrated(path: Path | str) -> None:
    """Raise a transient OSError if the placeholder cannot be fetched."""
    if is_dataless(path) and not hydrate(path):
        raise OSError(errno.EAGAIN, "hydration timeout (OneDrive download pending)")
