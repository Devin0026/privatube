"""DB-backed job queue. The worker claims jobs with a lease, so a crashed worker's jobs
are picked up again after a restart."""
from __future__ import annotations

import sqlite3

from . import db

MAX_ATTEMPTS = 5


class Defer(Exception):
    """Try again later without counting it as a failed attempt (e.g. low disk)."""
    def __init__(self, seconds: int, reason: str):
        super().__init__(reason)
        self.seconds = seconds


class Permanent(Exception):
    """Retrying won't help (video deleted, private, file missing...)."""


class MembersOnly(Permanent):
    """Only the channel's paying members can watch it. Never downloaded on purpose."""


def enqueue(conn: sqlite3.Connection, kind: str, *, video_id: int | None = None,
            channel_id: int | None = None, priority: int = 100, delay: int = 0) -> int | None:
    """Add a job unless an identical one is already pending/running."""
    dup = conn.execute(
        "SELECT id FROM jobs WHERE kind=? AND video_id IS ? AND channel_id IS ? "
        "AND status IN ('pending','running')", (kind, video_id, channel_id)).fetchone()
    if dup:
        return None
    t = db.now()
    cur = conn.execute(
        "INSERT INTO jobs(kind,video_id,channel_id,priority,run_after,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?)", (kind, video_id, channel_id, priority, t + delay, t, t))
    return cur.lastrowid


def claim(conn: sqlite3.Connection, node: str, lease: int, kinds: tuple[str, ...]) -> sqlite3.Row | None:
    t = db.now()
    marks = ",".join("?" * len(kinds))
    return conn.execute(
        f"""UPDATE jobs SET status='running', node=?, lease_until=?, attempts=attempts+1,
                updated_at=?, progress=NULL
            WHERE id = (SELECT id FROM jobs WHERE status='pending' AND run_after<=?
                        AND kind IN ({marks}) ORDER BY priority, id LIMIT 1)
            RETURNING *""",
        (node, t + lease, t, t, *kinds)).fetchone()


def finish(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute("UPDATE jobs SET status='done', error=NULL, lease_until=NULL, updated_at=? "
                 "WHERE id=?", (db.now(), job_id))


def defer(conn: sqlite3.Connection, job_id: int, seconds: int, reason: str) -> None:
    """Put back without counting it as a failed attempt."""
    conn.execute("UPDATE jobs SET status='pending', attempts=MAX(attempts-1,0), run_after=?, "
                 "error=?, lease_until=NULL, updated_at=? WHERE id=?",
                 (db.now() + seconds, reason, db.now(), job_id))


def fail(conn: sqlite3.Connection, job: sqlite3.Row, error: str, permanent: bool = False) -> bool:
    """Retry with backoff (10m, 20m, 40m, ...); returns True if given up."""
    attempts = job["attempts"]
    give_up = permanent or attempts >= MAX_ATTEMPTS
    conn.execute(
        "UPDATE jobs SET status=?, error=?, run_after=?, lease_until=NULL, updated_at=? WHERE id=?",
        ("failed" if give_up else "pending", error[:2000],
         db.now() + 600 * 2 ** (attempts - 1), db.now(), job["id"]))
    return give_up


def recover(conn: sqlite3.Connection, node: str | None = None) -> None:
    """Return expired leases (or all of this node's running jobs at startup)."""
    t = db.now()
    if node:
        conn.execute("UPDATE jobs SET status='pending', lease_until=NULL, updated_at=? "
                     "WHERE status='running' AND node=?", (t, node))
    conn.execute("UPDATE jobs SET status='pending', lease_until=NULL, updated_at=? "
                 "WHERE status='running' AND lease_until < ?", (t, t))


# ---------------------------------------------------------------- download order
# Lower runs first. Moves (60) come before downloads because they free the space downloads need.
PRIO_BY_CHANNEL = {1: 70, 0: 100, -1: 130}
PRIO_SINGLE = 80        # a single video the admin added by URL
PRIORITY_NAMES = {1: "High", 0: "Normal", -1: "Low"}


def download_priority(conn: sqlite3.Connection, video_id: int) -> int:
    r = conn.execute("SELECT c.priority FROM videos v LEFT JOIN channels c ON c.id=v.channel_id "
                     "WHERE v.id=?", (video_id,)).fetchone()
    if r is None or r["priority"] is None:
        return PRIO_SINGLE
    return PRIO_BY_CHANNEL.get(r["priority"], 100)


def queue_download(conn: sqlite3.Connection, video_id: int) -> None:
    conn.execute("UPDATE videos SET status='queued', error=NULL WHERE id=? AND status<>'ready'", (video_id,))
    enqueue(conn, "download", video_id=video_id, priority=download_priority(conn, video_id))


def reprioritise_channel(conn: sqlite3.Connection, channel_id: int) -> None:
    r = conn.execute("SELECT priority FROM channels WHERE id=?", (channel_id,)).fetchone()
    if r:
        conn.execute("UPDATE jobs SET priority=? WHERE kind='download' AND status='pending' AND video_id IN "
                     "(SELECT id FROM videos WHERE channel_id=?)", (PRIO_BY_CHANNEL.get(r["priority"], 100), channel_id))


# ---------------------------------------------------------------- "latest 20" / "last 3 months"
RANGES = [("", "Nothing yet (pick by hand)"), ("n:10", "Latest 10"), ("n:20", "Latest 20"),
          ("n:50", "Latest 50"), ("d:30", "Last month"), ("d:91", "Last 3 months"),
          ("d:182", "Last 6 months"), ("d:365", "Last year")]


def parse_range(spec: str | None) -> tuple[str, int] | None:
    if not spec or spec not in dict(RANGES):
        return None
    k, _, n = spec.partition(":")
    return k, int(n)


def queue_range(conn: sqlite3.Connection, channel_id: int, spec: str | None) -> int:
    """Queue the channel's not-yet-downloaded videos in the range (newest first), skipping
    Shorts unless the channel includes them, members-only videos, and titles its keyword
    filter rejects. "Latest 20" means the latest 20 public videos."""
    from . import keywords
    rng = parse_range(spec)
    ch = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    if rng is None or ch is None:
        return 0
    kind, n = rng
    rows = conn.execute("SELECT id, status, title, alt_title, published_at, is_short FROM videos "
                        "WHERE channel_id=? AND (is_short=0 OR ?) AND members=0 "
                        "ORDER BY published_at IS NULL, published_at DESC, id",
                        (channel_id, ch["include_shorts"])).fetchall()
    rows = [r for r in rows if keywords.matches(keywords.titles(r["title"], r["alt_title"]),
                                                ch["include_keywords"], ch["exclude_keywords"])]
    if kind == "n":
        rows = rows[:n]
    else:
        since = db.now() - n * 86400
        rows = [r for r in rows if r["published_at"] and r["published_at"] >= since]
    picked = [r["id"] for r in rows if r["status"] in ("listed", "failed")]
    for vid in picked:
        queue_download(conn, vid)
    return len(picked)


def cancel_channel_queue(conn: sqlite3.Connection, channel_id: int) -> int:
    ids = [r[0] for r in conn.execute(
        "SELECT j.video_id FROM jobs j JOIN videos v ON v.id=j.video_id WHERE j.kind='download' "
        "AND j.status='pending' AND v.channel_id=?", (channel_id,)).fetchall()]
    for vid in ids:
        conn.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE kind='download' AND video_id=? "
                     "AND status='pending'", (db.now(), vid))
        conn.execute("UPDATE videos SET status='listed' WHERE id=? AND status='queued'", (vid,))
    return len(ids)
