"""Health checks and alerts.

The worker runs `run()` every few minutes: it stores the current problems in the kv table
(the admin pages show them as a red banner) and, if PT_ALERT_URL is set, pushes a
message to your phone when a problem appears, a daily reminder while it lasts, and a
note when everything is fine again.

PT_ALERT_URL receives a form POST with `title` and `desp`/`body` fields, which works with
push services such as Bark (https://api.day.app/<key>) and ServerChan, or your own webhook.
"""
from __future__ import annotations

import logging
import shutil
import socket
import ssl
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

from . import db, security, storage
from .config import settings

log = logging.getLogger("privatube.health")

EVERY = 600                 # seconds between checks
REMIND = 86400              # repeat an unresolved alert once a day
CERT_WARN_DAYS = 14
WORKER_STALE = 900          # the web app warns if the worker hasn't checked in for this long


def problems(conn: sqlite3.Connection) -> list[dict]:
    """Everything that needs the admin's attention, as [{key, msg}]."""
    out: list[dict] = []
    t = db.now()

    settings.media_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(settings.media_dir).free
    if free < settings.min_free_bytes:
        out.append({"key": "disk", "msg": f"Disk nearly full ({free / 1024**3:.1f} GB free): downloads are "
                                         "paused. Delete videos in Admin → Videos to make room."})

    # The last few downloads all failed: usually YouTube changed something and yt-dlp
    # needs its daily update, or the server's IP is being blocked.
    last = conn.execute("SELECT status, error FROM jobs WHERE kind='download' AND status IN ('done','failed') "
                        "AND updated_at > ? ORDER BY updated_at DESC LIMIT 4", (t - 2 * 86400,)).fetchall()
    if len(last) >= 3 and all(r["status"] == "failed" for r in last):
        err = last[0]["error"] or ""
        if "not a bot" in err:
            have = settings.ytdlp_cookies.is_file()
            out.append({"key": "downloads", "msg": "YouTube is asking the server to sign in (“not a bot”): "
                        + ("the cookies have expired, upload fresh ones" if have else "upload cookies")
                        + " in Admin → YouTube login."})
        else:
            out.append({"key": "downloads", "msg": "The last downloads all failed: " + err[:200]})
    pause = db.kv_get(conn, "yt_pause")
    if pause and pause.get("streak", 0) >= 3:
        have = settings.ytdlp_cookies.is_file()
        out.append({"key": "bot_check", "msg": "YouTube keeps asking the server to confirm it's “not a bot” "
                    f"(since {_hours_ago(pause.get('since', t), t)}); downloads, subtitles and channel checks "
                    "are waiting. " + ("The cookies have probably expired: upload fresh ones" if have else
                                       "Upload cookies") + " in Admin → YouTube login, then press Test now."})
    n = conn.execute("SELECT COUNT(*) FROM channels WHERE subscribed=1 AND last_error IS NOT NULL").fetchone()[0]
    if n:
        out.append({"key": "channels", "msg": f"{n} subscribed channel(s) can't be checked for new videos."})

    backup = db.kv_get(conn, "backup") or {}
    if backup and t - backup.get("at", 0) > 2 * settings.backup_hours * 3600 + 3600:
        out.append({"key": "backup", "msg": "No database backup in the last "
                                            f"{2 * settings.backup_hours} hours (see the worker log)."})

    out += delivery_problems(conn)

    est = storage.queue_estimate(conn)
    space = storage.room(conn)
    if est["bytes"] > space:
        gb = 1024 ** 3
        out.append({"key": "queue_space", "msg": f"The download queue (~{est['bytes'] / gb:.0f} GB, {est['n']} videos) is "
                    f"bigger than the free space (~{space / gb:.0f} GB): downloads will pause when the disk is full. "
                    "Cancel some in Admin → Jobs, lower the quality, or delete old videos."})

    for host, port in _cert_hosts():
        days = cert_days_left(host, port)
        if days is None:
            out.append({"key": f"cert:{host}", "msg": f"Can't reach https://{host}:{port} to check its certificate."})
        elif days < CERT_WARN_DAYS:
            out.append({"key": f"cert:{host}", "msg": f"Certificate for {host} expires in {days} days "
                                                      "(certbot should have renewed it)."})
    return out


def _hours_ago(at: int, now: int) -> str:
    h = max(0, now - at) // 3600
    return f"{h} h ago" if h else "less than an hour ago"


def delivery_problems(conn: sqlite3.Connection) -> list[dict]:
    """Fetch one byte of the newest video the way a viewer's browser does (through nginx and
    its signature check). Catches a wrong media secret, permissions or nginx config."""
    v = conn.execute("SELECT path FROM videos WHERE status='ready' AND path IS NOT NULL "
                     "ORDER BY ready_at DESC LIMIT 1").fetchone()
    if v is None or settings.serve_media:
        return []
    err = fetch_first_byte(settings.site_url + security.sign_media(v["path"], "health"))
    if err:
        return [{"key": "delivery", "msg": f"Videos can't be fetched from {settings.site_url}: {err}. "
                                           "Run sudo bash ./deploy/check-signature.sh"}]
    return []


def fetch_first_byte(url: str) -> str | None:
    """None if the link works, else what went wrong."""
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": "PrivaTube-health"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status not in (200, 206):
                return f"HTTP {resp.status}"
            if not (resp.headers.get("Content-Type") or "").startswith("video/"):
                return f"wrong content type {resp.headers.get('Content-Type')}"
    except urllib.error.HTTPError as e:
        return {403: "HTTP 403 (link refused)", 404: "HTTP 404 (file missing)",
                410: "HTTP 410 (link expired)"}.get(e.code, f"HTTP {e.code}")
    except OSError as e:
        return f"no connection ({e})"
    return None


def _cert_hosts() -> list[tuple[str, int]]:
    u = urllib.parse.urlsplit(settings.site_url)
    return [(u.hostname, u.port or 443)] if u.scheme == "https" and u.hostname else []


def cert_days_left(host: str, port: int) -> int | None:
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=10) as s, \
                ctx.wrap_socket(s, server_hostname=host) as tls:
            not_after = ssl.cert_time_to_seconds(tls.getpeercert()["notAfter"])
    except (OSError, ssl.SSLError, KeyError, ValueError):
        return None
    return int((not_after - time.time()) // 86400)


def run(conn: sqlite3.Connection, force: bool = False) -> list[dict] | None:
    t = db.now()
    if t - db.kv_get(conn, "worker_seen", 0) >= 60:
        db.kv_set(conn, "worker_seen", t)
    if not force and t - db.kv_get(conn, "health_at", 0) < EVERY:
        return None
    db.kv_set(conn, "health_at", t)
    now = problems(conn)
    db.kv_set(conn, "health", now)
    _alert(conn, now)
    return now


def _alert(conn: sqlite3.Connection, now: list[dict]) -> None:
    if not settings.alert_url:
        return
    t = db.now()
    sent = db.kv_get(conn, "alerted", {})          # key -> time last alerted
    new = [p for p in now if t - sent.get(p["key"], 0) > REMIND]
    if new:
        lines = "\n\n".join(f"- {p['msg']}" for p in now)
        if _push(f"[PrivaTube] {len(now)} problem(s): {new[0]['msg'][:60]}", lines):
            for p in now:
                sent[p["key"]] = t
    elif sent and not now:
        if _push("[PrivaTube] All clear", "Everything is working again."):
            sent = {}
    sent = {k: v for k, v in sent.items() if any(p["key"] == k for p in now)} if now else sent
    db.kv_set(conn, "alerted", sent)


def _push(title: str, text: str) -> bool:
    data = urllib.parse.urlencode({"title": title, "desp": text, "body": text}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(settings.alert_url, data=data), timeout=20):
            return True
    except OSError as e:
        log.warning("alert push failed: %s", e)
        return False


def for_admin(conn: sqlite3.Connection) -> list[dict]:
    """Problems for the admin banner, plus a stale-worker check done from the web app."""
    out = list(db.kv_get(conn, "health", []) or [])
    seen = db.kv_get(conn, "worker_seen", 0)
    if db.now() - seen > WORKER_STALE:
        out.insert(0, {"key": "worker", "msg": "The worker (downloads, backups) isn't running: "
                                              "sudo systemctl status privatube-worker"})
    return out
