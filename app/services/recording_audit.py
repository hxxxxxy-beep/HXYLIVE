"""Durable recording lifecycle audit (survives container recreate).

Writes append-only JSONL under ``<output_dir>/audit/recording-lifecycle.jsonl``
and a small ``media-busy.json`` snapshot used after process boot to detect
orphan recordings (busy before restart, process gone after).

Fixed event vocabulary (keep this list small — forensic timeline, not a metrics system):

- ``record_start`` / ``record_stop`` / ``record_discarded``
- ``orphan_after_restart`` (busy snapshot said recording; process gone after boot)
- ``convert_start`` / ``convert_ok`` / ``convert_fail`` / ``ts_deleted`` / ``mp4_ready``
- ``recording_deleted`` / ``vps_purged_after_mac``
- ``shutdown_begin``
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional, Set

_LOCK = threading.Lock()
_OUTPUT_DIR: Optional[Path] = None
_CONVERTING: dict[str, dict[str, Any]] = {}


def configure(output_dir: Path | str) -> None:
    global _OUTPUT_DIR
    _OUTPUT_DIR = Path(output_dir)


def _root() -> Path:
    if _OUTPUT_DIR is not None:
        return _OUTPUT_DIR
    return Path(os.getenv("OUTPUT_DIR", "data"))


def audit_dir() -> Path:
    path = _root() / "audit"
    path.mkdir(parents=True, exist_ok=True)
    return path


def lifecycle_log_path() -> Path:
    return audit_dir() / "recording-lifecycle.jsonl"


def busy_snapshot_path() -> Path:
    return audit_dir() / "media-busy.json"


def emit(event: str, **fields: Any) -> None:
    """Append one lifecycle event. Best-effort; never raise to callers."""
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ts_unix": int(time.time()),
        "event": str(event),
    }
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, Path):
            payload[key] = str(value)
        else:
            payload[key] = value
    line = json.dumps(payload, ensure_ascii=False, default=str)
    try:
        with _LOCK:
            path = lifecycle_log_path()
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except OSError:
        pass


def mark_converting(
    *,
    username: str,
    filename: str,
    path: Path | str,
    recording_id: Optional[str] = None,
) -> None:
    key = str(path)
    with _LOCK:
        _CONVERTING[key] = {
            "username": username,
            "filename": filename,
            "path": key,
            "recordingId": recording_id,
            "startedAt": int(time.time()),
        }
    emit(
        "convert_start",
        username=username,
        filename=filename,
        path=key,
        recordingId=recording_id,
    )


def clear_converting(path: Path | str) -> None:
    key = str(path)
    with _LOCK:
        _CONVERTING.pop(key, None)


def converting_items() -> list[dict[str, Any]]:
    with _LOCK:
        return list(_CONVERTING.values())


def refresh_busy_snapshot(ffmpeg_statuses: Optional[list[dict]] = None) -> dict[str, Any]:
    """Rewrite media-busy.json for post-restart orphan reconcile."""
    recording = []
    for status in ffmpeg_statuses or []:
        if not status.get("running"):
            continue
        recording.append({
            "sessionId": status.get("id"),
            "username": status.get("person") or status.get("name"),
            "recordPath": status.get("record_path"),
            "bytesWritten": int(status.get("bytes_written") or 0),
            "sourceType": status.get("source_type"),
        })
    converting = converting_items()
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ts_unix": int(time.time()),
        "busy": bool(recording or converting),
        "recordingCount": len(recording),
        "convertingCount": len(converting),
        "recording": recording,
        "converting": converting,
    }
    try:
        path = busy_snapshot_path()
        tmp = path.with_suffix(".tmp")
        with _LOCK:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
    except OSError:
        pass
    return payload


def read_busy_snapshot() -> dict[str, Any]:
    path = busy_snapshot_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "busy": False,
            "recordingCount": 0,
            "convertingCount": 0,
            "recording": [],
            "converting": [],
            "missing": True,
        }


def write_shutdown_snapshot(ffmpeg_statuses: Optional[list[dict]] = None) -> dict[str, Any]:
    """Emit shutdown_begin and freeze the busy file for post-mortem."""
    snapshot = refresh_busy_snapshot(ffmpeg_statuses)
    emit(
        "shutdown_begin",
        busy=snapshot.get("busy"),
        recordingCount=snapshot.get("recordingCount"),
        convertingCount=snapshot.get("convertingCount"),
        recording=snapshot.get("recording"),
        converting=snapshot.get("converting"),
    )
    snapshot["shutdown"] = True
    try:
        path = busy_snapshot_path()
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return snapshot


def _path_stat(path: Optional[str]) -> tuple[bool, int]:
    if not path:
        return False, 0
    try:
        st = os.stat(path)
        return True, int(st.st_size)
    except OSError:
        return False, 0


def reconcile_orphans_after_restart(
    alive_session_ids: Optional[Iterable[str]] = None,
) -> list[dict[str, Any]]:
    """Compare previous media-busy.json to live sessions after process boot.

    Emits ``orphan_after_restart`` for each recording that was marked busy but
    is no longer an alive ffmpeg session. Reports whether the TS path still
    exists so a missing Mac sync can be explained from the JSONL alone.
    """
    previous = read_busy_snapshot()
    if previous.get("missing"):
        return []

    alive: Set[str] = {str(x) for x in (alive_session_ids or []) if x}
    orphans: list[dict[str, Any]] = []

    for item in previous.get("recording") or []:
        session_id = str(item.get("sessionId") or "")
        if session_id and session_id in alive:
            continue
        record_path = item.get("recordPath")
        exists, size = _path_stat(record_path)
        payload = {
            "sessionId": session_id or None,
            "username": item.get("username"),
            "path": record_path,
            "bytesWrittenPrior": item.get("bytesWritten"),
            "sourceType": item.get("sourceType"),
            "pathExists": exists,
            "pathBytes": size,
            "previousBusyTs": previous.get("ts"),
            "previousShutdown": bool(previous.get("shutdown")),
            "reason": (
                "shutdown_snapshot"
                if previous.get("shutdown")
                else "busy_without_alive_session"
            ),
        }
        emit("orphan_after_restart", **payload)
        orphans.append(payload)

    for item in previous.get("converting") or []:
        convert_path = item.get("path")
        exists, size = _path_stat(convert_path)
        payload = {
            "username": item.get("username"),
            "filename": item.get("filename"),
            "path": convert_path,
            "recordingId": item.get("recordingId"),
            "pathExists": exists,
            "pathBytes": size,
            "previousBusyTs": previous.get("ts"),
            "previousShutdown": bool(previous.get("shutdown")),
            "reason": "converting_interrupted",
        }
        emit("orphan_after_restart", **payload)
        orphans.append(payload)

    # Clear stale converting memory; disk snapshot will be rewritten by caller.
    with _LOCK:
        _CONVERTING.clear()

    return orphans


# ---------------------------------------------------------------------------
# UI helpers: read JSONL + group into one row per recording session
# ---------------------------------------------------------------------------

_EVENT_TEXT = {
    "record_start": "Started recording",
    "record_stop": "Stopped recording",
    "record_discarded": "Discarded recording",
    "convert_start": "Convert started",
    "convert_ok": "Convert OK",
    "convert_fail": "Convert failed",
    "ts_deleted": "TS deleted after convert",
    "mp4_ready": "MP4 ready",
    "orphan_after_restart": "Orphan after restart",
    "recording_deleted": "Recording deleted",
    "vps_purged_after_mac": "VPS purged after Mac sync",
    "disk_pressure_delete": "Deleted under disk pressure",
    "shutdown_begin": "Shutdown begin",
    "sync_queued": "Mac sync queued",
}

def _basename(path: Optional[str]) -> str:
    if not path:
        return ""
    return Path(str(path)).name


def _stem(path: Optional[str]) -> str:
    name = _basename(path)
    if not name:
        return ""
    return Path(name).stem


def _format_bytes(n: Any) -> str:
    try:
        value = int(n)
    except (TypeError, ValueError):
        return ""
    if value < 0:
        return ""
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{value} B"


def _format_duration(seconds: Any) -> str:
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    if total < 0:
        return ""
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def humanize_event(event: dict[str, Any]) -> str:
    """One-line English label for a raw lifecycle event."""
    name = str(event.get("event") or "")
    base = _EVENT_TEXT.get(name, name.replace("_", " ") or "event")
    bits: list[str] = [base]
    reason = event.get("reason")
    if reason:
        if name == "record_discarded" and reason == "empty":
            bits.append("empty fragment, no video written")
        else:
            bits.append(str(reason))
    if name == "record_stop":
        bw = _format_bytes(event.get("bytesWritten"))
        if bw:
            bits.append(bw)
        elapsed = _format_duration(event.get("elapsedSeconds"))
        if elapsed:
            bits.append(elapsed)
        if event.get("exitCode") not in (None, 0):
            bits.append(f"exit {event.get('exitCode')}")
    elif name == "convert_ok":
        size = _format_bytes(event.get("mp4Size"))
        if size:
            bits.append(size)
    elif name == "convert_fail":
        attempt = event.get("attempt")
        if attempt is not None:
            bits.append(f"attempt {attempt}")
    elif name == "sync_queued":
        count = event.get("itemCount")
        if count is not None:
            bits.append(f"{count} item(s)")
    elif name == "orphan_after_restart":
        if event.get("pathExists") is False:
            bits.append("path missing")
        elif event.get("pathBytes") is not None:
            bw = _format_bytes(event.get("pathBytes"))
            if bw:
                bits.append(bw)
    return " — ".join(bits)


def read_lifecycle_events(
    *,
    since_unix: Optional[int] = None,
    until_unix: Optional[int] = None,
    max_lines: int = 20000,
) -> list[dict[str, Any]]:
    """Read recent JSONL events (oldest→newest within the window)."""
    path = lifecycle_log_path()
    if not path.exists():
        return []
    max_lines = max(1, min(int(max_lines or 20000), 100000))
    try:
        with path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    if len(lines) > max_lines:
        lines = lines[-max_lines:]

    events: list[dict[str, Any]] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        ts_unix = row.get("ts_unix")
        try:
            ts_unix_i = int(ts_unix) if ts_unix is not None else None
        except (TypeError, ValueError):
            ts_unix_i = None
        if since_unix is not None and ts_unix_i is not None and ts_unix_i < since_unix:
            continue
        if until_unix is not None and ts_unix_i is not None and ts_unix_i > until_unix:
            continue
        if ts_unix_i is not None:
            row["ts_unix"] = ts_unix_i
        events.append(row)
    return events


def _path_keys(event: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for field in ("path", "recordPath"):
        value = event.get(field)
        if value:
            keys.append(f"path:{value}")
            stem = _stem(str(value))
            user = event.get("username") or ""
            if stem:
                keys.append(f"stem:{user}:{stem}")
    filename = event.get("filename")
    if filename:
        user = event.get("username") or ""
        keys.append(f"stem:{user}:{_stem(str(filename))}")
        keys.append(f"name:{user}:{filename}")
    return keys


def _session_lookup_keys(event: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    session_id = event.get("sessionId")
    if session_id:
        keys.append(f"session:{session_id}")
    recording_id = event.get("recordingId")
    if recording_id:
        keys.append(f"rec:{recording_id}")
    keys.extend(_path_keys(event))
    return keys


def _derive_outcome(events: list[dict[str, Any]]) -> tuple[str, str]:
    """Return (outcome, summary) for a grouped session."""
    names = [str(e.get("event") or "") for e in events]
    last_by_name = {str(e.get("event") or ""): e for e in events}

    if "record_discarded" in names:
        reason = str(last_by_name["record_discarded"].get("reason") or "discarded")
        if reason == "empty":
            return "discarded", "Empty fragment — no video written"
        return "discarded", f"Discarded — {reason}"
    if "convert_fail" in names:
        attempt = last_by_name["convert_fail"].get("attempt")
        suffix = f" (attempt {attempt})" if attempt is not None else ""
        return "failed", f"Convert failed{suffix}"
    if "orphan_after_restart" in names:
        reason = last_by_name["orphan_after_restart"].get("reason") or "interrupted"
        return "interrupted", f"Interrupted — {reason}"

    stop = last_by_name.get("record_stop")
    if stop is not None:
        reason = str(stop.get("reason") or "")
        exit_code = stop.get("exitCode")
        if reason == "ffmpeg_error" or (exit_code not in (None, 0) and reason != "api_stop"):
            return "failed", f"Recording failed — {reason or f'exit {exit_code}'}"

    if "convert_ok" in names or "mp4_ready" in names:
        return "ok", "Converted OK"
    if "record_start" in names and "record_stop" not in names:
        return "recording", "Recording in progress"
    if "record_stop" in names:
        bw = stop.get("bytesWritten") if stop else None
        try:
            has_bytes = int(bw or 0) > 0
        except (TypeError, ValueError):
            has_bytes = False
        if has_bytes:
            return "stopped", "Stopped (awaiting convert or kept as TS)"
        return "discarded", "Stopped with no data"
    if any(n in names for n in ("recording_deleted", "vps_purged_after_mac", "disk_pressure_delete")):
        reason = (
            last_by_name.get("recording_deleted")
            or last_by_name.get("vps_purged_after_mac")
            or last_by_name.get("disk_pressure_delete")
            or {}
        ).get("reason")
        return "deleted", f"Deleted{f' — {reason}' if reason else ''}"
    if "shutdown_begin" in names:
        return "info", "Shutdown"
    if "sync_queued" in names:
        return "info", "Mac sync queued"
    return "info", names[-1].replace("_", " ") if names else "event"


def aggregate_lifecycle_sessions(
    events: list[dict[str, Any]],
    *,
    username: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Group raw events into one UI row per recording session."""
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    username_filter = (username or "").strip().lower() or None

    sessions: list[dict[str, Any]] = []
    index: dict[str, int] = {}

    def _new_session(seed: dict[str, Any]) -> dict[str, Any]:
        return {
            "username": seed.get("username"),
            "sessionId": seed.get("sessionId"),
            "recordingId": seed.get("recordingId"),
            "path": seed.get("path") or seed.get("recordPath"),
            "filename": seed.get("filename") or _basename(seed.get("path") or seed.get("recordPath")),
            "sourceType": seed.get("sourceType") or seed.get("source_type"),
            "startedAt": seed.get("ts"),
            "endedAt": seed.get("ts"),
            "startedUnix": seed.get("ts_unix"),
            "endedUnix": seed.get("ts_unix"),
            "events": [],
        }

    def _register(sess_idx: int, event: dict[str, Any]) -> None:
        for key in _session_lookup_keys(event):
            index[key] = sess_idx

    def _find(event: dict[str, Any]) -> Optional[int]:
        for key in _session_lookup_keys(event):
            if key in index:
                return index[key]
        return None

    for event in events:
        name = str(event.get("event") or "")
        if username_filter:
            event_user = str(event.get("username") or "").lower()
            if name in ("shutdown_begin", "sync_queued"):
                pass
            elif event_user != username_filter:
                continue

        existing = _find(event)
        if name == "record_start":
            sessions.append(_new_session(event))
            sess_idx = len(sessions) - 1
        elif existing is not None:
            sess_idx = existing
        else:
            sessions.append(_new_session(event))
            sess_idx = len(sessions) - 1

        sess = sessions[sess_idx]
        sess["events"].append(event)
        if event.get("username") and not sess.get("username"):
            sess["username"] = event.get("username")
        if event.get("sessionId"):
            sess["sessionId"] = event.get("sessionId")
        if event.get("recordingId"):
            sess["recordingId"] = event.get("recordingId")
        path = event.get("path") or event.get("recordPath")
        if path:
            sess["path"] = path
            sess["filename"] = event.get("filename") or _basename(str(path))
        elif event.get("filename"):
            sess["filename"] = event.get("filename")
        if event.get("sourceType") or event.get("source_type"):
            sess["sourceType"] = event.get("sourceType") or event.get("source_type")

        ts = event.get("ts")
        ts_unix = event.get("ts_unix")
        if not sess.get("startedAt"):
            sess["startedAt"] = ts
            sess["startedUnix"] = ts_unix
        sess["endedAt"] = ts or sess.get("endedAt")
        if ts_unix is not None:
            if sess.get("startedUnix") is None:
                sess["startedUnix"] = ts_unix
            sess["endedUnix"] = ts_unix

        _register(sess_idx, event)

    rows: list[dict[str, Any]] = []
    for sess in sessions:
        evs = sess.get("events") or []
        outcome, summary = _derive_outcome(evs)
        stop = next((e for e in reversed(evs) if e.get("event") == "record_stop"), None)
        start = next((e for e in evs if e.get("event") == "record_start"), None)
        elapsed = None
        if stop and stop.get("elapsedSeconds") is not None:
            elapsed = stop.get("elapsedSeconds")
        elif sess.get("startedUnix") is not None and sess.get("endedUnix") is not None:
            elapsed = max(0, int(sess["endedUnix"]) - int(sess["startedUnix"]))

        bytes_written = stop.get("bytesWritten") if stop else None
        reason = None
        for e in reversed(evs):
            if e.get("reason"):
                reason = e.get("reason")
                break

        steps = [{
            "ts": e.get("ts"),
            "ts_unix": e.get("ts_unix"),
            "event": e.get("event"),
            "text": humanize_event(e),
            "raw": e,
        } for e in evs]

        sid = (
            sess.get("sessionId")
            or sess.get("recordingId")
            or f"{sess.get('username') or 'unknown'}:{sess.get('startedUnix') or 0}:{len(rows)}"
        )
        rows.append({
            "id": str(sid),
            "username": sess.get("username"),
            "sessionId": sess.get("sessionId"),
            "recordingId": sess.get("recordingId"),
            "filename": sess.get("filename"),
            "path": sess.get("path"),
            "sourceType": sess.get("sourceType"),
            "outcome": outcome,
            "summary": summary,
            "reason": reason,
            "startedAt": sess.get("startedAt") or (start or {}).get("ts"),
            "endedAt": sess.get("endedAt"),
            "startedUnix": sess.get("startedUnix"),
            "endedUnix": sess.get("endedUnix"),
            "elapsedSeconds": elapsed,
            "bytesWritten": bytes_written,
            "eventCount": len(evs),
            "steps": steps,
        })

    rows.sort(key=lambda r: int(r.get("endedUnix") or r.get("startedUnix") or 0), reverse=True)
    total = len(rows)
    page = rows[offset: offset + limit]
    return {
        "sessions": page,
        "count": len(page),
        "total": total,
        "offset": offset,
        "limit": limit,
    }


def list_lifecycle_sessions(
    *,
    days: float = 1.0,
    username: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    max_lines: int = 20000,
) -> dict[str, Any]:
    """High-level API helper: filter by lookback window, then aggregate."""
    days = max(0.04, min(float(days or 1.0), 30.0))  # ~1 hour .. 30 days
    now = int(time.time())
    since_unix = now - int(days * 86400)
    events = read_lifecycle_events(since_unix=since_unix, max_lines=max_lines)
    payload = aggregate_lifecycle_sessions(
        events,
        username=username,
        limit=limit,
        offset=offset,
    )
    payload.update({
        "days": days,
        "sinceUnix": since_unix,
        "untilUnix": now,
        "scannedEvents": len(events),
        "busy": read_busy_snapshot(),
    })
    return payload
