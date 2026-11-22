"""Storage planning: how big will the download queue be, and does it fit?

Sizes are estimated from each video's length times a bytes-per-second rate. The rate is
learnt from what was already downloaded: per channel once it has 3+ videos (same kind of
content and quality), else across all videos, else a default for the quality setting.
"""
from __future__ import annotations

import shutil
import sqlite3

from .config import settings

# bytes/second when nothing has been downloaded yet (H.264 + AAC, typical YouTube encodes)
DEFAULT_RATE = {360: 45_000, 480: 75_000, 720: 140_000, 1080: 290_000}
MIN_SAMPLES = 3


def rates(conn: sqlite3.Connection) -> tuple[dict[int, float], float | None]:
    per_channel = {}
    for r in conn.execute("SELECT channel_id, COUNT(*) n, SUM(filesize) b, SUM(duration) d FROM videos "
                          "WHERE filesize > 0 AND duration > 0 GROUP BY channel_id"):
        if r["n"] >= MIN_SAMPLES and r["channel_id"] is not None:
            per_channel[r["channel_id"]] = r["b"] / r["d"]
    g = conn.execute("SELECT COUNT(*) n, SUM(filesize) b, SUM(duration) d FROM videos "
                     "WHERE filesize > 0 AND duration > 0").fetchone()
    return per_channel, (g["b"] / g["d"] if g["n"] >= MIN_SAMPLES else None)


def queue_estimate(conn: sqlite3.Connection) -> dict:
    """{bytes, n, unknown (no duration), by_channel {channel_id: bytes}} for queued downloads."""
    per_channel, overall = rates(conn)
    out = {"bytes": 0, "n": 0, "unknown": 0, "by_channel": {}}
    rows = conn.execute(
        "SELECT v.channel_id, v.duration, COALESCE(v.max_height, c.max_height, ?) AS h FROM videos v "
        "LEFT JOIN channels c ON c.id=v.channel_id WHERE v.status IN ('queued','downloading')",
        (settings.default_height,)).fetchall()
    known = [r["duration"] for r in rows if r["duration"]]
    typical = sorted(known)[len(known) // 2] if known else 1200     # median length, else 20 min
    for r in rows:
        rate = per_channel.get(r["channel_id"]) or overall or DEFAULT_RATE.get(r["h"], DEFAULT_RATE[720])
        size = int((r["duration"] or typical) * rate)
        out["bytes"] += size
        out["n"] += 1
        out["unknown"] += not r["duration"]
        if r["channel_id"] is not None:
            out["by_channel"][r["channel_id"]] = out["by_channel"].get(r["channel_id"], 0) + size
    return out


def room(conn: sqlite3.Connection) -> int:
    """Bytes that can still be downloaded before downloads pause (PT_MIN_FREE_GB)."""
    return max(0, shutil.disk_usage(settings.media_dir).free - settings.min_free_bytes)
