"""SQLite access: one connection per call site, WAL mode, numbered migrations."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator

from .config import settings

MIGRATIONS: list[str] = [
    # 1: schema. Add later changes as new entries (never edit one that has been released).
    """
    CREATE TABLE users(
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        pw_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('admin','viewer')),
        disabled INTEGER NOT NULL DEFAULT 0,
        pw_changed_at INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        last_login_at INTEGER,
        totp_secret TEXT,                   -- admins: two-factor secret
        totp_last_step INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE channels(
        id INTEGER PRIMARY KEY,
        yt_id TEXT UNIQUE,
        url TEXT NOT NULL,
        handle TEXT,
        title TEXT,
        description TEXT,
        avatar TEXT,                        -- path relative to the media dir
        subscribed INTEGER NOT NULL DEFAULT 1,
        auto_download INTEGER NOT NULL DEFAULT 1,
        include_shorts INTEGER NOT NULL DEFAULT 0,
        poll_minutes INTEGER NOT NULL DEFAULT 60,
        baseline_done INTEGER NOT NULL DEFAULT 0,
        last_polled_at INTEGER,
        last_error TEXT,
        created_at INTEGER NOT NULL,
        include_keywords TEXT NOT NULL DEFAULT '',
        exclude_keywords TEXT NOT NULL DEFAULT '',
        max_height INTEGER,                 -- NULL = the site default
        priority INTEGER NOT NULL DEFAULT 0,  -- 1 high, 0 normal, -1 low
        backfill TEXT                       -- first listing: "n:20" latest 20, "d:90" last 90 days, NULL by hand
    );

    CREATE TABLE videos(
        id INTEGER PRIMARY KEY,
        yt_id TEXT NOT NULL UNIQUE,
        channel_id INTEGER REFERENCES channels(id) ON DELETE SET NULL,
        title TEXT,
        alt_title TEXT,                     -- title in PT_TITLE_LANG (search, keyword filters)
        description TEXT,
        duration INTEGER,
        published_at INTEGER,
        is_short INTEGER NOT NULL DEFAULT 0,
        members INTEGER NOT NULL DEFAULT 0, -- members-only: never downloaded
        status TEXT NOT NULL DEFAULT 'listed'
            CHECK (status IN ('listed','queued','downloading','ready','failed','evicted')),
        path TEXT,                          -- directory relative to the media dir
        thumb TEXT,                         -- file name inside path
        filesize INTEGER,
        width INTEGER,
        height INTEGER,
        max_height INTEGER,
        comment_count INTEGER NOT NULL DEFAULT 0,
        comments_at INTEGER,
        subs TEXT,                          -- JSON list of {lang, auto, file}
        subs_at INTEGER,
        added_at INTEGER NOT NULL,
        ready_at INTEGER,
        last_viewed_at INTEGER,
        error TEXT
    );

    CREATE TABLE comments(
        id INTEGER PRIMARY KEY,
        video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        yt_id TEXT NOT NULL,
        parent TEXT NOT NULL DEFAULT 'root',
        author TEXT,
        text TEXT,
        likes INTEGER,
        published_at INTEGER,
        is_pinned INTEGER NOT NULL DEFAULT 0,
        UNIQUE(video_id, yt_id)
    );

    CREATE TABLE access_codes(
        id INTEGER PRIMARY KEY,
        code_hash TEXT NOT NULL UNIQUE,
        label TEXT,
        max_devices INTEGER NOT NULL DEFAULT 1,
        used_devices INTEGER NOT NULL DEFAULT 0,
        session_days INTEGER NOT NULL DEFAULT 7,
        expires_at INTEGER,
        disabled INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL
    );

    CREATE TABLE code_scopes(
        code_id INTEGER NOT NULL REFERENCES access_codes(id) ON DELETE CASCADE,
        channel_id INTEGER REFERENCES channels(id) ON DELETE CASCADE,
        video_id INTEGER REFERENCES videos(id) ON DELETE CASCADE
    );

    CREATE TABLE sessions(
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        code_id INTEGER REFERENCES access_codes(id) ON DELETE CASCADE,
        csrf TEXT NOT NULL,
        pending_pw_hash TEXT,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        last_seen_at INTEGER NOT NULL,
        ip TEXT,
        user_agent TEXT,
        mfa_pending INTEGER NOT NULL DEFAULT 0,
        pending_totp_secret TEXT,
        CHECK ((user_id IS NULL) != (code_id IS NULL))
    );

    CREATE TABLE jobs(
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,                 -- download, channel_sync, subs, comments, channel_info
        video_id INTEGER REFERENCES videos(id) ON DELETE CASCADE,
        channel_id INTEGER REFERENCES channels(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','running','done','failed','cancelled')),
        priority INTEGER NOT NULL DEFAULT 100,
        attempts INTEGER NOT NULL DEFAULT 0,
        run_after INTEGER NOT NULL,
        lease_until INTEGER,
        node TEXT,                          -- which worker holds the lease
        progress REAL,
        error TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );

    CREATE TABLE watch(
        viewer TEXT NOT NULL,               -- 'u<id>' or 'c<id>'
        video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        position REAL NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (viewer, video_id)
    );

    CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT NOT NULL, updated_at INTEGER NOT NULL);

    CREATE INDEX videos_feed ON videos(status, published_at DESC);
    CREATE INDEX videos_channel ON videos(channel_id, published_at DESC);
    CREATE INDEX jobs_queue ON jobs(status, priority, run_after);
    """,
]


def kv_get(conn: sqlite3.Connection, k: str, default=None):
    r = conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def kv_set(conn: sqlite3.Connection, k: str, v) -> None:
    conn.execute("INSERT INTO kv(k,v,updated_at) VALUES (?,?,?) ON CONFLICT(k) DO UPDATE SET "
                 "v=excluded.v, updated_at=excluded.updated_at", (k, json.dumps(v), now()))


def now() -> int:
    return int(time.time())


def connect() -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=30, isolation_level=None,
                           check_same_thread=False)  # one request at a time, across threadpool hops
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def get() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit write transaction (connections run in autocommit mode)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate() -> None:
    with get() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
            conn.executescript("BEGIN;" + script + f"; PRAGMA user_version={i}; COMMIT;")
