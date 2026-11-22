"""Passwords, access codes, sessions, media URL signing and login throttling."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from . import db
from .config import settings

# No 0/O/1/l/I: passwords and codes are meant to be written down by hand.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

_ph = PasswordHasher()  # argon2id, library defaults (64 MiB, t=3)


def _grouped(alphabet: str, length: int) -> str:
    raw = "".join(secrets.choice(alphabet) for _ in range(length))
    return "-".join(raw[i:i + 4] for i in range(0, length, 4))


def generate_password() -> str:
    """12 chars from 55 symbols ~ 69 bits, shown as e.g. 'Xk7p-Qm3r-Za9w'."""
    return _grouped(ALPHABET, settings.password_length)


def generate_code() -> str:
    """12 chars from 32 symbols = 60 bits, case-insensitive, e.g. 'K7PQ-M3RZ-A9WD'."""
    return _grouped(CODE_ALPHABET, 12)


def normalize_secret(s: str) -> str:
    return "".join(ch for ch in s.strip() if ch not in " -")


def normalize_code(s: str) -> str:
    return normalize_secret(s).upper()


def hash_password(pw: str) -> str:
    return _ph.hash(normalize_secret(pw))


def verify_password(pw_hash: str, pw: str) -> bool:
    try:
        return _ph.verify(pw_hash, normalize_secret(pw))
    except (VerificationError, InvalidHashError):
        return False


_DUMMY_HASH = _ph.hash("timing-equalizer")


def burn_time() -> None:
    """Spend the same time as a real verify, so unknown usernames are not revealed."""
    verify_password(_DUMMY_HASH, "x")


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ---------------------------------------------------------------- principals

@dataclass
class Principal:
    kind: str                     # "user" | "code"
    id: int
    name: str
    is_admin: bool
    csrf: str
    token_hash: str
    must_rotate: bool = False
    role_admin: bool = False          # has the admin role (is_admin also needs MFA)
    mfa_enabled: bool = False
    mfa_pending: bool = False         # password OK, waiting for the TOTP code
    channel_ids: tuple[int, ...] = ()   # code scope
    video_ids: tuple[int, ...] = ()     # code scope

    @property
    def viewer_key(self) -> str:
        return ("u" if self.kind == "user" else "c") + str(self.id)

    def scope_sql(self, alias: str = "v") -> tuple[str, list]:
        """SQL fragment restricting videos to what this principal may see."""
        if self.kind == "user":
            return "1=1", []
        parts, params = [], []
        if self.channel_ids:
            parts.append(f"{alias}.channel_id IN ({','.join('?' * len(self.channel_ids))})")
            params += list(self.channel_ids)
        if self.video_ids:
            parts.append(f"{alias}.id IN ({','.join('?' * len(self.video_ids))})")
            params += list(self.video_ids)
        return ("(" + " OR ".join(parts) + ")") if parts else "0=1", params

    def can_see_channel(self, channel_id: int) -> bool:
        return self.kind == "user" or channel_id in self.channel_ids


def create_session(conn: sqlite3.Connection, *, user_id: int | None = None,
                   code_id: int | None = None, seconds: int, ip: str, ua: str,
                   mfa_pending: bool = False) -> str:
    token = secrets.token_urlsafe(32)
    t = db.now()
    conn.execute(
        "INSERT INTO sessions(token_hash,user_id,code_id,csrf,created_at,expires_at,"
        "last_seen_at,ip,user_agent,mfa_pending) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sha256(token), user_id, code_id, secrets.token_urlsafe(24), t, t + seconds,
         t, ip, ua[:200], int(mfa_pending)),
    )
    return token


def limit_devices(conn: sqlite3.Connection, user_id: int, keep: str) -> int:
    """Keep the session `keep` plus the most recently used others, up to
    settings.max_devices in total; sign out the rest. Half-done MFA logins don't count."""
    return conn.execute(
        """DELETE FROM sessions WHERE user_id=? AND mfa_pending=0 AND token_hash<>? AND token_hash NOT IN (
               SELECT token_hash FROM sessions WHERE user_id=? AND mfa_pending=0 AND token_hash<>?
               ORDER BY last_seen_at DESC LIMIT ?)""",
        (user_id, keep, user_id, keep, settings.max_devices - 1)).rowcount


def load_principal(conn: sqlite3.Connection, token: str | None) -> Principal | None:
    if not token:
        return None
    th = sha256(token)
    s = conn.execute("SELECT * FROM sessions WHERE token_hash=?", (th,)).fetchone()
    t = db.now()
    if s is None or s["expires_at"] < t:
        return None
    if t - s["last_seen_at"] > 300:
        conn.execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (t, th))
    if s["user_id"] is not None:
        u = conn.execute("SELECT * FROM users WHERE id=?", (s["user_id"],)).fetchone()
        if u is None or u["disabled"]:
            return None
        max_age = settings.password_max_age_days * 86400
        role_admin = u["role"] == "admin"
        mfa_enabled = bool(u["totp_secret"])
        mfa_pending = bool(s["mfa_pending"])
        return Principal("user", u["id"], u["username"],
                         role_admin and mfa_enabled and not mfa_pending, s["csrf"], th,
                         must_rotate=t - u["pw_changed_at"] > max_age, role_admin=role_admin,
                         mfa_enabled=mfa_enabled, mfa_pending=mfa_pending)
    c = conn.execute("SELECT * FROM access_codes WHERE id=?", (s["code_id"],)).fetchone()
    if c is None or c["disabled"] or (c["expires_at"] and c["expires_at"] < t):
        return None
    scopes = conn.execute("SELECT channel_id, video_id FROM code_scopes WHERE code_id=?",
                          (c["id"],)).fetchall()
    return Principal(
        "code", c["id"], c["label"] or f"code #{c['id']}", False, s["csrf"], th,
        channel_ids=tuple(r["channel_id"] for r in scopes if r["channel_id"] is not None),
        video_ids=tuple(r["video_id"] for r in scopes if r["video_id"] is not None),
    )


# ---------------------------------------------------------------- TOTP (admin MFA)

TOTP_ISSUER = "PrivaTube"


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=TOTP_ISSUER)


def totp_qr_svg(uri: str) -> str:
    import io

    import qrcode
    import qrcode.image.svg

    buf = io.BytesIO()
    qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2).save(buf)
    svg = buf.getvalue().decode()
    return svg[svg.index("<svg"):]   # drop the XML declaration so it can be inlined


def totp_check(secret: str, code: str, last_step: int) -> int | None:
    """Return the matched 30 s time step, or None. Accepts one step of clock drift and
    refuses steps at or before `last_step`, so a code can't be used twice."""
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != 6:
        return None
    totp = pyotp.TOTP(secret)
    now_step = int(time.time()) // 30
    for step in (now_step - 1, now_step, now_step + 1):
        if step > last_step and hmac.compare_digest(totp.at(step * 30), code):
            return step
    return None


# ---------------------------------------------------------------- media URLs

def sign_media(rel_path: str, viewer: str, ttl: int | None = None) -> str:
    """Signed, expiring link to a media file, in the format nginx `secure_link` checks:

        md5("$secure_link_expires$uri$arg_u <secret>"), base64url without padding

    `viewer` is part of the signature (a link copied from one account names that account).
    Expiry is rounded up to the hour so the browser cache keeps working.
    """
    ttl = ttl or settings.media_ttl
    expires = (int(time.time()) + ttl) // 3600 * 3600 + 3600
    uri = "/m/" + rel_path.lstrip("/")
    digest = hashlib.md5(f"{expires}{uri}{viewer} {settings.media_secret}".encode()).digest()
    sig = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return f"{uri}?u={viewer}&e={expires}&s={sig}"


def check_media_signature(uri: str, viewer: str, expires: str, sig: str) -> bool:
    """Same check as nginx; used only by the dev media route and tests."""
    if not expires.isdigit() or int(expires) < time.time():
        return False
    digest = hashlib.md5(f"{expires}{uri}{viewer} {settings.media_secret}".encode()).digest()
    want = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return hmac.compare_digest(want, sig)


# ---------------------------------------------------------------- throttling

class Throttle:
    """In-memory failure counter (the web app runs as one process)."""

    def __init__(self, limit: int, window: int):
        self.limit, self.window = limit, window
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, *keys: str) -> bool:
        cutoff = time.time() - self.window
        with self._lock:
            for k in keys:
                hits = [h for h in self._hits.get(k, []) if h > cutoff]
                self._hits[k] = hits
                if len(hits) >= self.limit:
                    return True
        return False

    def fail(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._hits.setdefault(k, []).append(time.time())

    def reset(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._hits.pop(k, None)


login_throttle = Throttle(limit=8, window=15 * 60)
