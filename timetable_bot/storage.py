"""SQLite persistence and change-detection decisions."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS timetables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    url TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    last_modified TEXT,
    published_date TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_changed_at TEXT NOT NULL,
    last_posted_at TEXT,
    posted_version_hash TEXT,
    is_baseline INTEGER NOT NULL DEFAULT 0,
    UNIQUE(category, url)
);

CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    url TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    filename TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    last_modified TEXT,
    published_date TEXT,
    posted INTEGER NOT NULL DEFAULT 0,
    posted_at TEXT,
    is_baseline INTEGER NOT NULL DEFAULT 0,
    UNIQUE(category, content_hash)
);

CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    timetable_url TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    dry_run INTEGER,
    check_interval_minutes INTEGER,
    role_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_timetables_category ON timetables(category);
CREATE INDEX IF NOT EXISTS idx_versions_posted ON versions(category, content_hash, posted);
CREATE INDEX IF NOT EXISTS idx_guild_settings_enabled ON guild_settings(enabled);
"""


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    category: str
    url: str
    filename: str
    content_hash: str
    last_modified: str | None
    published_date: str | None
    first_seen_at: str
    last_seen_at: str
    last_changed_at: str
    last_posted_at: str | None
    posted_version_hash: str | None
    is_baseline: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "DocumentRecord":
        return cls(
            category=row["category"],
            url=row["url"],
            filename=row["filename"],
            content_hash=row["content_hash"],
            last_modified=row["last_modified"],
            published_date=row["published_date"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            last_changed_at=row["last_changed_at"],
            last_posted_at=row["last_posted_at"],
            posted_version_hash=row["posted_version_hash"],
            is_baseline=int(row["is_baseline"]),
        )


@dataclass(frozen=True, slots=True)
class Registration:
    """What the monitor should do with one downloaded document."""

    status: str  # baseline | new | updated | duplicate | unchanged | retry
    should_post: bool
    is_update: bool
    current: DocumentRecord
    previous_hash: str | None = None
    known_hash_posted: bool = False
    known_hash_baseline: bool = False


class Database:
    """Small thread-safe SQLite wrapper.

    The bot only performs a handful of writes per check, so stdlib sqlite3 is
    enough. ``check_same_thread=False`` plus an internal lock keeps things safe
    when the explorer/CLI and the bot share the same process.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            timeout=30,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Make databases created by older versions forward-compatible."""

        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(timetables)").fetchall()
        }
        if "is_baseline" not in columns:
            self._conn.execute(
                "ALTER TABLE timetables ADD COLUMN is_baseline INTEGER NOT NULL DEFAULT 0"
            )

        version_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(versions)").fetchall()
        }
        if "is_baseline" not in version_columns:
            self._conn.execute(
                "ALTER TABLE versions ADD COLUMN is_baseline INTEGER NOT NULL DEFAULT 0"
            )

        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                timetable_url TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                dry_run INTEGER,
                check_interval_minutes INTEGER,
                role_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_guild_settings_enabled ON guild_settings(enabled)"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------
    def get_state(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM bot_state WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO bot_state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_state_bool(self, key: str, default: bool = False) -> bool:
        value = self.get_state(key)
        if value is None:
            return default
        return value.strip().casefold() in {"1", "true", "yes", "on"}

    def increment_state(self, key: str, amount: int = 1) -> int:
        current = self.get_state(key)
        try:
            value = int(current or "0") + amount
        except ValueError:
            value = amount
        self.set_state(key, str(value))
        return value

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------
    def get_timetable(self, category: str, url: str) -> DocumentRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM timetables WHERE category = ? AND url = ?",
                (category, url),
            ).fetchone()
        return DocumentRecord.from_row(row) if row else None

    def get_version(self, category: str, content_hash: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM versions WHERE category = ? AND content_hash = ?",
                (category, content_hash),
            ).fetchone()

    def is_hash_posted(self, category: str, content_hash: str) -> bool:
        row = self.get_version(category, content_hash)
        return bool(row and int(row["posted"]) == 1)

    def is_hash_known(self, category: str, content_hash: str) -> bool:
        return self.get_version(category, content_hash) is not None

    # ------------------------------------------------------------------
    # Registration / decision logic
    # ------------------------------------------------------------------
    def register_document(
        self,
        *,
        category: str,
        url: str,
        filename: str,
        content_hash: str,
        detected_at: str,
        last_modified: str | None,
        published_date: str | None,
        baseline: bool = False,
    ) -> Registration:
        """Record a downloaded document and decide whether it must be posted.

        Rules:
        * A brand-new URL/content is posted once.
        * The same URL with the same bytes is never posted again.
        * The same URL with different bytes is posted as an update.
        * Identical content already posted for the same category (even under a
          different URL) is not posted again.
        * A failed Discord upload stays unposted and is retried on the next
          check.
        * In baseline mode, everything is recorded, nothing is posted.
        """

        with self._lock, self._conn:
            existing = self._get_timetable_locked(category, url)
            version = self._get_version_locked(category, content_hash)
            hash_posted = bool(version and int(version["posted"]) == 1)
            hash_baseline = bool(version and int(version["is_baseline"]) == 1)

            if existing and existing.content_hash == content_hash:
                self._touch_locked(
                    category,
                    url,
                    filename=filename,
                    detected_at=detected_at,
                    last_modified=last_modified,
                    published_date=published_date,
                )
                if baseline:
                    self._set_current_baseline_locked(category, url, True)
                    self._mark_version_baseline_locked(category, content_hash)
                    return Registration(
                        status="unchanged",
                        should_post=False,
                        is_update=False,
                        current=self._require_timetable_locked(category, url),
                        previous_hash=existing.content_hash,
                        known_hash_posted=hash_posted,
                        known_hash_baseline=True,
                    )
                if hash_posted:
                    self._mark_current_known_posted_locked(
                        category, url, content_hash, version
                    )
                    return Registration(
                        status="unchanged",
                        should_post=False,
                        is_update=False,
                        current=self._require_timetable_locked(category, url),
                        previous_hash=existing.content_hash,
                        known_hash_posted=True,
                        known_hash_baseline=hash_baseline,
                    )
                if hash_baseline:
                    return Registration(
                        status="unchanged",
                        should_post=False,
                        is_update=False,
                        current=self._require_timetable_locked(category, url),
                        previous_hash=existing.content_hash,
                        known_hash_posted=False,
                        known_hash_baseline=True,
                    )
                # Same bytes, but a previous Discord upload failed.
                self._set_current_baseline_locked(category, url, False)
                return Registration(
                    status="retry",
                    should_post=True,
                    is_update=False,
                    current=self._require_timetable_locked(category, url),
                    previous_hash=existing.content_hash,
                    known_hash_posted=False,
                    known_hash_baseline=False,
                )

            if existing:
                previous_hash = existing.content_hash
                self._upsert_current_locked(
                    category=category,
                    url=url,
                    filename=filename,
                    content_hash=content_hash,
                    detected_at=detected_at,
                    last_modified=last_modified,
                    published_date=published_date,
                    is_baseline=baseline,
                    changed=True,
                )
                self._insert_version_locked(
                    category=category,
                    url=url,
                    filename=filename,
                    content_hash=content_hash,
                    detected_at=detected_at,
                    last_modified=last_modified,
                    published_date=published_date,
                    is_baseline=baseline,
                )
                if baseline:
                    return Registration(
                        status="baseline",
                        should_post=False,
                        is_update=True,
                        current=self._require_timetable_locked(category, url),
                        previous_hash=previous_hash,
                        known_hash_posted=hash_posted,
                        known_hash_baseline=hash_baseline,
                    )
                if hash_posted:
                    self._mark_current_known_posted_locked(
                        category, url, content_hash, version
                    )
                    return Registration(
                        status="duplicate",
                        should_post=False,
                        is_update=True,
                        current=self._require_timetable_locked(category, url),
                        previous_hash=previous_hash,
                        known_hash_posted=True,
                        known_hash_baseline=hash_baseline,
                    )
                if hash_baseline:
                    self._set_current_baseline_locked(category, url, True)
                    return Registration(
                        status="duplicate",
                        should_post=False,
                        is_update=True,
                        current=self._require_timetable_locked(category, url),
                        previous_hash=previous_hash,
                        known_hash_posted=False,
                        known_hash_baseline=True,
                    )
                return Registration(
                    status="updated",
                    should_post=True,
                    is_update=True,
                    current=self._require_timetable_locked(category, url),
                    previous_hash=previous_hash,
                    known_hash_posted=False,
                    known_hash_baseline=False,
                )

            # Brand-new (category, URL) pair.
            self._upsert_current_locked(
                category=category,
                url=url,
                filename=filename,
                content_hash=content_hash,
                detected_at=detected_at,
                last_modified=last_modified,
                published_date=published_date,
                is_baseline=baseline,
                changed=True,
            )
            self._insert_version_locked(
                category=category,
                url=url,
                filename=filename,
                content_hash=content_hash,
                detected_at=detected_at,
                last_modified=last_modified,
                published_date=published_date,
                is_baseline=baseline,
            )
            current = self._require_timetable_locked(category, url)

            if baseline:
                return Registration(
                    status="baseline",
                    should_post=False,
                    is_update=False,
                    current=current,
                    previous_hash=None,
                    known_hash_posted=hash_posted,
                    known_hash_baseline=hash_baseline,
                )
            if hash_posted:
                self._mark_current_known_posted_locked(
                    category, url, content_hash, version
                )
                return Registration(
                    status="duplicate",
                    should_post=False,
                    is_update=False,
                    current=self._require_timetable_locked(category, url),
                    previous_hash=None,
                    known_hash_posted=True,
                    known_hash_baseline=hash_baseline,
                )
            if hash_baseline:
                self._set_current_baseline_locked(category, url, True)
                return Registration(
                    status="duplicate",
                    should_post=False,
                    is_update=False,
                    current=self._require_timetable_locked(category, url),
                    previous_hash=None,
                    known_hash_posted=False,
                    known_hash_baseline=True,
                )
            if version is not None:
                # Content was seen before but never posted (e.g. a previous
                # upload failed). Retry it instead of treating it as fresh.
                return Registration(
                    status="retry",
                    should_post=True,
                    is_update=False,
                    current=current,
                    previous_hash=None,
                    known_hash_posted=False,
                    known_hash_baseline=False,
                )
            return Registration(
                status="new",
                should_post=True,
                is_update=False,
                current=current,
                previous_hash=None,
                known_hash_posted=False,
                known_hash_baseline=False,
            )

    def mark_posted(
        self,
        *,
        category: str,
        url: str,
        content_hash: str,
        posted_at: str,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE versions SET posted = 1, posted_at = ?, is_baseline = 0 "
                "WHERE category = ? AND content_hash = ?",
                (posted_at, category, content_hash),
            )
            self._conn.execute(
                "UPDATE timetables SET posted_version_hash = ?, last_posted_at = ?, "
                "is_baseline = 0 WHERE category = ? AND url = ?",
                (content_hash, posted_at, category, url),
            )

    # ------------------------------------------------------------------
    # SQL helpers (caller must hold the lock)
    # ------------------------------------------------------------------
    def _get_timetable_locked(self, category: str, url: str) -> DocumentRecord | None:
        row = self._conn.execute(
            "SELECT * FROM timetables WHERE category = ? AND url = ?",
            (category, url),
        ).fetchone()
        return DocumentRecord.from_row(row) if row else None

    def _get_version_locked(self, category: str, content_hash: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM versions WHERE category = ? AND content_hash = ?",
            (category, content_hash),
        ).fetchone()

    def _require_timetable_locked(self, category: str, url: str) -> DocumentRecord:
        record = self._get_timetable_locked(category, url)
        if record is None:  # pragma: no cover - only possible on internal errors
            raise RuntimeError(f"timetable disappeared from database: {category} {url}")
        return record

    def _touch_locked(
        self,
        category: str,
        url: str,
        *,
        filename: str,
        detected_at: str,
        last_modified: str | None,
        published_date: str | None,
    ) -> None:
        self._conn.execute(
            "UPDATE timetables SET last_seen_at = ?, filename = ?, "
            "last_modified = COALESCE(?, last_modified), "
            "published_date = COALESCE(?, published_date) "
            "WHERE category = ? AND url = ?",
            (detected_at, filename, last_modified, published_date, category, url),
        )

    def _set_current_baseline_locked(self, category: str, url: str, value: bool) -> None:
        self._conn.execute(
            "UPDATE timetables SET is_baseline = ? WHERE category = ? AND url = ?",
            (1 if value else 0, category, url),
        )

    def _mark_version_baseline_locked(self, category: str, content_hash: str) -> None:
        self._conn.execute(
            "UPDATE versions SET is_baseline = 1 "
            "WHERE category = ? AND content_hash = ? AND posted = 0",
            (category, content_hash),
        )

    def _mark_current_known_posted_locked(
        self,
        category: str,
        url: str,
        content_hash: str,
        version: sqlite3.Row | None,
    ) -> None:
        """The current bytes were posted previously, even if under another URL."""

        posted_at = version["posted_at"] if version else None
        self._conn.execute(
            "UPDATE timetables SET posted_version_hash = ?, "
            "last_posted_at = COALESCE(last_posted_at, ?), is_baseline = 0 "
            "WHERE category = ? AND url = ?",
            (content_hash, posted_at, category, url),
        )

    def _upsert_current_locked(
        self,
        *,
        category: str,
        url: str,
        filename: str,
        content_hash: str,
        detected_at: str,
        last_modified: str | None,
        published_date: str | None,
        is_baseline: bool,
        changed: bool,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO timetables (
                category, url, filename, content_hash, last_modified,
                published_date, first_seen_at, last_seen_at, last_changed_at,
                last_posted_at, posted_version_hash, is_baseline
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            ON CONFLICT(category, url) DO UPDATE SET
                filename = excluded.filename,
                content_hash = excluded.content_hash,
                last_modified = COALESCE(excluded.last_modified, timetables.last_modified),
                published_date = COALESCE(excluded.published_date, timetables.published_date),
                last_seen_at = excluded.last_seen_at,
                last_changed_at = CASE WHEN ? THEN excluded.last_changed_at
                                       ELSE timetables.last_changed_at END,
                is_baseline = excluded.is_baseline
            """,
            (
                category,
                url,
                filename,
                content_hash,
                last_modified,
                published_date,
                detected_at,
                detected_at,
                detected_at,
                1 if is_baseline else 0,
                1 if changed else 0,
            ),
        )

    def _insert_version_locked(
        self,
        *,
        category: str,
        url: str,
        filename: str,
        content_hash: str,
        detected_at: str,
        last_modified: str | None,
        published_date: str | None,
        is_baseline: bool,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO versions (
                category, url, filename, content_hash, detected_at,
                last_modified, published_date, posted, posted_at, is_baseline
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)
            ON CONFLICT(category, content_hash) DO UPDATE SET
                filename = excluded.filename,
                last_modified = COALESCE(excluded.last_modified, versions.last_modified),
                published_date = COALESCE(excluded.published_date, versions.published_date),
                is_baseline = CASE WHEN excluded.is_baseline = 1 THEN 1
                                   ELSE versions.is_baseline END
            """,
            (
                category,
                url,
                filename,
                content_hash,
                detected_at,
                last_modified,
                published_date,
                1 if is_baseline else 0,
            ),
        )

    # ------------------------------------------------------------------
    # Guild settings (multi-server support)
    # ------------------------------------------------------------------
    def upsert_guild_setting(
        self,
        guild_id: int,
        channel_id: int,
        timetable_url: str | None = None,
        enabled: bool = True,
        dry_run: bool | None = None,
        check_interval_minutes: int | None = None,
        role_id: int | None = None,
    ) -> None:
        from .utils import utc_now_iso

        now = utc_now_iso()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO guild_settings (
                    guild_id, channel_id, timetable_url, enabled,
                    dry_run, check_interval_minutes, role_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    timetable_url = COALESCE(excluded.timetable_url, guild_settings.timetable_url),
                    enabled = excluded.enabled,
                    dry_run = excluded.dry_run,
                    check_interval_minutes = excluded.check_interval_minutes,
                    role_id = excluded.role_id,
                    updated_at = excluded.updated_at
                """,
                (
                    str(guild_id),
                    str(channel_id),
                    timetable_url,
                    1 if enabled else 0,
                    1 if dry_run is True else (0 if dry_run is False else None),
                    check_interval_minutes,
                    str(role_id) if role_id is not None else None,
                    now,
                    now,
                ),
            )

    def get_guild_setting(self, guild_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM guild_settings WHERE guild_id = ?",
                (str(guild_id),),
            ).fetchone()
        if not row:
            return None
        return dict(row)

    def get_enabled_guilds(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM guild_settings WHERE enabled = 1"
            ).fetchall()
        return [dict(row) for row in rows]

    def remove_guild_setting(self, guild_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM guild_settings WHERE guild_id = ?",
                (str(guild_id),),
            )

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------
    def get_status_counts(self) -> dict[str, dict[str, int]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    category,
                    COUNT(*) AS total,
                    SUM(CASE WHEN is_baseline = 1 THEN 1 ELSE 0 END) AS baseline,
                    SUM(CASE WHEN posted_version_hash = content_hash THEN 1 ELSE 0 END)
                        AS posted_current,
                    SUM(CASE WHEN is_baseline = 0 AND
                        (posted_version_hash IS NULL OR posted_version_hash != content_hash)
                        THEN 1 ELSE 0 END) AS pending
                FROM timetables
                GROUP BY category
                """
            ).fetchall()
        result: dict[str, dict[str, int]] = {}
        for row in rows:
            result[str(row["category"])] = {
                "total": int(row["total"] or 0),
                "baseline": int(row["baseline"] or 0),
                "posted_current": int(row["posted_current"] or 0),
                "pending": int(row["pending"] or 0),
            }
        return result

    def get_all_timetables(self) -> list[DocumentRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM timetables ORDER BY category, url"
            ).fetchall()
        return [DocumentRecord.from_row(row) for row in rows]

    def get_versions_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM versions").fetchone()
        return int(row["n"] or 0) if row else 0

    def get_posted_versions_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM versions WHERE posted = 1"
            ).fetchone()
        return int(row["n"] or 0) if row else 0

    def as_debug_dict(self) -> dict[str, Any]:
        return {
            "counts": self.get_status_counts(),
            "versions": self.get_versions_count(),
            "posted_versions": self.get_posted_versions_count(),
        }
