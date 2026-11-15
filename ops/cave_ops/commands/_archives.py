"""Backup archives: what ``backup`` writes, ``restore`` reads and ``status`` lists.

An archive ``cave-backup-YYYYMMDDTHHMMSSZ.tar.gz`` (mode 0600: it holds every secret) contains

``manifest.json``
    ``format``, ``created``, ``release`` (``git describe`` of the checkout), ``release_tag`` (the
    newest release it contains, or ``null``), ``data`` (``CAVE_DATA`` it was taken from) and
    ``databases`` (the SQLite files copied with the online backup).
``env``
    The checkout's ``.env``.
``data/...``
    The state entries of ``CAVE_DATA`` (``_server.state_entries``) with their modes, times and
    owners. Every SQLite database is copied with SQLite's online backup, which gives a
    consistent copy while Home Assistant writes to it; its ``-wal``, ``-shm`` and ``-journal``
    files are left out, because the copy already contains what they hold. Every other file is
    copied as it is at that moment.
"""

from __future__ import annotations

import datetime
import os
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

from ..errors import CaveError

PREFIX = "cave-backup-"
SUFFIX = ".tar.gz"
PARTIAL = ".partial"
FORMAT = 1
_NAME = re.compile(rf"^{PREFIX}(\d{{8}}T\d{{6}}Z)(?:-(\d+))?{re.escape(SUFFIX)}$")
_SQLITE_HEADER = b"SQLite format 3\x00"
SIDECARS = ("-wal", "-shm", "-journal")


def archive_name(directory: Path, when: datetime.datetime | None = None) -> str:
    """A new archive name in ``directory`` that sorts after every existing one of that second."""
    when = when or datetime.datetime.now(datetime.UTC)
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    counters = [
        int(match.group(2) or 1)
        for name in archives(directory)
        if (match := _NAME.match(name)) and match.group(1) == stamp
    ]
    if not counters:
        return f"{PREFIX}{stamp}{SUFFIX}"
    return f"{PREFIX}{stamp}-{max(counters) + 1}{SUFFIX}"


def archives(directory: Path) -> list[str]:
    """The archives in ``directory``, oldest first."""
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    found = [(match, name) for name in names if (match := _NAME.match(name))]
    # By time, then by the counter that a second archive in the same second gets ("-2").
    found.sort(key=lambda item: (item[0].group(1), int(item[0].group(2) or 1)))
    return [name for _match, name in found]


def prune(directory: Path, keep: int) -> list[str]:
    """Remove all but the newest ``keep`` archives, and unfinished ones; return what went."""
    names = archives(directory)
    removed = names[:-keep] if keep > 0 else names
    for name in removed:
        (directory / name).unlink()
    for name in os.listdir(directory):
        if name.startswith(f".{PREFIX}") and name.endswith(PARTIAL):
            (directory / name).unlink()
    return removed


def copy_archive(source: Path, directory: Path) -> Path:
    """Copy ``source`` into ``directory`` under the same name; complete or not at all."""
    partial = directory / f".{source.name}{PARTIAL}"
    try:
        shutil.copyfile(source, partial)
        os.chmod(partial, 0o600)
        with open(partial, "rb") as handle:
            os.fsync(handle.fileno())
        target = directory / source.name
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return target


# ----------------------------------------------------------------------------------- SQLite
def is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    except OSError:
        return False


def is_sidecar(path: Path) -> bool:
    """A ``-wal``, ``-shm`` or ``-journal`` file next to a SQLite database."""
    for suffix in SIDECARS:
        if path.name.endswith(suffix):
            return is_sqlite(path.with_name(path.name[: -len(suffix)]))
    return False


def online_copy(source: Path, target: Path) -> None:
    """Copy a live SQLite database with SQLite's online backup, and check the copy.

    The backup runs in one step under one read transaction, so the copy is the database as it
    was at one moment, whatever another process writes meanwhile.
    """
    uri = f"file:{urllib.parse.quote(str(source))}?mode=ro"
    reader = sqlite3.connect(uri, uri=True, timeout=60)
    try:
        writer = sqlite3.connect(target)
        try:
            reader.backup(writer)
            verdict = writer.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            writer.close()
    finally:
        reader.close()
    if verdict != "ok":
        raise CaveError(f"the copy of {source} failed SQLite's check: {verdict}")


# --------------------------------------------------------------------------------- writing
def _info(name: str, info: os.stat_result, kind: bytes, size: int = 0) -> tarfile.TarInfo:
    entry = tarfile.TarInfo(name)
    entry.type = kind
    entry.mode = stat.S_IMODE(info.st_mode)
    entry.uid, entry.gid = info.st_uid, info.st_gid
    entry.mtime = int(info.st_mtime)
    entry.size = size
    return entry


def add_file(archive: tarfile.TarFile, name: str, source: Path, info: os.stat_result) -> None:
    """Add the file ``source`` (a spooled copy) under ``name`` with the metadata ``info``."""
    with open(source, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        archive.addfile(_info(name, info, tarfile.REGTYPE, size), handle)


def add_tree(
    archive: tarfile.TarFile, data: Path, paths: Any, spool: Path, databases: list[str]
) -> int:
    """Add every path of ``paths`` (below ``data``) as ``data/<relative>``; return the count.

    A file is first copied to ``spool``, so that one which grows or shrinks while it is read
    (a log) cannot break the archive. A file that disappears meanwhile is left out.
    """
    count = 0
    for path in paths:
        relative = path.relative_to(data).as_posix()
        name = f"data/{relative}"
        try:
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                archive.addfile(_info(name, info, tarfile.DIRTYPE))
            elif stat.S_ISLNK(info.st_mode):
                entry = _info(name, info, tarfile.SYMTYPE)
                entry.linkname = os.readlink(path)
                archive.addfile(entry)
            elif stat.S_ISREG(info.st_mode):
                if is_sidecar(path):
                    continue
                copy = spool / "file"
                if is_sqlite(path):
                    copy.unlink(missing_ok=True)
                    online_copy(path, copy)
                    databases.append(relative)
                else:
                    shutil.copyfile(path, copy)
                add_file(archive, name, copy, info)
                copy.unlink()
            else:
                continue
        except FileNotFoundError:
            continue
        count += 1
    return count


def spool_dir(directory: Path) -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix=".cave-backup-spool-", dir=directory)
