"""Manual Mac download queue: ordered fragments, project/file concurrency scheduler."""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Optional

DEFAULT_PROJECT_CONCURRENCY = 2
DEFAULT_FILE_CONCURRENCY = 6
MIN_PROJECT_CONCURRENCY = 1
MAX_PROJECT_CONCURRENCY = 4
MIN_FILE_CONCURRENCY = 2
MAX_FILE_CONCURRENCY = 12

# Fragment task statuses
STATUS_QUEUED = "queued"
STATUS_DOWNLOADING = "downloading"
STATUS_PAUSED = "paused"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


def normalize_project_concurrency(value: object, default: int = DEFAULT_PROJECT_CONCURRENCY) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(MIN_PROJECT_CONCURRENCY, min(MAX_PROJECT_CONCURRENCY, n))


def normalize_file_concurrency(value: object, default: int = DEFAULT_FILE_CONCURRENCY) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(MIN_FILE_CONCURRENCY, min(MAX_FILE_CONCURRENCY, n))


def project_ready_rank(status: str) -> int:
    """Lower = higher priority when sorting a new append batch."""
    text = str(status or "").strip().lower()
    if text in {"ready", "local_complete", "syncing", "concatenated", "concat_failed"}:
        return 0
    if text in {"awaiting_gap"}:
        return 1
    return 2  # open / unknown


def sort_append_batch(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort one Media 'Download selected' batch: ready projects first, then Clip/P order."""

    def key(row: dict[str, Any]) -> tuple:
        return (
            project_ready_rank(str(row.get("projectStatus") or "")),
            int(row.get("projectStartedAt") or 0),
            str(row.get("projectId") or ""),
            int(row.get("startedAt") or 0),
            int(row.get("sortIndex") or 0),
            str(row.get("filename") or ""),
        )

    return sorted(entries, key=key)


class DownloadQueue:
    """Thread-safe in-memory download queue grouped by project."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Ordered list of task dicts (stable append + reorder).
        self._tasks: list[dict[str, Any]] = []
        # projectId -> paused flag (project-level pause does not occupy slots)
        self._project_paused: dict[str, bool] = {}
        # itemId -> taskId for duplicate skip
        self._by_item: dict[str, str] = {}
        self._by_task: dict[str, dict[str, Any]] = {}

    def reset(self) -> None:
        with self._lock:
            self._tasks.clear()
            self._project_paused.clear()
            self._by_item.clear()
            self._by_task.clear()

    def append_batch(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Append fragment tasks; skip itemIds already queued/downloading/paused."""
        ordered = sort_append_batch(list(entries or []))
        added: list[str] = []
        skipped: list[dict[str, str]] = []
        now = int(time.time())
        with self._lock:
            for entry in ordered:
                item_id = str(entry.get("itemId") or "").strip()
                if not item_id:
                    continue
                existing_id = self._by_item.get(item_id)
                if existing_id:
                    existing = self._by_task.get(existing_id)
                    status = str((existing or {}).get("status") or "")
                    if status in {STATUS_QUEUED, STATUS_DOWNLOADING, STATUS_PAUSED}:
                        skipped.append({"itemId": item_id, "reason": "already_queued"})
                        continue
                    # done/failed: allow re-queue by removing old index
                    if existing:
                        self._remove_task_locked(existing_id)
                task_id = secrets.token_urlsafe(12)
                task = {
                    "taskId": task_id,
                    "projectId": str(entry.get("projectId") or "") or "unknown",
                    "itemId": item_id,
                    "recordingId": str(entry.get("recordingId") or item_id),
                    "username": str(entry.get("username") or ""),
                    "filename": str(entry.get("filename") or ""),
                    "sizeBytes": max(0, int(entry.get("sizeBytes") or entry.get("size") or 0)),
                    "startedAt": int(entry.get("startedAt") or 0),
                    "sortIndex": int(entry.get("sortIndex") or 0),
                    "projectStatus": str(entry.get("projectStatus") or "open"),
                    "projectStartedAt": int(entry.get("projectStartedAt") or 0),
                    "projectTitle": str(entry.get("projectTitle") or ""),
                    "method": str(entry.get("method") or "motrix").strip().lower() or "motrix",
                    "status": STATUS_QUEUED,
                    "progressBytes": 0,
                    "progressPercent": 0.0,
                    "speedBytesPerSec": 0.0,
                    "jobId": None,
                    "createdAt": now,
                    "updatedAt": now,
                    "error": "",
                }
                self._tasks.append(task)
                self._by_task[task_id] = task
                self._by_item[item_id] = task_id
                self._project_paused.setdefault(task["projectId"], False)
                added.append(task_id)
        return {"added": added, "skipped": skipped, "addedCount": len(added)}

    def _remove_task_locked(self, task_id: str) -> None:
        task = self._by_task.pop(task_id, None)
        if not task:
            return
        item_id = str(task.get("itemId") or "")
        if self._by_item.get(item_id) == task_id:
            self._by_item.pop(item_id, None)
        self._tasks = [row for row in self._tasks if str(row.get("taskId")) != task_id]

    def list_grouped(self) -> dict[str, Any]:
        with self._lock:
            groups: dict[str, dict[str, Any]] = {}
            order: list[str] = []
            for task in self._tasks:
                status = str(task.get("status") or "")
                if status == STATUS_DONE:
                    continue
                project_id = str(task.get("projectId") or "unknown")
                if project_id not in groups:
                    groups[project_id] = {
                        "projectId": project_id,
                        "username": task.get("username") or "",
                        "title": task.get("projectTitle") or "",
                        "projectStatus": task.get("projectStatus") or "",
                        "paused": bool(self._project_paused.get(project_id)),
                        "tasks": [],
                        "totalSize": 0,
                        "downloadedSize": 0,
                    }
                    order.append(project_id)
                group = groups[project_id]
                group["tasks"].append(dict(task))
                group["totalSize"] += int(task.get("sizeBytes") or 0)
                group["downloadedSize"] += int(task.get("progressBytes") or 0)
            projects = [groups[pid] for pid in order]
            return {
                "projects": projects,
                "taskCount": sum(len(p["tasks"]) for p in projects),
                "projectCount": len(projects),
            }

    def snapshot_lite(self) -> dict[str, Any]:
        """Compact snapshot for heartbeat (no per-byte noise)."""
        with self._lock:
            active_projects = 0
            queued = 0
            downloading = 0
            paused = 0
            seen_active: set[str] = set()
            for task in self._tasks:
                status = str(task.get("status") or "")
                project_id = str(task.get("projectId") or "")
                if status == STATUS_DONE:
                    continue
                if status == STATUS_QUEUED:
                    queued += 1
                elif status == STATUS_DOWNLOADING:
                    downloading += 1
                    if project_id and not self._project_paused.get(project_id):
                        seen_active.add(project_id)
                elif status == STATUS_PAUSED:
                    paused += 1
            active_projects = len(seen_active)
            return {
                "queued": queued,
                "downloading": downloading,
                "paused": paused,
                "activeProjects": active_projects,
                "projectCount": len({
                    str(t.get("projectId") or "")
                    for t in self._tasks
                    if str(t.get("status") or "") not in {STATUS_DONE, ""}
                }),
            }

    def reorder(self, *, task_ids: Optional[list[str]] = None, project_ids: Optional[list[str]] = None) -> dict[str, Any]:
        """Reorder queued/paused tasks. Downloading tasks keep relative position."""
        with self._lock:
            if project_ids:
                wanted = [str(p).strip() for p in project_ids if str(p).strip()]
                movable = [
                    t for t in self._tasks
                    if str(t.get("status") or "") in {STATUS_QUEUED, STATUS_PAUSED}
                ]
                fixed = [
                    t for t in self._tasks
                    if str(t.get("status") or "") not in {STATUS_QUEUED, STATUS_PAUSED}
                ]
                by_project: dict[str, list[dict]] = {}
                for task in movable:
                    by_project.setdefault(str(task.get("projectId") or ""), []).append(task)
                new_movable: list[dict] = []
                seen: set[str] = set()
                for pid in wanted:
                    if pid in seen:
                        continue
                    seen.add(pid)
                    new_movable.extend(by_project.get(pid) or [])
                for pid, rows in by_project.items():
                    if pid not in seen:
                        new_movable.extend(rows)
                # Preserve non-movable (downloading/done) at their original indices roughly:
                # put fixed first in original order, then reordered movable — simpler: rebuild as
                # original with movable replaced by new_movable in encounter order.
                self._tasks = fixed + new_movable
            if task_ids:
                wanted_tasks = [str(t).strip() for t in task_ids if str(t).strip()]
                by_id = {str(t.get("taskId")): t for t in self._tasks}
                movable_ids = {
                    str(t.get("taskId"))
                    for t in self._tasks
                    if str(t.get("status") or "") in {STATUS_QUEUED, STATUS_PAUSED}
                }
                ordered_movable = [by_id[tid] for tid in wanted_tasks if tid in movable_ids and tid in by_id]
                seen = {str(t.get("taskId")) for t in ordered_movable}
                for task in self._tasks:
                    tid = str(task.get("taskId"))
                    if tid in movable_ids and tid not in seen:
                        ordered_movable.append(task)
                        seen.add(tid)
                fixed = [
                    t for t in self._tasks
                    if str(t.get("status") or "") not in {STATUS_QUEUED, STATUS_PAUSED}
                ]
                self._tasks = fixed + ordered_movable
            return self.list_grouped()

    def pause(
        self,
        *,
        task_ids: Optional[list[str]] = None,
        project_ids: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        with self._lock:
            target_tasks = self._resolve_targets(task_ids, project_ids)
            for task in target_tasks:
                status = str(task.get("status") or "")
                if status in {STATUS_QUEUED, STATUS_DOWNLOADING}:
                    task["status"] = STATUS_PAUSED
                    task["jobId"] = None
                    task["updatedAt"] = int(time.time())
            if project_ids:
                for pid in project_ids:
                    pid = str(pid or "").strip()
                    if pid:
                        self._project_paused[pid] = True
            elif not task_ids:
                for pid in list(self._project_paused):
                    self._project_paused[pid] = True
                    for task in self._tasks:
                        if str(task.get("projectId")) == pid and str(task.get("status")) in {
                            STATUS_QUEUED,
                            STATUS_DOWNLOADING,
                        }:
                            task["status"] = STATUS_PAUSED
                            task["jobId"] = None
            return {"paused": len(target_tasks), "queue": self.list_grouped()}

    def resume(
        self,
        *,
        task_ids: Optional[list[str]] = None,
        project_ids: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        with self._lock:
            if project_ids:
                for pid in project_ids:
                    pid = str(pid or "").strip()
                    if pid:
                        self._project_paused[pid] = False
            target_tasks = self._resolve_targets(task_ids, project_ids)
            for task in target_tasks:
                if str(task.get("status") or "") == STATUS_PAUSED:
                    task["status"] = STATUS_QUEUED
                    task["updatedAt"] = int(time.time())
            if not task_ids and not project_ids:
                for pid in list(self._project_paused):
                    self._project_paused[pid] = False
                for task in self._tasks:
                    if str(task.get("status") or "") == STATUS_PAUSED:
                        task["status"] = STATUS_QUEUED
                        task["updatedAt"] = int(time.time())
            return {"resumed": len(target_tasks), "queue": self.list_grouped()}

    def delete(
        self,
        *,
        task_ids: Optional[list[str]] = None,
        project_ids: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        with self._lock:
            target_tasks = self._resolve_targets(task_ids, project_ids)
            removed = 0
            for task in list(target_tasks):
                self._remove_task_locked(str(task.get("taskId")))
                removed += 1
            if project_ids:
                for pid in project_ids:
                    self._project_paused.pop(str(pid or "").strip(), None)
            return {"deleted": removed, "queue": self.list_grouped()}

    def _resolve_targets(
        self,
        task_ids: Optional[list[str]],
        project_ids: Optional[list[str]],
    ) -> list[dict[str, Any]]:
        if task_ids:
            wanted = {str(t).strip() for t in task_ids if str(t).strip()}
            return [t for t in self._tasks if str(t.get("taskId")) in wanted]
        if project_ids:
            wanted = {str(p).strip() for p in project_ids if str(p).strip()}
            return [t for t in self._tasks if str(t.get("projectId")) in wanted]
        return list(self._tasks)

    def mark_done_for_items(self, item_ids: set[str]) -> int:
        wanted = {str(i).strip() for i in item_ids if str(i).strip()}
        if not wanted:
            return 0
        marked = 0
        with self._lock:
            for task in list(self._tasks):
                if str(task.get("itemId") or "") not in wanted:
                    continue
                task["status"] = STATUS_DONE
                task["progressPercent"] = 100.0
                task["progressBytes"] = int(task.get("sizeBytes") or 0)
                task["updatedAt"] = int(time.time())
                marked += 1
                self._remove_task_locked(str(task.get("taskId")))
        return marked

    def update_progress(self, updates: list[dict[str, Any]]) -> int:
        """Apply helper-reported Motrix progress rows: itemId/recordingId + bytes/%."""
        applied = 0
        with self._lock:
            by_item = dict(self._by_item)
            for row in updates or []:
                if not isinstance(row, dict):
                    continue
                item_id = str(row.get("itemId") or row.get("recordingId") or "").strip()
                task_id = by_item.get(item_id)
                if not task_id:
                    continue
                task = self._by_task.get(task_id)
                if not task:
                    continue
                if "progressBytes" in row or "completedLength" in row:
                    try:
                        task["progressBytes"] = max(
                            0,
                            int(row.get("progressBytes") or row.get("completedLength") or 0),
                        )
                    except (TypeError, ValueError):
                        pass
                if "progressPercent" in row:
                    try:
                        task["progressPercent"] = max(0.0, min(100.0, float(row["progressPercent"])))
                    except (TypeError, ValueError):
                        pass
                elif int(task.get("sizeBytes") or 0) > 0 and int(task.get("progressBytes") or 0) > 0:
                    task["progressPercent"] = min(
                        100.0,
                        100.0 * int(task["progressBytes"]) / int(task["sizeBytes"]),
                    )
                if "speedBytesPerSec" in row or "downloadSpeed" in row:
                    try:
                        task["speedBytesPerSec"] = max(
                            0.0,
                            float(row.get("speedBytesPerSec") or row.get("downloadSpeed") or 0),
                        )
                    except (TypeError, ValueError):
                        pass
                status = str(row.get("status") or "").strip().lower()
                if status in {"active", "waiting", "downloading"} and str(task.get("status")) != STATUS_PAUSED:
                    task["status"] = STATUS_DOWNLOADING
                elif status in {"complete", "done"}:
                    task["status"] = STATUS_DONE
                    task["progressPercent"] = 100.0
                elif status in {"error", "failed"}:
                    task["status"] = STATUS_FAILED
                    task["error"] = str(row.get("error") or row.get("errorMessage") or "")
                task["updatedAt"] = int(time.time())
                applied += 1
                if str(task.get("status")) == STATUS_DONE:
                    self._remove_task_locked(str(task.get("taskId")))
        return applied

    def claim_next(
        self,
        *,
        project_concurrency: int,
        file_concurrency: int,
        local_session_id: str = "",
    ) -> list[dict[str, Any]]:
        """
        Select up to file_concurrency queued tasks respecting project_concurrency.

        Paused projects do not occupy project slots. Prefer filling active
        (already downloading) projects first, then walk queue order for new ones.
        Returns task dicts marked downloading (caller creates Motrix jobs).
        """
        n_projects = normalize_project_concurrency(project_concurrency)
        n_files = normalize_file_concurrency(file_concurrency)
        claimed: list[dict[str, Any]] = []
        with self._lock:
            active_by_project: dict[str, int] = {}
            for task in self._tasks:
                if str(task.get("status") or "") != STATUS_DOWNLOADING:
                    continue
                pid = str(task.get("projectId") or "")
                if self._project_paused.get(pid):
                    continue
                active_by_project[pid] = active_by_project.get(pid, 0) + 1

            active_project_count = len(active_by_project)
            active_file_count = sum(active_by_project.values())
            slots_files = max(0, n_files - active_file_count)
            if slots_files <= 0:
                return []

            def try_claim(task: dict[str, Any]) -> bool:
                nonlocal active_project_count, slots_files
                if slots_files <= 0:
                    return False
                if str(task.get("status") or "") != STATUS_QUEUED:
                    return False
                pid = str(task.get("projectId") or "")
                if self._project_paused.get(pid):
                    return False
                in_active = pid in active_by_project
                if not in_active and active_project_count >= n_projects:
                    return False
                task["status"] = STATUS_DOWNLOADING
                task["updatedAt"] = int(time.time())
                if local_session_id:
                    task["localSessionId"] = local_session_id
                if not in_active:
                    active_by_project[pid] = 0
                    active_project_count += 1
                active_by_project[pid] = active_by_project.get(pid, 0) + 1
                slots_files -= 1
                claimed.append(dict(task))
                return True

            # 1) Fill already-active projects first (queue order within project).
            for task in self._tasks:
                pid = str(task.get("projectId") or "")
                if pid not in active_by_project:
                    continue
                try_claim(task)
                if slots_files <= 0:
                    break

            # 2) Start new projects / remaining queued in global order.
            if slots_files > 0:
                for task in self._tasks:
                    try_claim(task)
                    if slots_files <= 0:
                        break

        return claimed

    def bind_job(self, task_ids: list[str], job_id: str) -> None:
        with self._lock:
            wanted = {str(t) for t in task_ids}
            for task in self._tasks:
                if str(task.get("taskId")) in wanted:
                    task["jobId"] = job_id
                    task["updatedAt"] = int(time.time())

    def all_downloads_paused(self) -> bool:
        """True when every non-done task is paused (or queue empty)."""
        with self._lock:
            live = [t for t in self._tasks if str(t.get("status") or "") != STATUS_DONE]
            if not live:
                return True
            return all(str(t.get("status") or "") == STATUS_PAUSED for t in live)


# Process-wide singleton
download_queue = DownloadQueue()
