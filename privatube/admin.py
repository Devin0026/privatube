"""Admin panel: add channels/videos, pick back-catalog, users, access codes, jobs."""
from __future__ import annotations

import re
import secrets
import shutil
import sqlite3
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from . import backup, db, jobs, keywords, security, youtube
from .config import settings
from .security import Principal
from .web import Redirect, form_with_csrf, get_conn, need_admin, render

router = APIRouter(prefix="/admin")

PRIO_ADMIN_SYNC = 10
USERNAME_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no 0/o/1/l/i


def _back(url: str, msg: str):
    raise Redirect(f"{url}{'&' if '?' in url else '?'}msg={quote(msg)}")


def _ids(form, name: str) -> list[int]:
    out = []
    for v in form.getlist(name):
        try:
            out.append(int(v))
        except ValueError:
            pass
    return out


HEIGHTS = (360, 480, 720, 1080)


def _height(v) -> int:
    try:
        h = int(v)
    except (TypeError, ValueError):
        return settings.default_height
    return h if h in HEIGHTS else settings.default_height


def _queue_video(conn: sqlite3.Connection, video_id: int) -> None:
    jobs.queue_download(conn, video_id)


def _priority(v) -> int:
    try:
        return max(-1, min(1, int(v)))
    except (TypeError, ValueError):
        return 0


# ----------------------------------------------------------------- dashboard

@router.get("")
async def dashboard(request: Request, p: Principal = Depends(need_admin),
                    conn: sqlite3.Connection = Depends(get_conn)):
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM videos GROUP BY status")}
    stored = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(filesize), 0) bytes FROM videos "
                          "WHERE status='ready'").fetchone()
    running = conn.execute(
        "SELECT j.*, v.title, v.yt_id, c.url AS ch_url FROM jobs j "
        "LEFT JOIN videos v ON v.id=j.video_id LEFT JOIN channels c ON c.id=j.channel_id "
        "WHERE j.status='running'").fetchall()
    pending = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0]
    failed = conn.execute(
        "SELECT j.*, v.title, v.yt_id FROM jobs j LEFT JOIN videos v ON v.id=j.video_id "
        "WHERE j.status='failed' ORDER BY j.updated_at DESC LIMIT 10").fetchall()
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(settings.media_dir)
    from . import storage
    estimate = storage.queue_estimate(conn)
    space = storage.room(conn)
    return render(request, "admin/index.html", p, heights=HEIGHTS, default_height=settings.default_height,
                  ranges=jobs.RANGES, priorities=jobs.PRIORITY_NAMES, estimate=estimate, space=space,
                  counts=counts, stored=stored, running=running,
                  pending=pending, failed=failed, disk=disk, min_free=settings.min_free_bytes,
                  backup=db.kv_get(conn, "backup"), backup_keep=settings.backup_keep, max_add=MAX_ADD)


@router.post("/backup")
async def backup_now(request: Request, p: Principal = Depends(need_admin),
                     conn: sqlite3.Connection = Depends(get_conn)):
    await form_with_csrf(request, p)
    await run_in_threadpool(backup.backup_db, conn, True)
    _back("/admin", f"Database backed up to {settings.backup_dir}.")


@router.get("/youtube")
async def youtube_login(request: Request, p: Principal = Depends(need_admin)):
    f = settings.ytdlp_cookies
    info = None
    if f.is_file():
        text = f.read_text(errors="replace")
        info = {"at": int(f.stat().st_mtime),
                "n": sum(1 for line in text.splitlines() if "youtube.com" in line and not line.startswith("# "))}
    return render(request, "admin/youtube.html", p, info=info)


@router.post("/youtube/{action}")
async def youtube_login_action(request: Request, action: str, p: Principal = Depends(need_admin)):
    form = await form_with_csrf(request, p)
    f = settings.ytdlp_cookies
    if action == "upload":
        up = form.get("file")
        text = (await up.read()).decode("utf-8", "replace") if hasattr(up, "read") else ""
        text = text or str(form.get("text", ""))
        from .cookies import to_netscape
        try:
            text, fmt = to_netscape(text)
        except (ValueError, KeyError, TypeError) as e:
            _back("/admin/youtube", f"Couldn't read those cookies ({e}). Use a Netscape cookies.txt, "
                  "a JSON export, or a Cookie header string from youtube.com.")
        tmp = f.with_name(".cookies.txt.new")
        tmp.write_text(text)
        tmp.chmod(0o600)
        tmp.replace(f)
        _back("/admin/youtube", f"Cookies saved ({fmt} format). Press Test to check that YouTube accepts them.")
    if action == "remove":
        f.unlink(missing_ok=True)
        _back("/admin/youtube", "Cookies removed. Downloads now run without a Google account.")
    if action == "test":
        from . import worker
        try:
            title = await run_in_threadpool(worker.probe)
        except Exception as e:
            _back("/admin/youtube", f"Test failed: {str(e)[:300]}")
        with db.get() as conn:
            n = worker.retry_bot_failures(conn)
        _back("/admin/youtube", f"Test OK: YouTube returned “{title}”."
              + (f" {n} job(s) that failed on the bot check will run again now." if n else ""))
    raise HTTPException(404)


# ----------------------------------------------------------------- add

MAX_ADD = 50
_SPLIT = re.compile(r"[\s,，、;；]+")


@router.post("/add")
async def add(request: Request, p: Principal = Depends(need_admin),
              conn: sqlite3.Connection = Depends(get_conn)):
    """Add one or more videos/channels: links separated by new lines, spaces or commas.
    The quality, priority, range and keyword settings apply to every one of them."""
    form = await form_with_csrf(request, p)
    items = list(dict.fromkeys(x for x in _SPLIT.split(str(form.get("url", ""))[:20000]) if x))
    if not items:
        _back("/admin", "Paste at least one YouTube link.")
    if len(items) > MAX_ADD:
        _back("/admin", f"That's {len(items)} links; add at most {MAX_ADD} at a time.")
    kind_wanted = form.get("kind", "auto")
    height = _height(form.get("max_height"))
    prio = _priority(form.get("priority"))
    backfill = str(form.get("backfill", "")) if jobs.parse_range(str(form.get("backfill", ""))) else None
    queued, already, channels, bad, wrong = [], [], [], [], []
    seen = set()
    for raw in items:
        parsed = youtube.parse(raw)
        if parsed is None:
            bad.append(raw[:80])
            continue
        kind, value = parsed
        if (kind, value) in seen:
            continue
        seen.add((kind, value))
        if kind_wanted in ("video", "channel") and kind_wanted != kind:
            wrong.append(raw[:80])
        elif kind == "video":
            (queued if _add_video(conn, value, height) else already).append(value)
        else:
            channels.append(_add_channel(conn, value, form, height, prio, backfill))
    if len(items) == 1 and len(channels) == 1:      # one channel: go and watch its list arrive
        ch_id, n = channels[0]
        what = dict(jobs.RANGES).get(backfill or "", "")
        _back(f"/admin/channels/{ch_id}", "Fetching the channel's video list. " + (
              f"{n} video(s) queued. " if n else "") + (
              f"“{what}” will be queued once the list is in. " if backfill and not n else
              "Refresh in a moment, then tick the older videos you want." if not backfill else ""))
    if len(items) == 1 and not channels:
        if bad:
            _back("/admin", "Not a recognised YouTube video or channel URL.")
        if wrong:
            kind = youtube.parse(items[0])[0]
            _back("/admin", f"That URL looks like a {kind}, not a {kind_wanted}.")
        _back("/admin", f"Queued video {queued[0]}." if queued else "That video is already downloaded.")
    parts = []
    if queued:
        parts.append(f"Queued {len(queued)} video(s).")
    if channels:
        parts.append(f"Added {len(channels)} channel(s); their video lists are being fetched.")
    if already:
        parts.append(f"{len(already)} already downloaded.")
    if wrong:
        parts.append(f"Skipped {len(wrong)} that aren't a {kind_wanted}: {', '.join(wrong[:5])}.")
    if bad:
        parts.append(f"Not recognised ({len(bad)}): {', '.join(bad[:5])}{' …' if len(bad) > 5 else ''}.")
    _back("/admin", " ".join(parts))


def _add_video(conn: sqlite3.Connection, yt_id: str, height: int | None) -> bool:
    """Queue a single video. False if it is already downloaded."""
    with db.tx(conn):
        row = conn.execute("SELECT id, status FROM videos WHERE yt_id=?", (yt_id,)).fetchone()
        if row and row["status"] == "ready":
            return False
        vid = row["id"] if row else conn.execute(
            "INSERT INTO videos(yt_id,status,added_at) VALUES (?,'queued',?)", (yt_id, db.now())).lastrowid
        conn.execute("UPDATE videos SET max_height=? WHERE id=?", (height, vid))
        _queue_video(conn, vid)
    return True


def _add_channel(conn: sqlite3.Connection, url: str, form, height, prio, backfill) -> tuple[int, int]:
    """Subscribe to a channel (or update it) and fetch its list. Returns (id, videos queued now)."""
    with db.tx(conn):
        row = conn.execute("SELECT id FROM channels WHERE url=?", (url,)).fetchone()
        if row:
            ch_id = row["id"]
            conn.execute("UPDATE channels SET subscribed=1, max_height=?, priority=? WHERE id=?",
                         (height, prio, ch_id))
            jobs.reprioritise_channel(conn, ch_id)
            n = jobs.queue_range(conn, ch_id, backfill)
        else:
            n = 0
            ch_id = conn.execute(
                "INSERT INTO channels(url,subscribed,auto_download,include_shorts,poll_minutes,"
                "include_keywords,exclude_keywords,max_height,priority,backfill,created_at) "
                "VALUES (?,1,?,?,?,?,?,?,?,?,?)",
                (url, int(form.get("auto_download") == "1"), int(form.get("include_shorts") == "1"),
                 settings.poll_minutes_default, str(form.get("include_keywords", ""))[:2000].strip(),
                 str(form.get("exclude_keywords", ""))[:2000].strip(), height, prio, backfill,
                 db.now())).lastrowid
        jobs.enqueue(conn, "channel_sync", channel_id=ch_id, priority=PRIO_ADMIN_SYNC)
    return ch_id, n


# ----------------------------------------------------------------- channels

@router.get("/channels")
async def channels(request: Request, p: Principal = Depends(need_admin),
                   conn: sqlite3.Connection = Depends(get_conn)):
    rows = conn.execute(
        """SELECT c.*,
             SUM(v.status='ready') AS n_ready, SUM(v.status IN ('queued','downloading')) AS n_queued,
             SUM(v.status='listed') AS n_listed, SUM(v.status='failed') AS n_failed
           FROM channels c LEFT JOIN videos v ON v.channel_id=c.id
           GROUP BY c.id ORDER BY c.subscribed DESC, c.title""").fetchall()
    return render(request, "admin/channels.html", p, channels=rows)


@router.get("/channels/{ch_id}")
async def channel(request: Request, ch_id: int, p: Principal = Depends(need_admin),
                  conn: sqlite3.Connection = Depends(get_conn)):
    ch = conn.execute("SELECT * FROM channels WHERE id=?", (ch_id,)).fetchone()
    if ch is None:
        raise HTTPException(404)
    syncing = conn.execute("SELECT status, error FROM jobs WHERE kind='channel_sync' AND channel_id=? "
                           "AND status IN ('pending','running')", (ch_id,)).fetchone()
    rows = conn.execute(
        "SELECT * FROM videos WHERE channel_id=? ORDER BY published_at IS NULL, published_at DESC, id",
        (ch_id,)).fetchall()
    has_filter = bool(ch["include_keywords"] or ch["exclude_keywords"])
    videos = [dict(v) | {"match": has_filter and keywords.matches(
        keywords.titles(v["title"], v["alt_title"]), ch["include_keywords"], ch["exclude_keywords"])}
        for v in rows]
    return render(request, "admin/channel.html", p, ch=ch, syncing=syncing, videos=videos,
                  has_filter=has_filter, heights=HEIGHTS, ranges=jobs.RANGES[1:],
                  priorities=jobs.PRIORITY_NAMES,
                  ch_height=ch["max_height"] or settings.default_height)


@router.post("/channels/{ch_id}/settings")
async def channel_settings(request: Request, ch_id: int, p: Principal = Depends(need_admin),
                           conn: sqlite3.Connection = Depends(get_conn)):
    form = await form_with_csrf(request, p)
    try:
        poll = max(15, min(24 * 60, int(form.get("poll_minutes", 60))))
    except ValueError:
        poll = 60
    conn.execute("UPDATE channels SET subscribed=?, auto_download=?, include_shorts=?, poll_minutes=?, "
                 "include_keywords=?, exclude_keywords=?, max_height=?, priority=? WHERE id=?",
                 (int(form.get("subscribed") == "1"), int(form.get("auto_download") == "1"),
                  int(form.get("include_shorts") == "1"), poll,
                  str(form.get("include_keywords", ""))[:2000].strip(),
                  str(form.get("exclude_keywords", ""))[:2000].strip(),
                  _height(form.get("max_height")), _priority(form.get("priority")), ch_id))
    jobs.reprioritise_channel(conn, ch_id)
    _back(f"/admin/channels/{ch_id}", "Saved. The quality applies to videos downloaded from now on.")


@router.post("/channels/{ch_id}/sync")
async def channel_sync(request: Request, ch_id: int, p: Principal = Depends(need_admin),
                       conn: sqlite3.Connection = Depends(get_conn)):
    await form_with_csrf(request, p)
    jobs.enqueue(conn, "channel_sync", channel_id=ch_id, priority=PRIO_ADMIN_SYNC)
    _back(f"/admin/channels/{ch_id}", "Sync queued.")


@router.post("/channels/{ch_id}/remove")
async def channel_remove(request: Request, ch_id: int, p: Principal = Depends(need_admin),
                         conn: sqlite3.Connection = Depends(get_conn)):
    """Cancel/remove a channel. Videos already downloaded or queued are kept (the channel
    then stays as an unsubscribed row holding them); everything else goes."""
    await form_with_csrf(request, p)
    with db.tx(conn):
        if not conn.execute("SELECT 1 FROM channels WHERE id=?", (ch_id,)).fetchone():
            raise HTTPException(404)
        conn.execute("DELETE FROM videos WHERE channel_id=? AND status IN ('listed','failed','evicted')",
                     (ch_id,))
        kept = conn.execute("SELECT COUNT(*) FROM videos WHERE channel_id=?", (ch_id,)).fetchone()[0]
        if kept:
            conn.execute("UPDATE channels SET subscribed=0, auto_download=0 WHERE id=?", (ch_id,))
            conn.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE channel_id=? "
                         "AND kind='channel_sync' AND status='pending'", (db.now(), ch_id))
        else:
            row = conn.execute("SELECT yt_id FROM channels WHERE id=?", (ch_id,)).fetchone()
            conn.execute("DELETE FROM channels WHERE id=?", (ch_id,))
            if row["yt_id"]:
                shutil.rmtree(settings.media_dir / row["yt_id"], ignore_errors=True)  # avatar
    if kept:
        _back(f"/admin/channels/{ch_id}", f"Unsubscribed. Kept {kept} downloaded or queued video(s); "
              "delete them from Videos if you don't want them.")
    _back("/admin/channels", "Channel removed.")


@router.post("/channels/{ch_id}/download")
async def channel_download(request: Request, ch_id: int, p: Principal = Depends(need_admin),
                           conn: sqlite3.Connection = Depends(get_conn)):
    form = await form_with_csrf(request, p)
    ids = _ids(form, "video")
    with db.tx(conn):
        for vid in ids:
            if conn.execute("SELECT 1 FROM videos WHERE id=? AND channel_id=?", (vid, ch_id)).fetchone():
                _queue_video(conn, vid)
    _back(f"/admin/channels/{ch_id}", f"Queued {len(ids)} video(s).")


@router.post("/channels/{ch_id}/queue-range")
async def channel_queue_range(request: Request, ch_id: int, p: Principal = Depends(need_admin),
                              conn: sqlite3.Connection = Depends(get_conn)):
    form = await form_with_csrf(request, p)
    from . import storage
    with db.tx(conn):
        n = jobs.queue_range(conn, ch_id, str(form.get("range", "")))
    est = storage.queue_estimate(conn)
    _back(f"/admin/channels/{ch_id}", f"Queued {n} video(s). Whole queue now ~{est['bytes'] / 1024**3:.1f} GB, "
          f"space before old videos get deleted ~{storage.room(conn) / 1024**3:.1f} GB.")


def _back_to(form, ch_id: int) -> str:
    return "/admin/jobs" if form.get("back") == "jobs" else f"/admin/channels/{ch_id}"


@router.post("/channels/{ch_id}/cancel-queue")
async def channel_cancel_queue(request: Request, ch_id: int, p: Principal = Depends(need_admin),
                               conn: sqlite3.Connection = Depends(get_conn)):
    form = await form_with_csrf(request, p)
    with db.tx(conn):
        n = jobs.cancel_channel_queue(conn, ch_id)
    _back(_back_to(form, ch_id), f"Cancelled {n} queued download(s); a download already running finishes. "
          "The videos stay listed on the channel page, so you can queue them again.")


@router.post("/channels/{ch_id}/priority")
async def channel_priority(request: Request, ch_id: int, p: Principal = Depends(need_admin),
                           conn: sqlite3.Connection = Depends(get_conn)):
    form = await form_with_csrf(request, p)
    with db.tx(conn):
        conn.execute("UPDATE channels SET priority=? WHERE id=?", (_priority(form.get("priority")), ch_id))
        jobs.reprioritise_channel(conn, ch_id)
    _back(_back_to(form, ch_id), "Priority saved; queued downloads re-ordered.")


# ----------------------------------------------------------------- videos

@router.get("/videos")
async def videos(request: Request, p: Principal = Depends(need_admin),
                 conn: sqlite3.Connection = Depends(get_conn)):
    status = request.query_params.get("status", "")
    where, params = ("v.status=?", [status]) if status else ("v.status<>'listed'", [])
    rows = conn.execute(
        f"SELECT v.*, c.title AS ch_title FROM videos v LEFT JOIN channels c ON c.id=v.channel_id "
        f"WHERE {where} ORDER BY v.added_at DESC LIMIT 500", params).fetchall()
    return render(request, "admin/videos.html", p, videos=rows, status=status)


@router.post("/videos/{vid}/{action}")
async def video_action(request: Request, vid: int, action: str, p: Principal = Depends(need_admin),
                       conn: sqlite3.Connection = Depends(get_conn)):
    form = await form_with_csrf(request, p)
    back = str(form.get("back") or "/admin/videos")
    back = back if back.startswith("/admin") else "/admin/videos"
    v = conn.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
    if v is None:
        raise HTTPException(404)
    if action == "download":
        with db.tx(conn):
            _queue_video(conn, vid)
        _back(back, "Queued.")
    if action == "evict":
        if v["path"]:  # only the video file; thumbnail and comments stay
            (settings.media_dir / v["path"]).unlink(missing_ok=True)
        conn.execute("UPDATE videos SET status='evicted' WHERE id=?", (vid,))
        _back(back, "Video file deleted; metadata and comments kept. Use Download to fetch it again.")
    raise HTTPException(404)


# ----------------------------------------------------------------- jobs

@router.get("/jobs")
async def jobs_page(request: Request, p: Principal = Depends(need_admin),
                    conn: sqlite3.Connection = Depends(get_conn)):
    rows = conn.execute(
        "SELECT j.*, v.title, v.yt_id, c.url AS ch_url, c.title AS ch_title FROM jobs j "
        "LEFT JOIN videos v ON v.id=j.video_id LEFT JOIN channels c ON c.id=j.channel_id "
        "ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 WHEN 'failed' THEN 2 "
        "ELSE 3 END, CASE WHEN j.status='pending' THEN j.priority END, "
        "CASE WHEN j.status='pending' THEN j.id END, j.updated_at DESC LIMIT 300").fetchall()
    queue = conn.execute(
        "SELECT c.id, c.title, c.url, c.priority, COUNT(*) AS n, SUM(j.run_after > ?) AS waiting "
        "FROM jobs j JOIN videos v ON v.id=j.video_id JOIN channels c ON c.id=v.channel_id "
        "WHERE j.kind='download' AND j.status='pending' GROUP BY c.id "
        "ORDER BY c.priority DESC, n DESC", (db.now(),)).fetchall()
    singles = conn.execute("SELECT COUNT(*) FROM jobs j JOIN videos v ON v.id=j.video_id WHERE j.kind='download' "
                           "AND j.status='pending' AND v.channel_id IS NULL").fetchone()[0]
    from . import storage
    estimate = storage.queue_estimate(conn)
    return render(request, "admin/jobs.html", p, jobs=rows, queue=queue, singles=singles,
                  estimate=estimate, space=storage.room(conn),
                  priorities=jobs.PRIORITY_NAMES)


@router.post("/jobs/clear-failed")
async def jobs_clear_failed(request: Request, p: Principal = Depends(need_admin),
                            conn: sqlite3.Connection = Depends(get_conn)):
    """Take failed jobs off the failure lists (they become "cancelled"; videos keep their
    status and can still be retried from the Videos page)."""
    form = await form_with_csrf(request, p)
    n = conn.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE status='failed'", (db.now(),)).rowcount
    _back("/admin/jobs" if form.get("back") == "jobs" else "/admin", f"Cleared {n} failure(s).")


@router.post("/jobs/{job_id}/{action}")
async def job_action(request: Request, job_id: int, action: str, p: Principal = Depends(need_admin),
                     conn: sqlite3.Connection = Depends(get_conn)):
    await form_with_csrf(request, p)
    if action == "retry":
        conn.execute("UPDATE jobs SET status='pending', attempts=0, run_after=?, error=NULL, "
                     "updated_at=? WHERE id=? AND status IN ('failed','cancelled','pending')",
                     (db.now(), db.now(), job_id))
        conn.execute("UPDATE videos SET status='queued' WHERE id=(SELECT video_id FROM jobs WHERE id=?) "
                     "AND status IN ('failed','listed')", (job_id,))
    elif action == "dismiss":
        conn.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE id=? AND status='failed'",
                     (db.now(), job_id))
    elif action == "cancel":
        conn.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE id=? AND status='pending'",
                     (db.now(), job_id))
        conn.execute("UPDATE videos SET status='listed' WHERE id=(SELECT video_id FROM jobs WHERE id=?) "
                     "AND status='queued'", (job_id,))
    else:
        raise HTTPException(404)
    _back("/admin/jobs", "Done.")


# ----------------------------------------------------------------- users

@router.get("/users")
async def users(request: Request, p: Principal = Depends(need_admin),
                conn: sqlite3.Connection = Depends(get_conn)):
    rows = conn.execute(
        "SELECT u.*, (SELECT COUNT(*) FROM sessions s WHERE s.user_id=u.id AND s.expires_at>?) AS n_sessions "
        "FROM users u ORDER BY u.username", (db.now(),)).fetchall()
    return render(request, "admin/users.html", p, users=rows, max_devices=settings.max_devices,
                  rotate_months=max(1, round(settings.password_max_age_days / 30.4)),
                  max_age=settings.password_max_age_days * 86400)


@router.post("/users")
async def user_create(request: Request, p: Principal = Depends(need_admin),
                      conn: sqlite3.Connection = Depends(get_conn)):
    form = await form_with_csrf(request, p)
    username = str(form.get("username", "")).strip()
    role = "admin" if form.get("role") == "admin" else "viewer"
    while not username:   # left blank: pick a random, easy-to-type name
        cand = "user-" + "".join(secrets.choice(USERNAME_ALPHABET) for _ in range(4))
        if not conn.execute("SELECT 1 FROM users WHERE username=?", (cand,)).fetchone():
            username = cand
    if not (2 <= len(username) <= 32) or not all(ch.isalnum() or ch in "._-" for ch in username):
        _back("/admin/users", "Username: 2-32 letters, digits, '.', '_' or '-'.")
    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        _back("/admin/users", "That username exists.")
    pw = security.generate_password()
    h = await run_in_threadpool(security.hash_password, pw)
    conn.execute("INSERT INTO users(username,pw_hash,role,pw_changed_at,created_at) VALUES (?,?,?,?,?)",
                 (username, h, role, db.now(), db.now()))
    return render(request, "admin/secret.html", p, what="password", who=username, secret=pw,
                  username=username, back="/admin/users")


@router.post("/users/{uid}/{action}")
async def user_action(request: Request, uid: int, action: str, p: Principal = Depends(need_admin),
                      conn: sqlite3.Connection = Depends(get_conn)):
    await form_with_csrf(request, p)
    u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if u is None:
        raise HTTPException(404)
    if uid == p.id and action in ("disable", "delete", "reset-mfa"):
        _back("/admin/users", "You can't disable or delete yourself.")
    if action == "reset":
        pw = security.generate_password()
        h = await run_in_threadpool(security.hash_password, pw)
        with db.tx(conn):
            conn.execute("UPDATE users SET pw_hash=?, pw_changed_at=? WHERE id=?", (h, db.now(), uid))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        return render(request, "admin/secret.html", p, what="new password", who=u["username"],
                      secret=pw, back="/admin/users")
    if action == "reset-mfa":
        with db.tx(conn):
            conn.execute("UPDATE users SET totp_secret=NULL, totp_last_step=0 WHERE id=?", (uid,))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        _back("/admin/users", f"MFA removed for {u['username']}; they must set it up again at next sign-in.")
    if action == "disable":
        with db.tx(conn):
            conn.execute("UPDATE users SET disabled=1-disabled WHERE id=?", (uid,))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        _back("/admin/users", "Updated.")
    if action == "delete":
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        _back("/admin/users", f"Deleted {u['username']}.")
    raise HTTPException(404)


# ----------------------------------------------------------------- access codes

@router.get("/codes")
async def codes(request: Request, p: Principal = Depends(need_admin),
                conn: sqlite3.Connection = Depends(get_conn)):
    rows = conn.execute(
        """SELECT a.*,
             (SELECT COUNT(*) FROM sessions s WHERE s.code_id=a.id AND s.expires_at>?) AS n_sessions,
             (SELECT GROUP_CONCAT(COALESCE(c.title, c.url), ', ') FROM code_scopes cs
                JOIN channels c ON c.id=cs.channel_id WHERE cs.code_id=a.id) AS ch_names,
             (SELECT COUNT(*) FROM code_scopes cs WHERE cs.code_id=a.id AND cs.video_id IS NOT NULL) AS n_videos
           FROM access_codes a ORDER BY a.created_at DESC""", (db.now(),)).fetchall()
    chans = conn.execute("SELECT id, title, url FROM channels WHERE yt_id IS NOT NULL "
                         "ORDER BY title").fetchall()
    return render(request, "admin/codes.html", p, codes=rows, channels=chans)


@router.post("/codes")
async def code_create(request: Request, p: Principal = Depends(need_admin),
                      conn: sqlite3.Connection = Depends(get_conn)):
    form = await form_with_csrf(request, p)
    try:
        max_devices = max(1, min(100, int(form.get("max_devices", 1))))
        session_days = max(1, min(365, int(form.get("session_days", 7))))
        expires_days = int(form.get("expires_days") or 0)
    except ValueError:
        _back("/admin/codes", "Numbers only, please.")
    channel_ids = _ids(form, "channel")
    video_ids, unknown = [], []
    for line in str(form.get("videos", "")).split():
        parsed = youtube.parse(line)
        row = conn.execute("SELECT id FROM videos WHERE yt_id=?", (parsed[1],)).fetchone() \
            if parsed and parsed[0] == "video" else None
        (video_ids.append(row["id"]) if row else unknown.append(line))
    if unknown:
        _back("/admin/codes", "Not in the library (add them first): " + ", ".join(unknown[:5]))
    if not channel_ids and not video_ids:
        _back("/admin/codes", "Pick at least one channel or video.")
    code = security.generate_code()
    with db.tx(conn):
        cid = conn.execute(
            "INSERT INTO access_codes(code_hash,label,max_devices,session_days,expires_at,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (security.sha256(security.normalize_code(code)), str(form.get("label", ""))[:100] or None,
             max_devices, session_days, db.now() + expires_days * 86400 if expires_days > 0 else None,
             db.now())).lastrowid
        conn.executemany("INSERT INTO code_scopes(code_id,channel_id) VALUES (?,?)",
                         [(cid, c) for c in channel_ids])
        conn.executemany("INSERT INTO code_scopes(code_id,video_id) VALUES (?,?)",
                         [(cid, v) for v in video_ids])
    return render(request, "admin/secret.html", p, what="access code",
                  who=str(form.get("label") or f"code #{cid}"), secret=code, back="/admin/codes")


@router.post("/codes/{cid}/{action}")
async def code_action(request: Request, cid: int, action: str, p: Principal = Depends(need_admin),
                      conn: sqlite3.Connection = Depends(get_conn)):
    await form_with_csrf(request, p)
    if action == "disable":
        conn.execute("UPDATE access_codes SET disabled=1-disabled WHERE id=?", (cid,))
    elif action == "revoke":  # sign out all devices and free their slots
        with db.tx(conn):
            conn.execute("DELETE FROM sessions WHERE code_id=?", (cid,))
            conn.execute("UPDATE access_codes SET used_devices=0 WHERE id=?", (cid,))
    elif action == "delete":
        conn.execute("DELETE FROM access_codes WHERE id=?", (cid,))
    else:
        raise HTTPException(404)
    _back("/admin/codes", "Done.")
