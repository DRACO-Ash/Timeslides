"""The one place this application writes a file.

Everything the app persists goes to the volume mounted at STORAGE_MOUNT_PATH,
and on this platform that volume is S3-backed. S3-backed FUSE mounts commonly
implement neither fsync nor rename, so the POSIX habit of writing a temp file
and renaming it over the target does not work there: it fails with ENOSYS,
Function not implemented.

That failure was fixed once in the group store and left in place in the job
runner, which wrote report files with exactly the same pattern. Two write
paths meant two chances to get it wrong, and the second one broke every render
on the deployed app. So there is one write path now, in this module, and both
callers use it.

The mechanism is discovered by probing rather than assumed:

  atomic    temp file beside the target, flush, fsync where implemented,
            rename. The one to prefer: a reader sees either the whole old file
            or the whole new one.
  direct    straight over the top of the target. For mounts with no rename.
  recreate  remove the target, then create it fresh. For object-store mounts
            that take a new key written sequentially but refuse to open an
            existing one for writing.

Only the first is crash-safe. The other two exist because a mount that cannot
rename would otherwise take the whole application down, and a file that
survives a restart but not a crash is far better than no file at all.
"""

from __future__ import annotations

import contextlib
import errno
import os
from pathlib import Path

from .audit import event

# fsync is a durability guarantee, not part of the atomicity guarantee: the
# temp-file-then-rename is what makes a replacement atomic. Several
# filesystems, S3-backed FUSE mounts among them, do not implement fsync and
# answer ENOSYS. Losing the flush on such a mount is a far better trade than
# refusing to write at all, which is what the deployed app did.
_FSYNC_UNSUPPORTED = frozenset(
    code for code in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP,
                      errno.ENOTSUP, errno.EBADF, errno.EPERM, errno.EACCES)
    if code is not None)

# What the operator should actually do, keyed on the errno rather than a guess.
# An earlier version said "if this is EACCES, set fsGroup" for every failure. A
# live deployment then reported a bare OSError, which cannot be EACCES at all:
# Python raises PermissionError for EACCES and EPERM, so a plain OSError means
# something else entirely, and the message sent the operator after the wrong
# cause.
_WRITE_ADVICE = {
    errno.EACCES: (
        "the volume is not writable by this container's user. The pod runs as "
        "uid 1000, so the storage volume needs fsGroup set in the pod "
        "securityContext."),
    errno.EPERM: (
        "the operation was not permitted. Check fsGroup in the pod "
        "securityContext and any securityContext capability restrictions."),
    errno.EROFS: (
        "the filesystem is read-only, which usually means no storage volume is "
        "mounted here at all and this path is part of the container's own "
        "read-only root. Enable the persistent storage add-on for the app and "
        "mount it at this path."),
    errno.ENOSPC: "the volume is full.",
    errno.EDQUOT: "the volume's quota is exhausted.",
    errno.EXDEV: (
        "the temporary file and the target are on different filesystems, so "
        "the atomic rename cannot complete. Something is mounted over part of "
        "this path."),
    errno.ENOENT: "the path does not exist and could not be created.",
    errno.ENOSYS: (
        "the filesystem does not implement that call. S3-backed FUSE mounts "
        "typically implement neither fsync nor rename, so the writer probes "
        "what the mount supports and picks a strategy to match."),
}


def write_failure_advice(exc: OSError) -> str:
    """Name the errno, quote the OS, then say what to do about it."""
    code = exc.errno
    name = errno.errorcode.get(code, "unknown")
    detail = f"{type(exc).__name__} {name}"
    if code is not None:
        detail += f" (errno {code})"
    if exc.strerror:
        detail += f": {exc.strerror}"
    advice = _WRITE_ADVICE.get(code)
    return f"{detail}. {advice}" if advice else detail


def _fsync_best_effort(fh) -> None:
    try:
        os.fsync(fh.fileno())
    except OSError as exc:
        if exc.errno not in _FSYNC_UNSUPPORTED:
            raise
        event("storage.fsync_unsupported",
              code=errno.errorcode.get(exc.errno, exc.errno))


def ensure_parent(target: Path) -> None:
    """Try to create the parent directory. Never fail because of it.

    On an object-store mount there are no real directories: the key separator
    only looks like one. mkdir there can fail with ENOSYS for a path that is
    perfectly writable, and mountpoint-for-s3 in particular refuses mkdir while
    accepting a write to a key beneath it. Raising on mkdir would have turned
    a working volume into an unusable one.

    So the write is the arbiter. If the parent really was needed and really
    could not be made, the write fails next and reports the actual errno.
    """
    parent = target.parent
    if parent.is_dir():
        return
    with contextlib.suppress(OSError):
        parent.mkdir(parents=True, exist_ok=True)


def _write_atomic(target: Path, payload: str) -> None:
    ensure_parent(target)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            _fsync_best_effort(fh)
        os.replace(tmp, target)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _write_direct(target: Path, payload: str) -> None:
    ensure_parent(target)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        _fsync_best_effort(fh)


def _write_recreate(target: Path, payload: str) -> None:
    """Remove the target, then create it fresh.

    The removal is the dangerous part. Two earlier versions got it wrong: the
    first unlinked with no way back, so on a volume where the write then also
    failed the file was simply gone; the second restored it afterwards, which
    only worked because the restore used a different call than the write.

    So nothing is removed until the mount has proved it will take a write. A
    sibling file is written first, and if that fails the target is never
    touched.
    """
    ensure_parent(target)
    spare = target.with_name(f".{target.name}.{os.getpid()}.spare")
    try:
        _write_direct(spare, payload)
    except OSError:
        with contextlib.suppress(OSError):
            spare.unlink(missing_ok=True)
        raise
    try:
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
        _write_direct(target, payload)
    finally:
        with contextlib.suppress(OSError):
            spare.unlink(missing_ok=True)


WRITE_STRATEGIES = (
    ("atomic", _write_atomic),
    ("direct", _write_direct),
    ("recreate", _write_recreate),
)
STRATEGY_NOTES = {
    "atomic": "atomic replace (temp file and rename)",
    "direct": ("written in place, because this mount does not support rename. "
               "A crash part-way through a write could leave a file truncated"),
    "recreate": ("removed and rewritten, because this mount supports neither "
                 "rename nor overwrite. A crash part-way through a write could "
                 "lose a file"),
}


class VolumeWriter:
    """Writes files to one volume, using the best mechanism it supports.

    Shared by the group store and the job runner, and probed once per process
    rather than per write, so a mount without rename costs one failed attempt
    in total instead of one per save.
    """

    def __init__(self):
        self._strategy = None

    @property
    def strategy(self):
        """Which mechanism the writer settled on, once it has written."""
        return self._strategy

    def write(self, target, payload: str) -> str:
        """Write payload to target. Returns the strategy that worked.

        Raises the last OSError if no mechanism works. Callers decide what to
        do about that; this module does not paper over an unusable volume.
        """
        target = Path(target)
        strategies = list(WRITE_STRATEGIES)
        if self._strategy is not None:
            at = [i for i, (name, _) in enumerate(strategies)
                  if name == self._strategy]
            strategies = strategies[at[0]:] if at else strategies
        last = None
        for name, write in strategies:
            try:
                write(target, payload)
            except OSError as exc:
                last = exc
                event("storage.strategy_failed", strategy=name,
                      code=errno.errorcode.get(exc.errno, exc.errno))
                continue
            if name != self._strategy:
                self._strategy = name
                event("storage.strategy", strategy=name, path=str(target))
            return name
        raise last

    def probe(self, near) -> tuple:
        """(ok, detail). Run the real write mechanisms against a probe file
        beside `near`, and settle on the best one this mount supports.

        stat tells you almost nothing useful here: not whether the volume is
        read-only, not whether fsGroup was applied, and not whether the
        filesystem implements fsync or rename. An earlier probe wrote an empty
        file with write_text, which exercised none of the steps a real write
        takes, so it reported an S3-backed mount writable while every write on
        it failed. A probe that does not run the real path is worse than no
        probe: it manufactures confidence.
        """
        near = Path(near)
        probe = near.with_name(f".writetest.{os.getpid()}")
        try:
            strategy = self.write(probe, '{"probe": true}')
        except OSError as exc:
            return False, write_failure_advice(exc)
        finally:
            with contextlib.suppress(OSError):
                probe.unlink(missing_ok=True)
        return True, f"writable, {STRATEGY_NOTES[strategy]}"
