#!/usr/bin/env python3
"""Local-only bridge between the HXYLIVE web UI, Chrome/Motrix downloads, and the video folder."""

import argparse
import base64
import errno
import hashlib
import http.client
import json
import os
import plistlib
import queue
import re
import secrets
import shutil
import socket
import sqlite3
import struct
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".ts"}
LEDGER_NAME = "download-ledger.json"
PENDING_RELOCATE_NAME = "pending-relocate.json"
LEDGER_VERSION = 2
# How long to wait for Chrome/Motrix to finish one file (large VPS recordings).
DOWNLOAD_WAIT_SECONDS = 6 * 60 * 60
DOWNLOAD_POLL_SECONDS = 1.0
SIZE_STABLE_POLLS = 2
# Folder listing is cheap; metadata is cached by mtime/size. 10s is enough for
# auto-download sync without hammering Spotlight/ffprobe as the library grows.
VPS_SYNC_INTERVAL_SECONDS = 10.0
COMMAND_POLL_INTERVAL_SECONDS = 0.5
# Heartbeat reuses the last folder snapshot so a quiet External HDD is not woken
# every sync tick. Explicit /scan and post-file snapshots still force a live walk.
SCAN_CACHE_MAX_AGE_SECONDS = 300.0
# Collapse identical transient errors; log a repeat summary at most this often.
TRANSIENT_ERROR_LOG_INTERVAL_SECONDS = 60.0
# Pending relocate: short poll when a file may be ready to move; idle poll when
# every pending item is still downloading (do not hammer External HDDs).
PENDING_RELOCATE_POLL_SECONDS = 2.0
PENDING_RELOCATE_IDLE_POLL_SECONDS = 45.0
# While staging is incomplete, skip library-path probes on /Volumes entirely.
# When nothing is on disk yet, re-check the library at most this often.
LIBRARY_PROBE_WHILE_WAITING_SECONDS = 300.0
MOTRIX_RPC_URL = "http://127.0.0.1:16800/jsonrpc"
MOTRIX_RPC_TIMEOUT_SECONDS = 10.0
MOTRIX_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "Motrix"
# Per-task aria2 options for Media-page Motrix downloads (saturate bandwidth).
MOTRIX_SPLIT = 64
MOTRIX_MAX_CONNECTION_PER_SERVER = 64
CHROME_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
CHROME_APP_INFO_PLIST = Path("/Applications/Google Chrome.app/Contents/Info.plist")
CHROME_SAFE_STORAGE_SERVICE = "Chrome Safe Storage"
CHROME_SAFE_STORAGE_ACCOUNT = "Chrome"
CHROME_COOKIE_SALT = b"saltysalt"
CHROME_COOKIE_PBKDF2_ITERATIONS = 1003
CHROME_COOKIE_KEY_LENGTH = 16
CHROME_COOKIE_IV_HEX = "20" * 16
CHROME_EPOCH_OFFSET_SECONDS = 11644473600
CHROME_KEYCHAIN_TIMEOUT_SECONDS = 90
PROVIDER_COOKIE_SPECS = {
    "bilibili": {
        "label": "Bilibili",
        "domains": ("bilibili.com",),
        "required": ("SESSDATA", "bili_jct"),
        "optional": ("DedeUserID", "DedeUserID__ckMd5", "sid", "buvid3", "buvid4"),
        "username_cookie": None,
        "login_url": "https://www.bilibili.com",
    },
    "twitch": {
        "label": "Twitch",
        "domains": ("twitch.tv",),
        "required": ("auth-token",),
        "optional": ("login", "persistent", "unique_id", "unique_id_durable"),
        "username_cookie": "login",
        "login_url": "https://www.twitch.tv/login",
    },
    "chaturbate": {
        "label": "Chaturbate",
        "domains": ("chaturbate.com",),
        "required": ("sessionid",),
        "optional": ("csrftoken", "cf_clearance", "__cf_bm"),
        "username_cookie": None,
        "login_url": "https://chaturbate.com/auth/login/",
    },
    "stripchat": {
        "label": "Stripchat",
        "domains": ("stripchat.com", "xhamsterlive.com"),
        # Chrome now stores stripchat_com_sessionId; older profiles may still use sessionId.
        "required": (),
        "any_of_required": (("sessionId", "stripchat_com_sessionId"),),
        "optional": (
            "sessionId",
            "stripchat_com_sessionId",
            "sessionRemember",
            "stripchat_com_sessionRemember",
            "csrfToken",
            "guestID",
            "localeDomain",
            "auth",
            "cf_clearance",
            "__cf_bm",
        ),
        "username_cookie": None,
        "login_url": "https://stripchat.com/login",
    },
    "youtube": {
        "label": "YouTube",
        "domains": ("youtube.com", "google.com"),
        # Prefer .youtube.com when the same name exists on .google.com too.
        "preferred_domains": ("youtube.com",),
        "required": (),
        "optional": (
            "SAPISID",
            "__Secure-1PAPISID",
            "__Secure-3PAPISID",
            "SID",
            "HSID",
            "SSID",
            "APISID",
            "LOGIN_INFO",
            "SIDCC",
            "__Secure-1PSID",
            "__Secure-3PSID",
            "__Secure-1PSIDCC",
            "__Secure-3PSIDCC",
            "__Secure-1PSIDTS",
            "__Secure-3PSIDTS",
        ),
        "username_cookie": None,
        "login_url": "https://www.youtube.com",
        "any_of_required": (
            ("SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"),
            # Session continuity cookies; without them YouTube treats the
            # imported SID/SAPISID pair as logged out on the VPS.
            ("SIDCC", "__Secure-1PSIDCC", "__Secure-3PSIDCC"),
        ),
    },
}


def provider_cookie_spec(source_type):
    return PROVIDER_COOKIE_SPECS.get(str(source_type or "").strip().lower())


def chrome_host_matches(host_key, domains):
    host = str(host_key or "").strip().lower().lstrip(".")
    if not host:
        return False
    for domain in domains or ():
        needle = str(domain or "").strip().lower().lstrip(".")
        if not needle:
            continue
        if host == needle or host.endswith("." + needle):
            return True
    return False


def chrome_cookie_expired(expires_utc, now=None):
    try:
        expires_utc = int(expires_utc or 0)
    except (TypeError, ValueError):
        return False
    if expires_utc <= 0:
        return False
    unix = (expires_utc / 1_000_000) - CHROME_EPOCH_OFFSET_SECONDS
    return unix < float(time.time() if now is None else now)


def _pkcs7_unpad(data):
    if not data:
        return None
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 16:
        return None
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        return None
    return data[:-pad_len]


def derive_chrome_aes_key(passphrase):
    secret = passphrase if isinstance(passphrase, (bytes, bytearray)) else str(passphrase).encode("utf-8")
    return hashlib.pbkdf2_hmac(
        "sha1",
        secret,
        CHROME_COOKIE_SALT,
        CHROME_COOKIE_PBKDF2_ITERATIONS,
        dklen=CHROME_COOKIE_KEY_LENGTH,
    )


def decrypt_chrome_v10_value(encrypted_value, aes_key, db_version=0):
    blob = bytes(encrypted_value or b"")
    if len(blob) < 4 or blob[:3] != b"v10":
        return None
    ciphertext = blob[3:]
    if not ciphertext or len(ciphertext) % 16:
        return None
    try:
        result = subprocess.run(
            [
                "openssl",
                "enc",
                "-aes-128-cbc",
                "-d",
                "-K",
                aes_key.hex(),
                "-iv",
                CHROME_COOKIE_IV_HEX,
                "-nopad",
            ],
            input=ciphertext,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    decrypted = _pkcs7_unpad(result.stdout)
    if decrypted is None:
        return None
    candidates = []
    if int(db_version or 0) >= 24 and len(decrypted) > 32:
        candidates.append(decrypted[32:])
    candidates.append(decrypted)
    if len(decrypted) > 32:
        stripped = decrypted[32:]
        if stripped not in candidates:
            candidates.append(stripped)
    for candidate in candidates:
        try:
            text = candidate.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if "\x00" in text:
            continue
        return text
    return None


def chrome_keychain_passphrase():
    commands = [
        [
            "security",
            "find-generic-password",
            "-w",
            "-s",
            CHROME_SAFE_STORAGE_SERVICE,
            "-a",
            CHROME_SAFE_STORAGE_ACCOUNT,
        ],
        [
            "security",
            "find-generic-password",
            "-w",
            "-s",
            CHROME_SAFE_STORAGE_SERVICE,
        ],
    ]
    last_error = "Chrome Safe Storage was not found in Keychain"
    for command in commands:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=CHROME_KEYCHAIN_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError:
            raise RuntimeError("Keychain access is only available on macOS") from None
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "Allow Keychain access for Chrome Safe Storage when macOS asks, then retry"
            ) from None
        if result.returncode == 0:
            passphrase = (result.stdout or "").strip()
            if passphrase:
                return passphrase.encode("utf-8")
            last_error = "Chrome Safe Storage Keychain item is empty"
            continue
        err = (result.stderr or result.stdout or "").strip() or "Keychain access denied"
        last_error = err
        if "User interaction is not allowed" in err or "could not be found" in err.lower():
            continue
    raise RuntimeError(
        "Allow Keychain access for Chrome Safe Storage when macOS asks, then retry. " + last_error
    )


def chrome_user_agent(info_plist=None):
    version = "126.0.0.0"
    path = Path(info_plist) if info_plist else CHROME_APP_INFO_PLIST
    try:
        data = plistlib.loads(path.read_bytes())
        raw = str(data.get("CFBundleShortVersionString") or "").strip()
        if raw:
            version = raw if raw.count(".") >= 3 else raw + ".0.0.0"
    except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
        pass
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{version} Safari/537.36"
    )


def chrome_profile_cookie_db(profile_dir):
    profile_dir = Path(profile_dir)
    for relative in ("Network/Cookies", "Cookies"):
        candidate = profile_dir / relative
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def iter_chrome_cookie_dbs(chrome_root=None):
    root = Path(chrome_root) if chrome_root else CHROME_SUPPORT_DIR
    try:
        children = list(root.iterdir())
    except OSError:
        return []
    profiles = []
    for child in children:
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        name = child.name
        if name != "Default" and not name.startswith("Profile "):
            continue
        db_path = chrome_profile_cookie_db(child)
        if db_path is None:
            continue
        try:
            mtime = db_path.stat().st_mtime
        except OSError:
            mtime = 0
        profiles.append((name, db_path, mtime))
    profiles.sort(key=lambda item: (0 if item[0] == "Default" else 1, -item[2], item[0]))
    return profiles


def copy_chrome_cookies_db(db_path, dest):
    db_path = Path(db_path)
    dest = Path(dest)
    shutil.copy2(db_path, dest)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    for suffix in ("-wal", "-shm"):
        extra = Path(str(db_path) + suffix)
        try:
            if not extra.exists():
                continue
        except OSError:
            continue
        target = Path(str(dest) + suffix)
        shutil.copy2(extra, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass


def chrome_cookie_db_version(cursor):
    try:
        cursor.execute("SELECT value FROM meta WHERE key = 'version'")
        row = cursor.fetchone()
        if row:
            return int(row[0])
    except (sqlite3.Error, TypeError, ValueError):
        pass
    return 0


def wanted_cookie_names(spec):
    required = tuple(spec.get("required") or ())
    optional = spec.get("optional")
    any_of_required = spec.get("any_of_required") or ()
    if optional is None and not any_of_required:
        return None
    names = set(required)
    if optional is not None:
        names.update(optional)
    for group in any_of_required:
        names.update(group or ())
    return names


def chrome_cookie_host_rank(host_key, preferred_domains):
    """Lower rank is better. preferred_domains are tried before other matched hosts."""
    host = str(host_key or "").strip().lower().lstrip(".")
    preferred = tuple(
        str(item or "").strip().lower().lstrip(".")
        for item in (preferred_domains or ())
        if str(item or "").strip()
    )
    for index, needle in enumerate(preferred):
        if host == needle or host.endswith("." + needle):
            return index
    return len(preferred)


def normalize_stripchat_session_cookies(cookies):
    """Keep Chrome's stripchat_com_* names and mirror legacy sessionId aliases."""
    items = [dict(cookie) for cookie in (cookies or []) if isinstance(cookie, dict) and cookie.get("name")]
    by_name = {str(item.get("name")): item for item in items}
    aliases = (
        ("stripchat_com_sessionId", "sessionId"),
        ("stripchat_com_sessionRemember", "sessionRemember"),
        ("sessionId", "stripchat_com_sessionId"),
        ("sessionRemember", "stripchat_com_sessionRemember"),
    )
    for source_name, alias_name in aliases:
        source = by_name.get(source_name)
        if not source or alias_name in by_name:
            continue
        alias = dict(source)
        alias["name"] = alias_name
        items.append(alias)
        by_name[alias_name] = alias
    return items


def extract_cookies_from_db(
    db_path,
    spec,
    *,
    aes_key=None,
    keychain_loader=None,
):
    tmp = None
    conn = None
    try:
        handle = tempfile.NamedTemporaryFile(prefix="hxylive-chrome-cookies-", suffix=".sqlite", delete=False)
        tmp = Path(handle.name)
        handle.close()
        copy_chrome_cookies_db(db_path, tmp)
        conn = sqlite3.connect(str(tmp))
        cursor = conn.cursor()
        db_version = chrome_cookie_db_version(cursor)
        try:
            cursor.execute(
                "SELECT host_key, name, value, encrypted_value, path, expires_utc FROM cookies"
            )
            rows = cursor.fetchall()
        except sqlite3.Error:
            cursor.execute("SELECT host_key, name, value, encrypted_value FROM cookies")
            rows = [(host, name, value, encrypted, "/", 0) for host, name, value, encrypted in cursor.fetchall()]
        names = wanted_cookie_names(spec)
        domains = spec.get("domains") or ()
        preferred_domains = spec.get("preferred_domains") or ()
        found = {}
        current_key = aes_key
        for host_key, name, value, encrypted_value, path, expires_utc in rows:
            name = str(name or "")
            if not name or not chrome_host_matches(host_key, domains):
                continue
            if names is not None and name not in names:
                continue
            if chrome_cookie_expired(expires_utc):
                continue
            plaintext = str(value or "")
            if not plaintext:
                blob = encrypted_value or b""
                if blob and bytes(blob)[:3] == b"v10":
                    if current_key is None:
                        loader = keychain_loader or chrome_keychain_passphrase
                        current_key = derive_chrome_aes_key(loader())
                    plaintext = decrypt_chrome_v10_value(blob, current_key, db_version) or ""
            if not plaintext:
                continue
            rank = chrome_cookie_host_rank(host_key, preferred_domains)
            existing = found.get(name)
            if existing is not None and int(existing.get("_rank", 999)) <= rank:
                continue
            found[name] = {
                "name": name,
                "value": plaintext,
                "domain": str(host_key or ""),
                "path": str(path or "/") or "/",
                "_rank": rank,
            }
        for item in found.values():
            item.pop("_rank", None)
        return found, current_key
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        if tmp is not None:
            for path in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
                try:
                    path.unlink()
                except OSError:
                    pass


def cookies_to_header(cookies):
    parts = []
    for cookie in cookies or []:
        name = str((cookie or {}).get("name") or "")
        value = (cookie or {}).get("value")
        if name and value is not None:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def export_provider_cookies_from_chrome(
    source_type,
    *,
    chrome_root=None,
    aes_key=None,
    keychain_loader=None,
    user_agent=None,
    all_cookies=False,
):
    spec = provider_cookie_spec(source_type)
    if not spec:
        return {
            "ok": False,
            "error": "Chrome import is not available for this provider",
            "status": 400,
        }
    if all_cookies:
        # Collect every cookie for the provider hosts (needed for Chrome CDP login).
        spec = dict(spec)
        spec["optional"] = None
        spec["any_of_required"] = ()
    profiles = iter_chrome_cookie_dbs(chrome_root)
    if not profiles:
        return {
            "ok": False,
            "error": "Google Chrome is not installed or its cookie store is unreadable",
            "loginUrl": spec["login_url"],
            "status": 404,
        }
    required = tuple(spec.get("required") or ())
    any_of_required = tuple(tuple(group) for group in (spec.get("any_of_required") or ()) if group)
    min_cookies = int(spec.get("min_cookies") or 0)
    last_error = None
    current_key = aes_key
    best = None
    for profile_name, db_path, _mtime in profiles:
        try:
            found, current_key = extract_cookies_from_db(
                db_path,
                spec,
                aes_key=current_key,
                keychain_loader=keychain_loader,
            )
        except RuntimeError as exc:
            last_error = str(exc)
            continue
        except (OSError, sqlite3.Error) as exc:
            last_error = f"Could not read Chrome cookies ({exc})"
            continue
        missing = [name for name in required if name not in found]
        for group in any_of_required:
            if not any(name in found for name in group):
                missing.append(" or ".join(group))
        if missing:
            if best is None:
                best = (profile_name, found, missing)
            continue
        if min_cookies and len(found) < min_cookies:
            if best is None:
                best = (profile_name, found, list(required))
            continue
        cookies = list(found.values())
        if str(source_type).strip().lower() == "stripchat":
            cookies = normalize_stripchat_session_cookies(cookies)
        username_cookie = spec.get("username_cookie")
        username = ""
        if username_cookie and username_cookie in found:
            username = str(found[username_cookie]["value"] or "").strip()
        return {
            "ok": True,
            "sourceType": str(source_type).strip().lower(),
            "profile": profile_name,
            "cookies": cookies,
            "cookieHeader": cookies_to_header(cookies),
            "cookieNames": [cookie["name"] for cookie in cookies],
            "username": username,
            "userAgent": user_agent or chrome_user_agent(),
            "loginUrl": spec["login_url"],
            "_aes_key": current_key,
        }
    found = (best[1] if best else {}) or {}
    missing = (best[2] if best else list(required)) or list(required)
    if last_error and not found:
        status = 403 if "Keychain" in last_error else 500
        return {
            "ok": False,
            "error": last_error,
            "loginUrl": spec["login_url"],
            "status": status,
            "_aes_key": current_key,
        }
    missing_label = ", ".join(missing) if missing else "a logged-in session"
    return {
        "ok": False,
        "error": (
            f"Log into {spec['label']} in Google Chrome, then click Import from Chrome again. "
            f"Missing: {missing_label}"
        ),
        "missing": missing,
        "loginUrl": spec["login_url"],
        "status": 409,
        "_aes_key": current_key,
    }


def open_chrome_url(url):
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Invalid Chrome URL")
    subprocess.Popen(
        ["/usr/bin/open", "-a", "Google Chrome", parsed.geturl()],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return parsed.geturl()


def _chrome_executable_path():
    path = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if path.is_file():
        return str(path)
    return ""


class _ChromeDevtoolsClient:
    """Minimal Chrome DevTools Protocol client (stdlib websocket)."""

    def __init__(self, ws_url, timeout=60):
        parsed = urllib.parse.urlparse(ws_url)
        if parsed.scheme not in {"ws", "http"}:
            raise RuntimeError(f"Unsupported DevTools URL: {ws_url}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path = path + "?" + parsed.query
        self._timeout = float(timeout)
        self._sock = socket.create_connection((host, port), timeout=self._timeout)
        self._sock.settimeout(self._timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sock.sendall(req.encode("ascii"))
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise RuntimeError("DevTools websocket closed during handshake")
            header += chunk
            if len(header) > 65536:
                raise RuntimeError("DevTools websocket handshake too large")
        status_line = header.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
        if "101" not in status_line:
            raise RuntimeError(f"DevTools websocket upgrade failed: {status_line}")
        self._next_id = 1
        leftover = header.split(b"\r\n\r\n", 1)[1]
        self._buffer = leftover

    def close(self):
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def _recv_frame(self):
        def read_exact(n):
            buf = b""
            while len(buf) < n:
                if self._buffer:
                    take = min(n - len(buf), len(self._buffer))
                    buf += self._buffer[:take]
                    self._buffer = self._buffer[take:]
                    continue
                chunk = self._sock.recv(max(4096, n - len(buf)))
                if not chunk:
                    raise RuntimeError("DevTools websocket closed")
                self._buffer += chunk
            return buf

        header = read_exact(2)
        b0, b1 = header[0], header[1]
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", read_exact(8))[0]
        mask = read_exact(4) if masked else b""
        payload = read_exact(length)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x8:
            raise RuntimeError("DevTools websocket closed by Chrome")
        if opcode == 0x9:  # ping
            self._send_frame(0xA, payload)
            return self._recv_frame()
        if opcode != 0x1:
            return self._recv_frame()
        return json.loads(payload.decode("utf-8"))

    def _send_frame(self, opcode, payload):
        payload = bytes(payload)
        header = bytearray()
        header.append(0x80 | (opcode & 0x0F))
        mask_bit = 0x80
        length = len(payload)
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(mask_bit | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(header + masked)

    def call(self, method, params=None, timeout=None):
        msg_id = self._next_id
        self._next_id += 1
        self._send_frame(0x1, json.dumps({
            "id": msg_id,
            "method": method,
            "params": params or {},
        }).encode("utf-8"))
        deadline = time.time() + float(timeout if timeout is not None else self._timeout)
        while time.time() < deadline:
            remaining = max(0.1, deadline - time.time())
            self._sock.settimeout(remaining)
            try:
                message = self._recv_frame()
            except socket.timeout:
                break
            if not isinstance(message, dict):
                continue
            if message.get("id") == msg_id:
                if message.get("error"):
                    raise RuntimeError(str(message["error"]))
                return message.get("result") or {}
        raise TimeoutError(f"CDP timeout waiting for {method}")


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
        self._ensure_video_dir()
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
        self.pending_relocate_path = self.support_dir / PENDING_RELOCATE_NAME
        self._pending_relocate_lock = threading.Lock()
        # Serialize sweeps so cross-volume copies never overlap (size-mismatch races).
        self._pending_relocate_sweep_lock = threading.Lock()
        # path -> (size, stable_count) across short-poll sweeps for unknown sizes.
        self._pending_relocate_size_state = {}
        self._pending_relocate_wake = threading.Event()
        # relativePath -> last time we probed video_dir (External) for this item.
        self._pending_library_probe_at = {}
        self._filed_lock = threading.Lock()
        self._filed_waiters = []
        self._thumb_queue = queue.Queue()
        self._thumbs_enqueued = set()
        self._thumb_worker_started = False
        self._chrome_aes_key = None
        self._chrome_aes_key_lock = threading.Lock()
        # path -> {stamp, durationSeconds, resolution, fps, bitrate}; skip re-probe on unchanged files.
        self._media_meta_cache = {}
        self._media_meta_cache_lock = threading.Lock()
        # path -> {stamp, meta}; share one ffprobe JSON parse across resolution/fps/bitrate.
        self._ffprobe_meta_cache = {}
        self._ffprobe_meta_cache_lock = threading.Lock()
        # Motrix GIDs / staging paths for manual Media-queue progress reporting.
        self._motrix_track_lock = threading.Lock()
        self._motrix_gids = set()
        self._motrix_paths = set()
        self._motrix_meta = {}  # gid -> {path, recordingId, itemId, relativePath}
        self._repeat_log_lock = threading.Lock()
        # key -> {message, last_log, suppressed}
        self._repeat_log = {}
        self._scan_cache_lock = threading.Lock()
        self._scan_cache = None
        self._scan_cache_at = 0.0

    def _log_repeated(self, key: str, message: str) -> None:
        """Log identical transient errors once, then summarize repeats periodically."""
        now = time.time()
        key = str(key or "").strip() or "error"
        message = str(message or "").strip() or "error"
        with self._repeat_log_lock:
            state = self._repeat_log.get(key)
            if state is None or state.get("message") != message:
                prior = state.get("suppressed", 0) if state else 0
                prior_msg = state.get("message") if state else ""
                if prior and prior_msg:
                    print(
                        f"[mac-helper] {prior_msg} (repeated {prior}x)",
                        flush=True,
                    )
                print(f"[mac-helper] {message}", flush=True)
                self._repeat_log[key] = {
                    "message": message,
                    "last_log": now,
                    "suppressed": 0,
                }
                return
            state["suppressed"] = int(state.get("suppressed") or 0) + 1
            if now - float(state.get("last_log") or 0) >= TRANSIENT_ERROR_LOG_INTERVAL_SECONDS:
                print(
                    f"[mac-helper] {message} (repeated {state['suppressed']}x)",
                    flush=True,
                )
                state["last_log"] = now
                state["suppressed"] = 0

    def _clear_repeated(self, key: str) -> None:
        """Clear a transient-error key; flush any suppressed count as recovered."""
        key = str(key or "").strip() or "error"
        with self._repeat_log_lock:
            state = self._repeat_log.pop(key, None)
        if not state:
            return
        suppressed = int(state.get("suppressed") or 0)
        message = str(state.get("message") or "").strip()
        if suppressed and message:
            print(
                f"[mac-helper] {message} (repeated {suppressed}x; recovered)",
                flush=True,
            )

    def invalidate_scan_cache(self) -> None:
        """Drop the cached folder snapshot so the next scan hits disk."""
        with self._scan_cache_lock:
            self._scan_cache = None
            self._scan_cache_at = 0.0

    @staticmethod
    def _volume_root(path: Path):
        """`/Volumes/Name` for removable-disk paths; otherwise None."""
        parts = Path(path).parts
        if len(parts) >= 3 and parts[0] == "/" and parts[1] == "Volumes":
            return Path("/") / "Volumes" / parts[2]
        return None

    def _volume_unmounted(self, root: Path) -> bool:
        """True when the `/Volumes/Name` disk is gone or only a leftover stub."""
        volume = self._volume_root(root)
        if volume is None:
            return False
        try:
            if not volume.exists():
                return True
        except OSError:
            return False
        try:
            return not os.path.ismount(str(volume))
        except OSError:
            return False

    def _video_dir_absent(self, root: Path) -> bool:
        """True when the folder or its volume is gone (not a TCC listing denial)."""
        if self._volume_unmounted(root):
            return True
        try:
            return not root.exists()
        except OSError:
            return False

    def _ensure_video_dir(self) -> Path:
        """Create the folder at startup when the volume is present. Never create `/Volumes` stubs."""
        resolved = Path(self.video_dir_arg).expanduser().resolve()
        self.video_dir = resolved
        if self._volume_unmounted(resolved):
            return resolved
        try:
            resolved.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"[mac-helper] could not create video dir {resolved}: {exc!r}", flush=True)
        return resolved

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

    def _pending_relocate_key(self, item: dict) -> str:
        relative = str(item.get("relativePath") or "").strip()
        recording_id = str(item.get("recordingId") or "").strip()
        download_name = str(
            item.get("downloadFilename") or item.get("filename") or ""
        ).strip()
        return relative or recording_id or download_name

    def _load_pending_relocates(self) -> list:
        try:
            data = json.loads(self.pending_relocate_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        if not isinstance(data, dict) or int(data.get("version") or 0) != 1:
            return []
        items = data.get("items")
        return items if isinstance(items, list) else []

    def _save_pending_relocates(self, items: list) -> None:
        payload = {"version": 1, "items": items}
        tmp = self.pending_relocate_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.pending_relocate_path)

    def _enqueue_pending_relocate(self, item: dict, method: str = "motrix") -> None:
        relative = str(item.get("relativePath") or "").strip()
        download_name = str(
            item.get("downloadFilename") or item.get("filename") or ""
        ).strip()
        if not relative or not download_name:
            return
        entry = {
            "recordingId": str(item.get("recordingId") or "").strip(),
            "downloadFilename": download_name,
            "relativePath": relative,
            "size": int(item.get("size") or 0),
            "method": str(method or "motrix").strip().lower() or "motrix",
            "enqueuedAt": int(time.time()),
        }
        key = self._pending_relocate_key(entry)
        with self._pending_relocate_lock:
            items = [
                row
                for row in self._load_pending_relocates()
                if self._pending_relocate_key(row) != key
            ]
            items.append(entry)
            self._save_pending_relocates(items)
        self._wake_pending_relocate()

    def _dequeue_pending_relocate(self, item: dict) -> None:
        key = self._pending_relocate_key(item)
        if not key:
            return
        with self._pending_relocate_lock:
            items = [
                row
                for row in self._load_pending_relocates()
                if self._pending_relocate_key(row) != key
            ]
            self._save_pending_relocates(items)

    def _wake_pending_relocate(self) -> None:
        self._pending_relocate_wake.set()

    def _download_candidate_present(self, path: Path) -> bool:
        """True if the target or a Motrix/Chrome in-progress sidecar exists."""
        try:
            if path.exists():
                return True
            if Path(str(path) + ".aria2").exists():
                return True
            if (path.parent / f"{path.name}.crdownload").exists():
                return True
        except OSError:
            pass
        return False

    def _staging_candidates_for_item(self, item: dict, method: str = "motrix") -> list:
        download_name = str(
            item.get("downloadFilename") or item.get("filename") or ""
        ).strip()
        relative = str(item.get("relativePath") or "").strip()
        download_method = str(method or "motrix").strip().lower() or "motrix"
        candidates = []
        seen = set()
        if download_method == "motrix" and relative:
            try:
                staging = self._motrix_staging_path(
                    relative_path=relative,
                    download_name=download_name,
                )
            except ValueError:
                staging = None
            if staging is not None:
                key = str(staging)
                if key not in seen:
                    seen.add(key)
                    candidates.append(staging)
        if download_name and "/" not in download_name and "\\" not in download_name:
            for path in self._candidate_download_paths(download_name, download_method):
                key = str(path)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(path)
        return candidates

    def _library_dest_for_item(self, item: dict):
        relative = str(item.get("relativePath") or "").strip()
        if not relative:
            raise ValueError("download item missing relativePath")
        rel = self._safe_relative_path(relative)
        root = self._refresh_video_dir()
        dest = (root / rel).resolve()
        root_resolved = root.resolve()
        if not str(dest).startswith(str(root_resolved) + os.sep):
            raise ValueError(f"relative path escapes video dir: {relative}")
        return rel, dest

    def _mark_item_filed(self, item: dict, rel: Path, size: int) -> None:
        recording_id = str(item.get("recordingId") or "").strip()
        if not recording_id:
            return
        self.upsert_ledger_entry(recording_id, rel.as_posix(), size)
        self.notify_filed(recording_id, rel.as_posix(), size)

    def _item_already_in_library(self, item: dict) -> bool:
        expected_size = int(item.get("size") or 0)
        try:
            _rel, dest = self._library_dest_for_item(item)
        except ValueError:
            return False
        if not dest.exists():
            return False
        size = self._path_size(dest) or 0
        return expected_size <= 0 or size == expected_size

    def _item_staging_ready(self, item: dict, method: str = "motrix") -> bool:
        expected_size = int(item.get("size") or 0)
        for target in self._staging_candidates_for_item(item, method):
            ready, _state = self._chrome_file_ready(target, expected_size, (None, 0))
            if ready:
                return True
        return False

    def _staging_looks_incomplete(self, path: Path) -> bool:
        """True when Motrix/Chrome still owns this staging path (do not touch library)."""
        try:
            if Path(str(path) + ".aria2").exists():
                return True
            if (path.parent / f"{path.name}.crdownload").exists():
                return True
            if self._is_incomplete_download(path):
                return True
        except OSError:
            return False
        return False

    def _item_staging_incomplete(self, item: dict, method: str = "motrix") -> bool:
        for target in self._staging_candidates_for_item(item, method):
            if self._staging_looks_incomplete(target):
                return True
        return False

    def _should_probe_library(self, relative: str, *, force: bool = False) -> bool:
        """Rate-limit video_dir probes so quiet External HDDs stay spun down."""
        key = str(relative or "").strip()
        if not key:
            return False
        if force:
            self._pending_library_probe_at[key] = time.time()
            return True
        now = time.time()
        last = float(self._pending_library_probe_at.get(key) or 0)
        if (now - last) < LIBRARY_PROBE_WHILE_WAITING_SECONDS:
            return False
        self._pending_library_probe_at[key] = now
        return True

    def _try_pending_relocate(self, item: dict, size_state: dict) -> str:
        """
        Non-blocking relocate attempt.
        Returns: filed | waiting | stale | error

        Staging (Mac Downloads) is checked first. Library paths under /Volumes are
        only touched when a staging file is ready to move, or on a long backoff
        when nothing is downloading (already-filed / missing source).
        """
        method = str(item.get("method") or "motrix").strip().lower() or "motrix"
        download_name = str(
            item.get("downloadFilename") or item.get("filename") or ""
        ).strip()
        relative = str(item.get("relativePath") or "").strip()
        expected_size = int(item.get("size") or 0)
        if not download_name or not relative:
            return "stale"
        if "/" in download_name or "\\" in download_name:
            return "stale"

        candidates = self._staging_candidates_for_item(item, method)
        source = None
        any_present = False
        any_incomplete = False
        for target in candidates:
            if self._download_candidate_present(target):
                any_present = True
            if self._staging_looks_incomplete(target):
                any_incomplete = True
            key = str(target)
            state = size_state.get(key, (None, 0))
            ready, state = self._chrome_file_ready(target, expected_size, state)
            size_state[key] = state
            if ready:
                source = target
                break

        if source is not None:
            try:
                rel, dest = self._library_dest_for_item(item)
            except ValueError:
                return "stale"
            try:
                size = self._path_size(source) or 0
                if source.resolve() != dest:
                    self._move_file(source, dest)
                    size = self._path_size(dest) or size
                    if method == "motrix":
                        for candidate in {source}:
                            leftover = Path(str(candidate) + ".aria2")
                            try:
                                if leftover.exists():
                                    leftover.unlink()
                            except OSError:
                                pass
                self._mark_item_filed(item, rel, size)
                self._pending_library_probe_at.pop(relative, None)
                if source.resolve() == dest:
                    print(f"[mac-helper] filed {relative} ({size} bytes)", flush=True)
                else:
                    print(
                        f"[mac-helper] filed {download_name} -> {relative} ({size} bytes)",
                        flush=True,
                    )
                return "filed"
            except Exception as exc:
                print(
                    f"[mac-helper] file/move failed for {relative or download_name}: {exc!r}",
                    flush=True,
                )
                return "error"

        # Incomplete download on the Mac volume: wait locally. Do not probe External.
        if any_incomplete:
            return "waiting"

        # No ready staging. Rarely check whether the library already has the file.
        if self._should_probe_library(relative):
            try:
                rel, dest = self._library_dest_for_item(item)
            except ValueError:
                return "stale"
            try:
                exists = dest.exists()
            except OSError:
                exists = False
            if exists:
                size = self._path_size(dest) or 0
                if expected_size <= 0 or size == expected_size:
                    self._mark_item_filed(item, rel, size)
                    self._pending_library_probe_at.pop(relative, None)
                    print(f"[mac-helper] already filed {relative}", flush=True)
                    return "filed"

        enqueued_at = int(item.get("enqueuedAt") or 0)
        if (
            enqueued_at > 0
            and (time.time() - enqueued_at) >= DOWNLOAD_WAIT_SECONDS
            and not any_present
        ):
            return "stale"
        return "waiting"

    def _sweep_pending_relocates(self) -> int:
        """Move every ready pending file; skip unfinished ones without blocking."""
        # Only one sweep at a time: overlapping cross-volume copy2 races corrupt dest.
        if not self._pending_relocate_sweep_lock.acquire(blocking=False):
            self._wake_pending_relocate()
            return 0
        try:
            with self._pending_relocate_lock:
                pending = list(self._load_pending_relocates())
            if not pending:
                return 0
            filed = 0
            for item in pending:
                key = self._pending_relocate_key(item)
                size_state = self._pending_relocate_size_state.setdefault(key, {})
                try:
                    result = self._try_pending_relocate(item, size_state)
                except Exception as exc:
                    print(
                        f"[mac-helper] pending relocate failed for "
                        f"{item.get('relativePath') or item.get('downloadFilename')}: {exc!r}",
                        flush=True,
                    )
                    continue
                if result == "filed":
                    self._dequeue_pending_relocate(item)
                    self._pending_relocate_size_state.pop(key, None)
                    filed += 1
                elif result == "stale":
                    self._dequeue_pending_relocate(item)
                    self._pending_relocate_size_state.pop(key, None)
                    self._pending_library_probe_at.pop(
                        str(item.get("relativePath") or "").strip(), None
                    )
                    print(
                        f"[mac-helper] dropped stale pending relocate "
                        f"{item.get('relativePath') or item.get('downloadFilename')}",
                        flush=True,
                    )
            return filed
        finally:
            self._pending_relocate_sweep_lock.release()

    def _pending_relocate_wait_seconds(self) -> float:
        """Longer sleep when every pending item is still downloading on the Mac."""
        with self._pending_relocate_lock:
            pending = list(self._load_pending_relocates())
        if not pending:
            return PENDING_RELOCATE_IDLE_POLL_SECONDS
        for item in pending:
            method = str(item.get("method") or "motrix").strip().lower() or "motrix"
            if self._item_staging_incomplete(item, method):
                continue
            # Ready / missing / library-check candidates keep the short poll.
            return PENDING_RELOCATE_POLL_SECONDS
        return PENDING_RELOCATE_IDLE_POLL_SECONDS

    def _pending_relocate_loop(self) -> None:
        """Background short-poll: relocate ready staging files as they finish."""
        time.sleep(1.0)
        with self._pending_relocate_lock:
            pending_n = len(self._load_pending_relocates())
        if pending_n:
            print(
                f"[mac-helper] pending relocate loop starting with {pending_n} item(s)",
                flush=True,
            )
        while True:
            with self._pending_relocate_lock:
                pending_n = len(self._load_pending_relocates())
            # No registered pending paths: deep sleep until enqueue wakes us
            # (do not busy-scan Downloads / whole library).
            if pending_n == 0:
                self._pending_relocate_wake.wait(timeout=120.0)
                self._pending_relocate_wake.clear()
                continue
            try:
                filed = self._sweep_pending_relocates()
                if filed:
                    print(
                        f"[mac-helper] relocated {filed} ready download(s) from staging",
                        flush=True,
                    )
            except Exception as exc:
                print(f"[mac-helper] pending relocate sweep failed: {exc!r}", flush=True)
            wait_s = self._pending_relocate_wait_seconds()
            self._pending_relocate_wake.wait(timeout=wait_s)
            self._pending_relocate_wake.clear()

    def _resume_pending_relocates(self) -> None:
        """One-shot sweep (tests / callers); production uses _pending_relocate_loop."""
        self._sweep_pending_relocates()

    def remember_confirmed_files(self, items: list) -> None:
        """Persist files actually seen on disk (survives launchd TCC blind spots).

        Merge with the previous ledger so date-renamed Motrix files keep their
        recordingId across scans. Drop entries whose path is no longer on disk.
        """
        now = int(time.time())
        disk_by_path = {}
        for item in items:
            filename = str(item.get("filename") or "").strip().replace("\\", "/")
            if not filename:
                continue
            disk_by_path[filename] = item

        merged = {}
        for entry in self._load_ledger():
            filename = str(entry.get("filename") or "").strip().replace("\\", "/")
            recording_id = str(entry.get("recordingId") or "").strip()
            if not filename or not recording_id:
                continue
            if filename not in disk_by_path:
                continue
            disk_item = disk_by_path[filename]
            merged[filename] = {
                "recordingId": recording_id,
                "filename": filename,
                "size": int(disk_item.get("size") or entry.get("size") or 0),
                "updatedAt": now,
            }

        for filename, item in disk_by_path.items():
            recording_id = str(item.get("recordingId") or "").strip()
            if not recording_id:
                continue
            merged[filename] = {
                "recordingId": recording_id,
                "filename": filename,
                "size": int(item.get("size") or 0),
                "updatedAt": now,
            }

        self._save_ledger(list(merged.values()))

    def upsert_ledger_entry(self, recording_id: str, filename: str, size: int) -> None:
        """Record one completed download so scan can map date-named files → recordingId."""
        self.invalidate_scan_cache()
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
        self.invalidate_scan_cache()
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
            self.invalidate_scan_cache()
            self._push_heartbeat_and_run_jobs()
        except Exception as exc:
            print(f"[mac-helper] snapshot after file failed: {exc!r}", flush=True)

    def _attach_recording_ids(self, files: list) -> list:
        """Map relative paths back to recordingIds via legacy names or the ledger."""
        ledger = self._load_ledger()
        by_name = {
            str(entry.get("filename") or "").strip().replace("\\", "/"): entry
            for entry in ledger
            if entry.get("filename") and entry.get("recordingId")
        }
        size_counts = {}
        for entry in ledger:
            if not entry.get("recordingId"):
                continue
            size = int(entry.get("size") or 0)
            if size <= 0:
                continue
            size_counts[size] = size_counts.get(size, 0) + 1
        by_unique_size = {
            int(entry.get("size") or 0): entry
            for entry in ledger
            if entry.get("recordingId")
            and int(entry.get("size") or 0) > 0
            and size_counts.get(int(entry.get("size") or 0), 0) == 1
        }
        for entry in files:
            if entry.get("recordingId"):
                continue
            filename = str(entry.get("filename") or "").strip().replace("\\", "/")
            prior = by_name.get(filename)
            if not prior:
                size = int(entry.get("size") or 0)
                prior = by_unique_size.get(size) if size > 0 else None
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
        """Open a Mac folder video in IINA (reveal still uses Finder)."""
        path = self._resolve_local_media(relative=relative, recording_id=recording_id)
        if reveal:
            subprocess.run(["/usr/bin/open", "-R", str(path)], check=False)
        else:
            opened = subprocess.run(
                ["/usr/bin/open", "-a", "IINA", str(path)],
                check=False,
            )
            if opened.returncode != 0:
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
        self.invalidate_scan_cache()
        return {
            "status": "ok",
            "relativePath": rel,
            "deleted": True,
        }

    def rename_streamer_folder(self, from_folder: str = "", to_folder: str = "") -> dict:
        """Rename ``<roomId>(source)`` to ``<displayName>(source)`` and rewrite ledger paths."""
        src_name = str(from_folder or "").strip().replace("\\", "/").strip("/")
        dst_name = str(to_folder or "").strip().replace("\\", "/").strip("/")
        if not src_name or not dst_name or "/" in src_name or "/" in dst_name:
            raise ValueError("fromFolder and toFolder must be single folder names")
        if src_name in {".", ".."} or dst_name in {".", ".."}:
            raise ValueError("invalid folder name")
        if src_name == dst_name:
            return {"status": "ok", "fromFolder": src_name, "toFolder": dst_name, "movedFiles": 0}

        root = self._refresh_video_dir().resolve()
        src = (root / src_name).resolve()
        dst = (root / dst_name).resolve()
        if not str(src).startswith(str(root) + os.sep) or not str(dst).startswith(str(root) + os.sep):
            raise ValueError("folder path escapes video directory")
        if not src.is_dir():
            return {"status": "ok", "fromFolder": src_name, "toFolder": dst_name, "movedFiles": 0}

        self._ensure_dir(dst)
        moved = 0
        for path in sorted(src.rglob("*")):
            if not path.is_file():
                continue
            rel_inside = path.relative_to(src)
            dest = dst / rel_inside
            self._move_file(path, dest)
            moved += 1

        for directory in sorted((p for p in src.rglob("*") if p.is_dir()), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            src.rmdir()
        except OSError:
            pass

        prefix = src_name + "/"
        rewritten = []
        for entry in self._load_ledger():
            filename = str(entry.get("filename") or "").replace("\\", "/")
            if filename == src_name or filename.startswith(prefix):
                suffix = filename[len(src_name):].lstrip("/")
                entry = dict(entry)
                entry["filename"] = f"{dst_name}/{suffix}" if suffix else dst_name
            rewritten.append(entry)
        self._save_ledger(rewritten)
        self.invalidate_scan_cache()
        return {
            "status": "ok",
            "fromFolder": src_name,
            "toFolder": dst_name,
            "movedFiles": moved,
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
        osascript = Path("/usr/bin/osascript")
        if not osascript.is_file():
            raise RuntimeError("osascript not available")
        try:
            proc = subprocess.run(
                [
                    str(osascript),
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
        except OSError as exc:
            raise RuntimeError(f"osascript failed: {exc}") from exc
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
        if name.endswith(".crdownload") or name.endswith(".download") or name.endswith(".aria2"):
            return True
        # Chrome on macOS often stages as "Unconfirmed <id>.crdownload".
        if name.startswith("Unconfirmed "):
            return True
        # Motrix/aria2 writes the final basename immediately and grows it in place,
        # with a sibling "<file>.aria2" control file until the transfer finishes.
        # Chrome may leave "<file>.crdownload" beside a premature target name.
        try:
            if Path(str(path) + ".aria2").exists():
                return True
            if (path.parent / f"{path.name}.crdownload").exists():
                return True
        except OSError:
            pass
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
            # Root-level files are ignored; only streamer subfolders are library.
            if "/" not in relative:
                continue
            size = None
            try:
                size = path.stat().st_size
            except OSError:
                size = self._stat_size_via_osascript(path)
            if size is None:
                continue
            duration = None
            resolution = None
            fps = None
            bitrate = None
            meta = self._cached_media_meta(path, size)
            if isinstance(meta, dict):
                duration = meta.get("durationSeconds")
                resolution = meta.get("resolution")
                fps = meta.get("fps")
                bitrate = meta.get("bitrate")
            entry = {
                "recordingId": self._recording_id_for_name(path.name),
                "filename": relative,
                "size": size,
            }
            if duration and duration > 0:
                entry["durationSeconds"] = duration
            if resolution:
                entry["resolution"] = resolution
            if fps and fps > 0:
                entry["fps"] = fps
            if bitrate and bitrate > 0:
                entry["bitrate"] = bitrate
            entries.append(entry)
        return entries, listing_trusted

    def _cached_media_meta(self, path: Path, size: int):
        """Return duration/resolution/fps/bitrate, reusing cache when unchanged."""
        stamp = None
        try:
            st = path.stat()
            stamp = f"{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            stamp = f"?:{int(size or 0)}"
        cache_key = str(path)
        with self._media_meta_cache_lock:
            cached = self._media_meta_cache.get(cache_key)
            if cached and cached.get("stamp") == stamp:
                return {
                    "durationSeconds": cached.get("durationSeconds"),
                    "resolution": cached.get("resolution"),
                    "fps": cached.get("fps"),
                    "bitrate": cached.get("bitrate"),
                }
        duration = self._probe_duration_seconds(path)
        resolution = self._probe_resolution(path)
        fps = self._probe_fps(path)
        bitrate = self._probe_bitrate(path, size=size, duration_seconds=duration)
        with self._media_meta_cache_lock:
            self._media_meta_cache[cache_key] = {
                "stamp": stamp,
                "durationSeconds": duration,
                "resolution": resolution,
                "fps": fps,
                "bitrate": bitrate,
            }
            # Bound cache growth if the folder is huge / files churn.
            if len(self._media_meta_cache) > 5000:
                overflow = len(self._media_meta_cache) - 4000
                for key in list(self._media_meta_cache.keys())[:overflow]:
                    self._media_meta_cache.pop(key, None)
        return {
            "durationSeconds": duration,
            "resolution": resolution,
            "fps": fps,
            "bitrate": bitrate,
        }

    def _probe_duration_seconds(self, path: Path):
        """Best-effort duration for Media cards (mdls, MP4 atoms, then ffprobe)."""
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
        mp4_duration = self._probe_duration_mp4(path)
        if mp4_duration and mp4_duration > 0:
            return mp4_duration
        ffprobe = self._ffprobe_bin()
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
    def _probe_duration_mp4(path: Path):
        """Parse MP4/M4V/MOV mvhd duration when Spotlight/ffprobe are unavailable."""
        if path.suffix.lower() not in {".mp4", ".m4v", ".mov"}:
            return None
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                end = handle.tell()
                handle.seek(0)

                def read_atoms(limit: int):
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
                        if typ_s == "moov":
                            nested = read_atoms(box_end)
                            if nested:
                                found = nested
                        elif typ_s == "mvhd":
                            data = handle.read(min(32, box_end - handle.tell()))
                            if not data:
                                handle.seek(box_end)
                                continue
                            ver = data[0]
                            try:
                                if ver == 0 and len(data) >= 20:
                                    timescale = struct.unpack(">I", data[12:16])[0]
                                    raw_duration = struct.unpack(">I", data[16:20])[0]
                                elif ver == 1 and len(data) >= 32:
                                    timescale = struct.unpack(">I", data[20:24])[0]
                                    raw_duration = struct.unpack(">Q", data[24:32])[0]
                                else:
                                    timescale = 0
                                    raw_duration = 0
                            except struct.error:
                                timescale = 0
                                raw_duration = 0
                            if timescale > 0 and raw_duration > 0:
                                seconds = int(round(raw_duration / float(timescale)))
                                if seconds > 0:
                                    found = seconds
                        handle.seek(box_end)
                    return found

                return read_atoms(end)
        except OSError:
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
    def _mdls_raw_float(path: Path, name: str):
        """Read one mdls floating-point attribute (e.g. frame rate)."""
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
            value = float(raw)
        except ValueError:
            return None
        return value if value > 0 else None

    @staticmethod
    def _parse_frame_rate(raw):
        text = str(raw or "").strip()
        if not text or text in {"0/0", "N/A", "nan"}:
            return None
        try:
            if "/" in text:
                num_s, den_s = text.split("/", 1)
                num = float(num_s)
                den = float(den_s)
                if den == 0:
                    return None
                value = num / den
            else:
                value = float(text)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
        if value <= 0 or value > 240:
            return None
        return round(value, 3)

    @staticmethod
    def _estimate_bitrate_bps(size_bytes, duration_seconds):
        try:
            size = int(size_bytes)
            duration = float(duration_seconds)
        except (TypeError, ValueError):
            return None
        if size <= 0 or duration <= 0:
            return None
        return max(1, int(round(size * 8.0 / duration)))

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
        probed = self._ffprobe_stream_meta(path)
        return probed.get("resolution")

    def _probe_fps(self, path: Path):
        """Best-effort frames/sec for Media cards (mdls, MP4 atoms, then ffprobe)."""
        mdls_fps = self._mdls_raw_float(path, "kMDItemVideoFrameRate")
        if mdls_fps and 0 < mdls_fps <= 240:
            return round(float(mdls_fps), 3)
        mp4_fps = self._probe_fps_mp4(path)
        if mp4_fps and 0 < mp4_fps <= 240:
            return round(float(mp4_fps), 3)
        probed = self._ffprobe_stream_meta(path)
        return probed.get("fps")

    @staticmethod
    def _probe_fps_mp4(path: Path):
        """Parse constant-frame-rate FPS from video track mdhd timescale + stts delta."""
        if path.suffix.lower() not in {".mp4", ".m4v", ".mov"}:
            return None
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                end = handle.tell()
                handle.seek(0)

                def walk_boxes(limit: int, on_box):
                    while handle.tell() + 8 <= limit:
                        start = handle.tell()
                        header = handle.read(8)
                        if len(header) < 8:
                            return
                        size, typ = struct.unpack(">I4s", header)
                        hdr_len = 8
                        if size == 1:
                            wide = handle.read(8)
                            if len(wide) < 8:
                                return
                            size = struct.unpack(">Q", wide)[0]
                            hdr_len = 16
                        elif size == 0:
                            size = limit - start
                        if size < hdr_len:
                            return
                        box_end = min(start + size, limit)
                        if box_end <= start:
                            return
                        typ_s = typ.decode("latin1", errors="ignore")
                        on_box(typ_s, box_end)
                        handle.seek(box_end)

                def parse_trak(trak_end: int):
                    timescale = None
                    is_video = False
                    sample_delta = None

                    def on_box(typ_s, box_end):
                        nonlocal timescale, is_video, sample_delta
                        if typ_s in {"mdia", "minf", "stbl"}:
                            walk_boxes(box_end, on_box)
                            return
                        if typ_s == "mdhd":
                            data = handle.read(min(32, box_end - handle.tell()))
                            if not data:
                                return
                            ver = data[0]
                            try:
                                if ver == 0 and len(data) >= 20:
                                    timescale = struct.unpack(">I", data[12:16])[0]
                                elif ver == 1 and len(data) >= 28:
                                    timescale = struct.unpack(">I", data[20:24])[0]
                            except struct.error:
                                timescale = None
                            return
                        if typ_s == "hdlr":
                            data = handle.read(min(24, box_end - handle.tell()))
                            if len(data) >= 12:
                                is_video = data[8:12] == b"vide"
                            return
                        if typ_s == "stts":
                            data = handle.read(min(16, box_end - handle.tell()))
                            if len(data) >= 16:
                                try:
                                    entry_count = struct.unpack(">I", data[4:8])[0]
                                    if entry_count >= 1:
                                        sample_delta = struct.unpack(">I", data[12:16])[0]
                                except struct.error:
                                    sample_delta = None

                    walk_boxes(trak_end, on_box)
                    if not is_video or not timescale or not sample_delta or sample_delta <= 0:
                        return None
                    fps = timescale / float(sample_delta)
                    if 0 < fps <= 240:
                        return round(fps, 3)
                    return None

                best = None

                def on_top(typ_s, box_end):
                    nonlocal best
                    if typ_s == "moov":
                        walk_boxes(box_end, on_top)
                        return
                    if typ_s == "trak":
                        fps = parse_trak(box_end)
                        if fps and (best is None or fps > best):
                            best = fps

                walk_boxes(end, on_top)
                return best
        except OSError:
            return None
        return None

    def _probe_bitrate(self, path: Path, size=None, duration_seconds=None):
        """Best-effort bits/sec (stream/container, else size/duration estimate)."""
        mdls_rate = self._mdls_raw_number(path, "kMDItemTotalBitRate")
        if not mdls_rate:
            mdls_rate = self._mdls_raw_number(path, "kMDItemVideoBitRate")
        if mdls_rate and mdls_rate > 0:
            return int(mdls_rate)
        probed = self._ffprobe_stream_meta(path)
        bitrate = probed.get("bitrate")
        if bitrate and bitrate > 0:
            return int(bitrate)
        return self._estimate_bitrate_bps(size, duration_seconds)

    def _ffprobe_stream_meta(self, path: Path):
        """One ffprobe JSON call for resolution / fps / bitrate."""
        empty = {"resolution": None, "fps": None, "bitrate": None}
        stamp = None
        try:
            st = path.stat()
            stamp = f"{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            stamp = "?"
        cache_key = str(path)
        with self._ffprobe_meta_cache_lock:
            cached = self._ffprobe_meta_cache.get(cache_key)
            if cached and cached.get("stamp") == stamp:
                return dict(cached.get("meta") or empty)
        ffprobe = self._ffprobe_bin()
        if not ffprobe:
            return empty
        try:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height,avg_frame_rate,r_frame_rate,bit_rate:format=bit_rate,duration",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                return empty
            payload = json.loads(proc.stdout or "{}")
        except (OSError, ValueError, json.JSONDecodeError):
            return empty
        streams = payload.get("streams") if isinstance(payload, dict) else None
        stream = streams[0] if isinstance(streams, list) and streams else {}
        fmt = payload.get("format") if isinstance(payload, dict) else {}
        if not isinstance(stream, dict):
            stream = {}
        if not isinstance(fmt, dict):
            fmt = {}
        meta = dict(empty)
        try:
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
        except (TypeError, ValueError):
            width = height = 0
        if width > 0 and height > 0:
            meta["resolution"] = f"{width}x{height}"
        fps = self._parse_frame_rate(stream.get("avg_frame_rate"))
        if fps is None:
            fps = self._parse_frame_rate(stream.get("r_frame_rate"))
        meta["fps"] = fps
        bitrate = None
        for raw in (stream.get("bit_rate"), fmt.get("bit_rate")):
            try:
                candidate = int(raw)
            except (TypeError, ValueError):
                candidate = 0
            if candidate > 0:
                bitrate = candidate
                break
        if bitrate is None:
            try:
                duration = float(fmt.get("duration") or 0)
            except (TypeError, ValueError):
                duration = 0
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            bitrate = self._estimate_bitrate_bps(size, duration)
        meta["bitrate"] = bitrate
        with self._ffprobe_meta_cache_lock:
            self._ffprobe_meta_cache[cache_key] = {"stamp": stamp, "meta": dict(meta)}
            if len(self._ffprobe_meta_cache) > 5000:
                overflow = len(self._ffprobe_meta_cache) - 4000
                for key in list(self._ffprobe_meta_cache.keys())[:overflow]:
                    self._ffprobe_meta_cache.pop(key, None)
        return meta

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

    def upload_thumbnail(self, relative: str = "", recording_id: str = "") -> dict:
        """Push one cached cover to the VPS for the Media page."""
        media = self._resolve_local_media(relative=relative, recording_id=recording_id).resolve()
        root = self._refresh_video_dir().resolve()
        rel = media.relative_to(root).as_posix()
        thumb = self.ensure_thumbnail(relative=rel, recording_id=recording_id)
        payload = thumb.read_bytes()
        if not payload:
            raise RuntimeError("empty thumbnail")
        content_type = "image/png" if thumb.suffix.lower() == ".png" else "image/jpeg"
        self._vps_json(
            "POST",
            "/api/mac/helper/thumb",
            {
                "localSessionId": self.session_id,
                "relativePath": rel,
                "recordingId": str(recording_id or "").strip(),
                "contentType": content_type,
                "imageBase64": base64.b64encode(payload).decode("ascii"),
            },
            timeout=20,
        )
        return {"status": "ok", "relativePath": rel}

    def _enqueue_thumb_uploads(self, files) -> None:
        if not self._thumb_worker_started:
            return
        for entry in files or []:
            relative = str(entry.get("filename") or "").strip()
            recording_id = str(entry.get("recordingId") or "").strip()
            if not relative and not recording_id:
                continue
            key = (relative, recording_id, int(entry.get("size") or 0))
            if key in self._thumbs_enqueued:
                continue
            self._thumbs_enqueued.add(key)
            self._thumb_queue.put((relative, recording_id, key))

    def _thumb_upload_loop(self) -> None:
        while True:
            relative, recording_id, key = self._thumb_queue.get()
            try:
                self.upload_thumbnail(relative=relative, recording_id=recording_id)
            except Exception as exc:
                self._thumbs_enqueued.discard(key)
                print(f"[mac-helper] thumb upload failed: {exc!r}", flush=True)

    def _stat_size_via_osascript(self, path: Path):
        osascript = Path("/usr/bin/osascript")
        if not osascript.is_file():
            return None
        try:
            proc = subprocess.run(
                [
                    str(osascript),
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
        except OSError:
            return None
        if proc.returncode != 0:
            return None
        try:
            return int(float((proc.stdout or "").replace("\r", "\n").strip()))
        except ValueError:
            return None

    def scan(self, force: bool = False):
        now = time.time()
        if not force:
            with self._scan_cache_lock:
                cached = self._scan_cache
                cached_at = self._scan_cache_at
            if cached is not None and (now - cached_at) < SCAN_CACHE_MAX_AGE_SECONDS:
                snap = dict(cached)
                snap["files"] = [dict(row) for row in (cached.get("files") or [])]
                snap["localSessionId"] = self.session_id
                snap["scanSource"] = "cache"
                return snap

        root = self._refresh_video_dir()
        if self._video_dir_absent(root):
            # Unplugged disk / deleted folder: empty presence, keep ledger for remount.
            print(
                f"[mac-helper] video folder unavailable ({root}); not using ledger ghosts",
                flush=True,
            )
            result = {
                "localSessionId": self.session_id,
                "directory": str(root),
                "scannedAt": int(time.time()),
                "scanSource": "disk",
                "files": [],
            }
            with self._scan_cache_lock:
                self._scan_cache = result
                self._scan_cache_at = time.time()
            return result
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
        result = {
            "localSessionId": self.session_id,
            "directory": str(root),
            "scannedAt": int(time.time()),
            "scanSource": scan_source,
            "files": files,
        }
        with self._scan_cache_lock:
            self._scan_cache = result
            self._scan_cache_at = time.time()
        return result

    def _proxy_is_reachable(self) -> bool:
        proxy_url = (self.proxy_url or "").strip() or self._detect_local_airport_proxy()
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

    @staticmethod
    def _detect_local_airport_proxy() -> str:
        """Clash HTTP port used on this development Mac for Twitch/WAN APIs."""
        try:
            with socket.create_connection(("127.0.0.1", 7897), timeout=0.4):
                return "http://127.0.0.1:7897"
        except OSError:
            return ""

    def _wan_proxy_url(self) -> str:
        """Proxy for WAN hosts (Twitch GQL / VPS). Prefer --proxy, else 7897."""
        configured = (self.proxy_url or "").strip()
        if configured:
            parsed = urllib.parse.urlparse(configured)
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if host:
                try:
                    with socket.create_connection((host, port), timeout=0.4):
                        return configured
                except OSError:
                    pass
        return self._detect_local_airport_proxy()

    def _want_proxy(self, parsed=None) -> bool:
        if not (self.proxy_url or "").strip():
            return False
        return self._proxy_is_reachable()

    def _note_proxy_mode(self, use_proxy: bool) -> None:
        if use_proxy == self._using_proxy:
            return
        self._using_proxy = use_proxy
        if not (self.proxy_url or "").strip():
            return
        print(
            "[mac-helper] VPS traffic via proxy"
            if use_proxy
            else "[mac-helper] reaching origin directly (no proxy)",
            flush=True,
        )

    def _opener(self):
        use_proxy = self._want_proxy()
        self._note_proxy_mode(use_proxy)
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
        want_proxy = self._want_proxy(parsed)
        self._note_proxy_mode(want_proxy)
        try:
            return self._vps_json_once(method, url, parsed, body, headers, timeout, want_proxy)
        except (OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
            status = getattr(exc, "code", None)
            if want_proxy and (status in {502, 503, 504} or status is None):
                print("[mac-helper] proxy request failed; reaching VPS directly", flush=True)
                self._using_proxy = False
                return self._vps_json_once(method, url, parsed, body, headers, timeout, False)
            raise

    def _vps_json_once(self, method, url, parsed, body, headers, timeout, use_proxy):
        # urllib negotiates HTTP CONNECT for https:// through an HTTP proxy.
        # Raw HTTPSConnection.set_tunnel is flaky with some local proxies (Clash).
        req_headers = {k: v for k, v in (headers or {}).items() if k.lower() != "host"}
        proxies = {"http": self.proxy_url, "https": self.proxy_url} if use_proxy else {}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        with opener.open(req, timeout=timeout) as resp:
            data = resp.read()
            if not data:
                return {}
            return json.loads(data.decode("utf-8"))

    def _heartbeat_files(self, snapshot: dict) -> list:
        files = []
        for entry in snapshot.get("files") or []:
            filename = str(entry.get("filename") or "").strip()
            if not filename:
                continue
            row = {
                "recordingId": str(entry.get("recordingId") or "") or None,
                "filename": filename,
                "size": int(entry.get("size") or 0),
            }
            duration = entry.get("durationSeconds")
            try:
                duration_i = int(duration) if duration is not None else 0
            except (TypeError, ValueError):
                duration_i = 0
            if duration_i > 0:
                row["durationSeconds"] = duration_i
            resolution = str(entry.get("resolution") or "").strip()
            if resolution:
                row["resolution"] = resolution
            try:
                fps = float(entry.get("fps")) if entry.get("fps") is not None else None
                if fps is not None and fps <= 0:
                    fps = None
            except (TypeError, ValueError):
                fps = None
            if fps is not None:
                row["fps"] = fps
            try:
                bitrate = int(entry.get("bitrate")) if entry.get("bitrate") is not None else None
                if bitrate is not None and bitrate <= 0:
                    bitrate = None
            except (TypeError, ValueError):
                bitrate = None
            if bitrate is not None:
                row["bitrate"] = bitrate
            files.append(row)
        return files

    def _push_heartbeat_and_run_jobs(self):
        snapshot = self.scan()
        payload = {
            "localSessionId": snapshot["localSessionId"],
            "files": self._heartbeat_files(snapshot),
            "origin": self.allowed_origin,
        }
        progress = self._motrix_download_progress_rows()
        if progress:
            payload["downloadProgress"] = progress
        with self._pending_relocate_lock:
            pending_n = len(self._load_pending_relocates())
        payload["downloadsPaused"] = pending_n == 0 and not self._motrix_gids and not self._motrix_paths
        result = self._vps_json(
            "POST",
            "/api/mac/helper/heartbeat",
            payload,
        )
        for job in result.get("pendingJobs") or []:
            job_id = str(job.get("jobId") or "")
            items = job.get("items") or []
            if not job_id or not items:
                continue
            # Auto Sync removed: ignore job.autoSync and always run as manual.
            print(
                f"[mac-helper] heartbeat claimed job {job_id} with {len(items)} item(s)",
                flush=True,
            )
            method = str(job.get("method") or "chrome")
            threading.Thread(
                target=self.open_downloads,
                args=(job_id, items, method),
                daemon=True,
            ).start()
        self._enqueue_thumb_uploads(snapshot.get("files") or [])
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
                elif ctype == "rename-folder":
                    result = self.rename_streamer_folder(
                        from_folder=str(cmd.get("fromFolder") or ""),
                        to_folder=str(cmd.get("toFolder") or ""),
                    )
                    print(
                        f"[mac-helper] renamed folder {result.get('fromFolder')} "
                        f"-> {result.get('toFolder')} "
                        f"({result.get('movedFiles') or 0} file(s))",
                        flush=True,
                    )
                    threading.Thread(target=self._push_folder_snapshot, daemon=True).start()
                elif ctype == "thumb":
                    result = self.upload_thumbnail(
                        relative=str(cmd.get("relativePath") or ""),
                        recording_id=str(cmd.get("recordingId") or ""),
                    )
                    print(
                        f"[mac-helper] uploaded thumb {result.get('relativePath')}",
                        flush=True,
                    )
                elif ctype == "import-session":
                    source_type = str(cmd.get("sourceType") or "")
                    self.import_provider_session_to_vps(source_type, command_id)
                elif ctype == "provider-follow":
                    source_type = str(cmd.get("sourceType") or "")
                    username = str(cmd.get("username") or "")
                    follow = bool(cmd.get("follow", True))
                    self.report_provider_follow_to_vps(
                        source_type,
                        username,
                        follow=follow,
                        command_id=command_id,
                    )
                elif ctype == "concat-project":
                    threading.Thread(
                        target=self._run_concat_project,
                        args=(cmd,),
                        daemon=True,
                    ).start()
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
                    self._clear_repeated("vps-sync")
                except Exception as exc:
                    self._log_repeated("vps-sync", f"VPS sync failed: {exc!r}")
                delay = VPS_SYNC_INTERVAL_SECONDS - (time.time() - started)
                time.sleep(delay if delay > 0.5 else 0.5)

        def command_loop():
            while True:
                try:
                    self._claim_and_run_commands()
                    self._clear_repeated("command-poll")
                except Exception as exc:
                    self._log_repeated(
                        "command-poll",
                        f"command poll failed: {exc!r}",
                    )
                time.sleep(COMMAND_POLL_INTERVAL_SECONDS)

        threading.Thread(target=loop, name="hxylive-vps-sync", daemon=True).start()
        threading.Thread(target=command_loop, name="hxylive-command-poll", daemon=True).start()
        self._thumb_worker_started = True
        threading.Thread(target=self._thumb_upload_loop, name="hxylive-thumb-upload", daemon=True).start()
        threading.Thread(
            target=self._pending_relocate_loop,
            name="hxylive-pending-relocate",
            daemon=True,
        ).start()

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
        if self._volume_unmounted(directory):
            raise RuntimeError(f"video volume is not mounted: {directory}")
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
        """Same-volume rename is instant; cross-volume copy2+unlink; Finder is TCC fallback."""
        self._ensure_dir(dest.parent)
        if dest.exists():
            dest.unlink()
        try:
            source.replace(dest)
            return
        except OSError as exc:
            # Cross-device (e.g. ~/Downloads → /Volumes/External/...) raises EXDEV.
            if getattr(exc, "errno", None) == errno.EXDEV:
                print(f"[mac-helper] cross-volume relocate {source} -> {dest}", flush=True)
            else:
                print(f"[mac-helper] rename failed ({exc!r}); trying copy then Finder", flush=True)
        try:
            shutil.copy2(source, dest)
            src_size = self._path_size(source)
            dst_size = self._path_size(dest)
            if src_size is not None and dst_size is not None and src_size != dst_size:
                try:
                    dest.unlink()
                except OSError:
                    pass
                raise RuntimeError(
                    f"cross-volume copy size mismatch {src_size} != {dst_size}"
                )
            source.unlink()
            return
        except OSError as copy_exc:
            print(f"[mac-helper] copy2 failed ({copy_exc!r}); trying Finder move", flush=True)
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

    def export_provider_cookies(
        self,
        source_type,
        chrome_root=None,
        open_login_if_missing=False,
        all_cookies=False,
    ):
        result = export_provider_cookies_from_chrome(
            source_type,
            chrome_root=chrome_root,
            aes_key=self._chrome_aes_key,
            keychain_loader=chrome_keychain_passphrase,
            user_agent=chrome_user_agent(),
            all_cookies=all_cookies,
        )
        derived = result.pop("_aes_key", None)
        if derived:
            with self._chrome_aes_key_lock:
                self._chrome_aes_key = derived
        if result.get("ok"):
            return result
        login_url = result.get("loginUrl") or ""
        opened = False
        if open_login_if_missing and login_url:
            try:
                open_chrome_url(login_url)
                opened = True
            except (OSError, ValueError):
                opened = False
        result["openedChrome"] = opened
        return result

    def import_provider_session_to_vps(self, source_type, command_id):
        source_type = str(source_type or "").strip().lower()
        command_id = str(command_id or "").strip()
        exported = self.export_provider_cookies(source_type, open_login_if_missing=True)
        payload = {
            "localSessionId": self.session_id,
            "commandId": command_id,
            "sourceType": source_type,
        }
        if exported.get("ok"):
            payload["cookieHeader"] = exported.get("cookieHeader") or ""
            payload["cookies"] = exported.get("cookies") or []
            payload["userAgent"] = exported.get("userAgent") or ""
            payload["username"] = exported.get("username") or ""
        else:
            payload["success"] = False
            payload["error"] = exported.get("error") or "Chrome import failed"
            payload["loginUrl"] = exported.get("loginUrl") or ""
        result = self._vps_json(
            "POST",
            "/api/mac/helper/provider-session",
            payload,
            timeout=90,
        )
        print(
            f"[mac-helper] Chrome import {source_type} "
            f"success={bool((result or {}).get('success'))}",
            flush=True,
        )
        return result

    def provider_remote_follow(self, source_type, username, follow=True):
        """Perform a remote follow/unfollow using Chrome cookies.

        Twitch GQL mutations require a Client-Integrity token that fails from
        datacenter IPs; running here keeps the request on the user's network.
        """
        source_type = str(source_type or "").strip().lower()
        username = str(username or "").strip().lstrip("@")
        if source_type != "twitch":
            return {"ok": False, "error": f"Remote follow via Mac Helper unsupported for {source_type or 'unknown'}"}
        if not username:
            return {"ok": False, "error": "Username required"}
        exported = self.export_provider_cookies(source_type, open_login_if_missing=False)
        if not exported.get("ok"):
            return {
                "ok": False,
                "error": exported.get("error") or "Twitch Chrome session missing",
                "loginUrl": exported.get("loginUrl") or "",
            }
        cookies = exported.get("cookies") or []
        # CDP needs the full Twitch cookie jar to look logged-in in a fresh profile.
        browser_cookies = cookies
        full_export = self.export_provider_cookies(
            source_type,
            open_login_if_missing=False,
            all_cookies=True,
        )
        if full_export.get("ok") and full_export.get("cookies"):
            browser_cookies = full_export.get("cookies") or cookies
        token = ""
        device = ""
        cookie_parts = []
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            if not name:
                continue
            cookie_parts.append(f"{name}={value}")
            if name == "auth-token":
                token = value
            if name.lower() in {"unique_id", "unique_id_durable"} and value and not device:
                device = value
        if not token:
            return {"ok": False, "error": "Twitch auth-token cookie missing"}
        if not device:
            device = uuid.uuid4().hex
        user_agent = (
            str(exported.get("userAgent") or "").strip()
            or chrome_user_agent()
            or (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            )
        )
        headers = {
            "Authorization": f"OAuth {token}",
            "Client-Id": "kimne78kx3ncx6brgo4mv6wki5h1ko",
            "Content-Type": "application/json",
            "Cookie": "; ".join(cookie_parts),
            "X-Device-Id": device,
            "User-Agent": user_agent,
        }

        def _post(url, body, extra=None, timeout=30):
            req_headers = dict(headers)
            if extra:
                req_headers.update(extra)
            data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
            # Chrome reaches Twitch via the airport proxy; integrity is IP-bound,
            # so Mac Helper must use the same path. Fall back to direct if needed.
            proxy = self._wan_proxy_url()
            attempts = []
            if proxy:
                attempts.append({"http": proxy, "https": proxy})
            attempts.append({})
            last_error = None
            for proxies in attempts:
                try:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
                    with opener.open(req, timeout=timeout) as resp:
                        raw = resp.read().decode("utf-8")
                        return resp.status, json.loads(raw) if raw else {}
                except urllib.error.HTTPError:
                    # Twitch returned an HTTP status — do not retry the other path.
                    raise
                except (OSError, urllib.error.URLError, ValueError) as exc:
                    last_error = exc
                    continue
            if last_error is not None:
                raise last_error
            raise RuntimeError("Twitch request failed")

        try:
            _, integ = _post("https://gql.twitch.tv/integrity", {})
            integrity = str((integ or {}).get("token") or "").strip()
            if not integrity:
                return {"ok": False, "error": "Twitch integrity token missing"}
            _, user_payload = _post(
                "https://gql.twitch.tv/gql",
                {
                    "query": "query HXYLIVEUserId($login: String!) { user(login: $login) { id login } }",
                    "variables": {"login": username},
                },
            )
            if isinstance(user_payload.get("errors"), list) and user_payload["errors"]:
                message = str((user_payload["errors"][0] or {}).get("message") or "user lookup failed")
                return {"ok": False, "error": message}
            target = str((((user_payload.get("data") or {}).get("user") or {}).get("id") or "")).strip()
            if not target:
                return {"ok": False, "error": f"Twitch user not found: {username}"}
            # Fresh integrity immediately before the mutation.
            _, integ2 = _post("https://gql.twitch.tv/integrity", {})
            integrity = str((integ2 or {}).get("token") or integrity).strip()
            if follow:
                mutation = (
                    "mutation HXYLIVEFollow($id: ID!) {"
                    " followUser(input: {disableNotifications: false, targetID: $id}) {"
                    " follow { user { id login } } } }"
                )
            else:
                mutation = (
                    "mutation HXYLIVEUnfollow($id: ID!) {"
                    " unfollowUser(input: {targetID: $id}) {"
                    " follow { user { id } } } }"
                )
            status, follow_payload = _post(
                "https://gql.twitch.tv/gql",
                {"query": mutation, "variables": {"id": target}},
                {"Client-Integrity": integrity},
            )
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8")[:240]
            except Exception:
                detail = str(exc)
            return {"ok": False, "error": f"Twitch HTTP {exc.code}: {detail}"}
        except (OSError, urllib.error.URLError, ValueError, TypeError) as exc:
            reason = getattr(exc, "reason", None) or exc
            return {
                "ok": False,
                "error": f"Twitch network error: {reason}",
            }

        if isinstance(follow_payload.get("errors"), list) and follow_payload["errors"]:
            message = str((follow_payload["errors"][0] or {}).get("message") or "follow failed")
            if "integrity" in message.lower():
                print(
                    f"[mac-helper] Twitch GQL integrity failed for {username}; "
                    "falling back to Chrome CDP click",
                    flush=True,
                )
                cdp_result = self._twitch_follow_via_chrome_cdp(
                    username,
                    follow=follow,
                    cookies=browser_cookies,
                )
                if cdp_result.get("ok"):
                    return cdp_result
                return {
                    "ok": False,
                    "error": cdp_result.get("error") or message,
                    "gqlError": message,
                }
            return {"ok": False, "error": message, "status": status}
        key = "followUser" if follow else "unfollowUser"
        data = (follow_payload.get("data") or {}).get(key)
        if data is None and follow_payload.get("data") is not None:
            # Unfollow can return null follow payload when already unfollowed.
            pass
        return {
            "ok": True,
            "success": True,
            "remote": True,
            "provider": "twitch",
            "username": username,
            "userId": target,
            "action": "follow" if follow else "unfollow",
            "via": "mac-helper",
        }

    def _twitch_follow_via_chrome_cdp(self, username, *, follow=True, cookies=None):
        """Click Twitch Follow/Unfollow in a short-lived Chrome via CDP.

        Runs without a visible window: headless first, then an off-screen
        window if Twitch blocks headless automation.
        """
        last = None
        for mode in ("headless", "offscreen"):
            last = self._twitch_follow_via_chrome_cdp_once(
                username,
                follow=follow,
                cookies=cookies,
                mode=mode,
            )
            if last.get("ok"):
                last["chromeMode"] = mode
                return last
            err = str(last.get("error") or "")
            # Same cookies either way — no point retrying a logged-out session.
            if "logged out" in err.lower() or "Chrome not found" in err:
                return last
            print(
                f"[mac-helper] Twitch Chrome CDP mode={mode} failed: {err!r}; "
                "trying next background mode" if mode == "headless" else "giving up",
                flush=True,
            )
        return last or {"ok": False, "error": "Chrome Twitch follow failed"}

    def _twitch_follow_via_chrome_cdp_once(
        self,
        username,
        *,
        follow=True,
        cookies=None,
        mode="headless",
    ):
        """One CDP attempt. mode: headless | offscreen."""
        username = str(username or "").strip().lstrip("@")
        if not username:
            return {"ok": False, "error": "Username required"}
        chrome_bin = _chrome_executable_path()
        if not chrome_bin:
            return {"ok": False, "error": "Google Chrome not found for Twitch follow fallback"}

        # Pick a free local debugging port.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        profile_dir = Path(self.support_dir) / "chrome-twitch-cdp"
        try:
            profile_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "error": f"Could not create Chrome profile dir: {exc}"}

        proxy = self._wan_proxy_url()
        args = [
            chrome_bin,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            "--disable-sync",
            "--disable-background-networking",
            "--window-size=1280,900",
        ]
        mode = str(mode or "headless").strip().lower()
        if mode == "headless":
            # New headless keeps a real browser engine; no on-screen window.
            args.extend([
                "--headless=new",
                "--hide-scrollbars",
                "--mute-audio",
            ])
        else:
            # Keep a real window but park it far off-screen so the user never sees it.
            args.extend([
                "--window-position=-32000,-32000",
            ])
        args.append("about:blank")
        if proxy:
            args.insert(-1, f"--proxy-server={proxy}")

        proc = None
        client = None
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            page_ws = ""
            deadline = time.time() + 25
            while time.time() < deadline:
                try:
                    # Prefer an existing page target; /json/new needs PUT on modern Chrome.
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/list",
                        timeout=1.5,
                    ) as resp:
                        targets = json.loads(resp.read().decode("utf-8"))
                    if isinstance(targets, list):
                        for target in targets:
                            if not isinstance(target, dict):
                                continue
                            if str(target.get("type") or "") != "page":
                                continue
                            candidate = str(target.get("webSocketDebuggerUrl") or "").strip()
                            if candidate:
                                page_ws = candidate
                                break
                    if page_ws:
                        break
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/json/new?about:blank",
                        method="PUT",
                    )
                    with urllib.request.urlopen(req, timeout=2) as resp:
                        created = json.loads(resp.read().decode("utf-8"))
                    page_ws = str((created or {}).get("webSocketDebuggerUrl") or "").strip()
                    if page_ws:
                        break
                except Exception:
                    time.sleep(0.25)
            if not page_ws:
                return {"ok": False, "error": "Chrome page DevTools endpoint did not become ready"}

            client = _ChromeDevtoolsClient(page_ws, timeout=90)
            client.call("Network.enable")
            client.call("Page.enable")
            client.call("Runtime.enable")

            for cookie in cookies or []:
                if not isinstance(cookie, dict):
                    continue
                name = str(cookie.get("name") or "").strip()
                value = str(cookie.get("value") or "")
                if not name:
                    continue
                domain = str(cookie.get("domain") or ".twitch.tv").strip() or ".twitch.tv"
                if domain.startswith("."):
                    pass
                elif "twitch.tv" in domain:
                    domain = ".twitch.tv"
                else:
                    domain = ".twitch.tv"
                path = str(cookie.get("path") or "/").strip() or "/"
                try:
                    client.call("Network.setCookie", {
                        "name": name,
                        "value": value,
                        "domain": domain,
                        "path": path,
                        "secure": True,
                        "httpOnly": name.lower() in {"auth-token"},
                        "sameSite": "no_restriction",
                    })
                except Exception:
                    continue

            # Warm the cookie jar on the site origin before opening the channel.
            client.call("Page.navigate", {"url": "https://www.twitch.tv"})
            time.sleep(3)
            channel_url = f"https://www.twitch.tv/{username}"
            client.call("Page.navigate", {"url": channel_url})
            time.sleep(6)

            js = r"""
(async () => {
  const wantFollow = %s;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const allButtons = () => Array.from(document.querySelectorAll('button, [role="button"]')).filter(visible);
  const byTarget = (name) => document.querySelector(`[data-a-target="${name}"]`);
  const findByText = (re) => allButtons().find((b) => re.test(norm(b.innerText || b.textContent)));
  const loginWall = !!(
    byTarget('login-button') ||
    findByText(/^Log ?In$/i) ||
    /Log In to Follow/i.test(document.body ? document.body.innerText : '')
  );
  if (loginWall) return 'not-logged-in';

  for (let i = 0; i < 10; i++) {
    const followTarget = byTarget('follow-button');
    const unfollowTarget = byTarget('unfollow-button');
    const followingText = findByText(/^Following$/i);
    const followText = findByText(/^Follow$/i);
    const unfollowText = findByText(/^Unfollow$/i);

    if (wantFollow) {
      if (followingText || unfollowTarget) return 'already-following';
      const btn = followTarget || followText;
      if (btn) {
        btn.click();
        await sleep(1600);
        if (byTarget('unfollow-button') || findByText(/^Following$/i)) return 'followed';
        return 'follow-clicked';
      }
    } else {
      if (followTarget || (followText && !followingText)) return 'already-unfollowed';
      const btn = followingText || unfollowTarget || unfollowText;
      if (btn) {
        btn.click();
        await sleep(900);
        const confirm = byTarget('unfollow-button') || findByText(/^Unfollow$/i);
        if (confirm) {
          confirm.click();
          await sleep(1200);
        }
        if (byTarget('follow-button') || findByText(/^Follow$/i)) return 'unfollowed';
        return 'unfollow-clicked';
      }
    }
    await sleep(1000);
  }

  const sample = allButtons().slice(0, 12).map((b) => norm(b.innerText || b.textContent)).filter(Boolean);
  return 'button-missing:' + sample.join('|');
})()
""" % ("true" if follow else "false")

            evaluated = client.call(
                "Runtime.evaluate",
                {
                    "expression": js,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
                timeout=60,
            )
            result = ((evaluated or {}).get("result") or {}).get("value")
            if isinstance(result, dict) and result.get("value") is not None:
                result = result.get("value")

            time.sleep(1.0)
            verify_js = r"""
(() => {
  const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const following = !!(
    document.querySelector('[data-a-target="unfollow-button"]') ||
    Array.from(document.querySelectorAll('button')).some((b) => /^Following$/i.test(norm(b.innerText || b.textContent)))
  );
  const follow = !!(
    document.querySelector('[data-a-target="follow-button"]') ||
    Array.from(document.querySelectorAll('button')).some((b) => /^Follow$/i.test(norm(b.innerText || b.textContent)))
  );
  return {
    following,
    follow,
    title: document.title || '',
    href: location.href || ''
  };
})()
"""
            verified = client.call(
                "Runtime.evaluate",
                {"expression": verify_js, "returnByValue": True},
                timeout=20,
            )
            state = ((verified or {}).get("result") or {}).get("value") or {}
            is_following = bool(state.get("following"))
            result_text = str(result or "")
            if result_text == "not-logged-in":
                return {
                    "ok": False,
                    "error": "Twitch Chrome session is logged out; Import from Chrome under Settings, then retry",
                    "via": "chrome-cdp",
                }
            ok = False
            if follow:
                ok = is_following or result_text in {"followed", "already-following", "follow-clicked"}
            else:
                ok = (not is_following) or result_text in {
                    "unfollowed",
                    "already-unfollowed",
                    "unfollow-clicked",
                }

            if not ok:
                return {
                    "ok": False,
                    "error": (
                        f"Chrome Twitch UI action incomplete ({result_text}); "
                        f"following={is_following}; title={state.get('title')!r}"
                    ),
                    "via": "chrome-cdp",
                }
            return {
                "ok": True,
                "success": True,
                "remote": True,
                "provider": "twitch",
                "username": username,
                "action": "follow" if follow else "unfollow",
                "via": "chrome-cdp",
                "uiResult": result_text,
            }
        except Exception as exc:
            return {"ok": False, "error": f"Chrome Twitch follow failed: {exc}"}
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

    def report_provider_follow_to_vps(self, source_type, username, *, follow=True, command_id=""):
        result = self.provider_remote_follow(source_type, username, follow=follow)
        payload = {
            "localSessionId": self.session_id,
            "commandId": str(command_id or "").strip(),
            "sourceType": str(source_type or "").strip().lower(),
            "username": str(username or "").strip(),
            "follow": bool(follow),
            "success": bool(result.get("ok")),
            "error": "" if result.get("ok") else (result.get("error") or "Remote follow failed"),
            "detail": result.get("error") or "",
        }
        if result.get("ok"):
            payload["action"] = result.get("action") or ("follow" if follow else "unfollow")
            payload["userId"] = result.get("userId") or ""
            payload["via"] = result.get("via") or "mac-helper"
        try:
            self._vps_json(
                "POST",
                "/api/mac/helper/provider-follow-result",
                payload,
                timeout=60,
            )
        except Exception as exc:
            print(f"[mac-helper] provider-follow report failed: {exc!r}", flush=True)
            raise
        print(
            f"[mac-helper] provider-follow {payload['sourceType']} "
            f"{payload['username']} success={payload['success']} "
            f"error={payload.get('error')!r} via={result.get('via')!r}",
            flush=True,
        )
        return result

    def _motrix_config(self) -> dict:
        path = MOTRIX_SUPPORT_DIR / "system.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _motrix_rpc_secret(self) -> str:
        return str(self._motrix_config().get("rpc-secret") or "").strip()

    def _motrix_rpc(self, method: str, *params):
        """Call Motrix/aria2 JSON-RPC. Returns the result field or raises."""
        args = list(params)
        secret = self._motrix_rpc_secret()
        if secret:
            args = [f"token:{secret}"] + args
        payload = {
            "jsonrpc": "2.0",
            "id": secrets.token_hex(8),
            "method": method,
            "params": args,
        }
        req = urllib.request.Request(
            MOTRIX_RPC_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=MOTRIX_RPC_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not isinstance(body, dict):
            raise RuntimeError(f"Motrix RPC bad response for {method}")
        if body.get("error"):
            raise RuntimeError(body["error"])
        return body.get("result")

    def detect_ffmpeg(self) -> str:
        """Return ffmpeg binary path or empty string if missing."""
        candidates = [
            "/opt/homebrew/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            str(Path.home() / "bin" / "ffmpeg"),
        ]
        for path in candidates:
            if Path(path).is_file() and os.access(path, os.X_OK):
                return path
        found = shutil.which("ffmpeg")
        return str(found or "").strip()

    def _motrix_download_progress_rows(self) -> list:
        """Build downloadProgress rows for heartbeat from tracked Motrix GIDs."""
        rows = []
        with self._motrix_track_lock:
            meta_by_gid = dict(self._motrix_meta)
            gids = list(self._motrix_gids)
        for gid in gids:
            meta = meta_by_gid.get(gid) or {}
            try:
                status = self._motrix_rpc(
                    "aria2.tellStatus",
                    gid,
                    [
                        "gid",
                        "status",
                        "totalLength",
                        "completedLength",
                        "downloadSpeed",
                        "errorMessage",
                    ],
                )
            except Exception:
                continue
            if not isinstance(status, dict):
                continue
            try:
                completed = int(status.get("completedLength") or 0)
            except (TypeError, ValueError):
                completed = 0
            try:
                total = int(status.get("totalLength") or 0)
            except (TypeError, ValueError):
                total = 0
            try:
                speed = float(status.get("downloadSpeed") or 0)
            except (TypeError, ValueError):
                speed = 0.0
            percent = (100.0 * completed / total) if total > 0 else 0.0
            rows.append({
                "gid": gid,
                "itemId": str(meta.get("itemId") or ""),
                "recordingId": str(meta.get("recordingId") or ""),
                "completedLength": completed,
                "totalLength": total,
                "progressBytes": completed,
                "progressPercent": percent,
                "downloadSpeed": speed,
                "speedBytesPerSec": speed,
                "status": str(status.get("status") or ""),
                "error": str(status.get("errorMessage") or ""),
            })
        return rows

    def _report_concat_status(self, cmd: dict, status: str, error: str = "", step=None):
        payload = {
            "localSessionId": self.session_id,
            "projectId": str(cmd.get("projectId") or ""),
            "taskId": str(cmd.get("taskId") or ""),
            "status": status,
            "error": error or "",
        }
        if step is not None:
            payload["step"] = int(step)
        final_path = str(cmd.get("finalRelativePath") or "")
        if final_path and status in {"done", "success", "concatenated"}:
            payload["finalRelativePath"] = final_path
        try:
            self._vps_json("POST", "/api/mac/helper/concat-status", payload, timeout=30)
        except Exception as exc:
            print(f"[mac-helper] concat-status report failed: {exc!r}", flush=True)

    def _run_concat_project(self, cmd: dict) -> None:
        """ffmpeg -f concat -safe 0 -c copy → streamer root; delete .projects/id/."""
        project_id = str(cmd.get("projectId") or "").strip()
        member_paths = [str(p).strip() for p in (cmd.get("memberPaths") or []) if str(p).strip()]
        final_rel = str(cmd.get("finalRelativePath") or "").strip()
        project_dir_rel = str(cmd.get("projectDir") or "").strip()
        ffmpeg_bin = self.detect_ffmpeg()
        if not ffmpeg_bin:
            print("[mac-helper] concat-project aborted: ffmpeg not found", flush=True)
            self._report_concat_status(cmd, "failed", error="ffmpeg not found")
            return
        if not project_id or not member_paths or not final_rel:
            self._report_concat_status(cmd, "failed", error="invalid concat-project payload")
            return
        self._report_concat_status(cmd, "running", step=0)
        try:
            abs_members = []
            for rel in member_paths:
                path = (self.video_dir / rel).resolve()
                if not path.is_file():
                    raise FileNotFoundError(f"missing member {rel}")
                abs_members.append(path)
            if project_dir_rel:
                work_dir = (self.video_dir / project_dir_rel).resolve()
            else:
                work_dir = abs_members[0].parent
            work_dir.mkdir(parents=True, exist_ok=True)
            list_path = work_dir / f".concat-{project_id}.txt"
            temp_out = work_dir / f".concat-{project_id}.tmp.mp4"
            final_path = (self.video_dir / final_rel).resolve()
            final_path.parent.mkdir(parents=True, exist_ok=True)
            with list_path.open("w", encoding="utf-8") as handle:
                for path in abs_members:
                    # ffmpeg concat demuxer: escape single quotes
                    escaped = str(path).replace("'", "'\\''")
                    handle.write(f"file '{escaped}'\n")
            cmd_line = [
                ffmpeg_bin,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c",
                "copy",
                str(temp_out),
            ]
            print(
                f"[mac-helper] concat-project {project_id}: {len(abs_members)} file(s) -> {final_rel}",
                flush=True,
            )
            proc = subprocess.run(
                cmd_line,
                capture_output=True,
                text=True,
                timeout=6 * 60 * 60,
            )
            if proc.returncode != 0 or not temp_out.is_file() or temp_out.stat().st_size <= 0:
                detail = (proc.stderr or proc.stdout or "ffmpeg failed").strip()[-500:]
                raise RuntimeError(detail or "ffmpeg concat failed")
            # Verify roughly: output should be at least as large as the largest member / 2.
            max_member = max(p.stat().st_size for p in abs_members)
            if temp_out.stat().st_size < max(1024, max_member // 4):
                raise RuntimeError("concat output suspiciously small")
            if final_path.exists():
                final_path.unlink()
            temp_out.replace(final_path)
            try:
                list_path.unlink(missing_ok=True)
            except TypeError:
                if list_path.exists():
                    list_path.unlink()
            # Delete entire .projects/<id>/ after successful promote.
            if project_dir_rel:
                project_root = (self.video_dir / project_dir_rel).resolve()
                if project_root.exists() and project_root.is_dir():
                    shutil.rmtree(project_root, ignore_errors=True)
            self._report_concat_status(cmd, "done", step=len(abs_members))
            print(f"[mac-helper] concat-project {project_id} done -> {final_rel}", flush=True)
            threading.Thread(target=self._push_folder_snapshot, daemon=True).start()
        except Exception as exc:
            print(f"[mac-helper] concat-project {project_id} failed: {exc!r}", flush=True)
            self._report_concat_status(cmd, "failed", error=str(exc))

    def _track_motrix(
        self,
        gid: str = "",
        dest_path: str = "",
        *,
        recording_id: str = "",
        item_id: str = "",
        relative_path: str = "",
    ) -> None:
        gid = str(gid or "").strip()
        dest_path = str(dest_path or "").strip()
        recording_id = str(recording_id or "").strip()
        item_id = str(item_id or "").strip()
        relative_path = str(relative_path or "").strip()
        with self._motrix_track_lock:
            if gid:
                self._motrix_gids.add(gid)
                meta = dict(self._motrix_meta.get(gid) or {})
                if dest_path:
                    meta["path"] = dest_path
                if recording_id:
                    meta["recordingId"] = recording_id
                if item_id:
                    meta["itemId"] = item_id
                if relative_path:
                    meta["relativePath"] = relative_path
                if meta:
                    self._motrix_meta[gid] = meta
            if dest_path:
                self._motrix_paths.add(dest_path)

    def _untrack_motrix(self, gid: str = "", dest_path: str = "") -> None:
        gid = str(gid or "").strip()
        dest_path = str(dest_path or "").strip()
        with self._motrix_track_lock:
            if gid:
                self._motrix_gids.discard(gid)
                meta = self._motrix_meta.pop(gid, None) or {}
                if not dest_path:
                    dest_path = str(meta.get("path") or "").strip()
            if dest_path:
                self._motrix_paths.discard(dest_path)

    def _motrix_status_path(self, status: dict) -> str:
        files = status.get("files") if isinstance(status, dict) else None
        if not isinstance(files, list) or not files:
            return ""
        first = files[0] if isinstance(files[0], dict) else {}
        return str(first.get("path") or "").strip()

    def _motrix_remove_gid(self, gid: str) -> None:
        gid = str(gid or "").strip()
        if not gid:
            return
        for method in ("aria2.forceRemove", "aria2.remove", "aria2.removeDownloadResult"):
            try:
                self._motrix_rpc(method, gid)
            except Exception:
                continue

    def _motrix_status_completed_length(self, status: dict) -> int:
        try:
            return int(status.get("completedLength") or 0)
        except (TypeError, ValueError):
            return 0

    def _motrix_collect_statuses_with_progress(self) -> list:
        """Active + waiting tasks with length fields for dedupe."""
        statuses = []
        keys = [
            "gid",
            "status",
            "files",
            "errorCode",
            "errorMessage",
            "completedLength",
            "totalLength",
        ]
        for method in ("aria2.tellActive", "aria2.tellWaiting"):
            try:
                if method == "aria2.tellWaiting":
                    result = self._motrix_rpc(method, 0, 1000, keys)
                else:
                    result = self._motrix_rpc(method, keys)
                self._clear_repeated(f"aria2-{method}")
            except Exception as exc:
                self._log_repeated(f"aria2-{method}", f"{method} failed: {exc!r}")
                continue
            if isinstance(result, list):
                statuses.extend(item for item in result if isinstance(item, dict))
        return statuses

    def _motrix_tasks_for_path(self, dest_path: str) -> list:
        want = str(dest_path or "").strip()
        if not want:
            return []
        try:
            want_resolved = str(Path(want).expanduser().resolve())
        except OSError:
            want_resolved = want
        matched = []
        for status in self._motrix_collect_statuses_with_progress():
            path = self._motrix_status_path(status)
            if not path:
                continue
            if path == want or path == want_resolved:
                matched.append(status)
                continue
            try:
                if str(Path(path).expanduser().resolve()) == want_resolved:
                    matched.append(status)
            except OSError:
                continue
        return matched

    def _motrix_collapse_duplicate_paths(self, dest_path: str = "") -> int:
        """Keep the Motrix task with the most progress per path; drop the rest.

        Never deletes media files — only removes duplicate aria2 GIDs.
        """
        collapsed = 0
        by_path = {}
        seen_gids = set()
        for status in self._motrix_collect_statuses_with_progress():
            gid = str(status.get("gid") or "").strip()
            path = self._motrix_status_path(status)
            if not path:
                continue
            if gid and gid in seen_gids:
                continue
            if gid:
                seen_gids.add(gid)
            if dest_path and path != dest_path:
                try:
                    if str(Path(path).resolve()) != str(Path(dest_path).resolve()):
                        continue
                except OSError:
                    continue
            by_path.setdefault(path, []).append(status)
        for path, rows in by_path.items():
            if len(rows) < 2:
                continue
            rows_sorted = sorted(
                rows,
                key=lambda row: self._motrix_status_completed_length(row),
                reverse=True,
            )
            keep = rows_sorted[0]
            keep_gid = str(keep.get("gid") or "").strip()
            for row in rows_sorted[1:]:
                gid = str(row.get("gid") or "").strip()
                if not gid or gid == keep_gid:
                    continue
                self._motrix_remove_gid(gid)
                self._untrack_motrix(gid=gid)
                collapsed += 1
                print(
                    f"[mac-helper] dropped duplicate Motrix gid {gid} for {path}",
                    flush=True,
                )
            if keep_gid:
                self._track_motrix(gid=keep_gid, dest_path=path)
        return collapsed

    def _mac_download_staging_dir(self) -> Path:
        """Mac download folder where Motrix grows files before relocate."""
        candidates = []
        if self.chrome_download_dir_arg:
            candidates.append(Path(self.chrome_download_dir_arg))
        candidates.extend(self._chrome_preference_download_dirs())
        candidates.append(Path.home() / "Downloads")
        for raw in candidates:
            try:
                path = Path(raw).expanduser().resolve()
            except OSError:
                continue
            try:
                if path.is_dir():
                    return path
            except OSError:
                continue
        fallback = (Path.home() / "Downloads").expanduser()
        self._ensure_dir(fallback)
        return fallback.resolve()

    def _motrix_download_dirs(self) -> list:
        ordered = []
        seen = set()
        candidates = []
        raw_dir = str(self._motrix_config().get("dir") or "").strip()
        if raw_dir:
            candidates.append(Path(raw_dir))
        candidates.append(self._mac_download_staging_dir())
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

    def _download_watch_dirs(self, method: str = "chrome") -> list:
        """
        Folders where Chrome or Motrix save completed files before the helper
        relocates them into video_dir/<streamer>/. Both stage on the Mac download folder.
        """
        ordered = []
        seen = set()
        candidates = []
        download_method = str(method or "chrome").strip().lower()
        if download_method == "motrix":
            candidates.append(self._mac_download_staging_dir())
            candidates.extend(self._motrix_download_dirs())
        if self.chrome_download_dir_arg:
            candidates.append(Path(self.chrome_download_dir_arg))
        candidates.extend(self._chrome_preference_download_dirs())
        if download_method == "motrix" and not candidates:
            candidates.append(Path.home() / "Downloads")
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

    def _candidate_download_paths(self, download_name: str, method: str = "chrome") -> list:
        """Exact basename plus Chrome/Motrix 'name (N).ext' uniquify variants."""
        stem = Path(download_name).stem
        suffix = Path(download_name).suffix
        names = [download_name]
        for index in range(1, 10):
            names.append(f"{stem} ({index}){suffix}")
        paths = []
        for root in self._download_watch_dirs(method):
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
        if Path(str(path) + ".aria2").exists():
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

    def _wait_for_download_file(
        self,
        download_name: str,
        expected_size: int,
        method: str = "chrome",
        final_dest: Path = None,
    ) -> Path:
        deadline = time.time() + DOWNLOAD_WAIT_SECONDS
        last_sizes = {}
        watch = self._download_watch_dirs(method)
        label = "Motrix" if str(method or "").lower() == "motrix" else "Chrome"
        candidates = []
        if final_dest is not None:
            candidates.append(Path(final_dest))
        candidates.extend(self._candidate_download_paths(download_name, method))
        if not candidates:
            raise RuntimeError(f"no {label} download folder to watch")
        watch_paths = []
        if final_dest is not None:
            watch_paths.append(str(Path(final_dest).parent))
        watch_paths.extend(str(path) for path in watch)
        watch_label = ", ".join(dict.fromkeys(watch_paths)) or "(final path)"
        print(
            f"[mac-helper] waiting for {label} file {download_name} in {watch_label}",
            flush=True,
        )
        while time.time() < deadline:
            for target in candidates:
                key = str(target)
                state = last_sizes.get(key, (None, 0))
                ready, state = self._chrome_file_ready(target, expected_size, state)
                last_sizes[key] = state
                if ready:
                    return target
            time.sleep(DOWNLOAD_POLL_SECONDS)
        raise TimeoutError(f"timed out waiting for {label} download: {download_name}")

    def _wait_for_chrome_download(self, download_name: str, expected_size: int) -> Path:
        return self._wait_for_download_file(download_name, expected_size, "chrome")

    def _relocate_one(self, item: dict, method: str = "chrome") -> None:
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

        download_method = str(method or "chrome").strip().lower()
        # Motrix and Chrome both stage on the Mac download folder, then relocate
        # into video_dir/<streamer>/ after the transfer finishes.
        staging = None
        if download_method == "motrix":
            staging = self._motrix_staging_path(
                relative_path=relative,
                download_name=download_name,
            )
        source = self._wait_for_download_file(
            download_name,
            expected_size,
            download_method,
            final_dest=staging,
        )
        size = self._path_size(source) or 0
        if source.resolve() != dest:
            self._move_file(source, dest)
            size = self._path_size(dest) or size
            if download_method == "motrix":
                for candidate in {source, staging}:
                    if candidate is None:
                        continue
                    leftover = Path(str(candidate) + ".aria2")
                    try:
                        if leftover.exists():
                            leftover.unlink()
                    except OSError:
                        pass
        if recording_id:
            self.upsert_ledger_entry(recording_id, rel.as_posix(), size)
            self.notify_filed(recording_id, rel.as_posix(), size)
        if source.resolve() == dest:
            print(f"[mac-helper] filed {relative} ({size} bytes)", flush=True)
        else:
            print(f"[mac-helper] filed {download_name} -> {relative} ({size} bytes)", flush=True)

    def _motrix_library_dest(self, relative_path: str) -> Path:
        rel = self._safe_relative_path(relative_path)
        root = self._refresh_video_dir()
        dest = (root / rel).resolve()
        root_resolved = root.resolve()
        if not str(dest).startswith(str(root_resolved) + os.sep):
            raise ValueError(f"relative path escapes video dir: {relative_path}")
        return dest

    def _motrix_staging_path(self, relative_path: str = "", download_name: str = "") -> Path:
        """Mac download path where Motrix grows the file before relocate."""
        staging_root = self._mac_download_staging_dir()
        relative = str(relative_path or "").strip()
        if relative:
            return (staging_root / self._motrix_library_dest(relative).name).resolve()
        name = str(download_name or "").strip()
        if not name or "/" in name or "\\" in name:
            raise ValueError(f"downloadFilename must be a basename: {download_name!r}")
        return (staging_root / name).resolve()

    def _motrix_task_options(
        self,
        download_name: str,
        relative_path: str = "",
        *,
        start_paused: bool = False,
    ) -> dict:
        """Options for Motrix jobs: stage on Mac Downloads, then relocate to streamer folder."""
        options = {
            "out": download_name,
            "pause": "true" if start_paused else "false",
            "auto-file-renaming": "false",
            "split": str(MOTRIX_SPLIT),
            "max-connection-per-server": str(MOTRIX_MAX_CONNECTION_PER_SERVER),
        }
        relative = str(relative_path or "").strip()
        staging_root = self._mac_download_staging_dir()
        if relative:
            dest = self._motrix_library_dest(relative)
            self._ensure_dir(dest.parent)
            options["dir"] = str(staging_root)
            options["out"] = dest.name
            return options
        options["dir"] = str(staging_root)
        if download_name:
            options["out"] = Path(download_name).name
        return options

    def _send_to_motrix(
        self,
        download_url: str,
        download_name: str,
        relative_path: str = "",
        *,
        recording_id: str = "",
        item_id: str = "",
    ) -> None:
        options = self._motrix_task_options(
            download_name,
            relative_path,
            start_paused=False,
        )
        dest_path = ""
        if options.get("dir") and options.get("out"):
            dest_path = str(Path(options["dir"]) / options["out"])
        # Reuse an existing Motrix task for this path instead of creating duplicates.
        if dest_path:
            existing = self._motrix_tasks_for_path(dest_path)
            if existing:
                self._motrix_collapse_duplicate_paths(dest_path)
                existing = self._motrix_tasks_for_path(dest_path) or existing
                best = max(existing, key=self._motrix_status_completed_length)
                gid = str(best.get("gid") or "").strip()
                self._track_motrix(
                    gid=gid,
                    dest_path=dest_path,
                    recording_id=recording_id,
                    item_id=item_id,
                    relative_path=relative_path,
                )
                print(
                    f"[mac-helper] reusing Motrix task {gid or '?'} for {dest_path}",
                    flush=True,
                )
                return
            with self._motrix_track_lock:
                already_tracked = dest_path in self._motrix_paths
            if already_tracked:
                print(
                    f"[mac-helper] skip Motrix re-add; already tracking {dest_path}",
                    flush=True,
                )
                return
        try:
            result = self._motrix_rpc("aria2.addUri", [download_url], options)
            gid = str(result or "").strip()
            # Track Motrix GIDs so heartbeat can report progress.
            self._track_motrix(
                gid=gid,
                dest_path=dest_path,
                recording_id=recording_id,
                item_id=item_id,
                relative_path=relative_path,
            )
            return
        except Exception as rpc_exc:
            print(f"[mac-helper] Motrix RPC failed ({rpc_exc!r}); trying mo:// protocol", flush=True)
        query_payload = {
            "uri": download_url,
            "out": options.get("out") or download_name,
            "split": str(MOTRIX_SPLIT),
            "max-connection-per-server": str(MOTRIX_MAX_CONNECTION_PER_SERVER),
        }
        if options.get("dir"):
            query_payload["dir"] = options["dir"]
        query = urllib.parse.urlencode(query_payload)
        subprocess.run(
            ["/usr/bin/open", f"mo://new-task?{query}"],
            check=True,
        )
        if dest_path:
            self._track_motrix(
                dest_path=dest_path,
                recording_id=recording_id,
                item_id=item_id,
                relative_path=relative_path,
            )

    def open_downloads(
        self,
        job_id: str,
        items: list,
        method: str = "chrome",
        auto_sync: bool = False,
    ):
        """
        Send each URL to Chrome or Motrix, then automatically place the file.
        Motrix stages on the Mac download folder, then relocates into the streamer folder.
        Ready files are relocated by the short-poll pending loop (non-blocking).
        auto_sync is ignored (Auto Sync removed); kept for call-site compatibility.
        """
        _ = auto_sync
        download_method = str(method or "chrome").strip().lower()
        if download_method not in {"chrome", "motrix"}:
            download_method = "chrome"
        try:
            seen_relatives = set()
            for item in items:
                download_url = item.get("url")
                download_name = str(
                    item.get("downloadFilename") or item.get("filename") or ""
                ).strip()
                relative = str(item.get("relativePath") or "").strip()
                if not relative or not download_name:
                    continue
                if relative in seen_relatives:
                    print(
                        f"[mac-helper] skip duplicate job item {relative}",
                        flush=True,
                    )
                    continue
                seen_relatives.add(relative)
                # Already in the library or finished in staging: enqueue only so the
                # short-poll loop can file it. Do not re-add to Motrix (that can
                # recreate a .aria2 sidecar beside a complete file).
                if self._item_already_in_library(item):
                    self._enqueue_pending_relocate(item, download_method)
                    print(
                        f"[mac-helper] already in library {relative}; queued relocate",
                        flush=True,
                    )
                    continue
                if download_method == "motrix" and self._item_staging_ready(
                    item, download_method
                ):
                    self._enqueue_pending_relocate(item, download_method)
                    print(
                        f"[mac-helper] staging ready {download_name}; queued relocate "
                        f"(skip Motrix re-add for {relative})",
                        flush=True,
                    )
                    continue
                if not download_url:
                    # No URL and not ready — still track if VPS sent a relocate-only row.
                    self._enqueue_pending_relocate(item, download_method)
                    continue
                if download_method == "motrix":
                    self._send_to_motrix(
                        str(download_url),
                        download_name,
                        relative,
                        recording_id=str(item.get("recordingId") or ""),
                        item_id=str(item.get("itemId") or item.get("recordingId") or ""),
                    )
                    print(
                        f"[mac-helper] sent {download_name} to Motrix "
                        f"(direct {relative or download_name})",
                        flush=True,
                    )
                else:
                    subprocess.run(
                        ["/usr/bin/open", "-a", "Google Chrome", download_url],
                        check=True,
                    )
                    print(
                        f"[mac-helper] sent {download_name or item.get('filename')} "
                        f"to Chrome (final {item.get('relativePath')})",
                        flush=True,
                    )
                self._enqueue_pending_relocate(item, download_method)
                time.sleep(0.4)
            # Kick an immediate sweep so already-complete staging files move now;
            # unfinished ones stay pending for the background short-poll loop.
            filed = self._sweep_pending_relocates()
            if filed:
                print(
                    f"[mac-helper] relocated {filed} ready download(s) after job {job_id}",
                    flush=True,
                )
            self._wake_pending_relocate()
        except Exception as exc:
            print(f"[mac-helper] open downloads for {job_id} failed: {exc!r}", flush=True)


def make_handler(helper: Helper):
    class Handler(BaseHTTPRequestHandler):
        def _origin_allowed(self):
            origin = self.headers.get("Origin", "").rstrip("/")
            if not origin:
                return True
            allowed = helper.allowed_origin.rstrip("/")
            return origin == allowed

        def _cors(self):
            origin = self.headers.get("Origin", "").rstrip("/")
            allow = helper.allowed_origin
            if origin and self._origin_allowed():
                allow = origin
            self.send_header("Access-Control-Allow-Origin", allow)
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
                    "chromeCookies": True,
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
                # Media Rescan and sync-snapshot must see the live folder.
                self._json(200, helper.scan(force=True))
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
                method = str(job.get("method") or body.get("method") or "chrome")
                threading.Thread(
                    target=helper.open_downloads,
                    args=(job_id, job.get("items") or [], method),
                    daemon=True,
                ).start()
                self._json(202, {
                    "status": "accepted",
                    "jobId": job_id,
                    "method": method,
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
            if self.path == "/provider-cookies":
                source_type = str(body.get("sourceType") or body.get("source") or "").strip().lower()
                if not provider_cookie_spec(source_type):
                    self._json(400, {"error": "Chrome import is not available for this provider"})
                    return
                result = helper.export_provider_cookies(
                    source_type,
                    open_login_if_missing=True,
                )
                result.pop("_aes_key", None)
                if result.get("ok"):
                    self._json(200, {
                        "ok": True,
                        "sourceType": result.get("sourceType"),
                        "profile": result.get("profile"),
                        "cookies": result.get("cookies") or [],
                        "cookieHeader": result.get("cookieHeader") or "",
                        "cookieNames": result.get("cookieNames") or [],
                        "username": result.get("username") or "",
                        "userAgent": result.get("userAgent") or "",
                    })
                    return
                status = int(result.get("status") or 500)
                self._json(status, {
                    "ok": False,
                    "error": result.get("error") or "Chrome import failed",
                    "missing": result.get("missing") or [],
                    "loginUrl": result.get("loginUrl") or "",
                    "openedChrome": bool(result.get("openedChrome")),
                })
                return
            if self.path == "/provider/follow":
                if body.get("localSessionId") != helper.session_id:
                    self._json(403, {"error": "Wrong local session"})
                    return
                source_type = str(body.get("sourceType") or body.get("source") or "").strip().lower()
                username = str(body.get("username") or "").strip()
                follow = body.get("follow")
                if follow is None:
                    follow = str(body.get("action") or "follow").strip().lower() != "unfollow"
                result = helper.provider_remote_follow(source_type, username, follow=bool(follow))
                if result.get("ok"):
                    self._json(200, result)
                    return
                self._json(400, result)
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
