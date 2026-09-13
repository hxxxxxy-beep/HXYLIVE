"""Dual-disk storage policy for HXYLIVE.

SSD (staging): hot .ts writes only. Stop captures when free space is critically
low; resume automatically only after staging recording files are fully cleared.

HDD (library): durable library + convert + app data. When free space falls below
the configured threshold, turn global Auto Record off and block re-enable until
space recovers; never auto-re-enable recording after a library stop.

Emergency library deletes from the old single-disk water-level design are gone.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

GB = 1024 ** 3

DEFAULT_STAGING_STOP_GB = 5
DEFAULT_LIBRARY_STOP_GB = 100
MIN_STAGING_STOP_GB = 2
MAX_STAGING_STOP_GB = 20
MIN_LIBRARY_STOP_GB = 50
MAX_LIBRARY_STOP_GB = 500

STORAGE_POLL_SECONDS = 60
STORAGE_POLL_STRESSED_SECONDS = 10


def normalize_staging_stop_gb(value: Any, default: int = DEFAULT_STAGING_STOP_GB) -> int:
    try:
        gb = int(value)
    except (TypeError, ValueError):
        gb = default
    return max(MIN_STAGING_STOP_GB, min(MAX_STAGING_STOP_GB, gb))


def normalize_library_stop_gb(value: Any, default: int = DEFAULT_LIBRARY_STOP_GB) -> int:
    try:
        gb = int(value)
    except (TypeError, ValueError):
        gb = default
    return max(MIN_LIBRARY_STOP_GB, min(MAX_LIBRARY_STOP_GB, gb))


@dataclass(frozen=True)
class StorageGateDecision:
    library_free_bytes: int
    staging_free_bytes: int
    library_blocked: bool
    staging_blocked: bool
    pause_convert: bool


def storage_gate_decision(
    *,
    library_free_bytes: int,
    staging_free_bytes: int,
    library_stop_bytes: int,
    staging_stop_bytes: int,
    library_blocked: bool,
    staging_blocked: bool,
    staging_has_files: bool,
) -> StorageGateDecision:
    """Evaluate library/staging gates for one free-space sample."""
    try:
        library_free = max(0, int(library_free_bytes))
    except (TypeError, ValueError):
        library_free = 0
    try:
        staging_free = max(0, int(staging_free_bytes))
    except (TypeError, ValueError):
        staging_free = 0
    try:
        library_stop = max(0, int(library_stop_bytes))
    except (TypeError, ValueError):
        library_stop = DEFAULT_LIBRARY_STOP_GB * GB
    try:
        staging_stop = max(0, int(staging_stop_bytes))
    except (TypeError, ValueError):
        staging_stop = DEFAULT_STAGING_STOP_GB * GB

    if library_free < library_stop:
        next_library_blocked = True
    elif library_blocked and library_free >= library_stop:
        # Space recovered: clear the hard disable so the user can turn Auto Record
        # back on, but do not imply recording should resume by itself.
        next_library_blocked = False
    else:
        next_library_blocked = bool(library_blocked)

    if staging_free < staging_stop:
        next_staging_blocked = True
    elif staging_blocked and not staging_has_files:
        next_staging_blocked = False
    else:
        next_staging_blocked = bool(staging_blocked)

    pause_convert = next_library_blocked or library_free < library_stop

    return StorageGateDecision(
        library_free_bytes=library_free,
        staging_free_bytes=staging_free,
        library_blocked=next_library_blocked,
        staging_blocked=next_staging_blocked,
        pause_convert=pause_convert,
    )


def disk_free_bytes(path: Path) -> int:
    """Return free bytes for the filesystem containing ``path``."""
    target = Path(path)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        return max(0, int(shutil.disk_usage(str(target)).free))
    except OSError:
        return 0


def staging_recording_files(staging_records_root: Path) -> list[Path]:
    """List recording-related files still on the staging volume."""
    root = Path(staging_records_root)
    if not root.exists():
        return []
    found: list[Path] = []
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            name = path.name
            suffix = path.suffix.lower()
            if suffix == ".ts" or name.endswith(".tmp.ts") or ".promoting." in name:
                found.append(path)
    except OSError:
        return found
    return found


def staging_has_recording_files(staging_records_root: Path) -> bool:
    return bool(staging_recording_files(staging_records_root))


def promote_staging_file_to_library(
    staging_path: Path,
    *,
    staging_records_root: Path,
    library_records_root: Path,
) -> Optional[Path]:
    """Move one closed staging file into the library tree. Returns dest path."""
    src = Path(staging_path).resolve()
    staging_root = Path(staging_records_root).resolve()
    library_root = Path(library_records_root).resolve()
    try:
        relative = src.relative_to(staging_root)
    except ValueError:
        return None
    if not src.is_file():
        return None

    dest = (library_root / relative).resolve()
    if not dest.is_relative_to(library_root):
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        # Avoid clobbering an existing library object; drop empty staging dupes.
        try:
            if src.stat().st_size <= 0:
                src.unlink(missing_ok=True)
                return dest
        except OSError:
            pass
        stem = dest.stem
        suffix = dest.suffix
        for index in range(1, 1000):
            candidate = dest.with_name(f"{stem}_staging{index}{suffix}")
            if not candidate.exists():
                dest = candidate
                break
        else:
            return None

    # Same-filesystem rename when possible; otherwise copy+delete across disks.
    try:
        os.replace(str(src), str(dest))
    except OSError:
        shutil.copy2(str(src), str(dest))
        try:
            src.unlink()
        except OSError:
            pass

    _cleanup_empty_parents(src.parent, stop_at=staging_root)
    return dest


def promote_closed_staging_files(
    *,
    staging_records_root: Path,
    library_records_root: Path,
    active_path_keys: Optional[Iterable[str]] = None,
    path_key_fn=None,
) -> list[Path]:
    """Promote every staging recording file that is not an active writer target."""
    active = {str(key) for key in (active_path_keys or set()) if str(key)}
    moved: list[Path] = []
    for src in staging_recording_files(staging_records_root):
        key = ""
        if path_key_fn is not None:
            try:
                key = str(path_key_fn(src) or "")
            except Exception:
                key = ""
        else:
            try:
                key = str(src.resolve())
            except OSError:
                key = str(src)
        if key and key in active:
            continue
        dest = promote_staging_file_to_library(
            src,
            staging_records_root=staging_records_root,
            library_records_root=library_records_root,
        )
        if dest is not None:
            moved.append(dest)
    return moved


def _cleanup_empty_parents(start: Path, *, stop_at: Path) -> None:
    current = Path(start)
    stop = Path(stop_at).resolve()
    while True:
        try:
            resolved = current.resolve()
        except OSError:
            break
        if resolved == stop or not resolved.is_relative_to(stop):
            break
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent
