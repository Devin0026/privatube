import re
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from privatube import db, jobs, security, worker, youtube
from privatube.config import settings


# ----------------------------------------------------------------- helpers

def make_user(name="alice", role="viewer", pw="Abcd-Efgh-Jkmn", age_days=0):
    with db.get() as c:
        c.execute("INSERT INTO users(username,pw_hash,role,pw_changed_at,created_at) VALUES (?,?,?,?,?)",
                  (name, security.hash_password(pw), role, db.now() - age_days * 86400, db.now()))
    return pw


def make_channel(yt="UC" + "a" * 22, title="Chan A"):
    with db.get() as c:
        return c.execute("INSERT INTO channels(yt_id,url,title,created_at,baseline_done) VALUES (?,?,?,?,1)",
                         (yt, f"https://www.youtube.com/channel/{yt}", title, db.now())).lastrowid


def make_video(ch, yt_id, title="vid", status="ready"):
    with db.get() as c:
        return c.execute(
            "INSERT INTO videos(yt_id,channel_id,title,status,path,thumb,added_at,published_at,duration)"
            " VALUES (?,?,?,?,?,'thumb.jpg',?,?,100)",
            (yt_id, ch, title, status, f"X/{yt_id}/video.mp4", db.now(), db.now())).lastrowid


ADMIN_TOTP = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


def make_admin(name="boss", pw="Abcd-Efgh-Jkmn"):
    make_user(name, role="admin", pw=pw)
    with db.get() as c:
        c.execute("UPDATE users SET totp_secret=?, totp_last_step=0 WHERE username=?", (ADMIN_TOTP, name))
    return pw


def login_admin(cl, name="boss", pw="Abcd-Efgh-Jkmn"):
    import pyotp
    r = login(cl, name, pw)
    assert r.headers["location"].startswith("/login/mfa")
    page = cl.get(r.headers["location"]).text
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    with db.get() as c:   # allow reusing the current code across logins within one test
        c.execute("UPDATE users SET totp_last_step=0 WHERE username=?", (name,))
    r = cl.post("/login/mfa", data={"csrf": tok, "code": pyotp.TOTP(ADMIN_TOTP).now(), "next": "/admin"},
                follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin", r.text[:300]


def client():
    from privatube.web import app
    return TestClient(app, headers={"Accept-Language": "en-US,en;q=0.9"})


def login(cl, name="alice", pw="Abcd-Efgh-Jkmn"):
    return cl.post("/login", data={"username": name, "password": pw}, follow_redirects=False)


def csrf_of(cl):
    html = cl.get("/").text
    return re.search(r'data-csrf="([^"]+)"', html).group(1)


# ----------------------------------------------------------------- units

@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1", ("video", "dQw4w9WgXcQ")),
    ("https://youtu.be/dQw4w9WgXcQ", ("video", "dQw4w9WgXcQ")),
    ("youtube.com/shorts/dQw4w9WgXcQ", ("video", "dQw4w9WgXcQ")),
    ("@SomeOne", ("channel", "https://www.youtube.com/@SomeOne")),
    ("https://m.youtube.com/@SomeOne/videos", ("channel", "https://www.youtube.com/@SomeOne")),
    ("https://www.youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa", ("channel", "https://www.youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa")),
    ("https://evil.com/watch?v=dQw4w9WgXcQ", None),
])
def test_parse(url, expected):
    assert youtube.parse(url) == expected


def test_password_and_code_format():
    pw = security.generate_password()
    assert re.fullmatch(r"[A-Za-z2-9]{4}-[A-Za-z2-9]{4}-[A-Za-z2-9]{4}", pw)
    h = security.hash_password(pw)
    assert security.verify_password(h, pw.replace("-", ""))      # dashes optional
    assert not security.verify_password(h, pw.lower() if pw != pw.lower() else pw.upper())
    code = security.generate_code()
    assert security.normalize_code(code.lower()) == code.replace("-", "")


def test_media_signature_roundtrip():
    url = security.sign_media("X/abc/video.mp4", "u1")
    path, q = url.split("?")
    qs = dict(p.split("=") for p in q.split("&"))
    assert security.check_media_signature(path, "u1", qs["e"], qs["s"])
    assert not security.check_media_signature(path, "u2", qs["e"], qs["s"])
    assert not security.check_media_signature("/m/X/other.mp4", "u1", qs["e"], qs["s"])


def test_nginx_secure_link_compat():
    # Same algorithm nginx documents: echo -n "$e$uri$u secret" | openssl md5 -binary | base64 | tr +/ -_ | tr -d =
    import base64, hashlib
    want = base64.urlsafe_b64encode(hashlib.md5(b"2000000000/m/a/b.mp4u1 test-secret").digest()).decode().rstrip("=")
    assert security.check_media_signature("/m/a/b.mp4", "u1", "2000000000", want)


# ----------------------------------------------------------------- web

def test_requires_login_and_login_flow():
    make_user()
    cl = client()
    r = cl.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert login(cl, pw="wrong").status_code == 401
    r = login(cl)
    assert r.status_code == 303
    assert cl.get("/").status_code == 200
    assert "Content-Security-Policy" in cl.get("/").headers


def test_login_throttle():
    make_user()
    cl = client()
    for _ in range(8):
        login(cl, pw="nope")
    assert login(cl).status_code == 429   # even the right password is refused now
    security.login_throttle._hits.clear()


def test_viewer_cannot_reach_admin():
    make_user()
    cl = client()
    login(cl)
    assert cl.get("/admin").status_code == 404


def test_feed_and_watch_signed_src():
    make_user()
    ch = make_channel()
    vid = make_video(ch, "AAAAAAAAAAA", "Hello world")
    make_video(ch, "BBBBBBBBBBB", "Not ready", status="listed")
    cl = client()
    login(cl)
    feed = cl.get("/").text
    assert "Hello world" in feed and "Not ready" not in feed
    page = cl.get(f"/v/{vid}").text
    import html as _html
    src = _html.unescape(re.search(r'data-src="([^"]+)"', page).group(1))
    assert src.startswith("/m/X/AAAAAAAAAAA/video.mp4?u=u1&")
    assert 'controlsList="nodownload' in page
    assert "Hello world" in cl.get("/search?q=world").text
    assert "Hello world" not in cl.get("/search?q=zzz").text


def test_csrf_required():
    make_user()
    cl = client()
    login(cl)
    assert cl.post("/logout", data={"csrf": "bad"}).status_code == 403
    assert cl.post("/logout", data={"csrf": csrf_of(cl)}, follow_redirects=False).status_code == 303


def test_cross_origin_post_refused():
    cl = client()
    r = cl.post("/login", data={"username": "x", "password": "y"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    host = urlparse(settings.site_url).hostname
    for origin in (settings.site_url, f"https://{host}:8443"):     # same site, either port
        r = cl.post("/login", data={"username": "x", "password": "y"}, headers={"Origin": origin})
        assert r.status_code == 401, origin
    r = cl.post("/login", data={"username": "x", "password": "y"}, headers={"Origin": f"https://{host}.evil.example"})
    assert r.status_code == 403


def test_password_rotation_forced_after_six_months():
    make_user("early", age_days=170)
    early = client()
    login(early, "early")
    assert early.get("/", follow_redirects=False).status_code == 200
    pw = make_user(age_days=200)
    cl = client()
    login(cl, pw=pw)
    r = cl.get("/", follow_redirects=False)
    assert r.headers["location"] == "/account/password"
    page = cl.get("/account/password").text
    assert "every 6 months" in page
    zh = cl.get("/account/password", headers={"Accept-Language": "zh-CN"}).text
    assert "密码每 6 个月更换一次" in zh
    new_pw = re.search(r'<p class="secret">([^<]+)</p>', page).group(1)
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    # wrong confirmation -> new password generated, still forced
    r = cl.post("/account/password", data={"csrf": tok, "confirm": "nope"}, follow_redirects=False)
    assert r.headers["location"].startswith("/account/password")
    page = cl.get("/account/password").text
    new_pw = re.search(r'<p class="secret">([^<]+)</p>', page).group(1)
    r = cl.post("/account/password", data={"csrf": tok, "confirm": new_pw}, follow_redirects=False)
    assert r.headers["location"].startswith("/?msg=")
    assert cl.get("/", follow_redirects=False).status_code == 200
    cl2 = client()
    assert login(cl2, pw=pw).status_code == 401
    assert login(cl2, pw=new_pw).status_code == 303


def test_access_code_scope_and_device_limit():
    make_admin()
    ch_a = make_channel("UC" + "a" * 22, "Chan A")
    ch_b = make_channel("UC" + "b" * 22, "Chan B")
    va = make_video(ch_a, "AAAAAAAAAAA", "In scope")
    vb = make_video(ch_b, "BBBBBBBBBBB", "Out of scope")
    admin = client()
    login_admin(admin)
    page = admin.post("/admin/codes", data={"csrf": csrf_of(admin), "label": "friend", "max_devices": "1",
                                            "session_days": "7", "expires_days": "30",
                                            "channel": str(ch_a)}).text
    code = re.search(r'<p class="secret">([^<]+)</p>', page).group(1)

    dev1 = client()
    assert dev1.post("/code", data={"code": code.lower()}, follow_redirects=False).status_code == 303
    feed = dev1.get("/").text
    assert "In scope" in feed and "Out of scope" not in feed
    assert dev1.get(f"/v/{va}").status_code == 200
    assert dev1.get(f"/v/{vb}").status_code == 404
    assert dev1.get(f"/c/{ch_b}").status_code == 404
    assert dev1.get("/account/password").status_code == 404

    dev2 = client()
    r = dev2.post("/code", data={"code": code})
    assert r.status_code == 403 and "maximum number of devices" in r.text
    security.login_throttle._hits.clear()


def test_admin_add_video_and_channel():
    make_admin()
    cl = client()
    login_admin(cl)
    tok = csrf_of(cl)
    r = cl.post("/admin/add", data={"csrf": tok, "url": "https://youtu.be/dQw4w9WgXcQ", "kind": "auto"},
                follow_redirects=False)
    assert "Queued" in r.headers["location"] or "Queued" in cl.get(r.headers["location"]).text
    r = cl.post("/admin/add", data={"csrf": tok, "url": "@SomeOne", "kind": "channel", "auto_download": "1"},
                follow_redirects=False)
    assert r.headers["location"].startswith("/admin/channels/")
    with db.get() as c:
        kinds = sorted(r["kind"] for r in c.execute("SELECT kind FROM jobs"))
    assert kinds == ["channel_sync", "download"]
    r = cl.post("/admin/add", data={"csrf": tok, "url": "@SomeOne", "kind": "video"}, follow_redirects=False)
    assert "looks%20like%20a%20channel" in r.headers["location"]


# ----------------------------------------------------------------- worker

def test_channel_listing_baseline_then_auto_queue():
    with db.get() as c:
        ch_id = c.execute("INSERT INTO channels(url,created_at) VALUES ('https://www.youtube.com/@x',?)",
                          (db.now(),)).lastrowid
        listing = {"yt_id": "UC" + "c" * 22, "title": "X", "handle": "@x", "description": "", "avatar": b"img",
                   "entries": [{"id": "OLD00000001", "title": "old", "duration": 5, "timestamp": None, "is_short": False}]}
        ch = c.execute("SELECT * FROM channels WHERE id=?", (ch_id,)).fetchone()
        assert worker.apply_channel_listing(c, ch, listing) == 0          # baseline: just listed
        assert c.execute("SELECT status FROM videos").fetchone()[0] == "listed"
        listing["entries"].insert(0, {"id": "NEW00000001", "title": "new", "duration": 5, "timestamp": None,
                                      "is_short": False})
        ch = c.execute("SELECT * FROM channels WHERE id=?", (ch_id,)).fetchone()
        assert worker.apply_channel_listing(c, ch, listing) == 1          # new upload auto-queued
        assert c.execute("SELECT status FROM videos WHERE yt_id='NEW00000001'").fetchone()[0] == "queued"
        assert (settings.media_dir / listing["yt_id"] / "avatar.jpg").read_bytes() == b"img"


def test_channel_merge_with_existing_row():
    yt = "UC" + "d" * 22
    with db.get() as c:
        old = c.execute("INSERT INTO channels(yt_id,url,subscribed,created_at) VALUES (?,?,0,?)",
                        (yt, "u", db.now())).lastrowid
        c.execute("INSERT INTO videos(yt_id,channel_id,status,added_at) VALUES ('SINGLE00001',?,'ready',?)",
                  (old, db.now()))
        new = c.execute("INSERT INTO channels(url,created_at) VALUES ('https://www.youtube.com/@d',?)",
                        (db.now(),)).lastrowid
        ch = c.execute("SELECT * FROM channels WHERE id=?", (new,)).fetchone()
        worker.apply_channel_listing(c, ch, {"yt_id": yt, "title": "D", "handle": None, "description": None,
                                             "avatar": None, "entries": []})
        assert c.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 1
        assert c.execute("SELECT channel_id FROM videos").fetchone()[0] == new


def test_download_job_end_to_end(monkeypatch):
    def fake_download(yt_id, workdir, on_progress, height=None):
        on_progress(0.5)
        (workdir / "video.mp4").write_bytes(b"x" * 1000)
        (workdir / "thumb.jpg").write_bytes(b"j")
        return {"id": yt_id, "title": "Fake", "description": "d", "duration": 61, "timestamp": 1700000000,
                "channel_id": "UC" + "e" * 22, "channel": "E", "uploader_id": "@e", "width": 1280,
                "height": 720, "comments": [{"id": "c1", "text": "hi", "author": "bob", "parent": "root"},
                                            {"id": "c2", "text": "re", "author": "ann", "parent": "c1"}]}
    monkeypatch.setattr(worker, "download", fake_download)
    with db.get() as c:
        vid = c.execute("INSERT INTO videos(yt_id,status,added_at) VALUES ('FAKE0000001','queued',?)",
                        (db.now(),)).lastrowid
        jobs.enqueue(c, "download", video_id=vid)
        assert worker.run_one(c)
        v = c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
        assert v["status"] == "ready" and v["path"] == f"UC{'e' * 22}/FAKE0000001/video.mp4"
        assert (settings.media_dir / v["path"]).stat().st_size == 1000
        assert c.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 2
        assert c.execute("SELECT subscribed FROM channels").fetchone()[0] == 0   # created, not subscribed
        assert c.execute("SELECT status FROM jobs").fetchone()[0] == "done"


def test_permanent_failure_and_defer(monkeypatch):
    def private(*a, **k):
        raise worker.Permanent("Private video")
    monkeypatch.setattr(worker, "download", private)
    with db.get() as c:
        vid = c.execute("INSERT INTO videos(yt_id,status,added_at) VALUES ('PRIV0000001','queued',?)",
                        (db.now(),)).lastrowid
        jobs.enqueue(c, "download", video_id=vid)
        worker.run_one(c)
        assert c.execute("SELECT status FROM videos").fetchone()[0] == "failed"
        assert c.execute("SELECT status FROM jobs").fetchone()[0] == "failed"

        monkeypatch.setattr(worker.shutil, "disk_usage", lambda p: type("U", (), {"free": 0})())
        vid2 = c.execute("INSERT INTO videos(yt_id,status,added_at) VALUES ('DISK0000001','queued',?)",
                         (db.now(),)).lastrowid
        jid = jobs.enqueue(c, "download", video_id=vid2)
        worker.run_one(c)
        j = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        assert j["status"] == "pending" and j["attempts"] == 0 and j["run_after"] > db.now()


def test_admin_pages_render():
    make_admin()
    ch = make_channel()
    make_video(ch, "AAAAAAAAAAA")
    cl = client()
    login_admin(cl)
    for path in ["/admin", "/admin/channels", f"/admin/channels/{ch}", "/admin/videos",
                 "/admin/videos?status=ready", "/admin/jobs", "/admin/users", "/admin/codes",
                 "/channels", f"/c/{ch}"]:
        assert cl.get(path).status_code == 200, path


# ----------------------------------------------------------------- MFA

def test_admin_must_enroll_mfa_then_code_required():
    import pyotp
    make_user("boss", role="admin")
    cl = client()
    assert login(cl, "boss").headers["location"] == "/"          # no MFA yet: normal login
    r = cl.get("/admin", follow_redirects=False)
    assert r.headers["location"] == "/account/mfa"                # ...but the panel demands setup
    page = cl.get("/account/mfa").text
    assert "<svg" in page
    secret = re.search(r'<p class="secret small">([A-Z2-7]+)</p>', page).group(1)
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = cl.post("/account/mfa", data={"csrf": tok, "code": "000000"}, follow_redirects=False)
    assert r.headers["location"].startswith("/account/mfa?msg=")
    page = cl.get("/account/mfa").text                               # new secret after a miss
    secret = re.search(r'<p class="secret small">([A-Z2-7]+)</p>', page).group(1)
    code = pyotp.TOTP(secret).now()
    r = cl.post("/account/mfa", data={"csrf": tok, "code": code}, follow_redirects=False)
    assert r.headers["location"].startswith("/admin")
    assert cl.get("/admin").status_code == 200

    cl2 = client()                                                   # next sign-in needs a code
    r = login(cl2, "boss")
    assert r.headers["location"].startswith("/login/mfa")
    assert cl2.get("/", follow_redirects=False).headers["location"].startswith("/login/mfa")
    page = cl2.get("/login/mfa").text
    tok2 = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = cl2.post("/login/mfa", data={"csrf": tok2, "code": code, "next": "/admin"}, follow_redirects=False)
    assert r.status_code == 401                                      # same code can't be replayed
    for _ in range(4):                                               # 1 replay + 4 wrong = limit
        cl2.post("/login/mfa", data={"csrf": tok2, "code": "123456", "next": "/"})
    r = cl2.post("/login/mfa", data={"csrf": tok2, "code": "123456"}, follow_redirects=False)
    assert r.headers["location"].startswith("/login?msg=")           # locked out, session dropped
    from privatube.web import mfa_throttle
    mfa_throttle._hits.clear()


def test_totp_check_window_and_replay():
    import pyotp, time
    secret = security.new_totp_secret()
    t = pyotp.TOTP(secret)
    step = int(time.time()) // 30
    assert security.totp_check(secret, t.at((step - 1) * 30), 0) == step - 1     # 30 s drift ok
    assert security.totp_check(secret, t.at((step - 3) * 30), 0) is None         # too old
    assert security.totp_check(secret, t.now(), step) is None                    # replay refused
    assert security.totp_check(secret, "12", 0) is None


# ----------------------------------------------------------------- keywords

def test_keyword_matching():
    from privatube import keywords
    assert keywords.matches("Daily NEWS 0412", "news, 访谈", "")
    assert keywords.matches("独家访谈：某某", "news，访谈", "")
    assert not keywords.matches("Movie trailer", "news", "")
    assert not keywords.matches("News live 直播", "news", "直播")
    assert keywords.matches("anything", "", "")
    assert not keywords.matches("a #shorts clip", "", "#Shorts")


def test_keyword_filter_limits_auto_download():
    with db.get() as c:
        ch_id = c.execute("INSERT INTO channels(url,created_at,baseline_done,include_keywords,exclude_keywords)"
                          " VALUES ('https://www.youtube.com/@k',?,1,'news','live')", (db.now(),)).lastrowid
        ch = c.execute("SELECT * FROM channels WHERE id=?", (ch_id,)).fetchone()
        entries = [{"id": i, "title": t, "duration": 5, "timestamp": None, "is_short": False}
                   for i, t in [("KW000000001", "Morning news"), ("KW000000002", "Cooking show"),
                                ("KW000000003", "News live stream")]]
        n = worker.apply_channel_listing(c, ch, {"yt_id": "UC" + "k" * 22, "title": "K", "handle": None,
                                                 "description": None, "avatar": None, "entries": entries})
        assert n == 1
        st = dict(c.execute("SELECT yt_id, status FROM videos").fetchall())
        assert st == {"KW000000001": "queued", "KW000000002": "listed", "KW000000003": "listed"}


def test_channel_remove_cancels_or_keeps_downloaded():
    make_admin()
    cl = client()
    login_admin(cl)
    tok = csrf_of(cl)
    # Freshly added channel, nothing downloaded: removed completely.
    ch = make_channel()
    make_video(ch, "aaaaaaaaaaa", status="listed")
    r = cl.post(f"/admin/channels/{ch}/remove", data={"csrf": tok}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/channels?")
    with db.get() as c:
        assert c.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0
    # Channel with a downloaded video: unsubscribed, downloaded video kept.
    ch = make_channel(yt="UC" + "b" * 22)
    make_video(ch, "bbbbbbbbbbb", status="ready")
    make_video(ch, "ccccccccccc", status="listed")
    with db.get() as c:
        jobs.enqueue(c, "channel_sync", channel_id=ch)
    cl.post(f"/admin/channels/{ch}/remove", data={"csrf": tok}, follow_redirects=False)
    with db.get() as c:
        row = c.execute("SELECT subscribed FROM channels WHERE id=?", (ch,)).fetchone()
        assert row["subscribed"] == 0
        assert [v[0] for v in c.execute("SELECT yt_id FROM videos")] == ["bbbbbbbbbbb"]
        assert c.execute("SELECT status FROM jobs WHERE kind='channel_sync'").fetchone()[0] == "cancelled"


def test_listing_for_removed_channel_is_ignored():
    ch = make_channel()
    with db.get() as c:
        row = c.execute("SELECT * FROM channels WHERE id=?", (ch,)).fetchone()
        c.execute("DELETE FROM channels WHERE id=?", (ch,))
        n = worker.apply_channel_listing(c, row, {"yt_id": "UC" + "a" * 22, "title": "x", "handle": None,
                                                  "description": "", "avatar": None,
                                                  "entries": [{"id": "ddddddddddd", "title": "t"}]})
        assert n == 0 and c.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0


def test_ytdlp_uses_ipv4_by_default():
    assert worker._base_opts()["source_address"] == "0.0.0.0"


def test_admin_link_only_for_admins():
    make_user()
    make_admin()
    viewer = client()
    login(viewer)
    assert 'href="/admin"' not in viewer.get("/").text
    assert viewer.get("/admin").status_code == 404
    admin = client()
    login_admin(admin)
    assert 'href="/admin"' in admin.get("/").text


def test_english_by_default_chinese_from_browser_and_switch():
    from privatube.web import app
    make_user()
    cl = TestClient(app)                      # no Accept-Language: English
    login(cl)
    assert "Home" in cl.get("/").text
    page = cl.get("/", headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}).text
    assert 'lang="zh-CN"' in page and "首页" in page
    cl.get("/lang/en?next=/", follow_redirects=False)
    assert "Home" in cl.get("/", headers={"Accept-Language": "zh-CN"}).text
    assert cl.get("/lang/xx").status_code == 404
    r = cl.get("/lang/zh?next=//evil.example", follow_redirects=False)
    assert r.headers["location"] == "/"


def test_letter_avatars():
    from privatube.web import _avatar_color, _initials
    assert _initials("@johnsmith") == "Jo"
    assert _initials("柴静") == "柴"
    assert _initials("") == "?"
    assert _avatar_color("x") == _avatar_color("x")


def test_viewer_share_code_is_fixed_and_opens_only_that_video():
    make_user()
    ch = make_channel()
    va = make_video(ch, "AAAAAAAAAAA", "Shared one")
    make_video(ch, "BBBBBBBBBBB", "Other one")
    cl = client()
    login(cl)
    # A viewer can't pick devices/days: extra fields are ignored.
    page = cl.post(f"/v/{va}/share", data={"csrf": csrf_of(cl), "max_devices": "50", "days": "300"}).text
    code = re.search(r'<p class="secret">([^<]+)</p>', page).group(1)
    assert f"/code?c={code}" in page
    with db.get() as c:
        row = c.execute("SELECT * FROM access_codes").fetchone()
        assert row["max_devices"] == 1 and row["session_days"] == 3
        assert 3 * 86400 - 5 <= row["expires_at"] - db.now() <= 3 * 86400
    friend = client()
    r = friend.post("/code", data={"code": code}, follow_redirects=False)
    assert r.headers["location"] == f"/v/{va}"
    feed = friend.get("/").text
    assert "Shared one" in feed and "Other one" not in feed
    # Code holders can't share further.
    assert friend.post(f"/v/{va}/share", data={"csrf": csrf_of(friend)}).status_code == 404
    # Second device is refused.
    assert client().post("/code", data={"code": code}).status_code == 403
    security.login_throttle._hits.clear()


def test_admin_share_code_is_configurable():
    make_admin()
    ch = make_channel()
    va = make_video(ch, "AAAAAAAAAAA", "Shared one")
    cl = client()
    login_admin(cl)
    page = cl.get(f"/v/{va}").text
    assert 'id="share-dialog"' in page
    cl.post(f"/v/{va}/share", data={"csrf": csrf_of(cl), "max_devices": "5", "days": "10"})
    with db.get() as c:
        row = c.execute("SELECT * FROM access_codes").fetchone()
        assert row["max_devices"] == 5 and row["session_days"] == 10


def test_viewer_share_rate_limit():
    from privatube.web import SHARES_PER_DAY
    make_user()
    va = make_video(make_channel(), "AAAAAAAAAAA", "x")
    cl = client()
    login(cl)
    tok = csrf_of(cl)
    for _ in range(SHARES_PER_DAY):
        cl.post(f"/v/{va}/share", data={"csrf": tok})
    r = cl.post(f"/v/{va}/share", data={"csrf": tok}, follow_redirects=False)
    assert r.status_code == 303 and "Try again tomorrow" in r.headers["location"].replace("%20", " ")


def test_blank_username_gets_random_name():
    make_admin()
    cl = client()
    login_admin(cl)
    page = cl.post("/admin/users", data={"csrf": csrf_of(cl), "username": "", "role": "viewer"}).text
    name = re.search(r"user-[a-z2-9]{4}", page).group(0)
    with db.get() as c:
        assert c.execute("SELECT role FROM users WHERE username=?", (name,)).fetchone()["role"] == "viewer"


def test_comment_refresh_for_recent_videos(monkeypatch):
    ch = make_channel()
    new = make_video(ch, "AAAAAAAAAAA", "new")
    old = make_video(ch, "BBBBBBBBBBB", "old")
    with db.get() as c:
        c.execute("UPDATE channels SET subscribed=0")   # only the comment job should run
        c.execute("UPDATE videos SET subs_at=?", (db.now(),))
        c.execute("UPDATE videos SET comments_at=? WHERE id=?", (db.now() - 2 * 86400, new))
        c.execute("UPDATE videos SET published_at=?, comments_at=0 WHERE id=?", (db.now() - 60 * 86400, old))
        c.execute("INSERT INTO comments(video_id,yt_id,author,text) VALUES (?,?,?,?)", (new, "c0", "a", "old"))
        worker.schedule_polls(c)
        queued = [r[0] for r in c.execute("SELECT video_id FROM jobs WHERE kind='comments'")]
        assert queued == [new]
        worker.schedule_polls(c)               # not queued twice
        assert c.execute("SELECT COUNT(*) FROM jobs WHERE kind='comments'").fetchone()[0] == 1
        monkeypatch.setattr(worker, "fetch_comments", lambda yt: {"comment_count": 7, "comments": [
            {"id": "c1", "author": "@x", "text": "fresh", "like_count": 3}]})
        monkeypatch.setattr(worker, "PAUSE_BETWEEN_DOWNLOADS", (0, 0))
        assert worker.run_one(c)
        assert [r[0] for r in c.execute("SELECT text FROM comments WHERE video_id=?", (new,))] == ["fresh"]
        v = c.execute("SELECT comment_count, comments_at, status FROM videos WHERE id=?", (new,)).fetchone()
        assert v["comment_count"] == 7 and v["comments_at"] >= db.now() - 5 and v["status"] == "ready"
        # An empty fetch keeps the comments we have.
        worker.apply_comments(c, new, {"comments": []})
        assert c.execute("SELECT COUNT(*) FROM comments WHERE video_id=?", (new,)).fetchone()[0] == 1


def test_account_limited_to_two_devices_oldest_signed_out():
    make_user()
    devs = []
    for i in range(3):
        cl = client()
        login(cl)
        devs.append(cl)
        with db.get() as c:   # make the order of use unambiguous
            c.execute("UPDATE sessions SET last_seen_at=last_seen_at-? WHERE last_seen_at=(SELECT MAX(last_seen_at) FROM sessions)", (100 - i,))
    with db.get() as c:
        assert c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
    assert devs[0].get("/", follow_redirects=False).status_code == 303    # first device signed out
    assert devs[1].get("/").status_code == 200 and devs[2].get("/").status_code == 200


def test_admin_device_limit_counts_after_mfa():
    make_admin()
    a1, a2 = client(), client()
    login_admin(a1)
    login_admin(a2)
    half = client()
    login(half, "boss")            # password only, MFA not done yet: doesn't push anyone out
    assert a1.get("/").status_code == 200 and a2.get("/").status_code == 200
    a3 = client()
    login_admin(a3)
    with db.get() as c:
        assert c.execute("SELECT COUNT(*) FROM sessions WHERE mfa_pending=0").fetchone()[0] == 2
    assert a3.get("/admin").status_code == 200


def test_no_crawling():
    cl = client()
    r = cl.get("/robots.txt")
    assert "Disallow: /" in r.text and "noindex" in r.headers["x-robots-tag"]
    assert "noindex" in cl.get("/login").headers["x-robots-tag"]


# ----------------------------------------------------------------- disk

def _media_file(rel, size=1000):
    f = settings.media_dir / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x" * size)
    return f


def _free_on_disk(monkeypatch, free):
    import collections
    import shutil
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(10 * 1024**3, 0, free))


# ----------------------------------------------------------------- languages & quality

def test_keyword_filter_matches_chinese_or_english_title():
    ch = make_channel()
    with db.get() as c:
        c.execute("UPDATE channels SET auto_download=1, include_keywords='news', exclude_keywords='直播' WHERE id=?", (ch,))
        row = c.execute("SELECT * FROM channels WHERE id=?", (ch,)).fetchone()
        n = worker.apply_channel_listing(c, row, {
            "yt_id": "UC" + "a" * 22, "title": "A", "handle": None, "description": "", "avatar": None,
            "entries": [
                {"id": "AAAAAAAAAAA", "title": "每周新闻", "alt_title": "Weekly news", "duration": 1, "timestamp": 1, "is_short": False},
                {"id": "BBBBBBBBBBB", "title": "直播回放", "alt_title": "News live replay", "duration": 1, "timestamp": 1, "is_short": False},
                {"id": "CCCCCCCCCCC", "title": "烹饪", "alt_title": None, "duration": 1, "timestamp": 1, "is_short": False},
            ]})
        assert n == 1   # matched via the English title; the 直播 one is excluded via the Chinese title
        st = {r["yt_id"]: (r["status"], r["title"], r["alt_title"]) for r in c.execute("SELECT * FROM videos")}
        assert st["AAAAAAAAAAA"] == ("queued", "每周新闻", "Weekly news")
        assert st["BBBBBBBBBBB"][0] == "listed" and st["CCCCCCCCCCC"][0] == "listed"


def test_download_keeps_listing_title_and_uses_channel_quality(monkeypatch):
    seen = {}

    def fake_download(yt_id, workdir, on_progress, height=None):
        seen["height"] = height
        (workdir / "video.mp4").write_bytes(b"x")
        return {"id": yt_id, "title": "Weekly news", "channel_id": "UC" + "a" * 22}
    monkeypatch.setattr(worker, "download", fake_download)
    monkeypatch.setattr(worker, "PAUSE_BETWEEN_DOWNLOADS", (0, 0))
    ch = make_channel()
    with db.get() as c:
        c.execute("UPDATE channels SET max_height=480, subscribed=0 WHERE id=?", (ch,))
        vid = c.execute("INSERT INTO videos(yt_id,channel_id,title,status,added_at) VALUES "
                        "('AAAAAAAAAAA',?,'每周新闻','queued',?)", (ch, db.now())).lastrowid
        jobs.enqueue(c, "download", video_id=vid)
        assert worker.run_one(c)
        v = c.execute("SELECT title, alt_title, status FROM videos WHERE id=?", (vid,)).fetchone()
        assert tuple(v) == ("每周新闻", "Weekly news", "ready") and seen["height"] == 480
    assert "height<=480" in settings.ytdlp_format.replace("{h}", "480")


def test_admin_add_with_quality_and_search_alt_title():
    make_admin()
    cl = client()
    login_admin(cl)
    tok = csrf_of(cl)
    cl.post("/admin/add", data={"csrf": tok, "url": "https://youtu.be/dQw4w9WgXcQ", "max_height": "480"})
    cl.post("/admin/add", data={"csrf": tok, "url": "@SomeOne", "kind": "channel", "max_height": "999"})
    with db.get() as c:
        assert c.execute("SELECT max_height FROM videos").fetchone()[0] == 480
        assert c.execute("SELECT max_height FROM channels").fetchone()[0] == 720   # invalid -> default
        ch = make_channel(yt="UC" + "z" * 22)
        vid = make_video(ch, "ZZZZZZZZZZZ", "每周新闻")
        c.execute("UPDATE videos SET alt_title='Weekly news' WHERE id=?", (vid,))
    assert "每周新闻" in cl.get("/search?q=weekly").text


def test_downloads_pause_below_min_free(monkeypatch):
    import collections
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(worker.shutil, "disk_usage", lambda p: usage(10, 0, settings.min_free_bytes - 1))
    vid = make_video(make_channel(), "AAAAAAAAAAA", status="queued")
    with db.get() as c:
        c.execute("UPDATE channels SET subscribed=0")
        jobs.enqueue(c, "download", video_id=vid)
        worker.run_one(c)
        j = c.execute("SELECT status, error FROM jobs").fetchone()
        assert j["status"] == "pending" and "low disk" in j["error"]
    assert settings.min_free_bytes == 2 * 1024**3


# ----------------------------------------------------------------- backup restore drill & health

def test_backup_restore_drill(monkeypatch, tmp_path, capsys):
    """Make a real backup, damage the live DB, restore, and check the data is back."""
    from privatube import backup as bk, cli
    make_user("before")
    ch = make_channel()
    on_a = make_video(ch, "AAAAAAAAAAA", "file on A")
    gone = make_video(ch, "BBBBBBBBBBB", "file lost")
    _media_file("X/AAAAAAAAAAA/video.mp4")
    with db.get() as c:
        assert bk.backup_db(c, force=True) is True
        assert bk.backup_db(c) is False                # not due again yet
        backup = settings.backup_dir / db.kv_get(c, "backup")["file"]
    assert backup.is_file()
    with db.get() as c:                       # after the backup: someone deletes things
        c.execute("DELETE FROM users")
    assert cli.main(["check-backup", str(backup)]) == 0
    assert "users 1" in capsys.readouterr().out
    assert cli.main(["restore", str(backup)]) == 0
    out = capsys.readouterr().out
    assert "previous database kept" in out and "evicted 1" in out
    with db.get() as c:
        assert c.execute("SELECT username FROM users").fetchone()[0] == "before"
        st = dict(c.execute("SELECT id, status FROM videos").fetchall())
        assert st[on_a] == "ready" and st[gone] == "evicted"
    assert list(settings.db_path.parent.glob("*.before-restore-*"))
    bad = tmp_path / "bad.db.gz"
    bad.write_bytes(b"not a backup")
    assert cli.main(["check-backup", str(bad)]) == 1


def test_health_problems_and_alerts(monkeypatch):
    from privatube import health
    pushed = []
    monkeypatch.setattr(health, "_push", lambda title, text: pushed.append(title) or True)
    monkeypatch.setattr(settings.__class__, "alert_url", "https://example.invalid/x", raising=False)
    object.__setattr__(settings, "alert_url", "https://example.invalid/x")
    try:
        ch = make_channel()
        with db.get() as c:
            c.execute("UPDATE channels SET subscribed=1, last_error='boom' WHERE id=?", (ch,))
            for i in range(3):
                c.execute("INSERT INTO jobs(kind,status,run_after,created_at,updated_at,error) "
                          "VALUES ('download','failed',0,?,?,'Sign in to confirm')", (db.now(), db.now() - i))
            now = health.run(c, force=True)
            assert {p["key"] for p in now} >= {"downloads", "channels"}
            assert len(pushed) == 1 and pushed[0].startswith("[PrivaTube] 2 problem")
            health.run(c, force=True)                 # same problems: no repeat within a day
            assert len(pushed) == 1
            c.execute("UPDATE channels SET last_error=NULL")
            c.execute("DELETE FROM jobs")
            assert health.run(c, force=True) == []
            assert pushed[-1] == "[PrivaTube] All clear"
    finally:
        object.__setattr__(settings, "alert_url", "")


def test_admin_banner_shows_problems():
    make_admin()
    cl = client()
    login_admin(cl)
    page = cl.get("/admin").text
    assert "Needs attention" in page and "worker" in page      # no worker has run in tests
    with db.get() as c:
        db.kv_set(c, "worker_seen", db.now())
        db.kv_set(c, "health", [])
    assert "Needs attention" not in cl.get("/admin").text


def test_youtube_cookies_upload_and_use():
    make_admin()
    cl = client()
    login_admin(cl)
    tok = csrf_of(cl)
    r = cl.post("/admin/youtube/upload", data={"csrf": tok, "text": "hello"}, follow_redirects=False)
    assert "Couldn" in r.headers["location"].replace("%20", " ") and not settings.ytdlp_cookies.exists()
    good = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n"
    cl.post("/admin/youtube/upload", data={"csrf": tok}, files={"file": ("cookies.txt", good, "text/plain")})
    assert settings.ytdlp_cookies.read_text() == good
    assert settings.ytdlp_cookies.stat().st_mode & 0o777 == 0o600
    assert worker._base_opts()["cookiefile"] == str(settings.ytdlp_cookies)
    assert "cookies in use" in cl.get("/admin/youtube").text
    cl.post("/admin/youtube/remove", data={"csrf": tok})
    assert not settings.ytdlp_cookies.exists() and "cookiefile" not in worker._base_opts()
    js = '[{"domain": ".youtube.com", "name": "SID", "value": "x1", "path": "/", "secure": true, "expirationDate": 1900000000.5}]'
    cl.post("/admin/youtube/upload", data={"csrf": tok, "text": js})
    assert ".youtube.com\tTRUE\t/\tTRUE\t1900000000\tSID\tx1" in settings.ytdlp_cookies.read_text()
    cl.post("/admin/youtube/upload", data={"csrf": tok, "text": "Cookie: SID=a=b; HSID=c"})
    t = settings.ytdlp_cookies.read_text()
    assert t.startswith("# Netscape") and "\tSID\ta=b" in t and "\tHSID\tc" in t
    r = cl.post("/admin/youtube/upload", data={"csrf": tok, "text": '[{"domain": ".google.com", "name": "x"}]'},
                follow_redirects=False)
    assert "no youtube.com cookies" in r.headers["location"].replace("%20", " ")


def test_bot_failures_rerun_after_successful_test(monkeypatch):
    make_admin()
    ch = make_channel()
    v = make_video(ch, "AAAAAAAAAAA", "blocked")
    with db.get() as c:
        c.execute("UPDATE videos SET status='failed' WHERE id=?", (v,))
        bot = "DownloadError: Sign in to confirm you’re not a bot."
        for kind in ("download", "subs"):
            c.execute("INSERT INTO jobs(kind,video_id,status,attempts,run_after,created_at,updated_at,error) "
                      "VALUES (?,?,'failed',5,0,0,0,?)", (kind, v, bot))
        c.execute("INSERT INTO jobs(kind,video_id,status,attempts,run_after,created_at,updated_at,error) "
                  "VALUES ('download',?,'failed',5,0,0,0,'Video unavailable')", (v,))
    monkeypatch.setattr(worker, "probe", lambda: "Me at the zoo")
    cl = client()
    login_admin(cl)
    r = cl.post("/admin/youtube/test", data={"csrf": csrf_of(cl)}, follow_redirects=False)
    assert "2 job" in r.headers["location"].replace("%20", " ")
    with db.get() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs WHERE status='pending' AND attempts=0").fetchone()[0] == 2
        assert c.execute("SELECT status FROM videos WHERE id=?", (v,)).fetchone()[0] == "queued"
        assert c.execute("SELECT status FROM jobs WHERE error='Video unavailable'").fetchone()[0] == "failed"


# ----------------------------------------------------------------- subtitles

def test_subtitle_pick_and_vtt_conversion():
    from privatube import subs
    assert subs.pick({"en": [], "zh-TW": [], "live_chat": []}, {"zh-Hans": [], "en": []}, ["zh", "en"]) == \
        [("zh-TW", False), ("en", False)]
    assert subs.pick({}, {"zh-Hans": [], "en": [], "fr": []}, ["zh", "en"]) == [("zh-Hans", True), ("en", True)]
    assert subs.pick({"de": []}, {}, ["zh", "en"]) == []
    assert subs.pick({"de": []}, {}, ["de"]) == [("de", False)]
    assert subs.pick({}, {"zh-Hans": [], "zh-orig": [], "en": []}, ["zh", "en"]) == [("zh-orig", True), ("en", True)]
    assert subs.pick({"en": [], "fr": []}, {}) == [("en", False)]                  # default PT_SUB_LANGS=en
    vtt = subs.json3_to_vtt({"events": [
        {"tStartMs": 0, "dDurationMs": 4000, "segs": [{"utf8": "hello "}, {"utf8": "<b>world</b>"}]},
        {"tStartMs": 1500, "dDurationMs": 3000, "segs": [{"utf8": "next & line"}]},
        {"tStartMs": 5000, "dDurationMs": 1000, "segs": [{"utf8": "\n"}]},
    ]})
    assert vtt.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:01.500\nhello &lt;b&gt;world&lt;/b&gt;" in vtt   # clipped, escaped
    assert "next &amp; line" in vtt and vtt.count("-->") == 2


def test_subtitles_job_and_serving(monkeypatch):
    import json as _json

    calls = []

    def fake_fetch(yt_id, folder, skip=frozenset()):
        calls.append(set(skip))
        folder.mkdir(parents=True, exist_ok=True)
        if len(calls) == 1:     # first try: English fine, the Chinese translation is rate-limited
            (folder / "sub.en.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello\n")
            return [{"lang": "en", "auto": True, "file": "sub.en.vtt"}], ["zh-Hans"]
        (folder / "sub.zh-Hans.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n你好\n")
        return [{"lang": "zh-Hans", "auto": True, "file": "sub.zh-Hans.vtt"}], []
    monkeypatch.setattr(worker, "fetch_subs", fake_fetch)
    monkeypatch.setattr(worker, "PAUSE_BETWEEN_DOWNLOADS", (0, 0))
    make_user()
    ch = make_channel()
    va = make_video(ch, "AAAAAAAAAAA", "with subs")
    vb = make_video(ch, "BBBBBBBBBBB", "other")
    with db.get() as c:
        c.execute("UPDATE channels SET subscribed=0")
        c.execute("UPDATE videos SET subs_at=? WHERE id=?", (db.now(), vb))
        worker.schedule_polls(c)                            # backfill picks up the video without subs
        assert worker.run_one(c)
        retry = c.execute("SELECT run_after FROM jobs WHERE kind='subs' AND status='pending'").fetchone()
        assert retry and retry[0] > db.now() + 80000             # Chinese retried tomorrow
        c.execute("UPDATE jobs SET run_after=0 WHERE kind='subs' AND status='pending'")
        assert worker.run_one(c) and calls[1] == {"en"}
        assert worker.run_one(c) is False or not c.execute(
            "SELECT 1 FROM jobs WHERE kind='subs' AND status='pending'").fetchone()
        langs = [t["lang"] for t in _json.loads(c.execute("SELECT subs FROM videos WHERE id=?", (va,)).fetchone()[0])]
        assert langs == ["en", "zh-Hans"]
    cl = client()
    login(cl)
    page = cl.get(f"/v/{va}").text
    assert f'src="/v/{va}/subs/zh-Hans.vtt"' in page and 'srclang="zh-Hans"' in page
    r = cl.get(f"/v/{va}/subs/zh-Hans.vtt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/vtt") and "你好" in r.text
    assert cl.get(f"/v/{va}/subs/fr.vtt").status_code == 404
    assert cl.get(f"/v/{va}/subs/..%2F..%2Fx.vtt").status_code == 404
    assert client().get(f"/v/{va}/subs/zh-Hans.vtt", follow_redirects=False).status_code == 303   # login needed


# ----------------------------------------------------------------- download ranges & priority

def _listing(n, days_apart=10):
    now = db.now()
    return {"yt_id": "UC" + "r" * 22, "title": "Ranged", "handle": "@r", "description": "", "avatar": None,
            "entries": [{"id": f"r{i:010d}", "title": f"video {i}", "alt_title": None, "duration": 600,
                         "timestamp": now - i * days_apart * 86400, "is_short": False} for i in range(n)]}


def test_new_channel_backfill_latest_and_months():
    from privatube import jobs
    for spec, expect in (("n:10", 10), ("d:91", 10), ("d:365", 37), ("", 0)):
        with db.get() as c:
            c.execute("DELETE FROM jobs"); c.execute("DELETE FROM videos"); c.execute("DELETE FROM channels")
            ch = c.execute("INSERT INTO channels(url,backfill,priority,created_at) VALUES ('u',?,1,?)",
                           (spec or None, db.now())).lastrowid
            row = c.execute("SELECT * FROM channels WHERE id=?", (ch,)).fetchone()
            n = worker.apply_channel_listing(c, row, _listing(60))
            assert n == expect, spec
            if expect:
                first = c.execute("SELECT v.title, j.priority FROM jobs j JOIN videos v ON v.id=j.video_id "
                                  "ORDER BY j.priority, j.id LIMIT 1").fetchone()
                assert first["title"] == "video 0" and first["priority"] == jobs.PRIO_BY_CHANNEL[1]


def test_channel_priority_range_and_cancel_from_jobs_page():
    from privatube import jobs
    make_admin()
    with db.get() as c:
        ch = c.execute("INSERT INTO channels(url,title,created_at,baseline_done,auto_download) VALUES ('u','Busy',?,1,0)",
                       (db.now(),)).lastrowid
        worker.apply_channel_listing(c, c.execute("SELECT * FROM channels WHERE id=?", (ch,)).fetchone(),
                                     _listing(30))
    cl = client()
    login_admin(cl)
    tok = csrf_of(cl)
    cl.post(f"/admin/channels/{ch}/queue-range", data={"csrf": tok, "range": "n:20"})
    with db.get() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs WHERE kind='download' AND status='pending'").fetchone()[0] == 20
        assert {r[0] for r in c.execute("SELECT priority FROM jobs")} == {jobs.PRIO_BY_CHANNEL[0]}
    page = cl.get("/admin/jobs").text
    assert "Download queue by channel" in page and "Ranged" in page
    cl.post(f"/admin/channels/{ch}/priority", data={"csrf": tok, "priority": "-1", "back": "jobs"})
    with db.get() as c:
        assert {r[0] for r in c.execute("SELECT priority FROM jobs")} == {jobs.PRIO_BY_CHANNEL[-1]}
    r = cl.post(f"/admin/channels/{ch}/cancel-queue", data={"csrf": tok, "back": "jobs"}, follow_redirects=False)
    assert r.headers["location"].startswith("/admin/jobs")
    with db.get() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM videos WHERE status='listed'").fetchone()[0] == 30
    cl.post(f"/admin/channels/{ch}/queue-range", data={"csrf": tok, "range": "bogus"})
    with db.get() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0] == 0


def test_add_channel_form_stores_backfill_and_priority():
    make_admin()
    cl = client()
    login_admin(cl)
    cl.post("/admin/add", data={"csrf": csrf_of(cl), "url": "https://www.youtube.com/@someone",
                                "kind": "channel", "backfill": "d:182", "priority": "1"})
    with db.get() as c:
        row = c.execute("SELECT backfill, priority FROM channels").fetchone()
        assert (row["backfill"], row["priority"]) == ("d:182", 1)
    assert "last 6 months" in cl.get("/admin").text


# ----------------------------------------------------------------- storage planning & delivery check

def test_queue_estimate_learns_from_channel_and_compares_with_room(monkeypatch):
    from privatube import storage
    ch = make_channel()
    with db.get() as c:
        for i in range(3):   # this channel: 50 kB per second of video
            c.execute("INSERT INTO videos(yt_id,channel_id,status,filesize,duration,added_at) VALUES (?,?,'ready',?,?,0)",
                      (f"d{i:010d}", ch, 50_000 * 1000, 1000))
        for i in range(4):
            c.execute("INSERT INTO videos(yt_id,channel_id,status,duration,added_at) VALUES (?,?,'queued',?,0)",
                      (f"q{i:010d}", ch, 2000 if i < 3 else None))
        est = storage.queue_estimate(c)
        assert est["n"] == 4 and est["unknown"] == 1
        assert est["bytes"] == 4 * 2000 * 50_000 == est["by_channel"][ch]    # unknown length -> median
        monkeypatch.setattr(storage, "room", lambda conn: 10 ** 8)
        from privatube import health
        assert any(p["key"] == "queue_space" for p in health.problems(c))


# ----------------------------------------------------------------- continue watching / history / app manifest

def test_continue_watching_and_history():
    make_user()
    ch = make_channel()
    half = make_video(ch, "AAAAAAAAAAA", "half watched")
    done = make_video(ch, "BBBBBBBBBBB", "finished")
    make_video(ch, "CCCCCCCCCCC", "never opened")
    with db.get() as c:
        c.execute("UPDATE videos SET duration=1000")
        uid = c.execute("SELECT id FROM users").fetchone()[0]
        c.execute("INSERT INTO watch(viewer,video_id,position,updated_at) VALUES (?,?,400,?)", (f"u{uid}", half, db.now()))
        c.execute("INSERT INTO watch(viewer,video_id,position,updated_at) VALUES (?,?,990,?)", (f"u{uid}", done, db.now() - 5))
    cl = client()
    login(cl)
    home = cl.get("/").text
    resume = home.split("Continue watching", 1)[1].split("Latest", 1)[0]
    assert "half watched" in resume and "finished" not in resume and "never opened" not in resume
    assert 'data-more' not in resume
    hist = cl.get("/history").text
    assert hist.index("half watched") < hist.index("finished") and "never opened" not in hist
    cl.post("/history/clear", data={"csrf": csrf_of(cl)})
    assert "Nothing watched yet." in cl.get("/history").text
    assert "Continue watching" not in cl.get("/").text
    cl.cookies.set("pt_lang", "zh")
    assert "观看记录" in cl.get("/history").text


def test_app_manifest_and_icons():
    cl = client()
    import json as _json
    m = _json.loads(cl.get("/static/manifest.json").text)
    assert m["display"] == "standalone" and m["name"] == "PrivaTube"
    for icon in m["icons"]:
        assert cl.get(icon["src"]).status_code == 200
    assert 'rel="manifest"' in cl.get("/login").text


def test_static_files_cached_with_versioned_urls():
    cl = client()
    page = cl.get("/login")
    assert "app;dur=" in page.headers["server-timing"]
    assert page.headers["cache-control"] == "no-store"
    css = re.search(r'href="(/static/style\.css\?v=[0-9a-f]{10})"', page.text).group(1)
    r = cl.get(css)
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    assert "immutable" not in cl.get("/static/style.css").headers["cache-control"]
    assert re.search(r'src="/static/app\.js\?v=', page.text)


# ----------------------------------------------------------------- members-only videos

def _entry(yt_id, title, ts, members=False):
    return {"id": yt_id, "title": title, "duration": 60, "timestamp": ts, "is_short": False, "members": members}


def test_members_only_never_queued_public_version_is():
    now = db.now()
    with db.get() as c:
        ch_id = c.execute("INSERT INTO channels(url,backfill,created_at) VALUES ('https://www.youtube.com/@m','n:10',?)",
                          (now,)).lastrowid
        listing = {"yt_id": "UC" + "m" * 22, "title": "M", "handle": "@m", "description": "", "avatar": None,
                   "entries": [_entry("MEMB0000001", "Episode 5 (members)", now - 100, members=True),
                               _entry("PUBL0000001", "Episode 5", now - 200)]}
        ch = c.execute("SELECT * FROM channels WHERE id=?", (ch_id,)).fetchone()
        assert worker.apply_channel_listing(c, ch, listing) == 1    # only the public copy, though both are among the latest 10
        st = dict(c.execute("SELECT yt_id, status FROM videos").fetchall())
        assert st == {"MEMB0000001": "listed", "PUBL0000001": "queued"}
        # later uploads: members copy skipped, public copy auto-queued
        listing["entries"][:0] = [_entry("MEMB0000002", "Ep 6", now - 10, members=True),
                                  _entry("PUBL0000002", "Ep 6", now - 20)]
        ch = c.execute("SELECT * FROM channels WHERE id=?", (ch_id,)).fetchone()
        assert worker.apply_channel_listing(c, ch, listing) == 1
        assert c.execute("SELECT status FROM videos WHERE yt_id='MEMB0000002'").fetchone()[0] == "listed"
        # "members first": the same video turns public later -> downloaded then
        listing["entries"][0]["members"] = False
        assert worker.apply_channel_listing(c, ch, listing) == 1
        assert tuple(c.execute("SELECT status, members FROM videos WHERE yt_id='MEMB0000002'").fetchone()) == ("queued", 0)


def test_members_only_found_after_queueing_is_unqueued(monkeypatch):
    with db.get() as c:
        ch_id = c.execute("INSERT INTO channels(url,baseline_done,created_at) VALUES ('https://www.youtube.com/@q',1,?)",
                          (db.now(),)).lastrowid
        vid = c.execute("INSERT INTO videos(yt_id,channel_id,status,added_at) VALUES ('MEMQ0000001',?,'queued',?)",
                        (ch_id, db.now())).lastrowid
        jobs.queue_download(c, vid)
        ch = c.execute("SELECT * FROM channels WHERE id=?", (ch_id,)).fetchone()
        worker.apply_channel_listing(c, ch, {"yt_id": "UC" + "q" * 22, "title": "Q", "handle": None, "description": None,
                                             "avatar": None, "entries": [_entry("MEMQ0000001", "x", None, True)]})
        assert tuple(c.execute("SELECT status, members FROM videos").fetchone()) == ("listed", 1)
        assert c.execute("SELECT status FROM jobs WHERE kind='download'").fetchone()[0] == "cancelled"
        assert jobs.queue_range(c, ch_id, "n:10") == 0


def test_members_only_download_is_skipped_not_failed(monkeypatch):
    from yt_dlp.utils import DownloadError
    assert isinstance(worker._classify(DownloadError(
        "ERROR: [youtube] abc: Join this channel to get access to members-only content like this video")),
        jobs.MembersOnly)
    assert not isinstance(worker._classify(DownloadError("ERROR: Private video")), jobs.MembersOnly)

    class FakeYDL:   # the cookies' account is a member: yt-dlp would download it, we refuse
        def __init__(self, opts): self.opts = opts
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download):
            info = {"id": "x", "availability": "subscriber_only", "live_status": "not_live"}
            assert self.opts["match_filter"](info) == "members-only"
            return info
    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    with pytest.raises(jobs.MembersOnly):
        worker.download("MEMD0000001", settings.media_dir, lambda f: None)
    with db.get() as c:
        vid = c.execute("INSERT INTO videos(yt_id,status,added_at) VALUES ('MEMD0000001','queued',?)",
                        (db.now(),)).lastrowid
        jobs.enqueue(c, "download", video_id=vid)
        worker.run_one(c)
        v = c.execute("SELECT status, members, error FROM videos").fetchone()
        assert (v["status"], v["members"]) == ("listed", 1) and "Members-only" in v["error"]
        assert c.execute("SELECT status FROM jobs").fetchone()[0] == "cancelled"   # skipped, not a failure


def test_admin_channel_page_marks_members_only():
    make_admin()
    cl = client()
    login_admin(cl)
    with db.get() as c:
        ch_id = c.execute("INSERT INTO channels(url,title,created_at) VALUES ('https://www.youtube.com/@a','A',?)",
                          (db.now(),)).lastrowid
        c.execute("INSERT INTO videos(yt_id,channel_id,title,status,members,added_at) "
                  "VALUES ('MEMA0000001',?,'Paid ep','listed',1,?)", (ch_id, db.now()))
    page = cl.get(f"/admin/channels/{ch_id}").text
    assert "Members only" in page and 'name="video"' not in page


def test_job_cut_off_by_worker_restart_is_not_a_failure(monkeypatch):
    def killed(*a, **k):
        worker.STOPPING.set()      # systemctl restart: SIGTERM reaches rsync/ssh too
        raise RuntimeError("rsync error: unexplained error (code 255) at rsync.c(874)")
    monkeypatch.setattr(worker, "download", killed)
    try:
        with db.get() as c:
            vid = c.execute("INSERT INTO videos(yt_id,status,added_at) VALUES ('STOP0000001','queued',?)",
                            (db.now(),)).lastrowid
            jid = jobs.enqueue(c, "download", video_id=vid)
            worker.run_one(c)
            j = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            assert j["status"] == "pending" and j["attempts"] == 0 and "restart" in j["error"]
            assert c.execute("SELECT status FROM videos").fetchone()[0] == "queued"
    finally:
        worker.STOPPING.clear()


# ----------------------------------------------------------------- pictures, adding, dashboard

def test_thumbnails_and_avatars_come_from_the_site():
    make_user()
    ch = make_channel()
    vid = make_video(ch, "TTTTTTTTTTT", "Thumb")
    other = make_video(make_channel("UC" + "z" * 22, "Other"), "OOOOOOOOOOO", "Other")
    d = settings.media_dir / "X" / "TTTTTTTTTTT"
    d.mkdir(parents=True, exist_ok=True)
    (d / "thumb.jpg").write_bytes(b"\xff\xd8jpeg")
    (settings.media_dir / "UCav").mkdir(exist_ok=True)
    (settings.media_dir / "UCav" / "avatar.jpg").write_bytes(b"\xff\xd8avatar")
    with db.get() as c:
        c.execute("UPDATE channels SET avatar='UCav/avatar.jpg' WHERE id=?", (ch,))
    cl = client()
    login(cl)
    feed = cl.get("/").text
    # signed links on this site, checked by nginx (or the app in dev mode)
    m = re.search(r'<img src="(/m/X/TTTTTTTTTTT/thumb\.jpg\?[^"]+)"', feed)
    assert m and cl.get(m.group(1).replace("&amp;", "&")).content == b"\xff\xd8jpeg"
    assert re.search(r'src="/m/UCav/avatar\.jpg\?[^"]+"', feed)
    assert "data-alt" not in feed


def test_admin_add_several_links_at_once():
    make_admin()
    cl = client()
    login_admin(cl)
    pasted = ("https://youtu.be/dQw4w9WgXcQ\nhttps://www.youtube.com/watch?v=jNQXAC9IVRw, "
              "https://youtu.be/dQw4w9WgXcQ  @SomeOne，not-a-link\nhttps://www.youtube.com/shorts/aqz-KE-bpKQ")
    r = cl.post("/admin/add", data={"csrf": csrf_of(cl), "url": pasted, "kind": "auto", "max_height": "480"},
                follow_redirects=False)
    msg = r.headers["location"]
    assert msg.startswith("/admin?") and "Queued%203%20video" in msg and "Added%201%20channel" in msg
    assert "not-a-link" in msg
    with db.get() as c:
        assert sorted(r[0] for r in c.execute("SELECT yt_id FROM videos")) == ["aqz-KE-bpKQ", "dQw4w9WgXcQ", "jNQXAC9IVRw"]
        assert {r[0] for r in c.execute("SELECT max_height FROM videos")} == {480}      # settings apply to all
        assert c.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 1
    # "videos only": the channel is skipped, not added
    r = cl.post("/admin/add", data={"csrf": csrf_of(cl), "url": "@Other https://youtu.be/BaW_jenozKc", "kind": "video"},
                follow_redirects=False)
    assert "Skipped%201" in r.headers["location"]
    with db.get() as c:
        assert c.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 1


def test_dashboard_storage_summary():
    make_admin()
    cl = client()
    login_admin(cl)
    page = cl.get("/admin").text
    assert "Nothing waiting to download." in page and "about ." not in page
    assert "before downloads pause" in page and "<textarea name=\"url\"" in page
    assert "Back up now" in page
    r = cl.post("/admin/backup", data={"csrf": csrf_of(cl)}, follow_redirects=False)
    assert "backed%20up" in r.headers["location"]
    assert list(settings.backup_dir.glob("privatube-*.db.gz"))


def test_channel_from_single_video_gets_its_picture(monkeypatch):
    yt = "UC" + "p" * 22
    with db.get() as c:
        ch = c.execute("INSERT INTO channels(yt_id,url,subscribed,created_at) VALUES (?,?,0,?)",
                       (yt, f"https://www.youtube.com/channel/{yt}", db.now())).lastrowid
        worker.schedule_polls(c)
        assert c.execute("SELECT COUNT(*) FROM jobs WHERE kind='channel_info' AND channel_id=?", (ch,)).fetchone()[0] == 1
        worker.schedule_polls(c)                                  # no duplicate while pending
        assert c.execute("SELECT COUNT(*) FROM jobs WHERE kind='channel_info'").fetchone()[0] == 1
        calls = []
        monkeypatch.setattr(worker, "channel_info", lambda url: calls.append(url) or
                            {"title": "Pic", "handle": "@pic", "description": "d", "avatar": b"\xff\xd8pic"})
        job = c.execute("SELECT * FROM jobs WHERE kind='channel_info'").fetchone()
        worker.run_channel_info(c, job)
        row = c.execute("SELECT avatar, title, subscribed FROM channels WHERE id=?", (ch,)).fetchone()
        assert row["avatar"] == f"{yt}/avatar.jpg" and row["title"] == "Pic" and row["subscribed"] == 0
        assert (settings.media_dir / yt / "avatar.jpg").read_bytes() == b"\xff\xd8pic"
        assert c.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0      # no listing, no downloads
        c.execute("UPDATE jobs SET status='done', updated_at=? WHERE kind='channel_info'", (db.now(),))
        c.execute("UPDATE channels SET avatar=NULL WHERE id=?", (ch,))
        worker.schedule_polls(c)                                  # tried this week: not again yet
        assert c.execute("SELECT COUNT(*) FROM jobs WHERE kind='channel_info'").fetchone()[0] == 1


def test_admin_channel_list_has_search_box():
    make_admin()
    cl = client()
    login_admin(cl)
    ch = make_channel()
    make_video(ch, "SSSSSSSSSSS", "Findable title", status="listed")
    page = cl.get(f"/admin/channels/{ch}").text
    assert 'data-filter-rows="table.picklist tr[data-title]"' in page and 'data-title="Findable title' in page


def test_bot_check_pauses_all_youtube_work(monkeypatch):
    def bot(*a, **k):
        raise RuntimeError("ERROR: [youtube] x: Sign in to confirm you’re not a bot. This helps protect our community.")
    monkeypatch.setattr(worker, "download", bot)
    from privatube import health
    with db.get() as c:
        v1 = c.execute("INSERT INTO videos(yt_id,status,added_at) VALUES ('BOT00000001','queued',?)", (db.now(),)).lastrowid
        v2 = c.execute("INSERT INTO videos(yt_id,status,added_at) VALUES ('BOT00000002','queued',?)", (db.now(),)).lastrowid
        j1, j2 = jobs.enqueue(c, "download", video_id=v1), jobs.enqueue(c, "download", video_id=v2)
        assert worker.run_one(c)
        j = c.execute("SELECT * FROM jobs WHERE id=?", (j1,)).fetchone()
        assert j["status"] == "pending" and j["attempts"] == 0 and j["run_after"] >= db.now() + 1700
        assert "not a bot" in j["error"]
        assert c.execute("SELECT status FROM videos WHERE id=?", (v1,)).fetchone()[0] == "queued"
        assert not worker.run_one(c)                      # paused: the other download waits too
        assert c.execute("SELECT status FROM jobs WHERE id=?", (j2,)).fetchone()[0] == "pending"
        # three in a row: the pause grows and the admin banner asks for fresh cookies
        for _ in range(2):
            db.kv_set(c, "yt_pause", db.kv_get(c, "yt_pause") | {"until": 0})
            c.execute("UPDATE jobs SET run_after=0 WHERE status='pending'")
            assert worker.run_one(c)
        p = db.kv_get(c, "yt_pause")
        assert p["streak"] == 3 and p["until"] >= db.now() + 4 * 1800 - 5
        assert any(x["key"] == "bot_check" for x in health.problems(c))
        # YouTube answers again: everything resumes
        monkeypatch.setattr(worker, "download", lambda *a, **k: (_ for _ in ()).throw(worker.Permanent("Private video")))
        worker.retry_bot_failures(c)
        assert db.kv_get(c, "yt_pause") is None


def test_clear_and_dismiss_failures():
    make_admin()
    cl = client()
    login_admin(cl)
    with db.get() as c:
        vid = c.execute("INSERT INTO videos(yt_id,status,added_at) VALUES ('FAIL0000001','failed',?)", (db.now(),)).lastrowid
        a = jobs.enqueue(c, "download", video_id=vid)
        b = jobs.enqueue(c, "subs", video_id=vid)
        c.execute("UPDATE jobs SET status='failed', error='boom'")
    assert "Clear failures" in cl.get("/admin").text
    cl.post(f"/admin/jobs/{a}/dismiss", data={"csrf": csrf_of(cl)})
    with db.get() as c:
        assert dict(c.execute("SELECT id, status FROM jobs").fetchall()) == {a: "cancelled", b: "failed"}
    r = cl.post("/admin/jobs/clear-failed", data={"csrf": csrf_of(cl)}, follow_redirects=False)
    assert "Cleared%201" in r.headers["location"]
    with db.get() as c:
        assert {s for (s,) in c.execute("SELECT status FROM jobs")} == {"cancelled"}
        assert c.execute("SELECT status FROM videos").fetchone()[0] == "failed"     # still retryable
    assert "Recent failures" not in cl.get("/admin").text


def test_pages_prefetch_on_hover_without_side_effects():
    import json as _json
    from privatube import web
    make_admin()
    cl = client()
    login_admin(cl)
    ch = make_channel()
    vid = make_video(ch, "PREFETCH001", "Prefetched")
    r = cl.get("/")
    rules_url = r.headers["speculation-rules"].strip('"')
    assert rules_url == web.SPECULATION_URL
    rr = cl.get(rules_url)
    assert rr.headers["content-type"].startswith("application/speculationrules+json")
    rule = _json.loads(rr.text)["prefetch"][0]
    allowed = rule["where"]["and"][0]["href_matches"]
    assert "/v/*" in allowed and not any(x.startswith(("/lang", "/account", "/admin", "/login")) for x in allowed)
    assert "speculation-rules" not in {k.lower() for k in cl.get(f"/v/{vid}/thumb.jpg").headers}
    with db.get() as c:
        c.execute("UPDATE videos SET last_viewed_at=NULL WHERE id=?", (vid,))
    assert cl.get(f"/v/{vid}", headers={"Sec-Purpose": "prefetch"}).status_code == 200
    with db.get() as c:
        assert c.execute("SELECT last_viewed_at FROM videos WHERE id=?", (vid,)).fetchone()[0] is None
    page = cl.get(f"/v/{vid}").text                       # a real visit still counts
    with db.get() as c:
        assert c.execute("SELECT last_viewed_at FROM videos WHERE id=?", (vid,)).fetchone()[0]
        c.execute("UPDATE videos SET last_viewed_at=NULL WHERE id=?", (vid,))
    import re as _re
    csrf = _re.search(r'name="csrf-token" content="([^"]+)"', page) or _re.search(r'data-csrf="([^"]+)"', page)
    assert cl.post("/api/progress", json={"video_id": vid, "position": 12},
                   headers={"x-csrf": csrf.group(1)}).status_code == 200      # playing counts as viewed
    with db.get() as c:
        assert c.execute("SELECT last_viewed_at FROM videos WHERE id=?", (vid,)).fetchone()[0]
