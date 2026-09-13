"""
SQLite database management for the model cache
"""
import aiosqlite
import json
import re
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime
from ..logger import logger

class Database:
    # Default timeout (ms) waiting for a SQLite lock before "database is locked"
    BUSY_TIMEOUT_MS = 10000

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._initialized = False

    @staticmethod
    def _normalize_source_type(source_type: Optional[str]) -> str:
        return (source_type or "").strip().lower() or "chaturbate"

    @staticmethod
    def _default_record_path(username: str) -> str:
        model_folder = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(username or "").strip())
        model_folder = model_folder.strip(".-") or "model"
        return f"{model_folder}/videos/record"

    def _connect(self):
        """Open an aiosqlite connection with a lock timeout.

        Returns an async context manager. The Python timeout avoids immediate
        "database is locked" errors when several tasks write/read
        in parallel (monitoring, sync, conversion, API endpoints).
        """
        # timeout (s): max wait for a lock before OperationalError
        return aiosqlite.connect(self.db_path, timeout=self.BUSY_TIMEOUT_MS / 1000)

    async def _apply_pragmas(self, db):
        """Enable WAL + busy_timeout on a connection."""
        await db.execute(f"PRAGMA busy_timeout = {self.BUSY_TIMEOUT_MS}")

    async def initialize(self):
        """Initialize the database and create tables"""
        if self._initialized:
            return

        async with self._connect() as db:
            # Enable WAL: reads are not blocked by concurrent writes
            # (fixes 500s on /api/models and /api/following under load)
            try:
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("PRAGMA synchronous=NORMAL")
            except Exception as e:
                logger.warning("Unable to enable WAL", error=str(e))
            await self._apply_pragmas(db)
            # Table for models and their status
            await db.execute("""
                CREATE TABLE IF NOT EXISTS models (
                    username TEXT NOT NULL,
                    display_name TEXT,
                    is_online BOOLEAN DEFAULT 0,
                    is_recording BOOLEAN DEFAULT 0,
                    viewers INTEGER DEFAULT 0,
                    thumbnail_path TEXT,
                    thumbnail_updated_at INTEGER,
                    last_check_at INTEGER,
                    auto_record BOOLEAN DEFAULT 0,
                    record_quality TEXT DEFAULT 'best',
                    monthly_quota_gb INTEGER,
                    retention_days INTEGER DEFAULT 30,
                    record_path TEXT,
                    source_type TEXT NOT NULL DEFAULT 'chaturbate',
                    room_status TEXT,
                    created_at INTEGER,
                    updated_at INTEGER,
                    PRIMARY KEY(username, source_type)
                )
            """)

            # Enriched local cards for the media library. This information
            # stays separate from stream settings in models.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS media_profiles (
                    username TEXT PRIMARY KEY,
                    display_name TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    age INTEGER,
                    birth_date TEXT,
                    profile_image_url TEXT,
                    profile_image_source_url TEXT,
                    profile_image_path TEXT,
                    address TEXT,
                    city TEXT,
                    region TEXT,
                    postal_code TEXT,
                    country TEXT,
                    aliases TEXT,
                    tags TEXT,
                    notes TEXT,
                    social_urls TEXT,
                    stream_urls TEXT,
                    profile_urls TEXT,
                    created_at INTEGER,
                    updated_at INTEGER
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS media_profile_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_username TEXT NOT NULL,
                    source_type TEXT NOT NULL DEFAULT 'chaturbate',
                    channel_username TEXT NOT NULL,
                    channel_url TEXT,
                    auto_record BOOLEAN DEFAULT 0,
                    record_quality TEXT DEFAULT 'best',
                    monthly_quota_gb INTEGER,
                    retention_days INTEGER DEFAULT 30,
                    record_path TEXT,
                    created_at INTEGER,
                    updated_at INTEGER,
                    UNIQUE(profile_username, source_type, channel_username)
                )
            """)

            # Table for recordings
            await db.execute("""
                CREATE TABLE IF NOT EXISTS recordings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    recording_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_size INTEGER,
                    duration_seconds INTEGER,
                    resolution TEXT,
                    fps REAL,
                    bitrate INTEGER,
                    thumbnail_path TEXT,
                    mp4_path TEXT,
                    mp4_size INTEGER,
                    is_converted BOOLEAN DEFAULT 0,
                    media_kind TEXT DEFAULT 'recording',
                    title TEXT,
                    import_status TEXT,
                    import_error TEXT,
                    source_mtime INTEGER,
                    playable_path TEXT,
                    playable_size INTEGER,
                    protected_from_retention BOOLEAN DEFAULT 0,
                    created_at INTEGER,
                    UNIQUE(username, filename)
                )
            """)

            # Tombstones: keep evidence after VPS files / live rows are removed.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS recording_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recording_id TEXT,
                    username TEXT NOT NULL,
                    filename TEXT,
                    file_path TEXT,
                    file_size INTEGER,
                    mp4_path TEXT,
                    mp4_size INTEGER,
                    duration_seconds INTEGER,
                    media_kind TEXT,
                    final_status TEXT NOT NULL,
                    reason TEXT,
                    created_at INTEGER,
                    archived_at INTEGER NOT NULL,
                    extra_json TEXT
                )
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_recording_history_username
                ON recording_history(username, archived_at DESC)
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_recording_history_recording_id
                ON recording_history(recording_id, archived_at DESC)
            """)

            # Durable live sessions for Media timeline (was-live vs recorded).
            await db.execute("""
                CREATE TABLE IF NOT EXISTS live_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_username TEXT NOT NULL,
                    source_type TEXT NOT NULL DEFAULT 'chaturbate',
                    profile_username TEXT,
                    started_at INTEGER NOT NULL,
                    ended_at INTEGER,
                    last_seen_at INTEGER NOT NULL,
                    detection TEXT DEFAULT 'monitor',
                    UNIQUE(channel_username, source_type, started_at)
                )
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_live_sessions_channel_time
                ON live_sessions(channel_username, source_type, started_at DESC)
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_live_sessions_profile_time
                ON live_sessions(profile_username, started_at DESC)
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_live_sessions_open
                ON live_sessions(ended_at, last_seen_at)
            """)

            # Recording projects: group fragments across segment parts and resume gaps.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS recording_projects (
                    project_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    source_type TEXT NOT NULL DEFAULT 'chaturbate',
                    started_at INTEGER NOT NULL,
                    ended_at INTEGER,
                    ready_at INTEGER,
                    status TEXT NOT NULL DEFAULT 'open',
                    concat_status TEXT NOT NULL DEFAULT 'none',
                    gap_buffer_seconds INTEGER NOT NULL DEFAULT 1800,
                    created_at INTEGER,
                    updated_at INTEGER
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_recording_projects_user_status
                ON recording_projects(username, status, started_at DESC)
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS recording_project_members (
                    project_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    recording_id TEXT,
                    item_id TEXT,
                    started_at INTEGER,
                    ended_at INTEGER,
                    duration_seconds INTEGER DEFAULT 0,
                    size_bytes INTEGER DEFAULT 0,
                    part_label TEXT,
                    session_key TEXT,
                    sort_index INTEGER DEFAULT 0,
                    location TEXT NOT NULL DEFAULT 'vps',
                    PRIMARY KEY(username, filename)
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_recording_project_members_project
                ON recording_project_members(project_id, started_at, sort_index)
            """)
            try:
                await db.execute(
                    "ALTER TABLE recording_project_members ADD COLUMN location TEXT NOT NULL DEFAULT 'vps'"
                )
            except Exception:
                pass

            # Table for Chaturbate authentication
            await db.execute("""
                CREATE TABLE IF NOT EXISTS chaturbate_auth (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    username TEXT,
                    password_hash TEXT,
                    is_logged_in BOOLEAN DEFAULT 0,
                    session_cookies TEXT,
                    cf_clearance TEXT,
                    csrf_token TEXT,
                    last_login_at INTEGER,
                    last_error TEXT,
                    updated_at INTEGER
                )
            """)

            # Table for Chaturbate followed models
            await db.execute("""
                CREATE TABLE IF NOT EXISTS followed_models (
                    username TEXT NOT NULL,
                    display_name TEXT,
                    is_online BOOLEAN DEFAULT 0,
                    viewers INTEGER DEFAULT 0,
                    thumbnail_url TEXT,
                    profile_image_url TEXT,
                    last_seen_online_at INTEGER,
                    synced_at INTEGER,
                    source_type TEXT NOT NULL DEFAULT 'chaturbate',
                    room_status TEXT,
                    followers INTEGER,
                    PRIMARY KEY(username, source_type)
                )
            """)

            # Table for settings (blacklisted tags, etc.)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at INTEGER
                )
            """)

            # Table for playback position (resume)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS playback_positions (
                    recording_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    position_seconds REAL DEFAULT 0,
                    duration_seconds REAL DEFAULT 0,
                    watched_at INTEGER,
                    updated_at INTEGER
                )
            """)

            # Table for installed plugins (third-party streaming sources)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS plugins (
                    id TEXT PRIMARY KEY,
                    version TEXT NOT NULL,
                    source_type TEXT NOT NULL UNIQUE,
                    source_repo TEXT,
                    enabled BOOLEAN DEFAULT 1,
                    installed BOOLEAN DEFAULT 1,
                    status TEXT DEFAULT 'pending_restart',
                    last_error TEXT,
                    manifest_json TEXT,
                    installed_at INTEGER,
                    updated_at INTEGER
                )
            """)

            # Generic sessions per provider. Credentials are local-only and
            # allow reconnect retries after provider sessions expire.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS provider_sessions (
                    source_type TEXT PRIMARY KEY,
                    username TEXT,
                    is_logged_in BOOLEAN DEFAULT 0,
                    session_cookies TEXT,
                    local_storage TEXT,
                    credential_username TEXT,
                    credential_password TEXT,
                    credentials_updated_at INTEGER,
                    last_login_at INTEGER,
                    last_error TEXT,
                    updated_at INTEGER
                )
            """)

            # Indexes for frequent queries
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_models_online
                ON models(is_online, username)
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_models_source_username
                ON models(source_type, username)
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_followed_source_username
                ON followed_models(source_type, username)
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_recordings_username
                ON recordings(username, created_at DESC)
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_media_profile_sources_profile
                ON media_profile_sources(profile_username)
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_media_profile_sources_auto
                ON media_profile_sources(auto_record, source_type, channel_username)
            """)

            # Idempotent migrations for existing schemas
            await self._migrate_schema(db)
            await self._migrate_provider_identity_keys(db)
            await self._migrate_explicit_recording_opt_in(db)

            await db.commit()

        self._initialized = True
        logger.info("Database initialized", db_path=str(self.db_path))

    async def _migrate_explicit_recording_opt_in(self, db):
        """Suspend legacy automatic recording once; users must opt in explicitly."""
        migration_key = "explicit_recording_opt_in_v1"
        cursor = await db.execute(
            "SELECT value FROM settings WHERE key = ? LIMIT 1",
            (migration_key,),
        )
        if await cursor.fetchone():
            return
        now = int(datetime.now().timestamp())
        await db.execute("UPDATE models SET auto_record = 0, updated_at = ?", (now,))
        await db.execute(
            "UPDATE media_profile_sources SET auto_record = 0, updated_at = ?",
            (now,),
        )
        await db.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, '1', ?)
            """,
            (migration_key, now),
        )

    async def _migrate_schema(self, db):
        """Add missing columns on existing DBs (lightweight migrations)."""
        migrations = [
            ("recordings", "conversion_attempts", "INTEGER DEFAULT 0"),
            ("recordings", "conversion_error", "TEXT"),
            ("recordings", "last_conversion_attempt", "INTEGER"),
            ("recordings", "media_kind", "TEXT DEFAULT 'recording'"),
            ("recordings", "title", "TEXT"),
            ("recordings", "import_status", "TEXT"),
            ("recordings", "import_error", "TEXT"),
            ("recordings", "source_mtime", "INTEGER"),
            ("recordings", "playable_path", "TEXT"),
            ("recordings", "playable_size", "INTEGER"),
            ("recordings", "protected_from_retention", "BOOLEAN DEFAULT 0"),
            ("recordings", "resolution", "TEXT"),
            ("recordings", "fps", "REAL"),
            ("recordings", "bitrate", "INTEGER"),
            ("media_profiles", "birth_date", "TEXT"),
            ("media_profiles", "profile_image_url", "TEXT"),
            ("media_profiles", "profile_image_source_url", "TEXT"),
            ("media_profiles", "profile_image_path", "TEXT"),
            ("media_profiles", "last_seen_online_at", "INTEGER"),
            ("models", "source_type", "TEXT DEFAULT 'chaturbate'"),
            ("models", "room_status", "TEXT"),
            ("models", "record_path", "TEXT"),
            ("followed_models", "source_type", "TEXT DEFAULT 'chaturbate'"),
            ("followed_models", "room_status", "TEXT"),
            ("followed_models", "profile_image_url", "TEXT"),
            ("followed_models", "followers", "INTEGER"),
            ("provider_sessions", "credential_username", "TEXT"),
            ("provider_sessions", "credential_password", "TEXT"),
            ("provider_sessions", "credentials_updated_at", "INTEGER"),
            ("playback_positions", "watched_at", "INTEGER"),
            ("models", "monthly_quota_gb", "INTEGER"),
            ("media_profile_sources", "monthly_quota_gb", "INTEGER"),
        ]
        for table, column, ddl in migrations:
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                logger.info("Migration: column added", table=table, column=column)
            except Exception:
                # Column already exists - SQLite raises "duplicate column"
                pass

    async def _migrate_provider_identity_keys(self, db):
        """Upgrade legacy username-only tables to provider-aware identities."""

        async def table_pk_columns(table: str) -> list[str]:
            cursor = await db.execute(f"PRAGMA table_info({table})")
            rows = await cursor.fetchall()
            return [
                row[1]
                for row in sorted(
                    [row for row in rows if int(row[5] or 0) > 0],
                    key=lambda item: int(item[5] or 0),
                )
            ]

        async def rebuild_models() -> None:
            await db.execute("ALTER TABLE models RENAME TO models_legacy_provider_identity")
            await db.execute("""
                CREATE TABLE models (
                    username TEXT NOT NULL,
                    display_name TEXT,
                    is_online BOOLEAN DEFAULT 0,
                    is_recording BOOLEAN DEFAULT 0,
                    viewers INTEGER DEFAULT 0,
                    thumbnail_path TEXT,
                    thumbnail_updated_at INTEGER,
                    last_check_at INTEGER,
                    auto_record BOOLEAN DEFAULT 1,
                    record_quality TEXT DEFAULT 'best',
                    retention_days INTEGER DEFAULT 30,
                    record_path TEXT,
                    source_type TEXT NOT NULL DEFAULT 'chaturbate',
                    room_status TEXT,
                    created_at INTEGER,
                    updated_at INTEGER,
                    PRIMARY KEY(username, source_type)
                )
            """)
            await db.execute("""
                INSERT OR REPLACE INTO models (
                    username, display_name, is_online, is_recording, viewers,
                    thumbnail_path, thumbnail_updated_at, last_check_at,
                    auto_record, record_quality, retention_days, record_path, source_type,
                    room_status, created_at, updated_at
                )
                SELECT
                    legacy.username, legacy.display_name, legacy.is_online,
                    legacy.is_recording, legacy.viewers, legacy.thumbnail_path,
                    legacy.thumbnail_updated_at, legacy.last_check_at,
                    legacy.auto_record, legacy.record_quality,
                    legacy.retention_days, legacy.record_path,
                    CASE
                        WHEN COALESCE(NULLIF(legacy.source_type, ''), 'chaturbate') = 'chaturbate'
                         AND EXISTS (
                            SELECT 1 FROM followed_models fm
                            WHERE fm.username = legacy.username
                              AND fm.source_type = 'cam4'
                         )
                         AND NOT EXISTS (
                            SELECT 1 FROM followed_models fm
                            WHERE fm.username = legacy.username
                              AND fm.source_type = 'chaturbate'
                         )
                        THEN 'cam4'
                        ELSE COALESCE(NULLIF(legacy.source_type, ''), 'chaturbate')
                    END,
                    legacy.room_status, legacy.created_at, legacy.updated_at
                FROM models_legacy_provider_identity AS legacy
                WHERE legacy.username IS NOT NULL AND legacy.username != ''
            """)
            await db.execute("DROP TABLE models_legacy_provider_identity")

        async def rebuild_followed() -> None:
            await db.execute("ALTER TABLE followed_models RENAME TO followed_models_legacy_provider_identity")
            await db.execute("""
                CREATE TABLE followed_models (
                    username TEXT NOT NULL,
                    display_name TEXT,
                    is_online BOOLEAN DEFAULT 0,
                    viewers INTEGER DEFAULT 0,
                    thumbnail_url TEXT,
                    last_seen_online_at INTEGER,
                    synced_at INTEGER,
                    source_type TEXT NOT NULL DEFAULT 'chaturbate',
                    room_status TEXT,
                    PRIMARY KEY(username, source_type)
                )
            """)
            await db.execute("""
                INSERT OR REPLACE INTO followed_models (
                    username, display_name, is_online, viewers, thumbnail_url,
                    last_seen_online_at, synced_at, source_type, room_status
                )
                SELECT
                    username, display_name, is_online, viewers, thumbnail_url,
                    last_seen_online_at, synced_at,
                    COALESCE(NULLIF(source_type, ''), 'chaturbate'),
                    room_status
                FROM followed_models_legacy_provider_identity
                WHERE username IS NOT NULL AND username != ''
            """)
            await db.execute("DROP TABLE followed_models_legacy_provider_identity")

        try:
            if await table_pk_columns("models") != ["username", "source_type"]:
                await rebuild_models()
                logger.info("Migration: models uses username+source_type")
        except Exception as exc:
            logger.warning("Migration provider-aware models failed", error=str(exc))

        try:
            if await table_pk_columns("followed_models") != ["username", "source_type"]:
                await rebuild_followed()
                logger.info("Migration: followed_models uses username+source_type")
        except Exception as exc:
            logger.warning("Migration provider-aware followed_models failed", error=str(exc))

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_models_online
            ON models(is_online, username)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_models_source_username
            ON models(source_type, username)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_followed_source_username
            ON followed_models(source_type, username)
        """)

    async def add_or_update_model(
        self,
        username: str,
        display_name: Optional[str] = None,
        auto_record: bool = False,
        record_quality: str = "best",
        retention_days: int = 30,
        record_path: Optional[str] = None,
        source_type: Optional[str] = None,
        monthly_quota_gb: Optional[int] = None,
    ):
        """Add or update a model"""
        await self.initialize()

        now = int(datetime.now().timestamp())
        source_type = self._normalize_source_type(source_type)

        async with self._connect() as db:
            await db.execute("""
                INSERT INTO models (
                    username, display_name, auto_record, record_quality,
                    monthly_quota_gb, retention_days, record_path, source_type,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username, source_type) DO UPDATE SET
                    display_name = COALESCE(?, display_name),
                    auto_record = ?,
                    record_quality = ?,
                    monthly_quota_gb = ?,
                    retention_days = ?,
                    record_path = COALESCE(?, record_path),
                    updated_at = ?
            """, (
                username, display_name, auto_record, record_quality,
                monthly_quota_gb, retention_days, record_path, source_type, now, now,
                display_name, auto_record, record_quality, monthly_quota_gb,
                retention_days, record_path, now
            ))
            await db.commit()

        logger.debug("Model added/updated", username=username, source_type=source_type)

    async def reconcile_model_sources_from_followed(self) -> int:
        """Repair only genuinely source-less legacy rows.

        ``source_type`` is part of the current model identity. A Chaturbate row
        must never be rewritten merely because another provider follows the same
        username. Old username-only CAM4 rows are repaired once during schema
        migration instead.
        """
        await self.initialize()

        async with self._connect() as db:
            cursor = await db.execute("""
                UPDATE models
                SET source_type = (
                    SELECT fm.source_type
                    FROM followed_models fm
                    WHERE fm.username = models.username
                      AND fm.source_type IS NOT NULL
                      AND fm.source_type != ''
                      AND fm.source_type != 'chaturbate'
                    LIMIT 1
                )
                WHERE (source_type IS NULL OR source_type = '')
                  AND EXISTS (
                    SELECT 1
                    FROM followed_models fm
                    WHERE fm.username = models.username
                      AND fm.source_type IS NOT NULL
                      AND fm.source_type != ''
                      AND fm.source_type != 'chaturbate'
                  )
            """)
            await db.commit()
            return cursor.rowcount or 0

    async def update_model_status(
        self,
        username: str,
        is_online: bool,
        viewers: int = 0,
        is_recording: bool = False,
        thumbnail_path: Optional[str] = None,
        room_status: Optional[str] = None,
        source_type: Optional[str] = None,
    ):
        """Update a model's status"""
        await self.initialize()

        now = int(datetime.now().timestamp())

        async with self._connect() as db:
            update_fields = {
                'is_online': is_online,
                'viewers': viewers,
                'is_recording': is_recording,
                'room_status': room_status,
                'last_check_at': now,
                'updated_at': now
            }

            if thumbnail_path:
                update_fields['thumbnail_path'] = thumbnail_path
                update_fields['thumbnail_updated_at'] = now

            placeholders = ', '.join(f"{k} = ?" for k in update_fields.keys())
            values = list(update_fields.values()) + [username]
            if source_type:
                values.append(self._normalize_source_type(source_type))
                await db.execute(
                    f"UPDATE models SET {placeholders} WHERE username = ? AND source_type = ?",
                    values,
                )
            else:
                await db.execute(
                    f"UPDATE models SET {placeholders} WHERE username = ?",
                    values,
                )
            await db.commit()

        if is_online:
            try:
                await self.note_last_seen_online(
                    username,
                    seen_at=now,
                    source_type=source_type,
                )
            except Exception:
                pass

        try:
            await self.observe_live_status(
                username,
                is_online=bool(is_online),
                source_type=source_type,
                seen_at=now,
                detection="monitor",
            )
        except Exception:
            pass

    # Brief false-offline blips (API/CDN failures) reopen the prior session.
    LIVE_SESSION_GAP_MERGE_SECONDS = 300

    async def observe_live_status(
        self,
        channel_username: str,
        *,
        is_online: bool,
        source_type: Optional[str] = None,
        profile_username: Optional[str] = None,
        seen_at: Optional[int] = None,
        detection: str = "monitor",
        gap_merge_seconds: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Record online/offline transitions into live_sessions.

        Open sessions have ``ended_at IS NULL``. Short offline gaps reopen the
        previous row instead of fragmenting one broadcast into many sessions.
        """
        await self.initialize()
        channel = str(channel_username or "").strip()
        if not channel:
            return None
        source = self._normalize_source_type(source_type) if source_type else "chaturbate"
        now = int(seen_at or datetime.now().timestamp())
        if now <= 0:
            now = int(datetime.now().timestamp())
        gap = int(
            gap_merge_seconds
            if gap_merge_seconds is not None
            else self.LIVE_SESSION_GAP_MERGE_SECONDS
        )
        gap = max(60, min(gap, 3600))

        profile = str(profile_username or "").strip() or None
        if not profile:
            try:
                names = await self.media_profile_usernames_for_channel(channel, source)
                if names:
                    profile = names[0]
            except Exception:
                profile = None
        if not profile:
            profile = channel

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM live_sessions
                WHERE lower(channel_username) = lower(?)
                  AND source_type = ?
                  AND ended_at IS NULL
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (channel, source),
            )
            open_row = await cursor.fetchone()

            if is_online:
                if open_row:
                    await db.execute(
                        """
                        UPDATE live_sessions
                        SET last_seen_at = CASE
                                WHEN last_seen_at IS NULL OR last_seen_at < ? THEN ?
                                ELSE last_seen_at
                            END,
                            profile_username = COALESCE(?, profile_username)
                        WHERE id = ?
                        """,
                        (now, now, profile, int(open_row["id"])),
                    )
                    await db.commit()
                    return dict(open_row)

                cursor = await db.execute(
                    """
                    SELECT * FROM live_sessions
                    WHERE lower(channel_username) = lower(?)
                      AND source_type = ?
                      AND ended_at IS NOT NULL
                    ORDER BY ended_at DESC
                    LIMIT 1
                    """,
                    (channel, source),
                )
                last_closed = await cursor.fetchone()
                if last_closed and (now - int(last_closed["ended_at"] or 0)) <= gap:
                    await db.execute(
                        """
                        UPDATE live_sessions
                        SET ended_at = NULL,
                            last_seen_at = ?,
                            profile_username = COALESCE(?, profile_username),
                            detection = COALESCE(?, detection)
                        WHERE id = ?
                        """,
                        (now, profile, detection, int(last_closed["id"])),
                    )
                    await db.commit()
                    merged = dict(last_closed)
                    merged["ended_at"] = None
                    merged["last_seen_at"] = now
                    return merged

                await db.execute(
                    """
                    INSERT INTO live_sessions (
                        channel_username, source_type, profile_username,
                        started_at, ended_at, last_seen_at, detection
                    )
                    VALUES (?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (channel, source, profile, now, now, detection),
                )
                await db.commit()
                cursor = await db.execute(
                    """
                    SELECT * FROM live_sessions
                    WHERE lower(channel_username) = lower(?)
                      AND source_type = ?
                      AND ended_at IS NULL
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (channel, source),
                )
                row = await cursor.fetchone()
                return dict(row) if row else None

            # Offline: close any open session at last_seen (not "now") so the
            # bar ends when we last confirmed live, not at the first miss.
            if open_row:
                end_at = int(open_row["last_seen_at"] or open_row["started_at"] or now)
                if end_at > now:
                    end_at = now
                await db.execute(
                    """
                    UPDATE live_sessions
                    SET ended_at = ?,
                        profile_username = COALESCE(?, profile_username)
                    WHERE id = ?
                    """,
                    (end_at, profile, int(open_row["id"])),
                )
                await db.commit()
                closed = dict(open_row)
                closed["ended_at"] = end_at
                return closed
            return None

    async def get_live_sessions_in_range(
        self,
        *,
        from_ts: int,
        to_ts: int,
        profile_usernames: Optional[List[str]] = None,
        limit: int = 5000,
    ) -> List[Dict[str, Any]]:
        """Return live sessions overlapping [from_ts, to_ts] (unix seconds)."""
        await self.initialize()
        start = int(from_ts)
        end = int(to_ts)
        if end < start:
            start, end = end, start
        limit = max(1, min(int(limit or 5000), 20000))
        names = sorted({
            str(name).strip()
            for name in (profile_usernames or [])
            if str(name or "").strip()
        })

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            # Overlap: started_at <= end AND coalesce(ended_at, now) >= start
            now = int(datetime.now().timestamp())
            if names:
                placeholders = ",".join("?" for _ in names)
                # Match profile card OR channel username (legacy / unlinked).
                lower_names = [n.lower() for n in names]
                cursor = await db.execute(
                    f"""
                    SELECT * FROM live_sessions
                    WHERE started_at <= ?
                      AND COALESCE(ended_at, ?) >= ?
                      AND (
                        lower(profile_username) IN ({placeholders})
                        OR lower(channel_username) IN ({placeholders})
                      )
                    ORDER BY started_at ASC
                    LIMIT ?
                    """,
                    (end, now, start, *lower_names, *lower_names, limit),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT * FROM live_sessions
                    WHERE started_at <= ?
                      AND COALESCE(ended_at, ?) >= ?
                    ORDER BY started_at ASC
                    LIMIT ?
                    """,
                    (end, now, start, limit),
                )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_recording_intervals_in_range(
        self,
        *,
        from_ts: int,
        to_ts: int,
        usernames: Optional[List[str]] = None,
        limit: int = 5000,
    ) -> List[Dict[str, Any]]:
        """Recording coverage intervals from live rows + history tombstones."""
        await self.initialize()
        start = int(from_ts)
        end = int(to_ts)
        if end < start:
            start, end = end, start
        limit = max(1, min(int(limit or 5000), 20000))
        names = sorted({
            str(name).strip()
            for name in (usernames or [])
            if str(name or "").strip()
        })
        lower_names = [n.lower() for n in names]
        out: List[Dict[str, Any]] = []

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row

            def _append_rows(rows, *, location: str) -> None:
                for row in rows:
                    data = dict(row)
                    created = int(data.get("created_at") or 0)
                    duration = int(data.get("duration_seconds") or 0)
                    if created <= 0:
                        continue
                    now_ts = int(datetime.now().timestamp())
                    if duration > 0:
                        seg_end = created + duration
                    elif location == "vps" and (now_ts - created) <= 6 * 3600:
                        # Likely still recording / not probed yet.
                        seg_end = now_ts
                    else:
                        # Unknown length: keep a short stub so the chart
                        # does not paint a multi-day false-positive bar.
                        seg_end = created + 60
                    if seg_end < start or created > end:
                        continue
                    out.append({
                        "username": data.get("username"),
                        "recordingId": data.get("recording_id"),
                        "filename": data.get("filename"),
                        "start": created,
                        "end": seg_end,
                        "location": location,
                        "fileSize": data.get("file_size") or data.get("mp4_size"),
                        "mediaKind": data.get("media_kind") or "recording",
                    })

            if lower_names:
                placeholders = ",".join("?" for _ in lower_names)
                cursor = await db.execute(
                    f"""
                    SELECT * FROM recordings
                    WHERE lower(username) IN ({placeholders})
                      AND created_at <= ?
                      AND (
                        (COALESCE(duration_seconds, 0) > 0
                          AND (created_at + duration_seconds) >= ?)
                        OR (COALESCE(duration_seconds, 0) <= 0 AND created_at >= ?)
                        OR COALESCE(duration_seconds, 0) <= 0
                      )
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (*lower_names, end, start, start - 86400 * 2, limit),
                )
                _append_rows(await cursor.fetchall(), location="vps")

                cursor = await db.execute(
                    f"""
                    SELECT * FROM recording_history
                    WHERE lower(username) IN ({placeholders})
                      AND created_at IS NOT NULL
                      AND created_at <= ?
                      AND (
                        (COALESCE(duration_seconds, 0) > 0
                          AND (created_at + duration_seconds) >= ?)
                        OR COALESCE(duration_seconds, 0) <= 0
                      )
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (*lower_names, end, start, limit),
                )
                _append_rows(await cursor.fetchall(), location="history")
            else:
                cursor = await db.execute(
                    """
                    SELECT * FROM recordings
                    WHERE created_at <= ?
                      AND (
                        (COALESCE(duration_seconds, 0) > 0
                          AND (created_at + duration_seconds) >= ?)
                        OR COALESCE(duration_seconds, 0) <= 0
                      )
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (end, start, limit),
                )
                _append_rows(await cursor.fetchall(), location="vps")

                cursor = await db.execute(
                    """
                    SELECT * FROM recording_history
                    WHERE created_at IS NOT NULL
                      AND created_at <= ?
                      AND (
                        (COALESCE(duration_seconds, 0) > 0
                          AND (created_at + duration_seconds) >= ?)
                        OR COALESCE(duration_seconds, 0) <= 0
                      )
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (end, start, limit),
                )
                _append_rows(await cursor.fetchall(), location="history")

        out.sort(key=lambda item: (str(item.get("username") or ""), int(item.get("start") or 0)))
        return out[:limit]

    async def get_model(
        self, username: str, source_type: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Fetch information for a model"""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            if source_type:
                cursor = await db.execute(
                    "SELECT * FROM models WHERE username = ? AND source_type = ?",
                    (username, self._normalize_source_type(source_type)),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT * FROM models
                    WHERE username = ?
                    ORDER BY CASE WHEN source_type = 'chaturbate' THEN 0 ELSE 1 END, source_type
                    LIMIT 1
                    """,
                    (username,),
                )
            row = await cursor.fetchone()

            if row:
                return dict(row)
            return None

    async def get_all_models(self) -> List[Dict[str, Any]]:
        """Fetch all models"""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM models ORDER BY username"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_models_for_auto_record(self) -> List[Dict[str, Any]]:
        """Fetch models with auto-record enabled"""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM models WHERE auto_record = 1 ORDER BY username"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_model(self, username: str, source_type: Optional[str] = None):
        """Delete a model"""
        await self.initialize()

        async with self._connect() as db:
            if source_type:
                await db.execute(
                    "DELETE FROM models WHERE username = ? AND source_type = ?",
                    (username, self._normalize_source_type(source_type)),
                )
            else:
                await db.execute("DELETE FROM models WHERE username = ?", (username,))
            await db.commit()

        logger.info("Model deleted", username=username, source_type=source_type)

    @staticmethod
    def _json_list(value: Any) -> str:
        if value is None:
            return "[]"
        if isinstance(value, str):
            values = [line.strip() for line in value.splitlines() if line.strip()]
        elif isinstance(value, list):
            values = [str(item).strip() for item in value if str(item).strip()]
        else:
            values = []
        return json.dumps(values, ensure_ascii=False)

    @staticmethod
    def _decode_json_list(value: Any) -> List[str]:
        if not value:
            return []
        if isinstance(value, list):
            return [str(item) for item in value]
        try:
            decoded = json.loads(value)
            if isinstance(decoded, list):
                return [str(item) for item in decoded if str(item).strip()]
        except Exception:
            pass
        return [line.strip() for line in str(value).splitlines() if line.strip()]

    def _format_media_profile_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(row)
        data["social_urls"] = self._decode_json_list(data.get("social_urls"))
        data["stream_urls"] = self._decode_json_list(data.get("stream_urls"))
        data["profile_urls"] = self._decode_json_list(data.get("profile_urls"))
        return data

    def _format_media_profile_source_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(row)
        data["auto_record"] = bool(data.get("auto_record"))
        try:
            data["retention_days"] = int(data.get("retention_days") if data.get("retention_days") is not None else 30)
        except (TypeError, ValueError):
            data["retention_days"] = 30
        raw_quota = data.get("monthly_quota_gb")
        if raw_quota is None or raw_quota == "":
            data["monthly_quota_gb"] = None
        else:
            try:
                data["monthly_quota_gb"] = int(raw_quota)
            except (TypeError, ValueError):
                data["monthly_quota_gb"] = None
        return data

    async def touch_media_profile_last_seen(
        self,
        username: str,
        seen_at: Optional[int] = None,
    ) -> int:
        """Persist last-seen-online for a media profile without wiping other fields."""
        await self.initialize()
        username = str(username or "").strip()
        if not username:
            raise ValueError("username required")
        now = int(seen_at or datetime.now().timestamp())
        if now <= 0:
            now = int(datetime.now().timestamp())

        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO media_profiles (username, created_at, updated_at, last_seen_online_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    last_seen_online_at = CASE
                        WHEN media_profiles.last_seen_online_at IS NULL
                          OR media_profiles.last_seen_online_at < excluded.last_seen_online_at
                        THEN excluded.last_seen_online_at
                        ELSE media_profiles.last_seen_online_at
                    END,
                    updated_at = excluded.updated_at
                """,
                (username, now, now, now),
            )
            await db.commit()
        return now

    async def media_profile_usernames_for_channel(
        self,
        channel_username: str,
        source_type: Optional[str] = None,
    ) -> List[str]:
        """Map a stream channel identity to local Media profile folder name(s)."""
        await self.initialize()
        channel = str(channel_username or "").strip()
        if not channel:
            return []
        source = self._normalize_source_type(source_type) if source_type else None
        async with self._connect() as db:
            if source:
                cursor = await db.execute(
                    """
                    SELECT DISTINCT profile_username
                    FROM media_profile_sources
                    WHERE lower(channel_username) = lower(?)
                      AND source_type = ?
                    """,
                    (channel, source),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT DISTINCT profile_username
                    FROM media_profile_sources
                    WHERE lower(channel_username) = lower(?)
                    """,
                    (channel,),
                )
            rows = await cursor.fetchall()
        names = [str(row[0]).strip() for row in rows if row and row[0]]
        return names

    async def note_last_seen_online(
        self,
        username: str,
        *,
        seen_at: Optional[int] = None,
        source_type: Optional[str] = None,
    ) -> int:
        """Bump last-seen without requiring the Media page to be open.

        Updates:
        - media_profiles for the given username and any Media profile that
          lists this username as a channel source
        - followed_models.last_seen_online_at (monotonic) when a matching
          followed row exists — does not flip is_online
        """
        await self.initialize()
        channel = str(username or "").strip()
        if not channel:
            raise ValueError("username required")
        stamp = int(seen_at or datetime.now().timestamp())
        if stamp <= 0:
            stamp = int(datetime.now().timestamp())
        source = self._normalize_source_type(source_type) if source_type else None

        profile_names = {channel}
        for name in await self.media_profile_usernames_for_channel(channel, source):
            if name:
                profile_names.add(name)

        for profile_name in profile_names:
            await self.touch_media_profile_last_seen(profile_name, stamp)

        async with self._connect() as db:
            if source:
                await db.execute(
                    """
                    UPDATE followed_models
                    SET last_seen_online_at = CASE
                            WHEN last_seen_online_at IS NULL OR last_seen_online_at < ?
                            THEN ?
                            ELSE last_seen_online_at
                        END,
                        synced_at = ?
                    WHERE lower(username) = lower(?) AND source_type = ?
                    """,
                    (stamp, stamp, stamp, channel, source),
                )
            else:
                await db.execute(
                    """
                    UPDATE followed_models
                    SET last_seen_online_at = CASE
                            WHEN last_seen_online_at IS NULL OR last_seen_online_at < ?
                            THEN ?
                            ELSE last_seen_online_at
                        END,
                        synced_at = ?
                    WHERE lower(username) = lower(?)
                    """,
                    (stamp, stamp, stamp, channel),
                )
            await db.commit()
        return stamp

    async def get_media_profile(self, username: str) -> Optional[Dict[str, Any]]:
        """Fetch the local card for a media profile."""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM media_profiles WHERE username = ? LIMIT 1",
                (username,),
            )
            row = await cursor.fetchone()
            return self._format_media_profile_row(dict(row)) if row else None

    async def get_all_media_profiles(self) -> List[Dict[str, Any]]:
        """Fetch all local media cards."""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM media_profiles ORDER BY username")
            rows = await cursor.fetchall()
            return [self._format_media_profile_row(dict(row)) for row in rows]

    async def upsert_media_profile(self, username: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Create or update enriched info for a media profile."""
        await self.initialize()

        now = int(datetime.now().timestamp())
        age = data.get("age")
        try:
            age = int(age) if age not in (None, "") else None
        except (TypeError, ValueError):
            age = None

        payload = {
            "display_name": data.get("display_name"),
            "first_name": data.get("first_name"),
            "last_name": data.get("last_name"),
            "age": age,
            "birth_date": data.get("birth_date"),
            "profile_image_url": data.get("profile_image_url"),
            "profile_image_source_url": data.get("profile_image_source_url"),
            "profile_image_path": data.get("profile_image_path"),
            "address": data.get("address"),
            "city": data.get("city"),
            "region": data.get("region"),
            "postal_code": data.get("postal_code"),
            "country": data.get("country"),
            "aliases": data.get("aliases"),
            "tags": data.get("tags"),
            "notes": data.get("notes"),
            "social_urls": self._json_list(data.get("social_urls")),
            "stream_urls": self._json_list(data.get("stream_urls")),
            "profile_urls": self._json_list(data.get("profile_urls")),
        }

        async with self._connect() as db:
            await db.execute("""
                INSERT INTO media_profiles (
                    username, display_name, first_name, last_name, age,
                    birth_date, profile_image_url, profile_image_source_url, profile_image_path,
                    address, city, region, postal_code, country,
                    aliases, tags, notes, social_urls, stream_urls, profile_urls,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    display_name = ?,
                    first_name = ?,
                    last_name = ?,
                    age = ?,
                    birth_date = ?,
                    profile_image_url = ?,
                    profile_image_source_url = ?,
                    profile_image_path = ?,
                    address = ?,
                    city = ?,
                    region = ?,
                    postal_code = ?,
                    country = ?,
                    aliases = ?,
                    tags = ?,
                    notes = ?,
                    social_urls = ?,
                    stream_urls = ?,
                    profile_urls = ?,
                    updated_at = ?
            """, (
                username,
                payload["display_name"],
                payload["first_name"],
                payload["last_name"],
                payload["age"],
                payload["birth_date"],
                payload["profile_image_url"],
                payload["profile_image_source_url"],
                payload["profile_image_path"],
                payload["address"],
                payload["city"],
                payload["region"],
                payload["postal_code"],
                payload["country"],
                payload["aliases"],
                payload["tags"],
                payload["notes"],
                payload["social_urls"],
                payload["stream_urls"],
                payload["profile_urls"],
                now,
                now,
                payload["display_name"],
                payload["first_name"],
                payload["last_name"],
                payload["age"],
                payload["birth_date"],
                payload["profile_image_url"],
                payload["profile_image_source_url"],
                payload["profile_image_path"],
                payload["address"],
                payload["city"],
                payload["region"],
                payload["postal_code"],
                payload["country"],
                payload["aliases"],
                payload["tags"],
                payload["notes"],
                payload["social_urls"],
                payload["stream_urls"],
                payload["profile_urls"],
                now,
            ))
            await db.commit()

        profile = await self.get_media_profile(username)
        return profile or {"username": username}

    async def rename_media_profile(self, old_username: str, new_username: str) -> bool:
        """Rename local media-profile metadata without merging conflicting rows."""
        await self.initialize()
        old_username = str(old_username or "").strip()
        new_username = str(new_username or "").strip()
        if not old_username or not new_username or old_username == new_username:
            return False

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT 1 FROM media_profiles WHERE username = ? LIMIT 1",
                (old_username,),
            )
            if not await cursor.fetchone():
                return False
            cursor = await db.execute(
                "SELECT 1 FROM media_profiles WHERE username = ? LIMIT 1",
                (new_username,),
            )
            if await cursor.fetchone():
                return False
            now = int(datetime.now().timestamp())
            await db.execute(
                "UPDATE media_profiles SET username = ?, updated_at = ? WHERE username = ?",
                (new_username, now, old_username),
            )
            await db.execute(
                "UPDATE media_profile_sources SET profile_username = ? WHERE profile_username = ?",
                (new_username, old_username),
            )
            await db.commit()
            return True

    async def get_media_profile_sources(self, profile_username: str) -> List[Dict[str, Any]]:
        """List recording sources linked to a media profile."""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM media_profile_sources
                WHERE profile_username = ?
                ORDER BY source_type, channel_username
                """,
                (profile_username,),
            )
            rows = await cursor.fetchall()
            return [self._format_media_profile_source_row(dict(row)) for row in rows]

    async def get_all_media_profile_sources(self) -> List[Dict[str, Any]]:
        """List all Media recording sources."""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM media_profile_sources
                ORDER BY profile_username, source_type, channel_username
                """
            )
            rows = await cursor.fetchall()
            return [self._format_media_profile_source_row(dict(row)) for row in rows]

    async def get_media_profile_sources_for_auto_record(self) -> List[Dict[str, Any]]:
        """List Media sources with automatic recording enabled."""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM media_profile_sources
                WHERE auto_record = 1
                ORDER BY profile_username, source_type, channel_username
                """
            )
            rows = await cursor.fetchall()
            return [self._format_media_profile_source_row(dict(row)) for row in rows]

    async def upsert_media_profile_source(
        self,
        profile_username: str,
        source_type: str,
        channel_username: str,
        channel_url: Optional[str] = None,
        auto_record: bool = True,
        record_quality: str = "best",
        retention_days: int = 30,
        record_path: Optional[str] = None,
        monthly_quota_gb: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create or update a stream source for a media profile."""
        await self.initialize()

        now = int(datetime.now().timestamp())
        source_type = self._normalize_source_type(source_type)
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO media_profile_sources (
                    profile_username, source_type, channel_username, channel_url,
                    auto_record, record_quality, monthly_quota_gb, retention_days,
                    record_path, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_username, source_type, channel_username) DO UPDATE SET
                    channel_url = excluded.channel_url,
                    auto_record = excluded.auto_record,
                    record_quality = excluded.record_quality,
                    monthly_quota_gb = excluded.monthly_quota_gb,
                    retention_days = excluded.retention_days,
                    record_path = excluded.record_path,
                    updated_at = excluded.updated_at
                """,
                (
                    profile_username,
                    source_type,
                    channel_username,
                    channel_url,
                    int(bool(auto_record)),
                    record_quality,
                    monthly_quota_gb,
                    int(retention_days),
                    record_path,
                    now,
                    now,
                ),
            )
            await db.commit()

        sources = await self.get_media_profile_sources(profile_username)
        for source in sources:
            if (
                source.get("source_type") == source_type
                and source.get("channel_username") == channel_username
            ):
                return source
        return {
            "profile_username": profile_username,
            "source_type": source_type,
            "channel_username": channel_username,
            "channel_url": channel_url,
            "auto_record": bool(auto_record),
            "record_quality": record_quality,
            "monthly_quota_gb": monthly_quota_gb,
            "retention_days": int(retention_days),
            "record_path": record_path,
        }

    async def replace_media_profile_sources(
        self,
        profile_username: str,
        sources: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Replace all sources for a media profile."""
        await self.initialize()

        now = int(datetime.now().timestamp())
        async with self._connect() as db:
            await db.execute(
                "DELETE FROM media_profile_sources WHERE profile_username = ?",
                (profile_username,),
            )
            for source in sources:
                monthly_quota = source.get("monthly_quota_gb")
                if monthly_quota is None or monthly_quota == "":
                    monthly_quota = None
                else:
                    try:
                        monthly_quota = int(monthly_quota)
                    except (TypeError, ValueError):
                        monthly_quota = None
                await db.execute(
                    """
                    INSERT INTO media_profile_sources (
                        profile_username, source_type, channel_username, channel_url,
                        auto_record, record_quality, monthly_quota_gb, retention_days,
                        record_path, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        profile_username,
                        self._normalize_source_type(source.get("source_type")),
                        source.get("channel_username"),
                        source.get("channel_url"),
                        int(bool(source.get("auto_record"))),
                        source.get("record_quality") or "best",
                        monthly_quota,
                        int(source.get("retention_days") if source.get("retention_days") is not None else 30),
                        source.get("record_path"),
                        now,
                        now,
                    ),
                )
            await db.commit()

        return await self.get_media_profile_sources(profile_username)

    async def set_media_profile_auto_record(
        self,
        profile_username: str,
        auto_record: bool,
        *,
        source_type: Optional[str] = None,
        channel_username: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Enable or suspend recording for profile sources.

        When source_type is set, only that platform is toggled. Enabling a
        specific source also clears auto_record on the profile's other sources
        so a Twitch enable cannot leave a leftover Chaturbate row as "primary".
        """
        await self.initialize()

        now = int(datetime.now().timestamp())
        wanted_source = self._normalize_source_type(source_type) if source_type else ""
        wanted_channel = str(channel_username or "").strip()
        async with self._connect() as db:
            if wanted_source:
                if auto_record:
                    await db.execute(
                        """
                        UPDATE media_profile_sources
                        SET auto_record = 0, updated_at = ?
                        WHERE profile_username = ?
                        """,
                        (now, profile_username),
                    )
                if wanted_channel:
                    await db.execute(
                        """
                        UPDATE media_profile_sources
                        SET auto_record = ?, updated_at = ?
                        WHERE profile_username = ?
                          AND source_type = ?
                          AND lower(channel_username) = lower(?)
                        """,
                        (
                            int(bool(auto_record)),
                            now,
                            profile_username,
                            wanted_source,
                            wanted_channel,
                        ),
                    )
                else:
                    await db.execute(
                        """
                        UPDATE media_profile_sources
                        SET auto_record = ?, updated_at = ?
                        WHERE profile_username = ?
                          AND source_type = ?
                        """,
                        (int(bool(auto_record)), now, profile_username, wanted_source),
                    )
            else:
                await db.execute(
                    """
                    UPDATE media_profile_sources
                    SET auto_record = ?, updated_at = ?
                    WHERE profile_username = ?
                    """,
                    (int(bool(auto_record)), now, profile_username),
                )
            await db.commit()

        return await self.get_media_profile_sources(profile_username)

    async def delete_media_profile_sources(self, profile_username: str) -> int:
        """Delete stream sources linked to a media profile."""
        await self.initialize()

        async with self._connect() as db:
            cursor = await db.execute(
                "DELETE FROM media_profile_sources WHERE profile_username = ?",
                (profile_username,),
            )
            await db.commit()
            return cursor.rowcount or 0

    async def delete_media_profile(self, username: str) -> None:
        """Delete the local card for a media profile."""
        await self.initialize()

        async with self._connect() as db:
            await db.execute("DELETE FROM media_profile_sources WHERE profile_username = ?", (username,))
            await db.execute("DELETE FROM media_profiles WHERE username = ?", (username,))
            await db.commit()

    async def delete_recordings_for_username(self, username: str) -> int:
        """Delete all DB recordings associated with a profile."""
        await self.initialize()

        async with self._connect() as db:
            cursor = await db.execute("DELETE FROM recordings WHERE username = ?", (username,))
            await db.commit()
            return cursor.rowcount or 0

    async def update_all_models_retention_days(self, retention_days: int) -> int:
        """Apply one retention window to every tracked model."""
        await self.initialize()

        now = int(datetime.now().timestamp())
        async with self._connect() as db:
            cursor = await db.execute("""
                UPDATE models
                SET retention_days = ?, updated_at = ?
            """, (retention_days, now))
            await db.commit()
            return cursor.rowcount or 0

    async def add_or_update_recording(
        self,
        username: str,
        filename: str,
        file_path: str,
        file_size: int,
        recording_id: Optional[str] = None,
        duration_seconds: int = 0,
        resolution: Optional[str] = None,
        fps: Optional[float] = None,
        bitrate: Optional[int] = None,
        thumbnail_path: Optional[str] = None,
        mp4_path: Optional[str] = None,
        mp4_size: Optional[int] = None,
        is_converted: bool = False,
        media_kind: Optional[str] = None,
        title: Optional[str] = None,
        import_status: Optional[str] = None,
        import_error: Optional[str] = None,
        source_mtime: Optional[int] = None,
        playable_path: Optional[str] = None,
        playable_size: Optional[int] = None,
        protected_from_retention: Optional[bool] = None,
        created_at: Optional[int] = None,
        replace_media_paths: bool = False,
    ):
        """Add or update a recording"""
        await self.initialize()

        now = int(datetime.now().timestamp())
        created_at_update = created_at
        created_at = created_at or now
        media_kind = (media_kind or "recording").strip().lower() or "recording"
        protected_value = 1 if protected_from_retention else 0
        resolution_value = str(resolution or "").strip() or None
        try:
            fps_value = float(fps) if fps is not None and float(fps) > 0 else None
        except (TypeError, ValueError):
            fps_value = None
        try:
            bitrate_value = int(bitrate) if bitrate is not None and int(bitrate) > 0 else None
        except (TypeError, ValueError):
            bitrate_value = None

        # Generate recording_id if not provided
        if not recording_id:
            recording_id = f"{username}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        async with self._connect() as db:
            await db.execute("""
                INSERT INTO recordings (
                    username, recording_id, filename, file_path, file_size,
                    duration_seconds, resolution, fps, bitrate, thumbnail_path, mp4_path, mp4_size, is_converted,
                    media_kind, title, import_status, import_error, source_mtime,
                    playable_path, playable_size, protected_from_retention, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username, filename) DO UPDATE SET
                    file_path = ?,
                    file_size = ?,
                    duration_seconds = ?,
                    resolution = COALESCE(?, resolution),
                    fps = COALESCE(?, fps),
                    bitrate = COALESCE(?, bitrate),
                    thumbnail_path = CASE WHEN ? THEN ? ELSE COALESCE(?, thumbnail_path) END,
                    mp4_path = CASE WHEN ? THEN ? ELSE COALESCE(?, mp4_path) END,
                    mp4_size = CASE WHEN ? THEN ? ELSE COALESCE(?, mp4_size) END,
                    is_converted = ?,
                    media_kind = COALESCE(?, media_kind),
                    title = COALESCE(?, title),
                    import_status = COALESCE(?, import_status),
                    import_error = ?,
                    source_mtime = COALESCE(?, source_mtime),
                    playable_path = CASE WHEN ? THEN ? ELSE COALESCE(?, playable_path) END,
                    playable_size = CASE WHEN ? THEN ? ELSE COALESCE(?, playable_size) END,
                    protected_from_retention = ?,
                    created_at = COALESCE(?, created_at)
            """, (
                username, recording_id, filename, file_path, file_size,
                duration_seconds, resolution_value, fps_value, bitrate_value, thumbnail_path, mp4_path, mp4_size, is_converted,
                media_kind, title, import_status, import_error, source_mtime,
                playable_path, playable_size, protected_value, created_at,
                file_path, file_size, duration_seconds, resolution_value, fps_value, bitrate_value,
                replace_media_paths, thumbnail_path, thumbnail_path,
                replace_media_paths, mp4_path, mp4_path,
                replace_media_paths, mp4_size, mp4_size,
                is_converted,
                media_kind, title, import_status, import_error, source_mtime,
                replace_media_paths, playable_path, playable_path,
                replace_media_paths, playable_size, playable_size,
                protected_value, created_at_update
            ))
            await db.commit()

    async def set_recording_stream_meta(
        self,
        username: str,
        filename: str,
        *,
        resolution: Optional[str] = None,
        fps: Optional[float] = None,
        bitrate: Optional[int] = None,
    ) -> None:
        """Persist probed resolution / fps / bitrate without rewriting other fields."""
        resolution_value = str(resolution or "").strip() or None
        try:
            fps_value = float(fps) if fps is not None and float(fps) > 0 else None
        except (TypeError, ValueError):
            fps_value = None
        try:
            bitrate_value = int(bitrate) if bitrate is not None and int(bitrate) > 0 else None
        except (TypeError, ValueError):
            bitrate_value = None
        if not resolution_value and fps_value is None and bitrate_value is None:
            return
        await self.initialize()
        sets: list[str] = []
        values: list[Any] = []
        if resolution_value:
            sets.append("resolution = ?")
            values.append(resolution_value)
        if fps_value is not None:
            sets.append("fps = ?")
            values.append(fps_value)
        if bitrate_value is not None:
            sets.append("bitrate = ?")
            values.append(bitrate_value)
        values.extend([username, filename])
        async with self._connect() as db:
            await db.execute(
                f"""
                UPDATE recordings
                SET {", ".join(sets)}
                WHERE username = ? AND filename = ?
                """,
                values,
            )
            await db.commit()

    async def set_recording_resolution(
        self,
        username: str,
        filename: str,
        resolution: str,
    ) -> None:
        """Persist probed WxH resolution without rewriting other recording fields."""
        await self.set_recording_stream_meta(username, filename, resolution=resolution)
    async def get_recordings(self, username: str) -> List[Dict[str, Any]]:
        """Fetch recordings for a model"""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM recordings
                WHERE username = ?
                ORDER BY created_at DESC
                """,
                (username,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_recordings_for_usernames(self, usernames: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch recordings for multiple profiles in batches."""
        await self.initialize()

        clean_usernames = sorted({str(username) for username in usernames if str(username or "").strip()})
        grouped: Dict[str, List[Dict[str, Any]]] = {username: [] for username in clean_usernames}
        if not clean_usernames:
            return grouped

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            for start in range(0, len(clean_usernames), 500):
                chunk = clean_usernames[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                cursor = await db.execute(
                    f"""
                    SELECT * FROM recordings
                    WHERE username IN ({placeholders})
                    ORDER BY username, created_at DESC
                    """,
                    chunk,
                )
                rows = await cursor.fetchall()
                for row in rows:
                    data = dict(row)
                    grouped.setdefault(data.get("username"), []).append(data)
        return grouped

    async def get_import_recordings(self) -> List[Dict[str, Any]]:
        """Fetch imported media."""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM recordings
                WHERE media_kind = 'import'
                ORDER BY created_at DESC
                """
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_recordings_count(self, username: str) -> int:
        """Count recordings (converted or not)"""
        await self.initialize()

        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM recordings WHERE username = ?",
                (username,)
            )
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def archive_recording_row(
        self,
        row: dict,
        *,
        final_status: str,
        reason: str,
        extra: Optional[dict] = None,
    ) -> None:
        """Persist a tombstone before removing a live recordings row."""
        await self.initialize()
        now = int(datetime.now().timestamp())
        extra_json = None
        if extra:
            try:
                extra_json = json.dumps(extra, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                extra_json = None
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO recording_history (
                    recording_id, username, filename, file_path, file_size,
                    mp4_path, mp4_size, duration_seconds, media_kind,
                    final_status, reason, created_at, archived_at, extra_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("recording_id"),
                    row.get("username") or "",
                    row.get("filename"),
                    row.get("file_path"),
                    row.get("file_size"),
                    row.get("mp4_path"),
                    row.get("mp4_size"),
                    row.get("duration_seconds"),
                    row.get("media_kind"),
                    final_status,
                    reason,
                    row.get("created_at"),
                    now,
                    extra_json,
                ),
            )
            await db.commit()

    async def archive_and_delete_recording(
        self,
        username: str,
        filename: str,
        *,
        final_status: str,
        reason: str,
        extra: Optional[dict] = None,
    ) -> bool:
        """Archive then delete by username+filename. Returns True if a row existed."""
        await self.initialize()
        recordings = await self.get_recordings(username)
        row = next((item for item in recordings if item.get("filename") == filename), None)
        if row:
            await self.archive_recording_row(
                row,
                final_status=final_status,
                reason=reason,
                extra=extra,
            )
        await self.delete_recording(username, filename)
        return row is not None

    async def archive_and_delete_recording_by_id(
        self,
        recording_id: str,
        *,
        final_status: str,
        reason: str,
        extra: Optional[dict] = None,
    ) -> bool:
        """Archive then delete by recording_id. Returns True if a row existed."""
        await self.initialize()
        row = await self.get_recording_by_id(recording_id)
        if row:
            await self.archive_recording_row(
                row,
                final_status=final_status,
                reason=reason,
                extra=extra,
            )
        await self.delete_recording_by_id(recording_id)
        return row is not None

    async def get_recording_history(
        self,
        username: Optional[str] = None,
        *,
        limit: int = 50,
    ) -> list[dict]:
        await self.initialize()
        limit = max(1, min(int(limit or 50), 500))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            if username:
                cursor = await db.execute(
                    """
                    SELECT * FROM recording_history
                    WHERE username = ?
                    ORDER BY archived_at DESC
                    LIMIT ?
                    """,
                    (username, limit),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT * FROM recording_history
                    ORDER BY archived_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_recording(self, username: str, filename: str):
        """Delete a recording from the database"""
        await self.initialize()

        async with self._connect() as db:
            await db.execute(
                "DELETE FROM recordings WHERE username = ? AND filename = ?",
                (username, filename)
            )
            await db.commit()

    async def delete_recording_by_id(self, recording_id: str):
        """Delete a recording from the DB by its stable ID."""
        await self.initialize()

        async with self._connect() as db:
            await db.execute(
                "DELETE FROM recordings WHERE recording_id = ?",
                (recording_id,)
            )
            await db.commit()

    async def delete_playback_position(self, recording_id: str):
        """Delete the playback position associated with a media item."""
        await self.initialize()

        async with self._connect() as db:
            await db.execute(
                "DELETE FROM playback_positions WHERE recording_id = ?",
                (recording_id,)
            )
            await db.commit()

    async def mark_conversion_failed(self, username: str, filename: str, error: str):
        """Increment the failure counter and store the error for a recording."""
        await self.initialize()
        now = int(datetime.now().timestamp())
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE recordings
                SET conversion_attempts = COALESCE(conversion_attempts, 0) + 1,
                    conversion_error = ?,
                    last_conversion_attempt = ?
                WHERE username = ? AND filename = ?
                """,
                (error[:500], now, username, filename)
            )
            await db.commit()

    async def reset_conversion_failure(self, recording_id: str) -> bool:
        """Reset the failure counter (for manual retry). Returns True if found."""
        await self.initialize()
        async with self._connect() as db:
            cursor = await db.execute(
                """
                UPDATE recordings
                SET conversion_attempts = 0,
                    conversion_error = NULL,
                    last_conversion_attempt = NULL
                WHERE recording_id = ?
                """,
                (recording_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_recording_by_id(self, recording_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a recording by its recording_id."""
        await self.initialize()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM recordings WHERE recording_id = ? LIMIT 1",
                (recording_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    # ==========================================
    # Chaturbate Auth CRUD
    # ==========================================

    async def save_auth_state(
        self,
        username: str,
        password_hash: str,
        is_logged_in: bool = False,
        session_cookies: Optional[str] = None,
        cf_clearance: Optional[str] = None,
        csrf_token: Optional[str] = None,
        last_login_at: Optional[int] = None,
        last_error: Optional[str] = None
    ):
        """Save or update Chaturbate auth state"""
        await self.initialize()
        now = int(datetime.now().timestamp())

        async with self._connect() as db:
            await db.execute("""
                INSERT INTO chaturbate_auth (
                    id, username, password_hash, is_logged_in,
                    session_cookies, cf_clearance, csrf_token,
                    last_login_at, last_error, updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    username = ?,
                    password_hash = ?,
                    is_logged_in = ?,
                    session_cookies = COALESCE(?, session_cookies),
                    cf_clearance = COALESCE(?, cf_clearance),
                    csrf_token = COALESCE(?, csrf_token),
                    last_login_at = COALESCE(?, last_login_at),
                    last_error = ?,
                    updated_at = ?
            """, (
                username, password_hash, is_logged_in,
                session_cookies, cf_clearance, csrf_token,
                last_login_at, last_error, now,
                username, password_hash, is_logged_in,
                session_cookies, cf_clearance, csrf_token,
                last_login_at, last_error, now
            ))
            await db.commit()

    async def get_auth_state(self) -> Optional[Dict[str, Any]]:
        """Get Chaturbate auth state"""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM chaturbate_auth WHERE id = 1"
            )
            row = await cursor.fetchone()
            if row:
                return dict(row)
        return None

    async def clear_auth_state(self):
        """Clear Chaturbate auth state"""
        await self.initialize()

        async with self._connect() as db:
            await db.execute("DELETE FROM chaturbate_auth WHERE id = 1")
            await db.commit()

    # ==========================================
    # Generic Provider Sessions CRUD
    # ==========================================

    async def save_provider_session(
        self,
        source_type: str,
        username: Optional[str] = None,
        is_logged_in: bool = False,
        session_cookies: Optional[str] = None,
        local_storage: Optional[str] = None,
        last_login_at: Optional[int] = None,
        last_error: Optional[str] = None,
    ) -> None:
        await self.initialize()
        source_type = (source_type or "").strip().lower()
        if not source_type:
            raise ValueError("source_type is required")
        now = int(datetime.now().timestamp())
        last_login_at = last_login_at if last_login_at is not None else (now if is_logged_in else None)

        async with self._connect() as db:
            await db.execute("""
                INSERT INTO provider_sessions (
                    source_type, username, is_logged_in, session_cookies,
                    local_storage, last_login_at, last_error, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_type) DO UPDATE SET
                    username = COALESCE(?, username),
                    is_logged_in = ?,
                    session_cookies = COALESCE(?, session_cookies),
                    local_storage = COALESCE(?, local_storage),
                    last_login_at = ?,
                    last_error = ?,
                    updated_at = ?
            """, (
                source_type, username, is_logged_in, session_cookies,
                local_storage, last_login_at, last_error, now,
                username, is_logged_in, session_cookies, local_storage,
                last_login_at, last_error, now,
            ))
            await db.commit()

    async def save_provider_credentials(
        self,
        source_type: str,
        username: str,
        password: str,
    ) -> None:
        await self.initialize()
        source_type = (source_type or "").strip().lower()
        username = (username or "").strip()
        if not source_type:
            raise ValueError("source_type is required")
        if not username:
            raise ValueError("username is required")
        now = int(datetime.now().timestamp())

        async with self._connect() as db:
            await db.execute("""
                INSERT INTO provider_sessions (
                    source_type, username, credential_username,
                    credential_password, credentials_updated_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_type) DO UPDATE SET
                    username = ?,
                    credential_username = ?,
                    credential_password = ?,
                    credentials_updated_at = ?,
                    updated_at = ?
            """, (
                source_type, username, username, password, now, now,
                username, username, password, now, now,
            ))
            await db.commit()

    async def get_provider_session(self, source_type: str) -> Optional[Dict[str, Any]]:
        await self.initialize()
        source_type = (source_type or "").strip().lower()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM provider_sessions WHERE source_type = ?",
                (source_type,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def clear_provider_session(self, source_type: str) -> None:
        await self.initialize()
        source_type = (source_type or "").strip().lower()
        async with self._connect() as db:
            await db.execute(
                "DELETE FROM provider_sessions WHERE source_type = ?",
                (source_type,),
            )
            await db.commit()

    # ==========================================
    # Followed Models CRUD
    # ==========================================

    async def upsert_followed_model(
        self,
        username: str,
        display_name: Optional[str] = None,
        is_online: bool = False,
        viewers: int = 0,
        thumbnail_url: Optional[str] = None,
        source_type: str = "chaturbate",
        room_status: Optional[str] = None,
        profile_image_url: Optional[str] = None,
        followers: Optional[int] = None,
    ):
        """Add or update a followed model"""
        await self.initialize()
        now = int(datetime.now().timestamp())
        source_type = self._normalize_source_type(source_type)
        followers_value = None if followers is None else int(followers)

        async with self._connect() as db:
            await db.execute("""
                INSERT INTO followed_models (
                    username, display_name, is_online, viewers,
                    thumbnail_url, profile_image_url, last_seen_online_at, synced_at, source_type,
                    room_status, followers
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username, source_type) DO UPDATE SET
                    display_name = COALESCE(?, display_name),
                    is_online = ?,
                    viewers = ?,
                    thumbnail_url = COALESCE(?, thumbnail_url),
                    profile_image_url = COALESCE(?, profile_image_url),
                    last_seen_online_at = CASE WHEN ? THEN ? ELSE last_seen_online_at END,
                    synced_at = ?,
                    room_status = ?,
                    followers = COALESCE(?, followers)
            """, (
                username, display_name, is_online, viewers,
                thumbnail_url, profile_image_url, now if is_online else None, now, source_type,
                room_status, followers_value,
                display_name, is_online, viewers, thumbnail_url, profile_image_url,
                is_online, now, now, room_status, followers_value,
            ))
            await db.commit()

    async def get_all_followed(self) -> List[Dict[str, Any]]:
        """Get all followed models"""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM followed_models ORDER BY is_online DESC, username"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_followed_model(
        self, username: str, source_type: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Fetch a followed_model by username and optional source_type.
        Used to resolve the platform of a model that is not in
        `tracked_models` but is in the favorites list."""
        await self.initialize()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            if source_type:
                cursor = await db.execute(
                    "SELECT * FROM followed_models WHERE username = ? AND source_type = ? LIMIT 1",
                    (username, self._normalize_source_type(source_type)),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT * FROM followed_models
                    WHERE username = ?
                    ORDER BY CASE WHEN source_type = 'chaturbate' THEN 0 ELSE 1 END, source_type
                    LIMIT 1
                    """,
                    (username,),
                )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def clear_followed(self):
        """Clear all followed models"""
        await self.initialize()

        async with self._connect() as db:
            await db.execute("DELETE FROM followed_models")
            await db.commit()

    async def delete_followed_model(
        self, username: str, source_type: Optional[str] = None
    ) -> None:
        """Delete a followed_model by username (and optionally by
        source_type). Used after unfollow from the watch page."""
        await self.initialize()
        async with self._connect() as db:
            if source_type:
                await db.execute(
                    "DELETE FROM followed_models WHERE username = ? AND source_type = ?",
                    (username, self._normalize_source_type(source_type)),
                )
            else:
                await db.execute(
                    "DELETE FROM followed_models WHERE username = ?",
                    (username,),
                )
            await db.commit()

    async def remove_unfollowed(
        self, current_usernames: set, source_type: str = "chaturbate"
    ):
        """Remove followed models no longer in the followed list.

        Scoped by source_type so other sources are left untouched.
        """
        await self.initialize()
        source_type = self._normalize_source_type(source_type)

        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT username FROM followed_models WHERE source_type = ?",
                (source_type,),
            )
            rows = await cursor.fetchall()
            for row in rows:
                if row[0] not in current_usernames:
                    await db.execute(
                        "DELETE FROM followed_models WHERE username = ? AND source_type = ?",
                        (row[0], source_type),
                    )
            await db.commit()

    async def get_all_recordings_paginated(
        self,
        page: int = 1,
        limit: int = 20,
        username_filter: Optional[str] = None,
        show_ts: bool = False
    ) -> Dict[str, Any]:
        """Get all recordings with pagination"""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row

            # Raw transport streams are opt-in; directly playable WebM/MP4
            # captures and imported media remain visible by default.
            where_clauses = ["1=1"]
            where_params = []
            if not show_ts:
                where_clauses.append(
                    "(media_kind = 'import' OR is_converted = 1 OR mp4_path IS NOT NULL "
                    "OR LOWER(file_path) NOT LIKE '%.ts')"
                )
            if username_filter:
                where_clauses.append("username = ?")
                where_params.append(username_filter)

            where_sql = " AND ".join(where_clauses)

            # Count total
            count_sql = f"SELECT COUNT(*) FROM recordings WHERE {where_sql}"
            cursor = await db.execute(count_sql, where_params)
            row = await cursor.fetchone()
            total = row[0] if row else 0

            # Fetch page
            offset = (page - 1) * limit
            query_sql = f"SELECT * FROM recordings WHERE {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?"
            query_params = list(where_params) + [limit, offset]

            cursor = await db.execute(query_sql, query_params)
            rows = await cursor.fetchall()

            # Total size - respects show_ts filter
            if show_ts:
                size_sql = f"SELECT COALESCE(SUM(COALESCE(playable_size, mp4_size, file_size)), 0) FROM recordings WHERE {where_sql}"
            else:
                # The WHERE clause already excludes raw TS rows.
                size_sql = f"SELECT COALESCE(SUM(COALESCE(playable_size, mp4_size, file_size)), 0) FROM recordings WHERE {where_sql}"

            cursor = await db.execute(size_sql, where_params)
            size_row = await cursor.fetchone()
            total_size = size_row[0] if size_row else 0

            return {
                "recordings": [dict(row) for row in rows],
                "total": total,
                "total_size": total_size,
                "page": page,
                "limit": limit,
                "total_pages": max(1, (total + limit - 1) // limit)
            }

    async def get_distinct_recording_usernames(self) -> List[str]:
        """Get list of usernames that have recordings"""
        await self.initialize()

        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT DISTINCT username FROM recordings ORDER BY username"
            )
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def sum_recording_bytes_since(
        self,
        since_unix: int,
        usernames: Optional[List[str]] = None,
    ) -> Dict[str, int]:
        """Sum captured recording bytes per username since a unix timestamp.

        Prefers original capture size (file_size) over converted playable sizes
        so monthly traffic quota tracks inbound recording volume.
        """
        await self.initialize()
        since_unix = int(since_unix or 0)
        names = [
            str(name).strip()
            for name in (usernames or [])
            if str(name or "").strip()
        ]

        async with self._connect() as db:
            if names:
                placeholders = ",".join("?" for _ in names)
                cursor = await db.execute(
                    f"""
                    SELECT username,
                           COALESCE(SUM(COALESCE(NULLIF(file_size, 0), NULLIF(mp4_size, 0), NULLIF(playable_size, 0), 0)), 0)
                    FROM recordings
                    WHERE created_at >= ?
                      AND username IN ({placeholders})
                    GROUP BY username
                    """,
                    [since_unix, *names],
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT username,
                           COALESCE(SUM(COALESCE(NULLIF(file_size, 0), NULLIF(mp4_size, 0), NULLIF(playable_size, 0), 0)), 0)
                    FROM recordings
                    WHERE created_at >= ?
                    GROUP BY username
                    """,
                    (since_unix,),
                )
            rows = await cursor.fetchall()
            return {str(row[0]): int(row[1] or 0) for row in rows if row and row[0]}

    # ==========================================
    # Settings CRUD
    # ==========================================

    async def get_setting(self, key: str) -> Optional[str]:
        """Get a setting value by key"""
        await self.initialize()
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_setting(self, key: str, value: str):
        """Set a setting value"""
        await self.initialize()
        now = int(datetime.now().timestamp())
        async with self._connect() as db:
            await db.execute("""
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = ?, updated_at = ?
            """, (key, value, now, value, now))
            await db.commit()

    def _model_volume_key(self, username: str) -> str:
        """Stable settings key for a profile's playback volume."""
        normalized = (username or "").strip().lower()
        return f"model_volume:{normalized}"

    async def get_model_volume(self, username: str) -> Optional[float]:
        """Get the saved playback volume for a profile, if one exists."""
        normalized = (username or "").strip()
        if not normalized:
            return None

        value = await self.get_setting(self._model_volume_key(normalized))
        if value is None:
            return None

        try:
            volume = float(value)
        except (TypeError, ValueError):
            return None

        if 0 <= volume <= 1:
            return volume
        return None

    async def set_model_volume(self, username: str, volume: float):
        """Persist a profile's playback volume."""
        normalized = (username or "").strip()
        if not normalized:
            raise ValueError("username is required")
        if not 0 <= volume <= 1:
            raise ValueError("volume must be between 0 and 1")

        await self.set_setting(self._model_volume_key(normalized), f"{volume:.4f}")

    async def get_blacklisted_tags(self) -> List[str]:
        """Get blacklisted tags list"""
        value = await self.get_setting("blacklisted_tags")
        if value:
            return json.loads(value)
        return []

    async def set_blacklisted_tags(self, tags: List[str]):
        """Set blacklisted tags list"""
        await self.set_setting("blacklisted_tags", json.dumps(tags))

    async def get_disabled_providers(self) -> List[str]:
        """Get provider source types hidden from Discover."""
        value = await self.get_setting("disabled_providers")
        if not value:
            return []
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        if not isinstance(parsed, list):
            return []
        seen = set()
        providers = []
        for item in parsed:
            source_type = str(item or "").strip().lower()
            if not source_type or source_type in seen:
                continue
            seen.add(source_type)
            providers.append(source_type)
        return providers

    async def set_disabled_providers(self, providers: List[str]):
        """Set provider source types hidden from Discover."""
        normalized = sorted({
            str(source_type or "").strip().lower()
            for source_type in providers
            if str(source_type or "").strip()
        })
        await self.set_setting("disabled_providers", json.dumps(normalized))

    # ==========================================
    # Playback Positions CRUD
    # ==========================================

    async def get_playback_position(self, recording_id: str) -> Optional[Dict[str, Any]]:
        """Get playback position for a recording"""
        await self.initialize()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM playback_positions WHERE recording_id = ?",
                (recording_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def save_playback_position(
        self,
        recording_id: str,
        username: str,
        position_seconds: float,
        duration_seconds: float = 0,
        mark_watched: bool = False,
    ):
        """Save playback position for a recording"""
        await self.initialize()
        now = int(datetime.now().timestamp())
        watched_at = now if mark_watched else None
        async with self._connect() as db:
            await db.execute("""
                INSERT INTO playback_positions (
                    recording_id, username, position_seconds, duration_seconds, watched_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(recording_id) DO UPDATE SET
                    position_seconds = ?,
                    duration_seconds = ?,
                    watched_at = CASE
                        WHEN ? THEN COALESCE(playback_positions.watched_at, ?)
                        ELSE playback_positions.watched_at
                    END,
                    updated_at = ?
            """, (
                recording_id, username, position_seconds, duration_seconds, watched_at, now,
                position_seconds, duration_seconds, 1 if mark_watched else 0, watched_at, now,
            ))
            await db.commit()

    async def get_all_playback_positions(self, username: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all playback positions, optionally filtered by username"""
        await self.initialize()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            if username:
                cursor = await db.execute(
                    "SELECT * FROM playback_positions WHERE username = ? ORDER BY updated_at DESC",
                    (username,)
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM playback_positions ORDER BY updated_at DESC"
                )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_recordings_grouped_by_model(self, show_ts: bool = False) -> List[Dict[str, Any]]:
        """Get recordings grouped by model with stats"""
        await self.initialize()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            # Raw TS rows are opt-in; non-TS browser captures remain visible.
            if show_ts:
                where_clause = ""
            else:
                where_clause = (
                    "WHERE media_kind = 'import' OR is_converted = 1 OR mp4_path IS NOT NULL "
                    "OR LOWER(file_path) NOT LIKE '%.ts'"
                )
            cursor = await db.execute(f"""
                SELECT
                    username,
                    COUNT(*) as recording_count,
                    COALESCE(SUM(COALESCE(playable_size, mp4_size, file_size)), 0) as total_size,
                    MAX(created_at) as last_recording_at,
                    COALESCE(SUM(duration_seconds), 0) as total_duration
                FROM recordings
                {where_clause}
                GROUP BY username
                ORDER BY last_recording_at DESC
            """)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # ==========================================
    # Recording projects
    # ==========================================

    async def create_recording_project(
        self,
        *,
        project_id: str,
        username: str,
        source_type: str = "chaturbate",
        started_at: int,
        ended_at: Optional[int] = None,
        status: str = "open",
        gap_buffer_seconds: int = 1800,
    ) -> None:
        await self.initialize()
        now = int(datetime.now().timestamp())
        async with self._connect() as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO recording_projects (
                    project_id, username, source_type, started_at, ended_at,
                    status, concat_status, gap_buffer_seconds, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'none', ?, ?, ?)
                """,
                (
                    project_id,
                    username,
                    source_type or "chaturbate",
                    int(started_at),
                    ended_at,
                    status or "open",
                    int(gap_buffer_seconds or 1800),
                    now,
                    now,
                ),
            )
            await db.commit()

    async def get_recording_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        await self.initialize()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM recording_projects WHERE project_id = ?",
                (project_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def update_recording_project(self, project_id: str, **fields) -> None:
        await self.initialize()
        if not fields:
            return
        allowed = {
            "started_at",
            "ended_at",
            "ready_at",
            "status",
            "concat_status",
            "gap_buffer_seconds",
            "source_type",
            "updated_at",
        }
        sets = []
        values = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            values.append(value)
        if not sets:
            return
        values.append(project_id)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE recording_projects SET {', '.join(sets)} WHERE project_id = ?",
                tuple(values),
            )
            await db.commit()

    async def list_recording_projects(
        self,
        *,
        usernames: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        await self.initialize()
        clauses: List[str] = []
        params: List[Any] = []
        if usernames:
            placeholders = ",".join("?" for _ in usernames)
            clauses.append(f"username IN ({placeholders})")
            params.extend(usernames)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT * FROM recording_projects
                {where}
                ORDER BY started_at DESC
                """,
                tuple(params),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def list_open_recording_projects(self, username: str) -> List[Dict[str, Any]]:
        return await self.list_recording_projects(
            usernames=[username],
            statuses=["open", "awaiting_gap", "ready"],
        )

    async def delete_recording_project(self, project_id: str) -> None:
        await self.initialize()
        async with self._connect() as db:
            await db.execute(
                "DELETE FROM recording_project_members WHERE project_id = ?",
                (project_id,),
            )
            await db.execute(
                "DELETE FROM recording_projects WHERE project_id = ?",
                (project_id,),
            )
            await db.commit()

    async def upsert_project_member(
        self,
        *,
        project_id: str,
        username: str,
        filename: str,
        recording_id: Optional[str] = None,
        item_id: Optional[str] = None,
        started_at: int = 0,
        ended_at: int = 0,
        duration_seconds: int = 0,
        size_bytes: int = 0,
        part_label: Optional[str] = None,
        session_key: Optional[str] = None,
        sort_index: int = 0,
        location: Optional[str] = None,
    ) -> None:
        await self.initialize()
        loc = str(location or "vps").strip().lower() or "vps"
        if loc not in {"vps", "mac", "both"}:
            loc = "vps"
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO recording_project_members (
                    project_id, username, filename, recording_id, item_id,
                    started_at, ended_at, duration_seconds, size_bytes,
                    part_label, session_key, sort_index, location
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username, filename) DO UPDATE SET
                    project_id = excluded.project_id,
                    recording_id = COALESCE(excluded.recording_id, recording_id),
                    item_id = COALESCE(excluded.item_id, item_id),
                    started_at = excluded.started_at,
                    ended_at = excluded.ended_at,
                    duration_seconds = excluded.duration_seconds,
                    size_bytes = CASE
                        WHEN excluded.size_bytes > 0 THEN excluded.size_bytes
                        ELSE size_bytes
                    END,
                    part_label = COALESCE(excluded.part_label, part_label),
                    session_key = COALESCE(excluded.session_key, session_key),
                    sort_index = excluded.sort_index,
                    location = CASE
                        WHEN excluded.location = 'mac' THEN 'mac'
                        WHEN location = 'mac' THEN 'mac'
                        ELSE excluded.location
                    END
                """,
                (
                    project_id,
                    username,
                    filename,
                    recording_id,
                    item_id,
                    int(started_at or 0),
                    int(ended_at or 0),
                    int(duration_seconds or 0),
                    int(size_bytes or 0),
                    part_label,
                    session_key,
                    int(sort_index or 0),
                    loc,
                ),
            )
            await db.commit()

    async def set_project_member_location(
        self,
        username: str,
        filename: str,
        location: str,
    ) -> None:
        await self.initialize()
        loc = str(location or "vps").strip().lower() or "vps"
        if loc not in {"vps", "mac", "both"}:
            loc = "vps"
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE recording_project_members
                SET location = ?
                WHERE username = ? AND filename = ?
                """,
                (loc, username, Path(filename or "").name),
            )
            await db.commit()

    async def list_all_project_member_keys(self) -> List[tuple]:
        """Return (username, filename) for every project member."""
        await self.initialize()
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT username, filename FROM recording_project_members"
            )
            rows = await cursor.fetchall()
            return [(str(r[0]), str(r[1])) for r in rows]

    async def get_project_member(self, username: str, filename: str) -> Optional[Dict[str, Any]]:
        await self.initialize()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM recording_project_members
                WHERE username = ? AND filename = ?
                """,
                (username, filename),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def list_project_members(self, project_id: str) -> List[Dict[str, Any]]:
        await self.initialize()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM recording_project_members
                WHERE project_id = ?
                ORDER BY started_at ASC, sort_index ASC, filename ASC
                """,
                (project_id,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_project_member(self, username: str, filename: str) -> Optional[str]:
        """Delete member; return project_id. Caller may remove empty projects."""
        await self.initialize()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT project_id FROM recording_project_members
                WHERE username = ? AND filename = ?
                """,
                (username, filename),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            project_id = row["project_id"]
            await db.execute(
                """
                DELETE FROM recording_project_members
                WHERE username = ? AND filename = ?
                """,
                (username, filename),
            )
            await db.commit()
            return project_id

    async def count_project_members(self, project_id: str) -> int:
        await self.initialize()
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) AS c FROM recording_project_members WHERE project_id = ?",
                (project_id,),
            )
            row = await cursor.fetchone()
            return int(row[0] if row else 0)

    # ==========================================
    # JSON Migration
    # ==========================================

    async def migrate_from_json(self, json_path: Path):
        """Migrate data from the JSON file to SQLite"""
        if not json_path.exists():
            return

        await self.initialize()

        try:
            with open(json_path, 'r') as f:
                data = json.load(f)
                models = data.get('models', []) if isinstance(data, dict) else data

            for model in models:
                username = model.get('username')
                if username:
                    await self.add_or_update_model(
                        username=username,
                        auto_record=model.get('autoRecord', False),
                        record_quality=model.get('recordQuality', 'best'),
                        retention_days=model.get('retentionDays', 30),
                        source_type=model.get('sourceType') or model.get('source_type'),
                    )

            logger.info("JSON to SQLite migration finished", models_count=len(models))

        except Exception as e:
            logger.error("Error during JSON migration", error=str(e), exc_info=True)
