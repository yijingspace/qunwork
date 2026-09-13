"""Disk-space guard for write-heavy surfaces (the swarm, report exports, …).

Why a write PROBE and not just ``shutil.disk_usage``: we hit a box where ``C:``
reported 51.9 GB free yet **every** write >= 16 KB failed with ENOSPC (``E:`` even
reported a *negative* free-byte count). Statvfs was lying, so a swarm run started
happily, wrote six deliverables, then left the seventh as a 0-byte file and froze
as a permanently-"running" ghost while the run store silently stopped accepting
writes.

So the check is, in order:
  1. the directory can be created,
  2. statvfs free space clears a floor (cheap early signal, good error message),
  3. a real (small) probe file can actually be allocated and removed.

Callers get a human-readable ``error`` naming the volume and the reason, so the UI
can say "磁盘空间不足" instead of showing a blank document.
"""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

# Headroom a swarm run needs to be worth starting: a 7-task run writes ~0.2 MB of
# deliverables, but worker engines keep scratch files and the run store/logs grow.
# Below this we refuse up front rather than half-finish.
MIN_FREE_BYTES = 64 * 1024 * 1024
# Small enough to fit in the last free clusters (a nearly-full-but-working volume
# still passes), large enough to need a real allocation (the broken "reports free
# space but cannot allocate" volume fails).
PROBE_BYTES = 256 * 1024


@dataclass
class SpaceReport:
    ok: bool
    error: str = ""
    free_bytes: Optional[int] = None
    reason: str = "ok"  # "ok" | "unwritable" | "low-space" | "alloc-failed"


def human_size(n: Optional[int]) -> str:
    if n is None:
        return "unknown"
    for unit, step in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if n >= step:
            return f"{n / step:.1f} {unit}"
    return f"{n} B"


def free_bytes(path: Union[str, Path]) -> Optional[int]:
    """Free bytes on the volume holding ``path`` (None when it can't be queried)."""
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return None


def disk_label(path: Union[str, Path]) -> str:
    """The drive/volume a path lives on, for error copy ('C:\\', '/', …)."""
    p = Path(path)
    try:
        anchor = p.resolve().anchor
    except OSError:
        anchor = p.anchor
    return anchor or str(p)


def _probe_allocation(directory: Path, probe_bytes: int) -> None:
    """Allocate + remove a probe file. Raises OSError when the volume can't.

    Split out (rather than inlined) so tests can simulate an allocation failure
    without needing a full disk.
    """
    probe = directory / f".qunwork-writeprobe-{uuid.uuid4().hex[:8]}"
    try:
        with open(probe, "wb") as fh:
            fh.write(b"\0" * probe_bytes)
            fh.flush()
    finally:
        try:
            probe.unlink()
        except OSError:
            pass  # best effort — a broken volume may refuse even the unlink


def check_writable(
    path: Union[str, Path],
    *,
    min_free: int = MIN_FREE_BYTES,
    probe_bytes: int = PROBE_BYTES,
) -> SpaceReport:
    """Can this directory actually receive a write right now?

    ``ok=False`` carries a user-facing ``error`` when the directory is unusable, the
    volume is below ``min_free``, or the probe allocation fails.
    """
    directory = Path(path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return SpaceReport(
            ok=False,
            reason="unwritable",
            error=f"cannot create the working directory {directory}: {exc}",
        )

    free = free_bytes(directory)
    if free is not None and free < min_free:
        return SpaceReport(
            ok=False,
            reason="low-space",
            free_bytes=free,
            error=(
                f"{disk_label(directory)} has only {human_size(free)} free "
                f"(needs at least {human_size(min_free)})"
            ),
        )

    try:
        _probe_allocation(directory, probe_bytes)
    except OSError as exc:
        # The volume reported free space but cannot allocate: statvfs is wrong
        # (thin-provisioned/host-full volume, or a damaged free-space map).
        return SpaceReport(
            ok=False,
            reason="alloc-failed",
            free_bytes=free,
            error=(
                f"{disk_label(directory)} cannot allocate new data "
                f"(reports {human_size(free)} free): {exc}"
            ),
        )

    return SpaceReport(ok=True, reason="ok", free_bytes=free)


def assert_writable(path: Union[str, Path], **kwargs) -> None:
    """Raise ``OSError`` carrying the report's message when the path isn't writable."""
    report = check_writable(path, **kwargs)
    if not report.ok:
        raise OSError(report.error)
