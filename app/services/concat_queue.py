"""Manual Mac concat queue: serial ffmpeg concat-project commands for Helper."""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Optional

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_FAILED = "failed"
STATUS_DONE = "done"

CONCAT_CONCURRENCY = 1


class ConcatQueue:
    """Ordered concat tasks (project-level). Concurrent concat = 1."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: list[dict[str, Any]] = []
        self._by_project: dict[str, str] = {}
        self._by_task: dict[str, dict[str, Any]] = {}

    def reset(self) -> None:
        with self._lock:
            self._tasks.clear()
            self._by_project.clear()
            self._by_task.clear()

    def enqueue(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Append projects; skip duplicates already queued/running/paused."""
        added: list[str] = []
        skipped: list[dict[str, str]] = []
        now = int(time.time())
        with self._lock:
            for entry in entries or []:
                project_id = str(entry.get("projectId") or "").strip()
                if not project_id:
                    continue
                existing_id = self._by_project.get(project_id)
                if existing_id:
                    existing = self._by_task.get(existing_id)
                    status = str((existing or {}).get("status") or "")
                    if status in {STATUS_QUEUED, STATUS_RUNNING, STATUS_PAUSED}:
                        skipped.append({"projectId": project_id, "reason": "already_queued"})
                        continue
                    if existing:
                        self._remove_locked(existing_id)
                task_id = secrets.token_urlsafe(12)
                task = {
                    "taskId": task_id,
                    "projectId": project_id,
                    "username": str(entry.get("username") or ""),
                    "title": str(entry.get("title") or ""),
                    "memberCount": int(entry.get("memberCount") or 0),
                    "memberPaths": list(entry.get("memberPaths") or []),
                    "finalRelativePath": str(entry.get("finalRelativePath") or ""),
                    "projectDir": str(entry.get("projectDir") or ""),
                    "startedAt": int(entry.get("startedAt") or 0),
                    "endedAt": int(entry.get("endedAt") or 0),
                    "status": STATUS_QUEUED,
                    "step": 0,
                    "stepTotal": int(entry.get("memberCount") or len(entry.get("memberPaths") or [])),
                    "error": "",
                    "createdAt": now,
                    "updatedAt": now,
                    "commandId": None,
                }
                self._tasks.append(task)
                self._by_task[task_id] = task
                self._by_project[project_id] = task_id
                added.append(task_id)
        return {"added": added, "skipped": skipped, "addedCount": len(added)}

    def _remove_locked(self, task_id: str) -> None:
        task = self._by_task.pop(task_id, None)
        if not task:
            return
        pid = str(task.get("projectId") or "")
        if self._by_project.get(pid) == task_id:
            self._by_project.pop(pid, None)
        self._tasks = [row for row in self._tasks if str(row.get("taskId")) != task_id]

    def list_tasks(self, *, include_done: bool = False) -> dict[str, Any]:
        with self._lock:
            rows = []
            for task in self._tasks:
                status = str(task.get("status") or "")
                if not include_done and status == STATUS_DONE:
                    continue
                if status in {STATUS_QUEUED, STATUS_RUNNING, STATUS_PAUSED, STATUS_FAILED} or include_done:
                    rows.append(dict(task))
            return {"tasks": rows, "count": len(rows), "concurrency": CONCAT_CONCURRENCY}

    def snapshot_lite(self) -> dict[str, Any]:
        with self._lock:
            queued = running = paused = failed = 0
            for task in self._tasks:
                status = str(task.get("status") or "")
                if status == STATUS_QUEUED:
                    queued += 1
                elif status == STATUS_RUNNING:
                    running += 1
                elif status == STATUS_PAUSED:
                    paused += 1
                elif status == STATUS_FAILED:
                    failed += 1
            return {
                "queued": queued,
                "running": running,
                "paused": paused,
                "failed": failed,
            }

    def reorder(self, task_ids: Optional[list[str]] = None, project_ids: Optional[list[str]] = None) -> dict[str, Any]:
        with self._lock:
            movable = [
                t for t in self._tasks
                if str(t.get("status") or "") in {STATUS_QUEUED, STATUS_PAUSED, STATUS_FAILED}
            ]
            fixed = [
                t for t in self._tasks
                if str(t.get("status") or "") not in {STATUS_QUEUED, STATUS_PAUSED, STATUS_FAILED}
            ]
            if project_ids:
                wanted = [str(p).strip() for p in project_ids if str(p).strip()]
                by_pid = {str(t.get("projectId")): t for t in movable}
                new_movable = []
                seen = set()
                for pid in wanted:
                    if pid in by_pid and pid not in seen:
                        new_movable.append(by_pid[pid])
                        seen.add(pid)
                for task in movable:
                    pid = str(task.get("projectId"))
                    if pid not in seen:
                        new_movable.append(task)
                        seen.add(pid)
                self._tasks = fixed + new_movable
            elif task_ids:
                wanted = [str(t).strip() for t in task_ids if str(t).strip()]
                by_id = {str(t.get("taskId")): t for t in movable}
                new_movable = []
                seen = set()
                for tid in wanted:
                    if tid in by_id and tid not in seen:
                        new_movable.append(by_id[tid])
                        seen.add(tid)
                for task in movable:
                    tid = str(task.get("taskId"))
                    if tid not in seen:
                        new_movable.append(task)
                        seen.add(tid)
                self._tasks = fixed + new_movable
            return self.list_tasks()

    def pause(self, *, task_ids: Optional[list[str]] = None, project_ids: Optional[list[str]] = None) -> dict[str, Any]:
        with self._lock:
            targets = self._resolve(task_ids, project_ids)
            n = 0
            for task in targets:
                if str(task.get("status") or "") in {STATUS_QUEUED, STATUS_RUNNING}:
                    task["status"] = STATUS_PAUSED
                    task["updatedAt"] = int(time.time())
                    n += 1
            return {"paused": n, "queue": self.list_tasks()}

    def resume(self, *, task_ids: Optional[list[str]] = None, project_ids: Optional[list[str]] = None) -> dict[str, Any]:
        with self._lock:
            targets = self._resolve(task_ids, project_ids)
            n = 0
            for task in targets:
                if str(task.get("status") or "") in {STATUS_PAUSED, STATUS_FAILED}:
                    task["status"] = STATUS_QUEUED
                    task["error"] = ""
                    task["updatedAt"] = int(time.time())
                    n += 1
            return {"resumed": n, "queue": self.list_tasks()}

    def delete(self, *, task_ids: Optional[list[str]] = None, project_ids: Optional[list[str]] = None) -> dict[str, Any]:
        with self._lock:
            targets = self._resolve(task_ids, project_ids)
            n = 0
            for task in list(targets):
                self._remove_locked(str(task.get("taskId")))
                n += 1
            return {"deleted": n, "queue": self.list_tasks()}

    def _resolve(
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

    def claim_next(self) -> Optional[dict[str, Any]]:
        """Claim at most one non-paused queued task for the Helper."""
        with self._lock:
            running = sum(1 for t in self._tasks if str(t.get("status")) == STATUS_RUNNING)
            if running >= CONCAT_CONCURRENCY:
                return None
            for task in self._tasks:
                if str(task.get("status") or "") != STATUS_QUEUED:
                    continue
                task["status"] = STATUS_RUNNING
                task["updatedAt"] = int(time.time())
                return dict(task)
            return None

    def report(
        self,
        *,
        project_id: str = "",
        task_id: str = "",
        status: str,
        error: str = "",
        step: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        with self._lock:
            task = None
            if task_id and task_id in self._by_task:
                task = self._by_task[task_id]
            elif project_id and project_id in self._by_project:
                task = self._by_task.get(self._by_project[project_id])
            if not task:
                return None
            text = str(status or "").strip().lower()
            if text in {"done", "success", "concatenated", STATUS_DONE}:
                task["status"] = STATUS_DONE
                task["step"] = int(task.get("stepTotal") or task.get("step") or 0)
                task["error"] = ""
            elif text in {"failed", "error", STATUS_FAILED}:
                task["status"] = STATUS_FAILED
                task["error"] = str(error or "concat failed")
            elif text in {"running", "concatenating", STATUS_RUNNING}:
                task["status"] = STATUS_RUNNING
            if step is not None:
                try:
                    task["step"] = max(0, int(step))
                except (TypeError, ValueError):
                    pass
            task["updatedAt"] = int(time.time())
            result = dict(task)
            if task["status"] == STATUS_DONE:
                self._remove_locked(str(task.get("taskId")))
            return result


concat_queue = ConcatQueue()
