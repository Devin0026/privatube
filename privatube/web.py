"""Viewer-facing web app: login, access codes, feed, channels, watch, search.

Run with:  uvicorn privatube.web:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from starlette.concurrency import run_in_threadpool

from . import db, i18n, security, subs
from .config import settings
from .security import Principal

HERE = Path(__file__).parent
COOKIE = "pt_session"
PAGE = 24



@asynccontextmanager
async def lifespan(_app):
    db.migrate()
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


# ----------------------------------------------------------------- template helpers

def _duration(s) -> str:
    if not s:
        return ""
    s = int(s)
    h, m, sec = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02}:{sec:02}" if h else f"{m}:{sec:02}"


@pass_context
def _ago(ctx, ts) -> str:
    if not ts:
        return ""
    lang = ctx.get("lang", "en")
    d = db.now() - int(ts)
    for unit, n in (("year", 31536000), ("month", 2592000), ("week", 604800),
                    ("day", 86400), ("hour", 3600), ("minute", 60)):
        if d >= n:
            k = d // n
            return i18n.gettext(lang, f"{{n}} {unit}{'s' if k > 1 else ''} ago", n=k)
    return i18n.gettext(lang, "just now")


def _initials(name) -> str:
    """Letter avatar: one CJK character, or two letters/digits otherwise."""
    s = "".join(ch for ch in (name or "").lstrip("@") if ch.isalnum())
    if not s:
        return "?"
    return s[0] if ord(s[0]) > 0x2E7F else (s[0].upper() + s[1:2].lower())


def _avatar_color(name) -> str:
    return f"av{sum((name or '').encode()) % 10}"


def _date(ts) -> str:
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime("%Y-%m-%d") if ts else ""


def _size(n) -> str:
    if n is None or n == "":
        return ""     # unknown
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


templates.env.filters.update(duration=_duration, ago=_ago, date=_date, size=_size,
                             initials=_initials, avatar_color=_avatar_color)

_ASSET_V: dict[str, str] = {}


def _asset(path: str) -> str:
    """/static/<path>?v=<content hash>: browsers (and Cloudflare) keep it for a year and
    never re-ask on page clicks; a new version gets a new URL."""
    if path not in _ASSET_V:
        _ASSET_V[path] = hashlib.md5((HERE / "static" / path).read_bytes()).hexdigest()[:10]
    return f"/static/{path}?v={_ASSET_V[path]}"


templates.env.globals["asset"] = _asset


def render(request: Request, name: str, p: Principal | None, http_status: int = 200, **ctx) -> HTMLResponse:
    lang = i18n.pick(request.cookies.get(i18n.COOKIE), request.headers.get("accept-language"))
    if p and p.is_admin and request.url.path.startswith("/admin") and "health" not in ctx:
        from . import health
        with db.get() as conn:
            ctx["health"] = health.for_admin(conn)
    resp = templates.TemplateResponse(
        request, name, {"p": p, "msg": request.query_params.get("msg"), "lang": lang,
                        "_": lambda text, **kw: i18n.gettext(lang, text, **kw), **ctx},
        status_code=http_status)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ----------------------------------------------------------------- dependencies

class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


@app.exception_handler(Redirect)
async def _redirect(request: Request, exc: Redirect):
    return RedirectResponse(exc.url, status_code=303)


def get_conn() -> Iterator[sqlite3.Connection]:
    with db.get() as conn:
        yield conn


def optional_principal(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Principal | None:
    return security.load_principal(conn, request.cookies.get(COOKIE))


ROTATE_OK = {"/account/password", "/logout"}
MFA_OK = {"/login/mfa", "/logout"}


def need_login(request: Request, p: Principal | None = Depends(optional_principal)) -> Principal:
    nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if p is None:
        raise Redirect("/login?next=" + quote(nxt))
    if p.mfa_pending and request.url.path not in MFA_OK:
        raise Redirect("/login/mfa?next=" + quote(nxt))
    if p.must_rotate and request.url.path not in ROTATE_OK:
        raise Redirect("/account/password")
    return p


def need_admin(p: Principal = Depends(need_login)) -> Principal:
    if p.role_admin and not p.mfa_enabled:
        raise Redirect("/account/mfa")      # admins must set up MFA before using the panel
    if not p.is_admin:
        raise HTTPException(404)
    return p


async def form_with_csrf(request: Request, p: Principal) -> dict:
    form = await request.form()
    if form.get("csrf") != p.csrf:
        raise HTTPException(403, "Bad CSRF token; reload the page and try again.")
    return form


def client_ip(request: Request) -> str:
    host = request.client.host if request.client else ""
    if host in ("127.0.0.1", "::1"):  # behind nginx, which sets X-Real-IP
        return request.headers.get("x-real-ip", host)
    return host


def safe_next(url: str | None) -> str:
    if not url or not url.startswith("/") or url.startswith("//") or "\\" in url:
        return "/"
    return url


# ----------------------------------------------------------------- middleware

# Which links may be fetched early: viewer pages only (GETs without side effects; the watch
# page skips its "viewed" mark for these, playback marks it instead). Not /lang, /account,
# login or admin pages. "moderate": on hover (desktop) or touch (phones).
SPECULATION_RULES = json.dumps({"prefetch": [{
    "source": "document", "eagerness": "moderate",
    "where": {"and": [
        {"href_matches": ["/", "/history", "/channels", "/search", "/c/*", "/v/*"]},
        {"not": {"href_matches": ["/c/*/*", "/v/*/*"]}},
        {"not": {"selector_matches": "[download], [data-no-prefetch]"}},
    ]}}]})
SPECULATION_URL = "/speculation-rules.json?v=" + hashlib.md5(SPECULATION_RULES.encode()).hexdigest()[:10]


def _is_prefetch(request: Request) -> bool:
    return "prefetch" in request.headers.get("sec-purpose", "").lower()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.method == "POST":
        origin = request.headers.get("origin")
        if origin and origin != "null":
            # Same name on another port is still this site (e.g. :8443 behind a proxy reaches the same
            # app; nginx passes Host without the port, so compare names, not name:port).
            o = urlparse(origin)
            if o.hostname not in (urlparse(settings.site_url).hostname, request.url.hostname):
                return Response("Cross-origin request refused", status_code=403)
    t0 = time.perf_counter()
    resp = await call_next(request)
    # how long the app itself took (browser DevTools > Network > Timing); the rest is network
    resp.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - t0) * 1000:.1f}"
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = ("public, max-age=31536000, immutable"
                                         if request.query_params.get("v") else "public, max-age=86400")
    elif resp.headers.get("content-type", "").startswith("text/html"):
        # Browsers that support it fetch a viewer page while the pointer rests on its link,
        # so the round trip is mostly over by the click. Rules: SPECULATION_RULES.
        resp.headers["Speculation-Rules"] = f'"{SPECULATION_URL}"'
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: https://i.ytimg.com; media-src 'self'; connect-src 'self'; "
        "script-src 'self'; style-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow, noarchive, nosnippet, noimageindex")
    return resp


# ----------------------------------------------------------------- auth

def _set_cookie(resp: Response, token: str, days: int) -> None:
    resp.set_cookie(COOKIE, token, max_age=days * 86400, httponly=True,
                    secure=settings.cookie_secure, samesite="lax", path="/")


@app.get("/login")
async def login_form(request: Request, p: Principal | None = Depends(optional_principal)):
    if p:
        raise Redirect("/login/mfa" if p.mfa_pending else "/")
    return render(request, "login.html", None, next=safe_next(request.query_params.get("next")))


@app.post("/login")
async def login(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    username = str(form.get("username", "")).strip()[:64]
    password = str(form.get("password", ""))[:128]
    nxt = safe_next(str(form.get("next", "/")))
    ip = client_ip(request)
    keys = (f"ip:{ip}", f"user:{username.lower()}")
    if security.login_throttle.blocked(*keys):
        return render(request, "login.html", None, 429, next=nxt, username=username,
                      error="Too many failed attempts. Wait 15 minutes and try again.")
    u = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if u is None:
        await run_in_threadpool(security.burn_time)
        ok = False
    else:
        ok = await run_in_threadpool(security.verify_password, u["pw_hash"], password)
    if not ok or u["disabled"]:
        security.login_throttle.fail(*keys)
        return render(request, "login.html", None, 401, next=nxt, username=username,
                      error="Wrong username or password.")
    security.login_throttle.reset(f"user:{username.lower()}")
    needs_mfa = u["role"] == "admin" and bool(u["totp_secret"])
    # With MFA the session is only a 10-minute "half login" until the code is entered.
    token = security.create_session(
        conn, user_id=u["id"], seconds=MFA_WINDOW if needs_mfa else settings.session_days * 86400,
        ip=ip, ua=request.headers.get("user-agent", ""), mfa_pending=needs_mfa)
    if not needs_mfa:
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (db.now(), u["id"]))
        security.limit_devices(conn, u["id"], security.sha256(token))
    resp = RedirectResponse(("/login/mfa?next=" + quote(nxt)) if needs_mfa else nxt, status_code=303)
    _set_cookie(resp, token, settings.session_days)
    return resp


MFA_WINDOW = 600
mfa_throttle = security.Throttle(limit=5, window=15 * 60)


@app.get("/login/mfa")
async def mfa_form(request: Request, p: Principal = Depends(need_login)):
    if not p.mfa_pending:
        raise Redirect("/")
    return render(request, "mfa.html", p, next=safe_next(request.query_params.get("next")))


@app.post("/login/mfa")
async def mfa_verify(request: Request, p: Principal = Depends(need_login),
                     conn: sqlite3.Connection = Depends(get_conn)):
    form = await form_with_csrf(request, p)
    nxt = safe_next(str(form.get("next", "/")))
    if not p.mfa_pending:
        raise Redirect(nxt)
    key = f"mfa:{p.id}"
    if mfa_throttle.blocked(key):
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (p.token_hash,))
        raise Redirect("/login?msg=" + quote("Too many wrong codes. Sign in again in 15 minutes."))
    u = conn.execute("SELECT totp_secret, totp_last_step FROM users WHERE id=?", (p.id,)).fetchone()
    step = security.totp_check(u["totp_secret"], str(form.get("code", ""))[:20], u["totp_last_step"])
    if step is None:
        mfa_throttle.fail(key)
        return render(request, "mfa.html", p, 401, next=nxt, error="Wrong or expired code.")
    mfa_throttle.reset(key)
    t = db.now()
    with db.tx(conn):
        conn.execute("UPDATE users SET totp_last_step=?, last_login_at=? WHERE id=?", (step, t, p.id))
        conn.execute("UPDATE sessions SET mfa_pending=0, expires_at=?, last_seen_at=? WHERE token_hash=?",
                     (t + settings.session_days * 86400, t, p.token_hash))
        security.limit_devices(conn, p.id, p.token_hash)
    raise Redirect(nxt)


@app.get("/account/mfa")
async def mfa_setup_form(request: Request, p: Principal = Depends(need_login),
                         conn: sqlite3.Connection = Depends(get_conn)):
    if not p.role_admin:
        raise HTTPException(404)
    if p.mfa_enabled:
        return render(request, "mfa_setup.html", p, enabled=True)
    secret = security.new_totp_secret()
    conn.execute("UPDATE sessions SET pending_totp_secret=? WHERE token_hash=?", (secret, p.token_hash))
    uri = security.totp_uri(secret, p.name)
    return render(request, "mfa_setup.html", p, enabled=False, secret=secret,
                  qr_svg=security.totp_qr_svg(uri))


@app.post("/account/mfa")
async def mfa_setup(request: Request, p: Principal = Depends(need_login),
                    conn: sqlite3.Connection = Depends(get_conn)):
    if not p.role_admin or p.mfa_enabled:
        raise HTTPException(404)
    form = await form_with_csrf(request, p)
    s = conn.execute("SELECT pending_totp_secret FROM sessions WHERE token_hash=?",
                     (p.token_hash,)).fetchone()
    secret = s and s["pending_totp_secret"]
    step = security.totp_check(secret, str(form.get("code", ""))[:20], 0) if secret else None
    if step is None:
        raise Redirect("/account/mfa?msg=" + quote(
            "Code did not match. A new QR code was generated: remove the old entry from your app and scan again."))
    with db.tx(conn):
        conn.execute("UPDATE users SET totp_secret=?, totp_last_step=? WHERE id=?", (secret, step, p.id))
        conn.execute("UPDATE sessions SET pending_totp_secret=NULL WHERE token_hash=?", (p.token_hash,))
        # Other sessions of this admin were opened without MFA: sign them out.
        conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (p.id, p.token_hash))
    raise Redirect("/admin?msg=" + quote("Two-factor sign-in is on. You'll be asked for a code at each sign-in."))


@app.get("/lang/{lang}")
async def set_lang(request: Request, lang: str):
    if lang not in i18n.LANGS:
        raise HTTPException(404)
    resp = RedirectResponse(safe_next(request.query_params.get("next")), status_code=303)
    resp.set_cookie(i18n.COOKIE, lang, max_age=365 * 86400, secure=settings.cookie_secure,
                    samesite="lax", path="/")
    return resp


@app.get("/code")
async def code_form(request: Request):
    return render(request, "code.html", None, code=request.query_params.get("c", "")[:20])


@app.post("/code")
async def redeem_code(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    ip = client_ip(request)
    if security.login_throttle.blocked(f"ip:{ip}"):
        return render(request, "code.html", None, 429,
                      error="Too many failed attempts. Wait 15 minutes and try again.")
    code_hash = security.sha256(security.normalize_code(str(form.get("code", ""))[:64]))
    t = db.now()
    with db.tx(conn):
        c = conn.execute("SELECT * FROM access_codes WHERE code_hash=?", (code_hash,)).fetchone()
        error = None
        if c is None or c["disabled"]:
            error = "This code is not valid."
        elif c["expires_at"] and c["expires_at"] < t:
            error = "This code has expired."
        elif c["used_devices"] >= c["max_devices"]:
            error = "This code has already been used on the maximum number of devices."
        if error:
            security.login_throttle.fail(f"ip:{ip}")
        else:
            conn.execute("UPDATE access_codes SET used_devices=used_devices+1 WHERE id=?", (c["id"],))
            days = c["session_days"]
            if c["expires_at"]:
                days = max(1, min(days, (c["expires_at"] - t) // 86400 + 1))
            token = security.create_session(conn, code_id=c["id"], seconds=days * 86400, ip=ip,
                                            ua=request.headers.get("user-agent", ""))
    if error:
        return render(request, "code.html", None, 403, error=error)
    scopes = conn.execute("SELECT channel_id, video_id FROM code_scopes WHERE code_id=?",
                          (c["id"],)).fetchall()
    only_video = len(scopes) == 1 and scopes[0]["video_id"]   # a shared single video
    resp = RedirectResponse(f"/v/{only_video}" if only_video else "/", status_code=303)
    _set_cookie(resp, token, days)
    return resp


@app.post("/logout")
async def logout(request: Request, p: Principal = Depends(need_login),
                 conn: sqlite3.Connection = Depends(get_conn)):
    await form_with_csrf(request, p)
    conn.execute("DELETE FROM sessions WHERE token_hash=?", (p.token_hash,))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/account/password")
async def password_form(request: Request, p: Principal = Depends(need_login),
                        conn: sqlite3.Connection = Depends(get_conn)):
    if p.kind != "user":
        raise HTTPException(404)
    new_pw = security.generate_password()
    pending = await run_in_threadpool(security.hash_password, new_pw)
    conn.execute("UPDATE sessions SET pending_pw_hash=? WHERE token_hash=?", (pending, p.token_hash))
    return render(request, "password.html", p, new_password=new_pw,
                  rotate_months=max(1, round(settings.password_max_age_days / 30.4)))


@app.post("/account/password")
async def password_confirm(request: Request, p: Principal = Depends(need_login),
                           conn: sqlite3.Connection = Depends(get_conn)):
    if p.kind != "user":
        raise HTTPException(404)
    form = await form_with_csrf(request, p)
    s = conn.execute("SELECT pending_pw_hash FROM sessions WHERE token_hash=?",
                     (p.token_hash,)).fetchone()
    typed = str(form.get("confirm", ""))[:128]
    ok = bool(s and s["pending_pw_hash"]) and await run_in_threadpool(
        security.verify_password, s["pending_pw_hash"], typed)
    if not ok:
        raise Redirect("/account/password?msg=" + quote(
            "That did not match. A NEW password has been generated below; write it down carefully."))
    with db.tx(conn):
        conn.execute("UPDATE users SET pw_hash=?, pw_changed_at=? WHERE id=?",
                     (s["pending_pw_hash"], db.now(), p.id))
        conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (p.id, p.token_hash))
        conn.execute("UPDATE sessions SET pending_pw_hash=NULL WHERE token_hash=?", (p.token_hash,))
    raise Redirect("/?msg=" + quote("Password changed. Other devices have been signed out."))


# ----------------------------------------------------------------- viewing

def _cards(conn: sqlite3.Connection, p: Principal, where: str = "1=1", params: list | None = None,
           page: int = 0, order: str = "COALESCE(v.published_at, v.ready_at) DESC, v.id DESC") -> list[dict]:
    scope, sp = p.scope_sql("v")
    rows = conn.execute(
        f"""SELECT v.*, c.title AS ch_title, c.avatar AS ch_avatar, w.position AS watched
            FROM videos v LEFT JOIN channels c ON c.id=v.channel_id
            LEFT JOIN watch w ON w.video_id=v.id AND w.viewer=?
            WHERE v.status='ready' AND {scope} AND {where}
            ORDER BY {order} LIMIT ? OFFSET ?""",
        [p.viewer_key, *sp, *(params or []), PAGE, page * PAGE]).fetchall()
    return [_card(r, p) for r in rows]


def _thumb_url(v, p: Principal) -> str | None:
    if not (v["thumb"] and v["path"]):
        return None
    return security.sign_media(f"{v['path'].rsplit('/', 1)[0]}/{v['thumb']}", p.viewer_key, ttl=86400)


def _avatar_url(path: str | None, p: Principal) -> dict:
    return {"avatar_url": security.sign_media(path, p.viewer_key, ttl=86400) if path else None}


def _card(r, p: Principal) -> dict:
    d = dict(r)
    d["thumb_url"] = _thumb_url(r, p)
    d |= _avatar_url(d.get("ch_avatar"), p)
    if d.get("watched") and d.get("duration"):
        d["watched_pct"] = min(100, int(d["watched"] * 100 / d["duration"]))
    return d


def _page(request: Request) -> int:
    try:
        return max(0, min(1000, int(request.query_params.get("page", 0))))
    except ValueError:
        return 0


@app.get("/")
async def feed(request: Request, p: Principal = Depends(need_login),
               conn: sqlite3.Connection = Depends(get_conn)):
    page = _page(request)
    cards = _cards(conn, p, page=page)
    # Started but not finished (past the first 30 s, before the last 8%), most recent first.
    resume = _cards(conn, p, "w.position > 30 AND (v.duration IS NULL OR w.position < v.duration * 0.92)",
                    order="w.updated_at DESC")[:RESUME_ROW] if page == 0 else []
    tpl = "_cards.html" if request.query_params.get("partial") else "feed.html"
    return render(request, tpl, p, cards=cards, page=page, more=len(cards) == PAGE, resume=resume,
                  more_url=f"/?page={page + 1}&partial=1")


RESUME_ROW = 8


@app.get("/history")
async def history(request: Request, p: Principal = Depends(need_login),
                  conn: sqlite3.Connection = Depends(get_conn)):
    page = _page(request)
    cards = _cards(conn, p, "w.video_id IS NOT NULL", page=page, order="w.updated_at DESC")
    tpl = "_cards.html" if request.query_params.get("partial") else "history.html"
    return render(request, tpl, p, cards=cards, page=page, more=len(cards) == PAGE,
                  more_url=f"/history?page={page + 1}&partial=1")


@app.post("/history/clear")
async def history_clear(request: Request, p: Principal = Depends(need_login),
                        conn: sqlite3.Connection = Depends(get_conn)):
    await form_with_csrf(request, p)
    conn.execute("DELETE FROM watch WHERE viewer=?", (p.viewer_key,))
    return RedirectResponse("/history", status_code=303)


@app.get("/search")
async def search(request: Request, p: Principal = Depends(need_login),
                 conn: sqlite3.Connection = Depends(get_conn)):
    q = request.query_params.get("q", "").strip()[:100]
    page = _page(request)
    cards = []
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        cards = _cards(conn, p, "(v.title LIKE ? ESCAPE '\\' OR v.alt_title LIKE ? ESCAPE '\\' "
                       "OR v.description LIKE ? ESCAPE '\\')", [like, like, like], page)
    tpl = "_cards.html" if request.query_params.get("partial") else "feed.html"
    return render(request, tpl, p, cards=cards, q=q, page=page, more=len(cards) == PAGE,
                  more_url=f"/search?q={quote(q)}&page={page + 1}&partial=1")


@app.get("/channels")
async def channels(request: Request, p: Principal = Depends(need_login),
                   conn: sqlite3.Connection = Depends(get_conn)):
    scope, sp = p.scope_sql("v")
    rows = conn.execute(
        f"""SELECT c.*, COUNT(v.id) AS n, MAX(COALESCE(v.published_at, v.ready_at)) AS latest
            FROM channels c JOIN videos v ON v.channel_id=c.id
            WHERE v.status='ready' AND {scope}
            GROUP BY c.id ORDER BY latest DESC""", sp).fetchall()
    chans = [dict(r) | _avatar_url(r["avatar"], p) for r in rows]
    return render(request, "channels.html", p, channels=chans)


@app.get("/c/{channel_id}")
async def channel(request: Request, channel_id: int, p: Principal = Depends(need_login),
                  conn: sqlite3.Connection = Depends(get_conn)):
    ch = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    if ch is None:
        raise HTTPException(404)
    page = _page(request)
    cards = _cards(conn, p, "v.channel_id=?", [channel_id], page)
    if not cards and page == 0 and not p.can_see_channel(channel_id):
        raise HTTPException(404)
    tpl = "_cards.html" if request.query_params.get("partial") else "channel.html"
    return render(request, tpl, p, ch=dict(ch) | _avatar_url(ch["avatar"], p),
                  cards=cards, page=page, more=len(cards) == PAGE,
                  more_url=f"/c/{channel_id}?page={page + 1}&partial=1")


@app.get("/v/{video_id}")
async def watch(request: Request, video_id: int, p: Principal = Depends(need_login),
                conn: sqlite3.Connection = Depends(get_conn)):
    scope, sp = p.scope_sql("v")
    v = conn.execute(
        f"""SELECT v.*, c.title AS ch_title, c.avatar AS ch_avatar, c.handle AS ch_handle
            FROM videos v LEFT JOIN channels c ON c.id=v.channel_id
            WHERE v.id=? AND v.status='ready' AND {scope}""", [video_id, *sp]).fetchone()
    if v is None:
        raise HTTPException(404)
    if not _is_prefetch(request):      # fetched early on hover: playback marks it instead
        conn.execute("UPDATE videos SET last_viewed_at=? WHERE id=?", (db.now(), video_id))
    pos = conn.execute("SELECT position FROM watch WHERE viewer=? AND video_id=?",
                       (p.viewer_key, video_id)).fetchone()
    comments = conn.execute(
        "SELECT * FROM comments WHERE video_id=? ORDER BY is_pinned DESC, likes DESC LIMIT 2000",
        (video_id,)).fetchall()
    top = [dict(c) | {"replies": []} for c in comments if c["parent"] == "root"]
    by_id = {c["yt_id"]: c for c in top}
    for c in comments:
        if c["parent"] != "root" and c["parent"] in by_id:
            by_id[c["parent"]]["replies"].append(dict(c))
    for c in top:
        c["replies"].sort(key=lambda r: r["published_at"] or 0)
    up_next = _cards(conn, p, "v.channel_id=? AND v.id<>?", [v["channel_id"], video_id]) \
        if v["channel_id"] else []
    fresh = (v["published_at"] or v["ready_at"] or 0) > db.now() - COMMENT_REFRESH_DAYS * 86400
    tracks = [t | {"src": f"/v/{video_id}/subs/{t['lang']}.vtt"} for t in subs.loads(v["subs"])]
    return render(request, "watch.html", p, v=_card(v, p), tracks=tracks, sub_label=subs.label,
                  src=security.sign_media(v["path"], p.viewer_key),
                  position=pos["position"] if pos else 0, comments=top[:200], up_next=up_next[:12],
                  comments_fresh=fresh, share_days=SHARE_DAYS)


@app.get("/v/{video_id}/subs/{lang}.vtt")
async def subtitle(video_id: int, lang: str, p: Principal = Depends(need_login),
                   conn: sqlite3.Connection = Depends(get_conn)):
    scope, sp = p.scope_sql("v")
    v = conn.execute(f"SELECT v.path, v.subs FROM videos v WHERE v.id=? AND v.status='ready' AND {scope}",
                     [video_id, *sp]).fetchone()
    track = next((t for t in subs.loads(v["subs"] if v else None) if t["lang"] == lang), None)
    if not v or not track or not v["path"]:
        raise HTTPException(404)
    f = settings.media_dir / v["path"].rsplit("/", 1)[0] / track["file"]
    if not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, media_type="text/vtt; charset=utf-8",
                        headers={"Cache-Control": "private, max-age=86400"})


COMMENT_REFRESH_DAYS = 30     # must match worker.COMMENT_REFRESH_DAYS
SHARE_DAYS = 3                # what a viewer's share code is good for (admins can choose)
SHARES_PER_DAY = 10


@app.post("/v/{video_id}/share")
async def share(request: Request, video_id: int, p: Principal = Depends(need_login),
                conn: sqlite3.Connection = Depends(get_conn)):
    """Make an access code for just this video. Viewers get a fixed 1 device / 3 days;
    admins choose. Code holders can't share further."""
    if p.kind != "user":
        raise HTTPException(404)
    form = await form_with_csrf(request, p)
    v = conn.execute("SELECT id, title FROM videos WHERE id=? AND status='ready'", (video_id,)).fetchone()
    if v is None:
        raise HTTPException(404)
    devices, days = 1, SHARE_DAYS
    if p.is_admin:
        try:
            devices = max(1, min(100, int(form.get("max_devices", 1))))
            days = max(1, min(365, int(form.get("days", SHARE_DAYS))))
        except ValueError:
            pass
    elif conn.execute("SELECT COUNT(*) FROM access_codes WHERE created_by=? AND created_at>?",
                      (p.id, db.now() - 86400)).fetchone()[0] >= SHARES_PER_DAY:
        raise Redirect(f"/v/{video_id}?msg=" + quote(
            "You have created many share codes today. Try again tomorrow."))
    code = security.generate_code()
    t = db.now()
    with db.tx(conn):
        cid = conn.execute(
            "INSERT INTO access_codes(code_hash,label,max_devices,session_days,expires_at,created_at,"
            "created_by) VALUES (?,?,?,?,?,?,?)",
            (security.sha256(security.normalize_code(code)),
             f"Shared by {p.name}: {(v['title'] or '')[:60]}", devices, days, t + days * 86400, t,
             p.id)).lastrowid
        conn.execute("INSERT INTO code_scopes(code_id,video_id) VALUES (?,?)", (cid, video_id))
    return render(request, "share.html", p, v=v, code=code, devices=devices,
                  expires=t + days * 86400,
                  link=f"{settings.site_url}/code?c={code}", code_url=f"{settings.site_url}/code")


@app.post("/api/progress")
async def progress(request: Request, p: Principal = Depends(need_login),
                   conn: sqlite3.Connection = Depends(get_conn)):
    if request.headers.get("x-csrf") != p.csrf:
        raise HTTPException(403)
    data = await request.json()
    try:
        vid, pos = int(data["video_id"]), max(0.0, float(data["position"]))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400)
    scope, sp = p.scope_sql("v")
    if conn.execute(f"SELECT 1 FROM videos v WHERE v.id=? AND {scope}", [vid, *sp]).fetchone():
        conn.execute("INSERT INTO watch(viewer,video_id,position,updated_at) VALUES (?,?,?,?) "
                     "ON CONFLICT(viewer,video_id) DO UPDATE SET position=excluded.position, "
                     "updated_at=excluded.updated_at", (p.viewer_key, vid, pos, db.now()))
        conn.execute("UPDATE videos SET last_viewed_at=? WHERE id=? AND COALESCE(last_viewed_at,0) < ?",
                     (db.now(), vid, db.now() - 600))
    return JSONResponse({"ok": True})


@app.get("/speculation-rules.json")
async def speculation_rules():
    return Response(SPECULATION_RULES, media_type="application/speculationrules+json",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/robots.txt")
async def robots():
    return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ----------------------------------------------------------------- dev media

if settings.serve_media:
    @app.get("/m/{path:path}")
    async def dev_media(request: Request, path: str):
        q = request.query_params
        uri = "/m/" + path
        if not security.check_media_signature(uri, q.get("u", ""), q.get("e", ""), q.get("s", "")):
            raise HTTPException(403)
        f = (settings.media_dir / path).resolve()
        if settings.media_dir not in f.parents or not f.is_file():
            raise HTTPException(404)
        return FileResponse(f, content_disposition_type="inline")


from . import admin  # noqa: E402  (registers /admin routes)

app.include_router(admin.router)
