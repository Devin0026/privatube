"""Background worker: polls subscribed channels and downloads videos with yt-dlp.

Run with:  python -m privatube.worker
One download at a time (small servers have 1 GB RAM); the queue lives in SQLite.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import random
import shutil
import signal
import threading
import sqlite3
import time
from pathlib import Path

from . import backup, db, health, jobs, keywords, subs
from .jobs import Defer, MembersOnly, Permanent
from .config import settings
from .youtube import watch_url

log = logging.getLogger("privatube.worker")

# Set on SIGTERM (systemctl restart, installer). systemd signals the whole service, so a
# running rsync/ssh/ffmpeg dies with it; that is a restart, not a failed attempt.
STOPPING = threading.Event()
PAUSE_BETWEEN_DOWNLOADS = (int(os.environ.get("PT_PAUSE_MIN", 20)),
                           int(os.environ.get("PT_PAUSE_MAX", 60)))
PERMANENT_ERRORS = ("Private video", "Video unavailable", "This video has been removed",
                    "account associated with this video has been terminated",
                    "This video is not available")
MEMBERS_ERRORS = ("members-only", "Join this channel", "available to this channel's members")
# yt-dlp's "availability" values that need a paid membership or YouTube Premium
RESTRICTED = ("subscriber_only", "premium_only")
LIVE = ("is_live", "is_upcoming", "post_live")
# Jobs that talk to YouTube. When YouTube asks to "confirm you're not a bot", all of them
# pause together (30 min, doubling up to 6 h) instead of each one retrying on its own.
WORKER = "main"                 # lease owner name in the jobs table
YT_KINDS = ("channel_sync", "download", "comments", "subs", "channel_info")
BOT_PAUSE, BOT_PAUSE_MAX = 1800, 6 * 3600
MEMBERS_MSG = "Members-only video: not downloaded (the public version is, if the channel posts one)"




# ----------------------------------------------------------------- yt-dlp glue

class _YtLogger:
    def debug(self, msg):
        if not msg.startswith("[debug] "):
            log.debug(msg)

    def info(self, msg):
        log.debug(msg)

    def warning(self, msg):
        log.warning(msg)

    def error(self, msg):
        log.error(msg)


def _base_opts() -> dict:
    opts = {
        "quiet": True,
        "noprogress": True,
        "logger": _YtLogger(),
        "retries": 3,
        "socket_timeout": 30,
        "sleep_interval_requests": 1,
        "noplaylist": True,
    }
    if settings.ytdlp_ipv4:
        opts["source_address"] = "0.0.0.0"
    if settings.ytdlp_cookies.is_file():
        # Uploaded in Admin -> YouTube login. yt-dlp rewrites the file on exit (cookies
        # rotate), so it must stay writable by the privatube user.
        opts["cookiefile"] = str(settings.ytdlp_cookies)
    return opts


def _classify(e: Exception) -> Exception:
    msg = str(e)
    if any(p.lower() in msg.lower() for p in MEMBERS_ERRORS):
        return MembersOnly(MEMBERS_MSG)
    if any(p.lower() in msg.lower() for p in PERMANENT_ERRORS):
        return Permanent(msg)
    return e


def probe(video_yt_id: str = "jNQXAC9IVRw") -> str:
    """Can we get past YouTube's checks right now? Returns the title or raises."""
    from yt_dlp import YoutubeDL
    with YoutubeDL(_base_opts() | {"skip_download": True}) as ydl:
        return ydl.extract_info(watch_url(video_yt_id), download=False).get("title") or "?"


def _fetch_avatar(ydl, thumbs: list[dict]) -> bytes | None:
    """The channel picture: a square one <= 900px ("avatar_uncropped" is often 500 KB+).
    The wide ones are the banner."""
    squares = [t for t in thumbs if t.get("width") and t.get("width") == t.get("height")
               and t["width"] <= 900]
    pick = max(squares, key=lambda t: t["width"], default=None) or next(
        (t for t in thumbs if t.get("id") == "avatar_uncropped"), None)
    if pick and pick.get("url"):
        try:
            return ydl.urlopen(pick["url"]).read()
        except Exception as e:
            log.warning("avatar download failed: %s", e)
    return None


def channel_info(url: str) -> dict:
    """Name, handle, description and picture of a channel, without its video list: one
    request for the first page of /videos, cut to one entry."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
    try:
        with YoutubeDL(_base_opts() | {"extract_flat": "in_playlist", "playlistend": 1}) as ydl:
            info = ydl.extract_info(url + "/videos", download=False)
            return {"title": info.get("channel") or info.get("uploader") or info.get("title"),
                    "handle": info.get("uploader_id"), "description": info.get("description"),
                    "avatar": _fetch_avatar(ydl, info.get("thumbnails") or [])}
    except DownloadError as e:
        raise _classify(e) from e


def list_channel(url: str, include_shorts: bool, limit: int) -> dict:
    """Flat listing of a channel's uploads (no per-video requests)."""
    from yt_dlp import YoutubeDL

    opts = _base_opts() | {"extract_flat": "in_playlist", "playlistend": limit,
                           "skip_download": True, "noplaylist": False,
                           # "3 days ago" -> approximate timestamp for not-yet-downloaded videos
                           "extractor_args": {"youtubetab": {"approximate_date": [""]}}}
    # Titles in a second language (PT_TITLE_LANG): YouTube lists in English by default,
    # and that listing is also the only one with upload dates. One extra page request.
    localized: dict[str, str] = {}
    if settings.title_lang and settings.title_lang != "en":
        lopts = opts | {"extractor_args": {"youtube": {"lang": [settings.title_lang]}}}
        try:
            with YoutubeDL(lopts) as ydl:
                for tab in ["/videos"] + (["/shorts"] if include_shorts else []):
                    li = ydl.extract_info(url + tab, download=False)
                    localized |= {e["id"]: e.get("title") for e in (li.get("entries") or [])
                                  if e and e.get("id") and e.get("title")}
        except Exception as e:   # nice to have; the English listing below is what counts
            log.info("%s listing failed for %s: %s", settings.title_lang, url, e)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url + "/videos", download=False)
        entries = [(e, False) for e in (info.get("entries") or []) if e and e.get("id")]
        if include_shorts:
            try:
                s = ydl.extract_info(url + "/shorts", download=False)
                entries += [(e, True) for e in (s.get("entries") or []) if e and e.get("id")]
            except Exception as e:  # channel without a Shorts tab
                log.info("no shorts for %s: %s", url, e)
        avatar = _fetch_avatar(ydl, info.get("thumbnails") or [])
    return {
        "yt_id": info.get("channel_id") or info.get("id"),
        "title": info.get("channel") or info.get("uploader") or info.get("title"),
        "handle": info.get("uploader_id"),
        "description": info.get("description"),
        "avatar": avatar,
        "entries": [{"id": e["id"], "title": localized.get(e["id"]) or e.get("title"),
                     "alt_title": e.get("title") if localized.get(e["id"], e.get("title")) != e.get("title") else None,
                     "duration": e.get("duration"), "timestamp": e.get("timestamp"), "is_short": short,
                     "members": e.get("availability") in RESTRICTED}
                    for e, short in entries],
    }


def _comment_args() -> dict:
    n = settings.max_comments
    return {"getcomments": n > 0,
            "extractor_args": {"youtube": {"max_comments": [str(n), "all", "all", "20"],
                                           "comment_sort": ["top"]}}}


def fetch_subs(video_yt_id: str, folder: Path, skip: set[str] = frozenset()) -> tuple[list[dict], list[str]]:
    """Write sub.<lang>.vtt files into `folder`. Returns ([{lang, auto, file}], [langs to
    retry later]). A track that fails is skipped, so one bad language doesn't lose the others;
    languages in `skip` are already on disk."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    out, retry = [], []
    try:
        with YoutubeDL(_base_opts() | {"skip_download": True}) as ydl:
            raw = ydl.extract_info(watch_url(video_yt_id), download=False, process=False)
            for key, auto in subs.pick(raw.get("subtitles"), raw.get("automatic_captions")):
                lang = key.removesuffix("-orig")
                if lang in skip:
                    continue
                tracks = (raw.get("automatic_captions") if auto else raw.get("subtitles")) or {}
                fmt = next((t for t in tracks.get(key) or [] if t.get("ext") == "json3"), None)
                if not fmt:
                    continue
                try:
                    data = json.loads(ydl.urlopen(fmt["url"]).read())
                except Exception as e:
                    log.info("subtitle %s for %s failed: %s", key, video_yt_id, e)
                    if subs.is_translation(key, auto):
                        retry.append(lang)
                    continue
                folder.mkdir(parents=True, exist_ok=True)
                name = f"sub.{lang}.vtt"
                (folder / name).write_text(subs.json3_to_vtt(data), encoding="utf-8")
                out.append({"lang": lang, "auto": auto, "file": name})
    except DownloadError as e:
        raise _classify(e) from e
    return out, retry


def fetch_comments(video_yt_id: str) -> dict:
    """Metadata + comments only, no media download."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    opts = _base_opts() | _comment_args() | {"skip_download": True}
    try:
        with YoutubeDL(opts) as ydl:
            return ydl.sanitize_info(ydl.extract_info(watch_url(video_yt_id), download=False))
    except DownloadError as e:
        raise _classify(e) from e


def download(video_yt_id: str, workdir: Path, on_progress, height: int | None = None) -> dict:
    """Download one video into `workdir`; returns the sanitized info dict."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                on_progress(d.get("downloaded_bytes", 0) / total)

    def reject(info, *, incomplete=False):
        if info.get("live_status") in LIVE:
            return "live or upcoming"
        if info.get("availability") in RESTRICTED:   # even if the cookies' account is a member
            return "members-only"
        return None

    opts = _base_opts() | _comment_args() | {
        "format": settings.ytdlp_format.replace("{h}", str(height or settings.default_height)),
        "merge_output_format": "mp4",
        "outtmpl": {"default": str(workdir / "video.%(ext)s"),
                    "thumbnail": str(workdir / "thumb.%(ext)s")},
        "writethumbnail": True,
        "postprocessors": [{"key": "FFmpegThumbnailsConvertor", "format": "jpg",
                            "when": "before_dl"}],
        "progress_hooks": [hook],
        "match_filter": reject,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(watch_url(video_yt_id), download=True)
            # a rejected video comes back without a file
            if info is None or info.get("live_status") in LIVE:
                raise Defer(3600, "live or upcoming; will retry")
            if info.get("availability") in RESTRICTED:
                raise MembersOnly(MEMBERS_MSG)
            return ydl.sanitize_info(info)
    except DownloadError as e:
        raise _classify(e) from e


# ----------------------------------------------------------------- DB updates

def _upsert_channel_from_video(conn: sqlite3.Connection, info: dict) -> int | None:
    cid = info.get("channel_id")
    if not cid:
        return None
    row = conn.execute("SELECT id FROM channels WHERE yt_id=?", (cid,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO channels(yt_id,url,handle,title,subscribed,auto_download,poll_minutes,"
        "baseline_done,created_at) VALUES (?,?,?,?,0,0,?,0,?)",
        (cid, f"https://www.youtube.com/channel/{cid}", info.get("uploader_id"),
         info.get("channel") or info.get("uploader"), settings.poll_minutes_default, db.now()))
    jobs.enqueue(conn, "channel_info", channel_id=cur.lastrowid, priority=PRIO_CHANNEL_INFO)
    return cur.lastrowid


def _published(info: dict) -> int | None:
    if info.get("timestamp"):
        return int(info["timestamp"])
    d = info.get("upload_date")
    if d and len(d) == 8:
        return int(dt.datetime.strptime(d, "%Y%m%d").replace(tzinfo=dt.timezone.utc).timestamp())
    return None


def apply_channel_listing(conn: sqlite3.Connection, channel: sqlite3.Row, listing: dict) -> int:
    """Store a channel listing. Returns number of newly queued downloads."""
    ch_id = channel["id"]
    queued = 0
    with db.tx(conn):
        if not conn.execute("SELECT 1 FROM channels WHERE id=?", (ch_id,)).fetchone():
            return 0  # removed by the admin while the list was being fetched
        yt_id = listing["yt_id"]
        other = conn.execute("SELECT id FROM channels WHERE yt_id=? AND id<>?",
                             (yt_id, ch_id)).fetchone()
        if other:  # channel existed already (e.g. from a single video): merge into this row
            conn.execute("UPDATE videos SET channel_id=? WHERE channel_id=?", (ch_id, other["id"]))
            conn.execute("UPDATE code_scopes SET channel_id=? WHERE channel_id=?",
                         (ch_id, other["id"]))
            conn.execute("DELETE FROM channels WHERE id=?", (other["id"],))
        avatar_rel = channel["avatar"]
        if listing.get("avatar"):
            d = settings.media_dir / yt_id
            d.mkdir(parents=True, exist_ok=True)
            (d / "avatar.jpg").write_bytes(listing["avatar"])
            avatar_rel = f"{yt_id}/avatar.jpg"
        conn.execute(
            "UPDATE channels SET yt_id=?, title=?, handle=?, description=?, avatar=?, "
            "last_polled_at=?, last_error=NULL WHERE id=?",
            (yt_id, listing["title"], listing["handle"], listing["description"], avatar_rel,
             db.now(), ch_id))
        auto_channel = channel["baseline_done"] and channel["auto_download"]
        for e in listing["entries"]:
            row = conn.execute("SELECT id, channel_id, status, members FROM videos WHERE yt_id=?",
                               (e["id"],)).fetchone()
            alt = e.get("alt_title")
            members = bool(e.get("members"))
            # New upload: auto-download only if a title passes the channel's keyword filter.
            # Members-only videos never: the channel's public version is taken instead.
            auto = auto_channel and not members and keywords.matches(
                keywords.titles(e["title"], alt), channel["include_keywords"], channel["exclude_keywords"])
            if row:
                if row["channel_id"] is None:
                    conn.execute("UPDATE videos SET channel_id=? WHERE id=?", (ch_id, row["id"]))
                # keep titles fresh for not-yet-downloaded videos; fill in the other language
                conn.execute("UPDATE videos SET title=CASE WHEN status='listed' THEN ? ELSE title END, "
                             "alt_title=COALESCE(?, alt_title), members=? WHERE id=?",
                             (e["title"], alt, int(members), row["id"]))
                if members and row["status"] in ("queued", "failed"):   # queued before we knew
                    conn.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE kind='download' "
                                 "AND video_id=? AND status='pending'", (db.now(), row["id"]))
                    conn.execute("UPDATE videos SET status='listed', error=? WHERE id=?",
                                 (MEMBERS_MSG, row["id"]))
                elif row["members"] and not members and row["status"] == "listed":
                    # "members first": the same video is public now
                    conn.execute("UPDATE videos SET error=NULL WHERE id=?", (row["id"],))
                    if auto:
                        conn.execute("UPDATE videos SET status='queued' WHERE id=?", (row["id"],))
                        jobs.queue_download(conn, row["id"])
                        queued += 1
                continue
            cur = conn.execute(
                "INSERT INTO videos(yt_id,channel_id,title,alt_title,duration,published_at,is_short,"
                "members,status,added_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (e["id"], ch_id, e["title"], alt, e["duration"], e["timestamp"], int(e["is_short"]),
                 int(members), "queued" if auto else "listed", db.now()))
            if auto:
                jobs.queue_download(conn, cur.lastrowid)
                queued += 1
        if not channel["baseline_done"]:     # first listing: "latest 20" / "last 3 months" etc.
            queued += jobs.queue_range(conn, ch_id, channel["backfill"])
        conn.execute("UPDATE channels SET baseline_done=1 WHERE id=?", (ch_id,))
    return queued


def apply_download(conn: sqlite3.Connection, video: sqlite3.Row, info: dict, workdir: Path) -> None:
    files = [p for p in workdir.iterdir() if p.name.startswith("video.") and p.suffix != ".part"]
    if not files:
        raise RuntimeError("yt-dlp finished but no video file found")
    vfile = files[0]
    thumb = next((p.name for p in workdir.iterdir() if p.name.startswith("thumb.")), None)
    with db.tx(conn):
        ch_id = video["channel_id"] or _upsert_channel_from_video(conn, info)
        ch_yt = None
        if ch_id:
            r = conn.execute("SELECT yt_id FROM channels WHERE id=?", (ch_id,)).fetchone()
            ch_yt = r and r["yt_id"]
        rel_dir = f"{ch_yt or '_unknown'}/{video['yt_id']}"
        dest = settings.media_dir / rel_dir
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(workdir), str(dest))
        comments = info.get("comments") or []
        _store_comments(conn, video["id"], comments)
        conn.execute(
            # Keep the title from the channel listing (in PT_TITLE_LANG, if set) if we have one;
            # the video's own original-language title then goes to alt_title.
            """UPDATE videos SET channel_id=?, title=COALESCE(title, ?),
                   alt_title=CASE WHEN title IS NOT NULL AND title<>? THEN ? ELSE alt_title END,
                   description=?, duration=?,
                   published_at=COALESCE(?, published_at), width=?, height=?, filesize=?,
                   path=?, thumb=?, status='ready', ready_at=?, error=NULL,
                   comment_count=?, comments_at=? WHERE id=?""",
            (ch_id, info.get("title"), info.get("title"), info.get("title"),
             info.get("description"), info.get("duration"),
             _published(info), info.get("width"), info.get("height"),
             (dest / vfile.name).stat().st_size,
             f"{rel_dir}/{vfile.name}", thumb, db.now(),
             info.get("comment_count") or len(comments), db.now(), video["id"]))


def _store_comments(conn: sqlite3.Connection, video_id: int, comments: list[dict]) -> None:
    conn.execute("DELETE FROM comments WHERE video_id=?", (video_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO comments(video_id,yt_id,parent,author,text,likes,"
        "published_at,is_pinned) VALUES (?,?,?,?,?,?,?,?)",
        [(video_id, c.get("id"), c.get("parent") or "root", c.get("author"),
          c.get("text"), c.get("like_count"), c.get("timestamp"), int(bool(c.get("is_pinned"))))
         for c in comments if c.get("id")])


def apply_comments(conn: sqlite3.Connection, video_id: int, info: dict) -> None:
    comments = info.get("comments") or []
    with db.tx(conn):
        if comments:   # an empty fetch (hiccup, comments turned off) keeps what we have
            _store_comments(conn, video_id, comments)
            conn.execute("UPDATE videos SET comment_count=? WHERE id=?",
                         (info.get("comment_count") or len(comments), video_id))
        conn.execute("UPDATE videos SET comments_at=? WHERE id=?", (db.now(), video_id))


# ----------------------------------------------------------------- job runners

def run_channel_sync(conn: sqlite3.Connection, job: sqlite3.Row) -> None:
    ch = conn.execute("SELECT * FROM channels WHERE id=?", (job["channel_id"],)).fetchone()
    if ch is None:
        return
    limit = 30 if ch["baseline_done"] else 200
    try:
        listing = list_channel(ch["url"], bool(ch["include_shorts"]), limit)
    except Exception as e:
        conn.execute("UPDATE channels SET last_error=? WHERE id=?", (str(e)[:500], ch["id"]))
        raise _classify(e) from e
    n = apply_channel_listing(conn, ch, listing)
    log.info("channel %s synced: %d entries, %d queued", ch["url"], len(listing["entries"]), n)


def run_download(conn: sqlite3.Connection, job: sqlite3.Row) -> None:
    v = conn.execute("SELECT * FROM videos WHERE id=?", (job["video_id"],)).fetchone()
    if v is None or v["status"] == "ready":
        return
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(settings.media_dir).free
    if free < settings.min_free_bytes:     # free space by deleting videos in Admin -> Videos
        raise Defer(900, f"low disk: {free // 1024**2} MiB free")
    workdir = settings.media_dir / ".tmp" / v["yt_id"]
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    conn.execute("UPDATE videos SET status='downloading', error=NULL WHERE id=?", (v["id"],))
    last = [0.0]

    def progress(frac: float) -> None:
        if time.time() - last[0] > 5:
            last[0] = time.time()
            conn.execute("UPDATE jobs SET progress=?, lease_until=? WHERE id=?",
                         (round(frac, 3), db.now() + 3600, job["id"]))

    try:
        h = conn.execute("SELECT COALESCE(v.max_height, c.max_height) FROM videos v LEFT JOIN channels c "
                         "ON c.id=v.channel_id WHERE v.id=?", (v["id"],)).fetchone()[0]
        info = download(v["yt_id"], workdir, progress, h)
        apply_download(conn, v, info, workdir)
        jobs.enqueue(conn, "subs", video_id=v["id"], priority=PRIO_SUBS)
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    log.info("downloaded %s (%s)", v["yt_id"], info.get("title"))


def run_subs(conn: sqlite3.Connection, job: sqlite3.Row) -> None:
    v = conn.execute("SELECT id, yt_id, path, subs FROM videos WHERE id=? AND status='ready'",
                     (job["video_id"],)).fetchone()
    if v is None or not v["path"]:
        return
    have = subs.loads(v["subs"])
    new, retry = fetch_subs(v["yt_id"], settings.media_dir / v["path"].rsplit("/", 1)[0],
                            skip={t["lang"] for t in have})
    tracks = have + new
    conn.execute("UPDATE videos SET subs=?, subs_at=? WHERE id=?",
                 (json.dumps(tracks) if tracks else None, db.now(), v["id"]))
    tries = conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='subs' AND video_id=?", (v["id"],)).fetchone()[0]
    if retry and tries < SUBS_TRIES:   # YouTube limits machine translations: try again another day
        conn.execute("UPDATE jobs SET status='done', updated_at=? WHERE id=?", (db.now(), job["id"]))
        jobs.enqueue(conn, "subs", video_id=v["id"], priority=PRIO_SUBS, delay=tries * 86400)


def run_comments(conn: sqlite3.Connection, job: sqlite3.Row) -> None:
    v = conn.execute("SELECT id, yt_id FROM videos WHERE id=? AND status='ready'",
                     (job["video_id"],)).fetchone()
    if v is None:
        return
    apply_comments(conn, v["id"], fetch_comments(v["yt_id"]))


def run_channel_info(conn: sqlite3.Connection, job: sqlite3.Row) -> None:
    """Picture and description for a channel known only from single videos (it is never
    listed, so nothing else would fetch them). Its videos are left alone."""
    ch = conn.execute("SELECT id, url, yt_id, avatar FROM channels WHERE id=?", (job["channel_id"],)).fetchone()
    if ch is None or ch["avatar"]:
        return
    info = channel_info(ch["url"])
    avatar_rel = None
    if info["avatar"] and ch["yt_id"]:
        d = settings.media_dir / ch["yt_id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "avatar.jpg").write_bytes(info["avatar"])
        avatar_rel = f"{ch['yt_id']}/avatar.jpg"
    conn.execute("UPDATE channels SET avatar=COALESCE(?, avatar), title=COALESCE(title, ?), "
                 "handle=COALESCE(handle, ?), description=COALESCE(description, ?) WHERE id=?",
                 (avatar_rel, info["title"], info["handle"], info["description"], ch["id"]))


RUNNERS = {"channel_sync": run_channel_sync, "download": run_download, "comments": run_comments,
           "subs": run_subs, "channel_info": run_channel_info}
PRIO_CHANNEL_INFO = 210         # cosmetic: after everything else
PRIO_SUBS = 150
SUBS_TRIES = 4
COMMENT_REFRESH_DAYS = 30       # re-crawl comments daily while a video is this young
PRIO_COMMENTS = 200             # after downloads and channel checks


def schedule_polls(conn: sqlite3.Connection) -> None:
    t = db.now()
    due = conn.execute(
        "SELECT id FROM channels WHERE subscribed=1 AND baseline_done=1 AND "
        "(last_polled_at IS NULL OR last_polled_at + poll_minutes*60 <= ?)", (t,)).fetchall()
    for r in due:
        jobs.enqueue(conn, "channel_sync", channel_id=r["id"], priority=50)
        conn.execute("UPDATE channels SET last_polled_at=? WHERE id=?", (t, r["id"]))
    # Pictures for channels known only from single videos (or whose picture failed).
    for r in conn.execute(
            "SELECT c.id FROM channels c WHERE c.avatar IS NULL AND c.yt_id IS NOT NULL AND c.subscribed=0 "
            "AND NOT EXISTS "
            "(SELECT 1 FROM jobs j WHERE j.kind IN ('channel_info','channel_sync') AND j.channel_id=c.id "
            "AND (j.status IN ('pending','running') OR j.updated_at > ?)) LIMIT 3", (t - 7 * 86400,)).fetchall():
        jobs.enqueue(conn, "channel_info", channel_id=r["id"], priority=PRIO_CHANNEL_INFO)
    # Subtitles for videos downloaded before subtitles existed (a few at a time).
    for r in conn.execute("SELECT id FROM videos WHERE status='ready' AND subs_at IS NULL LIMIT 5").fetchall():
        jobs.enqueue(conn, "subs", video_id=r["id"], priority=PRIO_SUBS)
        conn.execute("UPDATE videos SET subs_at=? WHERE id=?", (t, r["id"]))
    # Comments of videos published in the last month: refresh once a day.
    stale = conn.execute(
        "SELECT id FROM videos WHERE status='ready' AND COALESCE(published_at, ready_at) > ? "
        "AND COALESCE(comments_at, 0) <= ? LIMIT 20",
        (t - COMMENT_REFRESH_DAYS * 86400, t - 86400)).fetchall()
    for r in stale:
        jobs.enqueue(conn, "comments", video_id=r["id"], priority=PRIO_COMMENTS)
        # Marks the attempt too, so a failing video is retried tomorrow, not in a loop.
        conn.execute("UPDATE videos SET comments_at=? WHERE id=?", (t, r["id"]))


def retry_bot_failures(conn: sqlite3.Connection) -> int:
    """After YouTube access is fixed (new cookies): rerun every job that failed or is waiting
    because of the "not a bot" check, right away."""
    resume_youtube(conn)
    t = db.now()
    rows = conn.execute("SELECT id, kind, video_id FROM jobs WHERE status IN ('failed','pending') "
                        "AND error LIKE '%not a bot%'").fetchall()
    for j in rows:
        if j["video_id"] and conn.execute("SELECT 1 FROM jobs WHERE kind=? AND video_id=? AND status IN "
                                          "('pending','running') AND id<>?", (j["kind"], j["video_id"], j["id"])
                                          ).fetchone():
            continue   # a newer job for the same thing is already queued
        conn.execute("UPDATE jobs SET status='pending', attempts=0, run_after=?, error=NULL, updated_at=? "
                     "WHERE id=?", (t, t, j["id"]))
        if j["kind"] == "download":
            conn.execute("UPDATE videos SET status='queued' WHERE id=? AND status='failed'", (j["video_id"],))
    return len(rows)


def _mark_video_failed(conn, job, err: str, gave_up: bool) -> None:
    if job["video_id"] and job["kind"] == "download":
        conn.execute("UPDATE videos SET status=?, error=? WHERE id=?",
                     ("failed" if gave_up else "queued", err[:500], job["video_id"]))


def is_bot_check(msg: str) -> bool:
    return "not a bot" in msg or "Sign in to confirm" in msg


def youtube_paused(conn: sqlite3.Connection) -> dict | None:
    p = db.kv_get(conn, "yt_pause")
    return p if p and p.get("until", 0) > db.now() else None


def pause_youtube(conn: sqlite3.Connection) -> int:
    """YouTube asked for the bot check: stop asking it anything for a while. Returns seconds."""
    p = db.kv_get(conn, "yt_pause") or {}
    streak = p.get("streak", 0) + 1
    wait = min(BOT_PAUSE * 2 ** (streak - 1), BOT_PAUSE_MAX)
    db.kv_set(conn, "yt_pause", {"until": db.now() + wait, "streak": streak, "since": p.get("since") or db.now()})
    log.warning("YouTube bot check (%d in a row): pausing YouTube jobs for %d min", streak, wait // 60)
    return wait


def resume_youtube(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM kv WHERE k='yt_pause'")


def run_one(conn: sqlite3.Connection) -> bool:
    """Claim and run one job. Returns True if a job ran."""
    jobs.recover(conn)
    schedule_polls(conn)
    try:
        backup.backup_db(conn)
    except Exception:
        log.exception("database backup failed")
    try:
        health.run(conn)
    except Exception:   # never let monitoring take the worker down
        log.exception("health check failed")
    kinds = tuple(k for k in RUNNERS if not (k in YT_KINDS and youtube_paused(conn)))
    job = jobs.claim(conn, WORKER, lease=3600, kinds=kinds)
    if job is None:
        return False
    log.info("job %d: %s video=%s channel=%s (attempt %d)", job["id"], job["kind"],
             job["video_id"], job["channel_id"], job["attempts"])
    try:
        RUNNERS[job["kind"]](conn, job)
    except Defer as e:
        log.info("job %d deferred %ds: %s", job["id"], e.seconds, e)
        jobs.defer(conn, job["id"], e.seconds, str(e))
        if job["kind"] == "download":
            conn.execute("UPDATE videos SET status='queued' WHERE id=? AND status='downloading'",
                         (job["video_id"],))
    except MembersOnly as e:      # skipped on purpose: "cancelled", not a failure
        log.info("job %d: %s", job["id"], e)
        conn.execute("UPDATE jobs SET status='cancelled', error=?, lease_until=NULL, updated_at=? WHERE id=?",
                     (str(e), db.now(), job["id"]))
        if job["video_id"]:
            conn.execute("UPDATE videos SET status='listed', members=1, error=? WHERE id=?",
                         (str(e), job["video_id"]))
    except Permanent as e:
        log.warning("job %d failed permanently: %s", job["id"], e)
        jobs.fail(conn, job, str(e), permanent=True)
        _mark_video_failed(conn, job, str(e), True)
    except Exception as e:
        if job["kind"] in YT_KINDS and is_bot_check(str(e)):
            wait = pause_youtube(conn)
            jobs.defer(conn, job["id"], wait, "YouTube asked to confirm “not a bot”; all YouTube work "
                       f"paused for {wait // 60} min (cookies: Admin → YouTube login)")
            if job["kind"] == "download":
                conn.execute("UPDATE videos SET status='queued' WHERE id=? AND status='downloading'",
                             (job["video_id"],))
            return True
        if STOPPING.is_set():
            log.info("job %d interrupted by shutdown (%s); continues after the restart", job["id"], e)
            jobs.defer(conn, job["id"], 30, "interrupted by a worker restart; continues shortly")
            if job["kind"] == "download":
                conn.execute("UPDATE videos SET status='queued', error=NULL WHERE id=?", (job["video_id"],))
            return True
        log.exception("job %d failed", job["id"])
        # A failed comment refresh is simply tried again tomorrow.
        gave_up = jobs.fail(conn, job, f"{type(e).__name__}: {e}", permanent=job["kind"] == "comments")
        _mark_video_failed(conn, job, str(e), gave_up)
    else:
        jobs.finish(conn, job["id"])
        if job["kind"] in YT_KINDS and db.kv_get(conn, "yt_pause"):
            resume_youtube(conn)           # YouTube answers again
        if job["kind"] in ("download", "comments", "subs"):   # don't hammer YouTube
            STOPPING.wait(random.randint(*PAUSE_BETWEEN_DOWNLOADS))
    return True


def main() -> None:
    logging.basicConfig(level=os.environ.get("PT_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.migrate()
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    STOPPING.clear()
    signal.signal(signal.SIGTERM, lambda *_: STOPPING.set())
    with db.get() as conn:
        jobs.recover(conn, node=WORKER)
        conn.execute("UPDATE videos SET status='queued' WHERE status='downloading'")
        log.info("worker started, media at %s", settings.media_dir)
        while not STOPPING.is_set():
            if not run_one(conn):
                STOPPING.wait(10)


if __name__ == "__main__":
    main()
