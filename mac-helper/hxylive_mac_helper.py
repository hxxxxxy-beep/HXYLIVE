#!/usr/bin/env python3
"""Local-only bridge between the HXYLIVE web UI, Chrome downloads, and the video folder."""

import argparse
import hashlib
import http.client
import json
import os
import queue
import re
import secrets
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".ts"}
LEDGER_NAME = "download-ledger.json"
LEDGER_VERSION = 2
# How long to wait for Chrome to finish one file (large VPS recordings).
DOWNLOAD_WAIT_SECONDS = 6 * 60 * 60
DOWNLOAD_POLL_SECONDS = 1.0
SIZE_STABLE_POLLS = 2
VPS_SYNC_INTERVAL_SECONDS = 3.0
COMMAND_POLL_INTERVAL_SECONDS = 0.5


class Helper:
    def __init__(
        self,
        video_dir: Path,
        allowed_origin: str,
        proxy_url: str,
        support_dir=None,
        chrome_download_dir=None,
    ):
        self.video_dir_arg = str(video_dir)
        self.video_dir = Path(video_dir).expanduser().resolve()
        try:
            self.video_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"[mac-helper] could not create video dir {self.video_dir}: {exc!r}", flush=True)
        self.allowed_origin = allowed_origin.rstrip("/")
        self.proxy_url = proxy_url
        # Optional override; otherwise only Chrome's configured download folder is watched.
        self.chrome_download_dir_arg = (
            str(chrome_download_dir).strip() if chrome_download_dir else ""
        )
        self.session_id = secrets.token_urlsafe(24)
        self._using_proxy = None
        self.support_dir = support_dir or (Path.home() / "Library" / "Application Support" / "HXYLIVE")
        self.support_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.support_dir / LEDGER_NAME
        self._filed_lock = threading.Lock()
        self._filed_waiters = []

    def _refresh_video_dir(self) -> Path:
        """Re-resolve on each scan so a late-mounted external disk is picked up."""
        resolved = Path(self.video_dir_arg).expanduser().resolve()
        self.video_dir = resolved
        return resolved

    def _recording_id_for_name(self, name: str):
        for prefix in ("hxylive-",):
            if name.startswith(prefix) and "__" in name:
                return name[len(prefix):].split("__", 1)[0]
        return None

    def _load_ledger(self) -> list:
        """Load confirmed-on-disk entries only. Discard v1 dispatch-time ledgers."""
        try:
            data = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        # v1 was a bare list written when Chrome was opened — that marked
        # in-flight downloads as present before the file existed.
        if isinstance(data, list):
            return []
        if not isinstance(data, dict) or int(data.get("version") or 0) != LEDGER_VERSION:
            return []
        files = data.get("files")
        return files if isinstance(files, list) else []

    def _save_ledger(self, entries: list) -> None:
        payload = {"version": LEDGER_VERSION, "files": entries}
        tmp = self.ledger_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.ledger_path)

    def remember_confirmed_files(self, items: list) -> None:
        """Persist files actually seen on disk (survives launchd TCC blind spots)."""
        now = int(time.time())
        entries = []
        for item in items:
            recording_id = str(item.get("recordingId") or "").strip()
            filename = str(item.get("filename") or "").strip()
            if not recording_id or not filename:
                continue
            entries.append({
                "recordingId": recording_id,
                "filename": filename,
                "size": int(item.get("size") or 0),
                "updatedAt": now,
            })
        self._save_ledger(entries)

    def upsert_ledger_entry(self, recording_id: str, filename: str, size: int) -> None:
        """Record one completed download so scan can map date-named files → recordingId."""
        recording_id = str(recording_id or "").strip()
        filename = str(filename or "").strip()
        if not recording_id or not filename:
            return
        ledger = self._load_ledger()
        by_id = {
            str(entry.get("recordingId")): entry
            for entry in ledger
            if entry.get("recordingId")
        }
        by_id[recording_id] = {
            "recordingId": recording_id,
            "filename": filename,
            "size": int(size or 0),
            "updatedAt": int(time.time()),
        }
        self._save_ledger(list(by_id.values()))

    def notify_filed(self, recording_id: str, relative: str, size: int) -> None:
        """Wake Media-page waiters after a file is in video_dir/<streamer>/."""
        event = {
            "recordingId": str(recording_id or "").strip(),
            "relativePath": str(relative or "").strip(),
            "size": int(size or 0),
            "filedAt": int(time.time()),
        }
        if not event["recordingId"]:
            return
        with self._filed_lock:
            waiters = list(self._filed_waiters)
        for waiter in waiters:
            try:
                waiter.put_nowait(event)
            except Exception:
                continue
        threading.Thread(target=self._push_folder_snapshot, daemon=True).start()

    def wait_for_filed(self, recording_ids: list, timeout_seconds: float) -> dict:
        wanted = {
            str(item or "").strip()
            for item in recording_ids
            if str(item or "").strip()
        }
        if not wanted:
            return {"filed": [], "remaining": []}
        already = {
            str(entry.get("recordingId") or "").strip()
            for entry in self._load_ledger()
            if str(entry.get("recordingId") or "").strip() in wanted
        }
        if already == wanted:
            return {"filed": sorted(wanted), "remaining": []}
        waiter = queue.Queue()
        with self._filed_lock:
            self._filed_waiters.append(waiter)
        filed = set(already)
        deadline = time.time() + max(0.2, float(timeout_seconds))
        try:
            while filed != wanted and time.time() < deadline:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    event = waiter.get(timeout=remaining)
                except queue.Empty:
                    break
                rid = str((event or {}).get("recordingId") or "").strip()
                if rid in wanted:
                    filed.add(rid)
                    if rid not in already:
                        break
        finally:
            with self._filed_lock:
                if waiter in self._filed_waiters:
                    self._filed_waiters.remove(waiter)
        return {
            "filed": sorted(filed),
            "remaining": sorted(wanted - filed),
        }

    def _push_folder_snapshot(self) -> None:
        """Update the VPS Media snapshot without dropping claimed jobs or commands."""
        try:
            self._push_heartbeat_and_run_jobs()
        except Exception as exc:
            print(f"[mac-helper] snapshot after file failed: {exc!r}", flush=True)

    def _attach_recording_ids(self, files: list) -> list:
        """Map relative paths back to recordingIds via legacy names or the ledger."""
        ledger = self._load_ledger()
        by_name = {
            str(entry.get("filename") or "").strip(): entry
            for entry in ledger
            if entry.get("filename") and entry.get("recordingId")
        }
        for entry in files:
            if entry.get("recordingId"):
                continue
            prior = by_name.get(str(entry.get("filename") or "").strip())
            if prior:
                entry["recordingId"] = prior.get("recordingId")
        return files

    def _safe_relative_path(self, relative: str) -> Path:
        rel = Path(str(relative or "").replace("\\", "/"))
        if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
            raise ValueError(f"invalid relative path: {relative!r}")
        return rel

    def _resolve_under_video_dir(self, relative: str) -> Path:
        root = self._refresh_video_dir().resolve()
        rel = self._safe_relative_path(relative)
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"path escapes video directory: {relative!r}") from exc
        return candidate

    def _resolve_local_media(self, relative: str = "", recording_id: str = "") -> Path:
        relative = str(relative or "").strip().replace("\\", "/")
        recording_id = str(recording_id or "").strip()
        if not relative and recording_id:
            for entry in self.scan().get("files") or []:
                if str(entry.get("recordingId") or "").strip() == recording_id:
                    relative = str(entry.get("filename") or "").strip()
                    break
            if not relative:
                raise FileNotFoundError(f"recording not found on Mac: {recording_id}")
        if not relative:
            raise ValueError("relativePath or recordingId required")
        path = self._resolve_under_video_dir(relative)
        if not path.is_file():
            raise FileNotFoundError(f"file not found: {relative}")
        return path

    def _remove_ledger_for(self, relative: str = "", recording_id: str = "") -> None:
        relative = str(relative or "").strip().replace("\\", "/")
        recording_id = str(recording_id or "").strip()
        if not relative and not recording_id:
            return
        kept = []
        for entry in self._load_ledger():
            entry_id = str(entry.get("recordingId") or "").strip()
            entry_name = str(entry.get("filename") or "").strip().replace("\\", "/")
            if recording_id and entry_id == recording_id:
                continue
            if relative and entry_name == relative:
                continue
            kept.append(entry)
        self._save_ledger(kept)

    def open_local(self, relative: str = "", recording_id: str = "", reveal: bool = False) -> dict:
        """Open a Mac folder video with the system default app (local playback speed)."""
        path = self._resolve_local_media(relative=relative, recording_id=recording_id)
        if reveal:
            subprocess.run(["/usr/bin/open", "-R", str(path)], check=False)
        else:
            subprocess.run(["/usr/bin/open", str(path)], check=False)
        root = self.video_dir.resolve()
        return {
            "status": "ok",
            "relativePath": path.relative_to(root).as_posix(),
            "reveal": bool(reveal),
        }

    def delete_local(self, relative: str = "", recording_id: str = "") -> dict:
        """Delete one Mac folder video and drop its ledger mapping."""
        path = self._resolve_local_media(relative=relative, recording_id=recording_id)
        root = self.video_dir.resolve()
        rel = path.relative_to(root).as_posix()
        path.unlink()
        parent = path.parent
        try:
            if parent != root and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass
        self._remove_ledger_for(relative=rel, recording_id=recording_id)
        return {
            "status": "ok",
            "relativePath": rel,
            "deleted": True,
        }

    def _paths_via_pathlib(self, root: Path):
        try:
            for path in root.rglob("*"):
                yield path
        except OSError as exc:
            print(f"[mac-helper] pathlib scan failed for {root}: {exc!r}", flush=True)

    def _listdir_ok(self, root: Path) -> bool:
        try:
            os.listdir(root)
            return True
        except OSError:
            return False

    def _paths_via_finder(self, root: Path):
        """
        launchd Python is often blocked by macOS TCC on removable volumes.
        Finder usually still has access in the user GUI session.
        Yields paths, or raises RuntimeError when Finder cannot list the folder.
        """
        proc = subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                "on run argv",
                "-e",
                'set rootPath to item 1 of argv',
                "-e",
                'tell application "Finder"',
                "-e",
                'set targetFolder to (POSIX file rootPath) as alias',
                "-e",
                'set out to ""',
                "-e",
                'repeat with f in (get every file of entire contents of folder targetFolder)',
                "-e",
                'set out to out & (POSIX path of (f as alias)) & linefeed',
                "-e",
                "end repeat",
                "-e",
                "return out",
                "-e",
                "end tell",
                "-e",
                "end run",
                "--",
                str(root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            print(f"[mac-helper] Finder scan failed for {root}: {err}", flush=True)
            raise RuntimeError(err or "Finder scan failed")
        text = (proc.stdout or "").replace("\r", "\n")
        for line in text.splitlines():
            line = line.strip()
            if line:
                yield Path(line)

    def _is_incomplete_download(self, path: Path) -> bool:
        name = path.name
        if name.endswith(".crdownload") or name.endswith(".download"):
            return True
        # Chrome on macOS often stages as "Unconfirmed <id>.crdownload".
        if name.startswith("Unconfirmed "):
            return True
        return False

    def _file_entries_from_disk(self, root: Path):
        """
        Returns (entries, listing_trusted).
        listing_trusted means we could observe the folder contents, so an empty
        video list is real (not a TCC blind spot that should fall back to ledger).
        """
        paths = list(self._paths_via_pathlib(root))
        source = "pathlib"
        listing_trusted = self._listdir_ok(root) or bool(paths)
        if not paths:
            try:
                paths = list(self._paths_via_finder(root))
                source = "finder"
                listing_trusted = True
                if paths:
                    print(
                        f"[mac-helper] pathlib saw 0 files under {root}; "
                        f"Finder returned {len(paths)}",
                        flush=True,
                    )
            except RuntimeError:
                pass
        entries = []
        for path in paths:
            if self._is_incomplete_download(path):
                continue
            if path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            if source == "pathlib":
                try:
                    if not path.is_file():
                        continue
                except OSError:
                    continue
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                relative = path.name
            size = None
            try:
                size = path.stat().st_size
            except OSError:
                size = self._stat_size_via_osascript(path)
            if size is None:
                continue
            duration = self._probe_duration_seconds(path)
            resolution = self._probe_resolution(path)
            entry = {
                "recordingId": self._recording_id_for_name(path.name),
                "filename": relative,
                "size": size,
            }
            if duration and duration > 0:
                entry["durationSeconds"] = duration
            if resolution:
                entry["resolution"] = resolution
            entries.append(entry)
        return entries, listing_trusted

    def _probe_duration_seconds(self, path: Path):
        """Best-effort duration for Media cards (mdls, then ffprobe)."""
        try:
            proc = subprocess.run(
                ["/usr/bin/mdls", "-raw", "-name", "kMDItemDurationSeconds", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                raw = (proc.stdout or "").strip()
                if raw and raw.lower() not in {"(null)", "null"}:
                    value = float(raw)
                    if value > 0:
                        return int(round(value))
        except (OSError, ValueError):
            pass
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return None
        try:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                return None
            value = float((proc.stdout or "").strip())
            if value > 0:
                return int(round(value))
        except (OSError, ValueError):
            return None
        return None

    @staticmethod
    def _mdls_raw_number(path: Path, name: str):
        """Read one mdls numeric attribute. Query singly — multi -name -raw uses NUL."""
        try:
            proc = subprocess.run(
                ["/usr/bin/mdls", "-raw", "-name", name, str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return None
        if proc.returncode != 0:
            return None
        raw = (proc.stdout or "").strip().split("\x00", 1)[0].strip()
        if not raw or raw.lower() in {"(null)", "null"}:
            return None
        try:
            value = int(float(raw))
        except ValueError:
            return None
        return value if value > 0 else None

    @staticmethod
    def _probe_resolution_mp4(path: Path):
        """Parse MP4/M4V tkhd / visual sample entry when Spotlight has no pixels."""
        if path.suffix.lower() not in {".mp4", ".m4v", ".mov"}:
            return None
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                end = handle.tell()
                handle.seek(0)

                def read_atoms(limit: int, depth: int = 0):
                    found = None
                    while handle.tell() + 8 <= limit:
                        start = handle.tell()
                        header = handle.read(8)
                        if len(header) < 8:
                            return found
                        size, typ = struct.unpack(">I4s", header)
                        hdr_len = 8
                        if size == 1:
                            wide = handle.read(8)
                            if len(wide) < 8:
                                return found
                            size = struct.unpack(">Q", wide)[0]
                            hdr_len = 16
                        elif size == 0:
                            size = limit - start
                        if size < hdr_len:
                            return found
                        box_end = min(start + size, limit)
                        typ_s = typ.decode("latin1", errors="ignore")
                        if typ_s in {"moov", "trak", "mdia", "minf", "stbl", "stsd"}:
                            nested = read_atoms(box_end, depth + 1)
                            if nested:
                                found = nested
                        elif typ_s == "tkhd":
                            data = handle.read(min(100, box_end - handle.tell()))
                            if data:
                                ver = data[0]
                                if ver == 0 and len(data) >= 84:
                                    width = struct.unpack(">I", data[76:80])[0] / 65536.0
                                    height = struct.unpack(">I", data[80:84])[0] / 65536.0
                                elif ver == 1 and len(data) >= 96:
                                    width = struct.unpack(">I", data[88:92])[0] / 65536.0
                                    height = struct.unpack(">I", data[92:96])[0] / 65536.0
                                else:
                                    width = height = 0
                                if width >= 2 and height >= 2:
                                    found = (int(round(width)), int(round(height)))
                        elif typ_s in {"avc1", "hvc1", "hev1", "mp4v", "encv", "vp09", "av01"}:
                            data = handle.read(min(40, box_end - handle.tell()))
                            if len(data) >= 28:
                                width, height = struct.unpack(">HH", data[24:28])
                                if width >= 2 and height >= 2:
                                    found = (width, height)
                        handle.seek(box_end)
                    return found

                dims = read_atoms(end)
                if dims:
                    return f"{dims[0]}x{dims[1]}"
        except OSError:
            return None
        return None

    def _ffprobe_bin(self):
        found = shutil.which("ffprobe")
        if found:
            return found
        for candidate in (
            "/opt/homebrew/bin/ffprobe",
            "/usr/local/bin/ffprobe",
        ):
            if Path(candidate).is_file():
                return candidate
        return None

    def _probe_resolution(self, path: Path):
        """Best-effort WxH for Media Quality (mdls, MP4 atoms, then ffprobe)."""
        width = self._mdls_raw_number(path, "kMDItemPixelWidth")
        height = self._mdls_raw_number(path, "kMDItemPixelHeight")
        if width and height:
            return f"{width}x{height}"
        mp4 = self._probe_resolution_mp4(path)
        if mp4:
            return mp4
        ffprobe = self._ffprobe_bin()
        if not ffprobe:
            return None
        try:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height",
                    "-of",
                    "csv=s=x:p=0",
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                return None
            raw = (proc.stdout or "").strip().splitlines()
            if not raw:
                return None
            match = re.fullmatch(r"(\d+)x(\d+)", raw[0].strip())
            if not match:
                return None
            width = int(match.group(1))
            height = int(match.group(2))
            if width > 0 and height > 0:
                return f"{width}x{height}"
        except (OSError, ValueError):
            return None
        return None

    def ensure_thumbnail(self, relative: str = "", recording_id: str = "") -> Path:
        """Return a cached cover image for one Mac video (qlmanage)."""
        path = self._resolve_local_media(relative=relative, recording_id=recording_id)
        try:
            st = path.stat()
            stamp = f"{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            stamp = str(path)
        key = hashlib.sha1(f"{path.resolve()}:{stamp}".encode("utf-8")).hexdigest()[:20]
        cache_dir = self.support_dir / "thumbnails"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached = cache_dir / f"{key}.png"
        if cached.is_file() and cached.stat().st_size > 0:
            return cached

        work_dir = cache_dir / f".work-{key}"
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(
                ["/usr/bin/qlmanage", "-t", "-s", "480", "-o", str(work_dir), str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout or "qlmanage failed").strip())
            produced = None
            for candidate in work_dir.iterdir():
                if candidate.is_file() and candidate.suffix.lower() in {".png", ".jpg", ".jpeg"}:
                    produced = candidate
                    break
            if produced is None:
                raise RuntimeError("qlmanage produced no thumbnail")
            produced.replace(cached)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        if not cached.is_file() or cached.stat().st_size <= 0:
            raise RuntimeError("thumbnail cache missing")
        return cached

    def _stat_size_via_osascript(self, path: Path):
        proc = subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                "on run argv",
                "-e",
                'tell application "Finder" to get size of ((POSIX file (item 1 of argv)) as alias)',
                "-e",
                "end run",
                "--",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return None
        try:
            return int(float((proc.stdout or "").replace("\r", "\n").strip()))
        except ValueError:
            return None

    def scan(self):
        root = self._refresh_video_dir()
        files, listing_trusted = self._file_entries_from_disk(root)
        files = self._attach_recording_ids(files)
        scan_source = "disk"
        if files:
            # Only remember files actually present with a known recordingId.
            self.remember_confirmed_files(files)
        elif listing_trusted:
            # Folder is readable and has no complete videos — do not keep ghosts.
            self.remember_confirmed_files([])
        else:
            # Home-dir ledger survives launchd TCC blind spots on external disks.
            scan_source = "ledger"
            for entry in self._load_ledger():
                recording_id = str(entry.get("recordingId") or "").strip()
                if not recording_id:
                    continue
                files.append({
                    "recordingId": recording_id,
                    "filename": str(entry.get("filename") or recording_id),
                    "size": int(entry.get("size") or 0),
                })
        return {
            "localSessionId": self.session_id,
            "directory": str(root),
            "scannedAt": int(time.time()),
            "scanSource": scan_source,
            "files": files,
        }

    def _proxy_is_reachable(self) -> bool:
        proxy_url = (self.proxy_url or "").strip()
        if not proxy_url:
            return False
        parsed = urllib.parse.urlparse(proxy_url)
        host = parsed.hostname
        if not host:
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            with socket.create_connection((host, port), timeout=0.4):
                return True
        except OSError:
            return False

    def _opener(self):
        use_proxy = bool((self.proxy_url or "").strip()) and self._proxy_is_reachable()
        if use_proxy != self._using_proxy:
            self._using_proxy = use_proxy
            if (self.proxy_url or "").strip():
                print(
                    "[mac-helper] VPS traffic via proxy"
                    if use_proxy
                    else "[mac-helper] proxy is down; reaching VPS directly",
                    flush=True,
                )
        proxies = {"http": self.proxy_url, "https": self.proxy_url} if use_proxy else {}
        return urllib.request.build_opener(urllib.request.ProxyHandler(proxies))

    def _vps_json(self, method: str, path: str, payload=None, timeout=20):
        url = self.allowed_origin.rstrip("/") + path
        parsed = urllib.parse.urlparse(url)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json", "Host": parsed.netloc}
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        use_proxy = bool((self.proxy_url or "").strip()) and self._proxy_is_reachable()
        if use_proxy != self._using_proxy:
            self._using_proxy = use_proxy
            if (self.proxy_url or "").strip():
                print(
                    "[mac-helper] VPS traffic via proxy"
                    if use_proxy
                    else "[mac-helper] proxy is down; reaching VPS directly",
                    flush=True,
                )
        if use_proxy:
            proxy = urllib.parse.urlparse(self.proxy_url)
            conn = http.client.HTTPConnection(
                proxy.hostname, proxy.port or 80, timeout=timeout
            )
            conn.request(method, url, body=body, headers=headers)
        else:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if parsed.scheme == "https":
                conn = http.client.HTTPSConnection(parsed.hostname, port, timeout=timeout)
            else:
                conn = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)
            request_path = parsed.path or "/"
            if parsed.query:
                request_path += "?" + parsed.query
            conn.request(method, request_path, body=body, headers=headers)
        try:
            resp = conn.getresponse()
            data = resp.read()
            if resp.status >= 400:
                raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, None)
            if not data:
                return {}
            return json.loads(data.decode("utf-8"))
        finally:
            conn.close()

    def _heartbeat_files(self, snapshot: dict) -> list:
        files = []
        for entry in snapshot.get("files") or []:
            filename = str(entry.get("filename") or "").strip()
            if not filename:
                continue
            files.append({
                "recordingId": str(entry.get("recordingId") or "") or None,
                "filename": filename,
                "size": int(entry.get("size") or 0),
            })
        return files

    def _push_heartbeat_and_run_jobs(self):
        snapshot = self.scan()
        result = self._vps_json(
            "POST",
            "/api/mac/helper/heartbeat",
            {
                "localSessionId": snapshot["localSessionId"],
                "files": self._heartbeat_files(snapshot),
            },
        )
        for job in result.get("pendingJobs") or []:
            job_id = str(job.get("jobId") or "")
            items = job.get("items") or []
            if not job_id or not items:
                continue
            print(
                f"[mac-helper] heartbeat claimed job {job_id} with {len(items)} item(s)",
                flush=True,
            )
            threading.Thread(
                target=self.open_downloads,
                args=(job_id, items),
                daemon=True,
            ).start()
        self._run_pending_commands(result.get("pendingCommands") or [])

    def _run_pending_commands(self, commands):
        for cmd in commands or []:
            ctype = str((cmd or {}).get("type") or "")
            command_id = str((cmd or {}).get("commandId") or "")
            try:
                if ctype == "open":
                    result = self.open_local(
                        relative=str(cmd.get("relativePath") or ""),
                        recording_id=str(cmd.get("recordingId") or ""),
                        reveal=bool(cmd.get("reveal")),
                    )
                    print(
                        f"[mac-helper] opened {result.get('relativePath')} "
                        f"(reveal={bool(cmd.get('reveal'))})",
                        flush=True,
                    )
                elif ctype == "delete":
                    deleted = 0
                    for item in cmd.get("items") or []:
                        if not isinstance(item, dict):
                            continue
                        self.delete_local(
                            relative=str(item.get("relativePath") or ""),
                            recording_id=str(item.get("recordingId") or ""),
                        )
                        deleted += 1
                    print(f"[mac-helper] deleted {deleted} Mac file(s)", flush=True)
                    threading.Thread(target=self._push_folder_snapshot, daemon=True).start()
                else:
                    print(f"[mac-helper] unknown command {command_id} type {ctype!r}", flush=True)
            except Exception as exc:
                print(
                    f"[mac-helper] command {command_id or ctype} failed: {exc!r}",
                    flush=True,
                )

    def _claim_and_run_commands(self):
        query = urllib.parse.urlencode({"localSessionId": self.session_id})
        result = self._vps_json(
            "GET",
            "/api/mac/helper/commands?" + query,
            timeout=8,
        )
        self._run_pending_commands(result.get("pendingCommands") or [])

    def start_vps_sync(self):
        def loop():
            while True:
                started = time.time()
                try:
                    self._push_heartbeat_and_run_jobs()
                except Exception as exc:
                    print(f"[mac-helper] VPS sync failed: {exc!r}", flush=True)
                delay = VPS_SYNC_INTERVAL_SECONDS - (time.time() - started)
                time.sleep(delay if delay > 0.5 else 0.5)

        def command_loop():
            last_error = None
            while True:
                try:
                    self._claim_and_run_commands()
                    last_error = None
                except Exception as exc:
                    message = repr(exc)
                    if message != last_error:
                        print(f"[mac-helper] command poll failed: {exc!r}", flush=True)
                        last_error = message
                time.sleep(COMMAND_POLL_INTERVAL_SECONDS)

        threading.Thread(target=loop, name="hxylive-vps-sync", daemon=True).start()
        threading.Thread(target=command_loop, name="hxylive-command-poll", daemon=True).start()

    def fetch_job(self, vps_base: str, job_id: str) -> dict:
        query = urllib.parse.urlencode({"localSessionId": self.session_id})
        url = f"{vps_base.rstrip('/')}/api/mac/download-jobs/{urllib.parse.quote(job_id)}?{query}"
        try:
            with self._opener().open(url, timeout=20) as response:
                job = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                print(f"[mac-helper] job {job_id} already claimed", flush=True)
                return {"jobId": job_id, "items": []}
            raise
        print(f"[mac-helper] claimed job {job_id} with {len(job.get('items', []))} item(s)", flush=True)
        return job

    def _ensure_dir(self, directory: Path) -> None:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            return
        except OSError as exc:
            print(f"[mac-helper] mkdir {directory} failed: {exc!r}; trying Finder", flush=True)
        proc = subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                "on run argv",
                "-e",
                'do shell script "mkdir -p " & quoted form of (item 1 of argv)',
                "-e",
                "end run",
                "--",
                str(directory),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(err or f"could not create directory {directory}")

    def _move_file(self, source: Path, dest: Path) -> None:
        """Same-volume rename is instant; Finder is a TCC fallback."""
        self._ensure_dir(dest.parent)
        if dest.exists():
            dest.unlink()
        try:
            source.replace(dest)
            return
        except OSError as exc:
            print(f"[mac-helper] rename failed ({exc!r}); trying Finder move", flush=True)
        proc = subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                "on run argv",
                "-e",
                'set srcPath to item 1 of argv',
                "-e",
                'set dstPath to item 2 of argv',
                "-e",
                'tell application "Finder"',
                "-e",
                'set srcItem to (POSIX file srcPath) as alias',
                "-e",
                'set dstFolder to (POSIX file (item 3 of argv)) as alias',
                "-e",
                'set moved to move srcItem to folder dstFolder with replacing',
                "-e",
                'set name of moved to item 4 of argv',
                "-e",
                "end tell",
                "-e",
                "end run",
                "--",
                str(source),
                str(dest),
                str(dest.parent),
                dest.name,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not dest.exists():
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(err or f"could not move {source} -> {dest}")

    def _path_size(self, path: Path):
        try:
            return path.stat().st_size
        except OSError:
            return self._stat_size_via_osascript(path)

    def _chrome_preference_download_dirs(self) -> list:
        """Read Google Chrome profile download folders."""
        found = []
        chrome_root = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
        try:
            prefs_paths = list(chrome_root.glob("*/Preferences"))
        except OSError:
            return found
        for prefs_path in prefs_paths:
            try:
                data = json.loads(prefs_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            raw = str((data.get("download") or {}).get("default_directory") or "").strip()
            if not raw:
                continue
            path = Path(raw).expanduser()
            try:
                if path.is_dir():
                    found.append(path)
            except OSError:
                continue
        return found

    def _download_watch_dirs(self) -> list:
        """
        Chrome's configured download folder only. Matching completed files
        are moved into video_dir/<streamer>/. The library itself is the
        destination, not a download watch dir, unless Chrome saves there.
        """
        ordered = []
        seen = set()
        candidates = []
        if self.chrome_download_dir_arg:
            candidates.append(Path(self.chrome_download_dir_arg))
        candidates.extend(self._chrome_preference_download_dirs())
        for raw in candidates:
            try:
                path = Path(raw).expanduser().resolve()
            except OSError:
                continue
            key = str(path)
            if key in seen:
                continue
            try:
                if not path.is_dir():
                    continue
            except OSError:
                continue
            seen.add(key)
            ordered.append(path)
        return ordered

    def _candidate_download_paths(self, download_name: str) -> list:
        """Exact basename plus Chrome's 'name (N).ext' uniquify variants."""
        stem = Path(download_name).stem
        suffix = Path(download_name).suffix
        names = [download_name]
        for index in range(1, 10):
            names.append(f"{stem} ({index}){suffix}")
        paths = []
        for root in self._download_watch_dirs():
            for name in names:
                paths.append(root / name)
        return paths

    def _chrome_file_ready(self, path: Path, expected_size: int, last_size) -> tuple:
        """
        Returns (ready, last_size_state).
        last_size_state is (size, stable_count) for unknown expected sizes.
        """
        if (path.parent / f"{path.name}.crdownload").exists():
            return False, (None, 0)
        if self._is_incomplete_download(path) or not path.exists():
            return False, (None, 0)
        size = self._path_size(path)
        if size is None or size <= 0:
            return False, (None, 0)
        if expected_size > 0:
            return size == expected_size, (size, 1)
        prev_size, prev_count = last_size if last_size else (None, 0)
        if prev_size == size:
            count = int(prev_count) + 1
        else:
            count = 1
        return count >= SIZE_STABLE_POLLS, (size, count)

    def _wait_for_chrome_download(self, download_name: str, expected_size: int) -> Path:
        deadline = time.time() + DOWNLOAD_WAIT_SECONDS
        last_sizes = {}
        watch = self._download_watch_dirs()
        if not watch:
            raise RuntimeError("no Chrome download folder to watch")
        watch_label = ", ".join(str(path) for path in watch)
        print(
            f"[mac-helper] waiting for Chrome file {download_name} in {watch_label}",
            flush=True,
        )
        while time.time() < deadline:
            for target in self._candidate_download_paths(download_name):
                key = str(target)
                state = last_sizes.get(key, (None, 0))
                ready, state = self._chrome_file_ready(target, expected_size, state)
                last_sizes[key] = state
                if ready:
                    return target
            time.sleep(DOWNLOAD_POLL_SECONDS)
        raise TimeoutError(f"timed out waiting for Chrome download: {download_name}")

    def _relocate_one(self, item: dict) -> None:
        recording_id = str(item.get("recordingId") or "").strip()
        download_name = str(
            item.get("downloadFilename") or item.get("filename") or ""
        ).strip()
        relative = str(item.get("relativePath") or "").strip()
        expected_size = int(item.get("size") or 0)
        if not download_name or not relative:
            raise ValueError("download item missing downloadFilename/relativePath")
        if "/" in download_name or "\\" in download_name:
            raise ValueError(f"downloadFilename must be a basename: {download_name!r}")
        rel = self._safe_relative_path(relative)
        root = self._refresh_video_dir()
        dest = (root / rel).resolve()
        root_resolved = root.resolve()
        if not str(dest).startswith(str(root_resolved) + os.sep):
            raise ValueError(f"relative path escapes video dir: {relative}")

        if dest.exists():
            size = self._path_size(dest) or 0
            if expected_size <= 0 or size == expected_size:
                if recording_id:
                    self.upsert_ledger_entry(recording_id, rel.as_posix(), size)
                    self.notify_filed(recording_id, rel.as_posix(), size)
                print(f"[mac-helper] already filed {relative}", flush=True)
                return

        source = self._wait_for_chrome_download(download_name, expected_size)
        size = self._path_size(source) or 0
        self._move_file(source, dest)
        if recording_id:
            self.upsert_ledger_entry(recording_id, rel.as_posix(), size)
            self.notify_filed(recording_id, rel.as_posix(), size)
        print(f"[mac-helper] filed {download_name} -> {relative} ({size} bytes)", flush=True)

    def open_downloads(self, job_id: str, items: list):
        """
        Send each URL to Chrome (fast path), then automatically:
        create HXYLIVE/<streamer>/ and rename the finished file into place.
        """
        try:
            pending = []
            for item in items:
                download_url = item.get("url")
                if not download_url:
                    continue
                subprocess.run(
                    ["/usr/bin/open", "-a", "Google Chrome", download_url],
                    check=True,
                )
                print(
                    f"[mac-helper] sent {item.get('downloadFilename') or item.get('filename')} "
                    f"to Chrome (final {item.get('relativePath')})",
                    flush=True,
                )
                pending.append(item)
                time.sleep(0.4)
            for item in pending:
                try:
                    self._relocate_one(item)
                except Exception as exc:
                    print(
                        f"[mac-helper] file/move failed for "
                        f"{item.get('relativePath') or item.get('filename')}: {exc!r}",
                        flush=True,
                    )
        except Exception as exc:
            print(f"[mac-helper] open downloads for {job_id} failed: {exc!r}", flush=True)


def make_handler(helper: Helper):
    class Handler(BaseHTTPRequestHandler):
        def _origin_allowed(self):
            origin = self.headers.get("Origin", "").rstrip("/")
            return not origin or origin == helper.allowed_origin

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", helper.allowed_origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")

        def _json(self, status, payload):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            if not self._origin_allowed():
                self._json(403, {"error": "Origin not allowed"})
                return
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if not self._origin_allowed():
                self._json(403, {"error": "Origin not allowed"})
                return
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/health":
                self._json(200, {
                    "status": "ok",
                    "localSessionId": helper.session_id,
                    "directory": str(helper.video_dir),
                })
                return
            if parsed.path == "/thumb":
                qs = urllib.parse.parse_qs(parsed.query)
                session = (qs.get("localSessionId") or [""])[0]
                if session != helper.session_id:
                    self._json(403, {"error": "Wrong local session"})
                    return
                relative = (qs.get("relativePath") or qs.get("filename") or [""])[0]
                recording_id = (qs.get("recordingId") or [""])[0]
                try:
                    thumb = helper.ensure_thumbnail(
                        relative=str(relative or ""),
                        recording_id=str(recording_id or ""),
                    )
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                except FileNotFoundError as exc:
                    self._json(404, {"error": str(exc)})
                    return
                except Exception as exc:
                    self._json(500, {"error": str(exc) or "Thumbnail failed"})
                    return
                data = thumb.read_bytes()
                ctype = "image/png" if thumb.suffix.lower() == ".png" else "image/jpeg"
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=86400")
                self.end_headers()
                self.wfile.write(data)
                return
            self._json(404, {"error": "Not found"})

        def do_POST(self):
            if not self._origin_allowed():
                self._json(403, {"error": "Origin not allowed"})
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json(400, {"error": "Invalid JSON"})
                return
            if self.path == "/scan":
                self._json(200, helper.scan())
                return
            if self.path == "/open":
                if body.get("localSessionId") != helper.session_id:
                    self._json(403, {"error": "Wrong local session"})
                    return
                try:
                    result = helper.open_local(
                        relative=str(body.get("relativePath") or body.get("filename") or ""),
                        recording_id=str(body.get("recordingId") or ""),
                        reveal=bool(body.get("reveal")),
                    )
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                except FileNotFoundError as exc:
                    self._json(404, {"error": str(exc)})
                    return
                self._json(200, result)
                return
            if self.path == "/delete":
                if body.get("localSessionId") != helper.session_id:
                    self._json(403, {"error": "Wrong local session"})
                    return
                raw_items = body.get("items")
                if raw_items is None:
                    raw_items = [{
                        "relativePath": body.get("relativePath") or body.get("filename") or "",
                        "recordingId": body.get("recordingId") or "",
                    }]
                if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 100:
                    self._json(400, {"error": "Select between 1 and 100 Mac videos"})
                    return
                deleted = []
                errors = []
                for item in raw_items:
                    if not isinstance(item, dict):
                        errors.append({"error": "Invalid item"})
                        continue
                    try:
                        result = helper.delete_local(
                            relative=str(item.get("relativePath") or item.get("filename") or ""),
                            recording_id=str(item.get("recordingId") or ""),
                        )
                        deleted.append(result)
                    except ValueError as exc:
                        errors.append({"error": str(exc)})
                    except FileNotFoundError as exc:
                        errors.append({"error": str(exc)})
                if not deleted and errors:
                    self._json(404, {"error": errors[0].get("error") or "Delete failed", "errors": errors})
                    return
                self._json(200, {
                    "status": "ok",
                    "deletedCount": len(deleted),
                    "deleted": deleted,
                    "errors": errors,
                })
                return
            if self.path == "/dispatch":
                if body.get("localSessionId") != helper.session_id:
                    self._json(403, {"error": "Wrong local session"})
                    return
                vps_base = str(body.get("vpsBase", ""))
                job_id = str(body.get("jobId", ""))
                parsed = urllib.parse.urlparse(vps_base)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc or not job_id:
                    self._json(400, {"error": "Invalid dispatch request"})
                    return
                try:
                    # Claim the job before acknowledging so the UI toast is truthful.
                    job = helper.fetch_job(vps_base, job_id)
                except Exception as exc:
                    print(f"[mac-helper] claim {job_id} failed: {exc!r}", flush=True)
                    self._json(502, {"error": f"Could not claim download job: {exc}"})
                    return
                threading.Thread(
                    target=helper.open_downloads,
                    args=(job_id, job.get("items") or []),
                    daemon=True,
                ).start()
                self._json(202, {
                    "status": "accepted",
                    "jobId": job_id,
                    "itemCount": len(job.get("items") or []),
                })
                return
            if self.path == "/wait-filed":
                if body.get("localSessionId") != helper.session_id:
                    self._json(403, {"error": "Wrong local session"})
                    return
                raw_ids = body.get("recordingIds")
                if not isinstance(raw_ids, list) or len(raw_ids) > 100:
                    self._json(400, {"error": "recordingIds must be a list of at most 100 ids"})
                    return
                try:
                    timeout_seconds = float(body.get("timeoutMs") or 25000) / 1000.0
                except (TypeError, ValueError):
                    timeout_seconds = 25.0
                timeout_seconds = min(30.0, max(0.5, timeout_seconds))
                self._json(200, helper.wait_for_filed(raw_ids, timeout_seconds))
                return
            self._json(404, {"error": "Not found"})

        def log_message(self, fmt, *args):
            print("[mac-helper] " + fmt % args)

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-dir", default=os.getenv("HXYLIVE_VIDEO_DIR", "~/Movies/HXYLIVE"))
    parser.add_argument("--origin", default=os.getenv("HXYLIVE_ORIGIN", ""))
    parser.add_argument("--port", type=int, default=17899)
    parser.add_argument("--proxy", default=os.getenv("HXYLIVE_PROXY_URL", ""))
    parser.add_argument(
        "--chrome-download-dir",
        default=os.getenv("HXYLIVE_CHROME_DOWNLOAD_DIR", ""),
        help="Optional Chrome download folder to watch (defaults to Chrome Preferences)",
    )
    args = parser.parse_args()
    if not args.origin:
        parser.error("--origin or HXYLIVE_ORIGIN is required")
    helper = Helper(
        Path(args.video_dir),
        args.origin,
        args.proxy,
        chrome_download_dir=args.chrome_download_dir or None,
    )
    helper.start_vps_sync()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(helper))
    print(f"HXYLIVE Mac helper listening on http://127.0.0.1:{args.port}")
    print(f"Video folder: {helper.video_dir}")
    watch = helper._download_watch_dirs()
    if watch:
        print("Watching Chrome downloads in: " + ", ".join(str(path) for path in watch))
    server.serve_forever()


if __name__ == "__main__":
    main()
