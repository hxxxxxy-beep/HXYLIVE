"""Recording project grouping: gap buffer, clips/parts, finalize ready."""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.recording_names import normalize_source_marker

_PART_RE = re.compile(r"(?i)_part(\d{3,})$")
_STEM_TS_RE = re.compile(
    r"(?<!\d)(\d{4})[-_]?(\d{2})[-_]?(\d{2})[ T_-](\d{2})[:.\-]?(\d{2})[:.\-]?(\d{2})"
)
_STEM_COMPACT_RE = re.compile(r"(?<!\d)(\d{8})[_-](\d{6})(?!\d)")

DEFAULT_GAP_BUFFER_MINUTES = 30
ALLOWED_GAP_BUFFER_MINUTES = frozenset({15, 30, 60, 90})


def normalize_gap_buffer_minutes(value: object, default: int = DEFAULT_GAP_BUFFER_MINUTES) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = default
    if minutes not in ALLOWED_GAP_BUFFER_MINUTES:
        return default
    return minutes


def new_project_id(username: str, started_at: int) -> str:
    stamp = datetime.fromtimestamp(max(0, int(started_at or 0))).strftime("%Y%m%d_%H%M%S")
    digest = hashlib.sha1(f"{username}\0{started_at}\0{secrets.token_hex(4)}".encode()).hexdigest()[:6]
    safe_user = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(username or "user")).strip(".-") or "user"
    return f"{stamp}__{safe_user[:24]}__{digest}"


def strip_part_suffix(stem: str) -> tuple[str, Optional[str], Optional[int]]:
    text = str(stem or "").strip()
    match = _PART_RE.search(text)
    if not match:
        return text, None, None
    part_num = int(match.group(1))
    base = text[: match.start()]
    return base, f"P{part_num}", part_num


def parse_started_at_from_stem(stem: str, fallback: int = 0) -> int:
    text = str(stem or "").strip()
    base, _, _ = strip_part_suffix(text)
    match = _STEM_TS_RE.search(base)
    if match:
        try:
            dt = datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                int(match.group(4)),
                int(match.group(5)),
                int(match.group(6)),
            )
            return int(dt.timestamp())
        except (OverflowError, OSError, ValueError):
            pass
    match = _STEM_COMPACT_RE.search(base)
    if match:
        ymd, hms = match.group(1), match.group(2)
        try:
            dt = datetime(
                int(ymd[0:4]),
                int(ymd[4:6]),
                int(ymd[6:8]),
                int(hms[0:2]),
                int(hms[2:4]),
                int(hms[4:6]),
            )
            return int(dt.timestamp())
        except (OverflowError, OSError, ValueError):
            pass
    return int(fallback or 0)


def session_key_from_stem(stem: str) -> str:
    """Group forced same-session parts that share stem before _partNNN."""
    base, part_label, _ = strip_part_suffix(stem)
    if part_label:
        return base
    return stem


def format_clock(ts: int) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def format_span_line(started_at: int, ended_at: int, duration_seconds: int) -> str:
    """Total duration / time span; show one value when they match."""
    span = max(0, int(ended_at or 0) - int(started_at or 0))
    dur = max(0, int(duration_seconds or 0))
    if span <= 0 and dur <= 0:
        return ""
    if span <= 0:
        return _fmt_hms(dur)
    if dur <= 0:
        return _fmt_hms(span)
    if abs(span - dur) <= 2:
        return _fmt_hms(dur)
    return f"{_fmt_hms(dur)} / {_fmt_hms(span)}"


def _fmt_hms(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m" if s == 0 else f"{h}h {m}m {s}s"
    if m:
        return f"{m}m" if s == 0 else f"{m}m {s}s"
    return f"{s}s"


def build_clip_structure(members: list[dict[str, Any]]) -> str:
    """Clip 1 (P1,P2) · Clip 2 (P1)."""
    if not members:
        return ""
    ordered = sorted(
        members,
        key=lambda row: (
            int(row.get("started_at") or 0),
            int(row.get("sort_index") or 0),
            str(row.get("filename") or ""),
        ),
    )
    clips: list[tuple[str, list[str]]] = []
    current_key = None
    current_parts: list[str] = []
    for row in ordered:
        stem = str(row.get("filename") or "")
        if stem.lower().endswith((".mp4", ".ts", ".mkv", ".webm", ".mov", ".m4v")):
            stem = stem.rsplit(".", 1)[0]
        key = session_key_from_stem(stem)
        _, part_label, _ = strip_part_suffix(stem)
        label = part_label or "P1"
        if key != current_key:
            if current_key is not None:
                clips.append((current_key, current_parts))
            current_key = key
            current_parts = [label]
        else:
            if label not in current_parts:
                current_parts.append(label)
    if current_key is not None:
        clips.append((current_key, current_parts))
    parts_out = []
    for idx, (_key, labels) in enumerate(clips, start=1):
        parts_out.append(f"Clip {idx} ({', '.join(labels)})")
    return " · ".join(parts_out)


def mac_project_relative_dir(streamer_folder: str, project_id: str) -> str:
    safe_project = re.sub(r"[\\/]+", "_", str(project_id or "").strip()) or "project"
    folder = str(streamer_folder or "unknown").strip().strip("/")
    return f"{folder}/.projects/{safe_project}"


def mac_final_relative_path(streamer_folder: str, started_at: int, ended_at: int, ext: str = ".mp4") -> str:
    folder = str(streamer_folder or "unknown").strip().strip("/")
    start = datetime.fromtimestamp(max(0, int(started_at or 0)))
    end = datetime.fromtimestamp(max(0, int(ended_at or started_at or 0)))
    name = (
        f"{start.strftime('%Y-%m-%d %H-%M-%S')}_to_{end.strftime('%H-%M-%S')}"
        f"{ext if ext.startswith('.') else '.' + ext}"
    )
    return f"{folder}/{name}"


async def resolve_gap_buffer_seconds(db, username: str) -> int:
    """Per-streamer override, else global setting, else 30m."""
    raw_user = None
    try:
        raw_user = await db.get_setting(f"gap_buffer_minutes:{username}")
    except Exception:
        raw_user = None
    if raw_user is not None and str(raw_user).strip() == "":
        raw_user = None
    if raw_user is None:
        try:
            model = await db.get_model(username)
            if model and model.get("gap_buffer_minutes") is not None:
                raw_user = model.get("gap_buffer_minutes")
        except Exception:
            pass
    if raw_user is not None and str(raw_user).strip() != "":
        return normalize_gap_buffer_minutes(raw_user) * 60
    raw = await db.get_setting("gap_buffer_minutes")
    return normalize_gap_buffer_minutes(raw) * 60


async def assign_fragment_to_project(
    db,
    *,
    username: str,
    filename: str,
    recording_id: Optional[str] = None,
    item_id: Optional[str] = None,
    started_at: int = 0,
    duration_seconds: int = 0,
    size_bytes: int = 0,
    source_type: Optional[str] = None,
    force_session_key: Optional[str] = None,
) -> dict[str, Any]:
    """Attach a converted MP4 (or ready fragment) to an open/ready-gap project."""
    stem = Path_stem(filename)
    parsed_start = parse_started_at_from_stem(stem, started_at or int(time.time()))
    started = int(started_at or parsed_start or time.time())
    duration = max(0, int(duration_seconds or 0))
    ended = started + duration if duration > 0 else started
    size_bytes = max(0, int(size_bytes or 0))
    source = normalize_source_marker(source_type) or "chaturbate"
    gap_seconds = await resolve_gap_buffer_seconds(db, username)
    session_key = force_session_key or session_key_from_stem(stem)
    _, part_label, part_num = strip_part_suffix(stem)

    existing = await db.get_project_member(username, filename)
    if existing:
        project = await db.get_recording_project(existing["project_id"])
        await db.upsert_project_member(
            project_id=existing["project_id"],
            username=username,
            filename=filename,
            recording_id=recording_id or existing.get("recording_id"),
            item_id=item_id or existing.get("item_id"),
            started_at=started,
            ended_at=ended,
            duration_seconds=duration,
            size_bytes=size_bytes or int(existing.get("size_bytes") or 0),
            part_label=part_label or existing.get("part_label"),
            session_key=session_key,
            sort_index=part_num or int(existing.get("sort_index") or 0),
        )
        if project:
            await _refresh_project_bounds(db, project["project_id"])
            return await db.get_recording_project(project["project_id"]) or project
        return existing

    # Prefer same session_key open project (forced parts), then gap merge.
    candidates = await db.list_open_recording_projects(username)
    chosen = None
    for project in candidates:
        if str(project.get("status") or "") not in {"open", "awaiting_gap", "ready"}:
            continue
        members = await db.list_project_members(project["project_id"])
        if any(str(m.get("session_key") or "") == session_key for m in members):
            chosen = project
            break
    if chosen is None:
        for project in candidates:
            if str(project.get("status") or "") not in {"open", "awaiting_gap", "ready"}:
                continue
            project_end = int(project.get("ended_at") or project.get("started_at") or 0)
            gap = started - project_end
            if 0 <= gap <= gap_seconds or gap < 0:
                chosen = project
                break

    if chosen is None:
        project_id = new_project_id(username, started)
        await db.create_recording_project(
            project_id=project_id,
            username=username,
            source_type=source,
            started_at=started,
            ended_at=ended,
            status="open",
            gap_buffer_seconds=gap_seconds,
        )
        chosen = await db.get_recording_project(project_id)

    project_id = str(chosen["project_id"])
    await db.upsert_project_member(
        project_id=project_id,
        username=username,
        filename=filename,
        recording_id=recording_id,
        item_id=item_id,
        started_at=started,
        ended_at=ended,
        duration_seconds=duration,
        size_bytes=size_bytes,
        part_label=part_label,
        session_key=session_key,
        sort_index=part_num or 0,
    )
    await _refresh_project_bounds(db, project_id)
    # New fragment after ready → reopen until gap elapses again.
    await db.update_recording_project(
        project_id,
        status="open",
        ready_at=None,
        updated_at=int(time.time()),
    )
    return await db.get_recording_project(project_id) or chosen


async def _refresh_project_bounds(db, project_id: str) -> None:
    members = await db.list_project_members(project_id)
    if not members:
        return
    started = min(int(m.get("started_at") or 0) for m in members)
    ended = max(int(m.get("ended_at") or m.get("started_at") or 0) for m in members)
    await db.update_recording_project(
        project_id,
        started_at=started or None,
        ended_at=ended or None,
        updated_at=int(time.time()),
    )


async def finalize_ready_projects(db, *, now: Optional[int] = None) -> list[str]:
    """Mark open/awaiting_gap projects ready when gap buffer elapsed."""
    now_ts = int(now or time.time())
    ready_ids: list[str] = []
    projects = await db.list_recording_projects(statuses=["open", "awaiting_gap"])
    for project in projects:
        project_id = str(project.get("project_id") or "")
        ended = int(project.get("ended_at") or project.get("started_at") or 0)
        gap = int(project.get("gap_buffer_seconds") or DEFAULT_GAP_BUFFER_MINUTES * 60)
        if ended <= 0:
            continue
        if now_ts - ended < gap:
            if str(project.get("status")) != "awaiting_gap":
                await db.update_recording_project(
                    project_id,
                    status="awaiting_gap",
                    updated_at=now_ts,
                )
            continue
        await db.update_recording_project(
            project_id,
            status="ready",
            ready_at=now_ts,
            updated_at=now_ts,
        )
        ready_ids.append(project_id)
    return ready_ids


def Path_stem(filename: str) -> str:
    name = str(filename or "").replace("\\", "/").split("/")[-1]
    if "." in name:
        return name.rsplit(".", 1)[0]
    return name


async def backfill_library_fragments(db, library_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Assign library MP4s missing from project_members into recording projects."""
    existing = set(await db.list_all_project_member_keys())
    assigned = 0
    skipped = 0
    for item in library_items or []:
        if str(item.get("type") or "") != "video":
            continue
        username = str(item.get("username") or "").strip()
        filename = str(item.get("filename") or "").strip()
        if not username or not filename:
            continue
        if not filename.lower().endswith((".mp4", ".mkv", ".webm", ".mov", ".m4v")):
            skipped += 1
            continue
        key = (username, Path(filename).name)
        if key in existing:
            skipped += 1
            continue
        try:
            await assign_fragment_to_project(
                db,
                username=username,
                filename=Path(filename).name,
                recording_id=str(item.get("recordingId") or "") or None,
                item_id=str(item.get("id") or "") or None,
                started_at=int(item.get("createdAt") or 0),
                duration_seconds=int(item.get("duration") or item.get("durationSeconds") or 0),
                size_bytes=int(item.get("size") or 0),
                source_type=str(item.get("sourceType") or item.get("source_type") or "") or None,
            )
            existing.add(key)
            assigned += 1
        except Exception:
            skipped += 1
    return {"assigned": assigned, "skipped": skipped}


def project_display_dict(
    project: dict[str, Any],
    members: list[dict[str, Any]],
    *,
    sync_statuses: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    sync_statuses = sync_statuses or {}
    total_size = sum(int(m.get("size_bytes") or 0) for m in members)
    synced_size = 0
    synced_count = 0
    for member in members:
        item_id = str(member.get("item_id") or "")
        status = sync_statuses.get(item_id) or member.get("sync_status") or ""
        if status == "synced" or str(member.get("location") or "") == "mac":
            synced_count += 1
            synced_size += int(member.get("size_bytes") or 0)
    duration = sum(int(m.get("duration_seconds") or 0) for m in members)
    started = int(project.get("started_at") or 0)
    ended = int(project.get("ended_at") or started)
    status = str(project.get("status") or "open")
    local_complete = synced_count == len(members) and len(members) > 0
    if local_complete and status in {"ready", "open", "awaiting_gap", "syncing"}:
        status_display = "local_complete"
    else:
        status_display = status
    title = format_clock(started)
    if status in {"ready", "local_complete", "concatenated", "concat_failed"} or local_complete:
        end_label = format_clock(ended)
        if end_label:
            title = f"{title} – {end_label}" if title else end_label
    return {
        "projectId": project.get("project_id"),
        "username": project.get("username"),
        "sourceType": project.get("source_type"),
        "status": status_display,
        "concatStatus": project.get("concat_status") or "none",
        "startedAt": started,
        "endedAt": ended,
        "readyAt": project.get("ready_at"),
        "title": title,
        "spanLine": format_span_line(started, ended, duration),
        "clipStructure": build_clip_structure(members),
        "memberCount": len(members),
        "syncedCount": synced_count,
        "totalSize": total_size,
        "syncedSize": synced_size,
        "durationSeconds": duration,
        "gapBufferSeconds": project.get("gap_buffer_seconds"),
        "members": members,
        "localComplete": local_complete,
    }
