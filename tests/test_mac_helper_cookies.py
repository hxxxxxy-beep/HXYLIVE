import importlib.util
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_helper_module():
    path = Path(__file__).resolve().parents[1] / "mac-helper" / "hxylive_mac_helper.py"
    spec = importlib.util.spec_from_file_location("hxylive_mac_helper_cookies", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper_mod = _load_helper_module()


PASSPHRASE = b"peanuts"


def _encrypt_v10(plaintext: str, passphrase: bytes = PASSPHRASE, hashed_prefix: bytes = b"") -> bytes:
    key = helper_mod.derive_chrome_aes_key(passphrase)
    raw = hashed_prefix + plaintext.encode("utf-8")
    pad = 16 - (len(raw) % 16)
    padded = raw + bytes([pad]) * pad
    result = subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-128-cbc",
            "-e",
            "-K",
            key.hex(),
            "-iv",
            helper_mod.CHROME_COOKIE_IV_HEX,
            "-nopad",
        ],
        input=padded,
        capture_output=True,
        check=True,
    )
    return b"v10" + result.stdout


def _write_cookie_db(path: Path, rows, version=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE cookies ("
            "host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, "
            "path TEXT, expires_utc INTEGER)"
        )
        conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        conn.execute("INSERT INTO meta (key, value) VALUES ('version', ?)", (str(version),))
        conn.executemany(
            "INSERT INTO cookies (host_key, name, value, encrypted_value, path, expires_utc) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


class ChromeCookieExportTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name) / "Chrome"
        self.default_db = self.root / "Default" / "Network" / "Cookies"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_host_match_and_expiry(self):
        self.assertTrue(helper_mod.chrome_host_matches(".bilibili.com", ("bilibili.com",)))
        self.assertTrue(helper_mod.chrome_host_matches("live.bilibili.com", ("bilibili.com",)))
        self.assertFalse(helper_mod.chrome_host_matches("evil-bilibili.com", ("bilibili.com",)))
        self.assertFalse(helper_mod.chrome_cookie_expired(0))
        past = int((1_000_000) * (helper_mod.CHROME_EPOCH_OFFSET_SECONDS + 1))
        self.assertTrue(helper_mod.chrome_cookie_expired(past, now=10_000))

    def test_decrypts_v10_and_strips_hash_prefix(self):
        key = helper_mod.derive_chrome_aes_key(PASSPHRASE)
        blob = _encrypt_v10("sess-value")
        self.assertEqual(helper_mod.decrypt_chrome_v10_value(blob, key), "sess-value")
        hashed = _encrypt_v10("sess-value", hashed_prefix=b"h" * 32)
        self.assertEqual(helper_mod.decrypt_chrome_v10_value(hashed, key, db_version=24), "sess-value")

    def test_exports_bilibili_cookies_from_chrome_profile(self):
        _write_cookie_db(self.default_db, [
            (".bilibili.com", "SESSDATA", "", _encrypt_v10("sess"), "/", 0),
            (".bilibili.com", "bili_jct", "", _encrypt_v10("csrf"), "/", 0),
            (".bilibili.com", "DedeUserID", "42", b"", "/", 0),
            (".unrelated.test", "SESSDATA", "nope", b"", "/", 0),
        ])
        result = helper_mod.export_provider_cookies_from_chrome(
            "bilibili",
            chrome_root=self.root,
            keychain_loader=lambda: PASSPHRASE,
        )
        result.pop("_aes_key", None)
        self.assertTrue(result["ok"])
        names = {cookie["name"]: cookie["value"] for cookie in result["cookies"]}
        self.assertEqual(names["SESSDATA"], "sess")
        self.assertEqual(names["bili_jct"], "csrf")
        self.assertEqual(names["DedeUserID"], "42")
        self.assertIn("SESSDATA=sess", result["cookieHeader"])
        self.assertNotIn("nope", result["cookieHeader"])

    def test_twitch_requires_auth_token_and_reads_login(self):
        _write_cookie_db(self.default_db, [
            (".twitch.tv", "login", "alice", b"", "/", 0),
        ])
        missing = helper_mod.export_provider_cookies_from_chrome(
            "twitch",
            chrome_root=self.root,
            keychain_loader=lambda: PASSPHRASE,
        )
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["status"], 409)
        self.assertIn("auth-token", missing["error"])

        _write_cookie_db(self.default_db, [
            (".twitch.tv", "auth-token", "oauth", b"", "/", 0),
            (".twitch.tv", "login", "alice", b"", "/", 0),
        ])
        found = helper_mod.export_provider_cookies_from_chrome(
            "twitch",
            chrome_root=self.root,
            keychain_loader=lambda: PASSPHRASE,
        )
        self.assertTrue(found["ok"])
        self.assertEqual(found["username"], "alice")

    def test_unknown_provider_is_rejected(self):
        result = helper_mod.export_provider_cookies_from_chrome("onlyfans", chrome_root=self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 400)

    def test_stripchat_requires_session_id(self):
        _write_cookie_db(self.default_db, [
            (".stripchat.com", "guestID", "guest", b"", "/", 0),
            (".stripchat.com", "firstVisit", "1", b"", "/", 0),
        ])
        missing = helper_mod.export_provider_cookies_from_chrome(
            "stripchat",
            chrome_root=self.root,
            keychain_loader=lambda: PASSPHRASE,
        )
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["status"], 409)
        self.assertIn("sessionId", missing["error"])

        _write_cookie_db(self.default_db, [
            (".stripchat.com", "sessionId", "sess", b"", "/", 0),
            (".stripchat.com", "guestID", "guest", b"", "/", 0),
        ])
        found = helper_mod.export_provider_cookies_from_chrome(
            "stripchat",
            chrome_root=self.root,
            keychain_loader=lambda: PASSPHRASE,
        )
        self.assertTrue(found["ok"])
        names = {cookie["name"] for cookie in found["cookies"]}
        self.assertIn("sessionId", names)
        self.assertIn("stripchat_com_sessionId", names)

    def test_stripchat_accepts_prefixed_session_cookie(self):
        _write_cookie_db(self.default_db, [
            (".stripchat.com", "stripchat_com_sessionId", "sess-new", b"", "/", 0),
            (".stripchat.com", "stripchat_com_sessionRemember", "1", b"", "/", 0),
        ])
        found = helper_mod.export_provider_cookies_from_chrome(
            "stripchat",
            chrome_root=self.root,
            keychain_loader=lambda: PASSPHRASE,
        )
        self.assertTrue(found["ok"])
        names = {cookie["name"] for cookie in found["cookies"]}
        self.assertIn("stripchat_com_sessionId", names)
        self.assertIn("sessionId", names)
        values = {cookie["name"]: cookie["value"] for cookie in found["cookies"]}
        self.assertEqual(values["sessionId"], "sess-new")
        self.assertEqual(values["stripchat_com_sessionId"], "sess-new")

    def test_youtube_requires_sapisid(self):
        _write_cookie_db(self.default_db, [
            (".youtube.com", "VISITOR_INFO1_LIVE", "anon", b"", "/", 0),
        ])
        missing = helper_mod.export_provider_cookies_from_chrome(
            "youtube",
            chrome_root=self.root,
            keychain_loader=lambda: PASSPHRASE,
        )
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["status"], 409)
        self.assertIn("SAPISID", missing["error"])

        _write_cookie_db(self.default_db, [
            (".youtube.com", "SAPISID", "", _encrypt_v10("sapisid"), "/", 0),
            (".youtube.com", "SID", "sid", b"", "/", 0),
        ])
        missing_cc = helper_mod.export_provider_cookies_from_chrome(
            "youtube",
            chrome_root=self.root,
            keychain_loader=lambda: PASSPHRASE,
        )
        self.assertFalse(missing_cc["ok"])
        self.assertEqual(missing_cc["status"], 409)
        self.assertIn("SIDCC", missing_cc["error"])

        _write_cookie_db(self.default_db, [
            (".youtube.com", "SAPISID", "", _encrypt_v10("sapisid"), "/", 0),
            (".youtube.com", "SID", "sid", b"", "/", 0),
            (".youtube.com", "__Secure-3PSIDCC", "psidcc-yt", b"", "/", 0),
            (".google.com", "__Secure-3PSIDCC", "psidcc-google", b"", "/", 0),
            (".google.com", "SID", "sid-google", b"", "/", 0),
        ])
        found = helper_mod.export_provider_cookies_from_chrome(
            "youtube",
            chrome_root=self.root,
            keychain_loader=lambda: PASSPHRASE,
        )
        self.assertTrue(found["ok"])
        names = {cookie["name"]: cookie["value"] for cookie in found["cookies"]}
        self.assertEqual(names["SAPISID"], "sapisid")
        self.assertEqual(names["__Secure-3PSIDCC"], "psidcc-yt")
        self.assertEqual(names["SID"], "sid")
        domains = {cookie["name"]: cookie["domain"] for cookie in found["cookies"]}
        self.assertEqual(domains["__Secure-3PSIDCC"], ".youtube.com")
        self.assertEqual(domains["SID"], ".youtube.com")
    def test_missing_chrome_profile_returns_404(self):
        result = helper_mod.export_provider_cookies_from_chrome(
            "bilibili",
            chrome_root=self.root / "missing",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 404)

    def test_helper_opens_chrome_when_session_is_missing(self):
        helper = helper_mod.Helper(
            Path(self.tmpdir.name) / "videos",
            "http://example.test",
            "",
            support_dir=Path(self.tmpdir.name) / "support",
        )
        (Path(self.tmpdir.name) / "videos").mkdir(exist_ok=True)
        (Path(self.tmpdir.name) / "support").mkdir(exist_ok=True)
        _write_cookie_db(self.default_db, [
            (".bilibili.com", "SESSDATA", "sess", b"", "/", 0),
        ])
        opened = []
        with patch.object(helper_mod, "open_chrome_url", side_effect=lambda url: opened.append(url) or url):
            result = helper.export_provider_cookies(
                "bilibili",
                chrome_root=self.root,
                open_login_if_missing=True,
            )
        self.assertFalse(result["ok"])
        self.assertTrue(result["openedChrome"])
        self.assertEqual(opened, ["https://www.bilibili.com"])

    def test_pending_import_session_command_posts_to_vps(self):
        helper = helper_mod.Helper(
            Path(self.tmpdir.name) / "videos",
            "http://example.test",
            "",
            support_dir=Path(self.tmpdir.name) / "support",
        )
        (Path(self.tmpdir.name) / "videos").mkdir(exist_ok=True)
        (Path(self.tmpdir.name) / "support").mkdir(exist_ok=True)
        posted = []

        def fake_import(source_type, command_id):
            posted.append((source_type, command_id))
            return {"success": True}

        helper.import_provider_session_to_vps = fake_import
        helper._run_pending_commands([{
            "type": "import-session",
            "commandId": "cmd1",
            "sourceType": "twitch",
        }])
        self.assertEqual(posted, [("twitch", "cmd1")])
