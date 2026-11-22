"""Runtime settings, read once from environment variables (see deploy/env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    media_dir: Path
    db_path: Path
    media_secret: str               # shared with nginx secure_link
    site_url: str
    cookie_secure: bool
    serve_media: bool               # dev only: let the app serve /m/ itself
    media_ttl: int                  # seconds a signed media URL stays valid
    session_days: int
    max_devices: int                # signed-in devices per account; oldest is signed out
    password_max_age_days: int
    password_length: int
    ytdlp_cookies: Path             # cookies.txt of a spare Google account; used if it exists
    ytdlp_format: str
    title_lang: str                 # also list channels in this language (titles), "" = off
    sub_langs: list                 # subtitle languages to keep, e.g. ["en", "de"]
    default_height: int             # 720: max video height unless the channel/video says otherwise
    ytdlp_ipv4: bool                # YouTube often blocks datacenter IPv6 with "not a bot"
    max_comments: int
    poll_minutes_default: int
    min_free_bytes: int             # downloads pause when the disk has less than this free
    backup_hours: int               # database backup every this many hours (into data/backups)
    backup_keep: int                # how many backups to keep
    alert_url: str                  # push URL for problems (POST title/desp/body); "" = off

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


def load() -> Settings:
    data_dir = Path(os.environ.get("PT_DATA_DIR", "./data")).resolve()
    media_dir = Path(os.environ.get("PT_MEDIA_DIR", data_dir / "media")).resolve()
    secret = os.environ.get("PT_MEDIA_SECRET", "")
    serve_media = _bool("PT_SERVE_MEDIA", False)
    if not secret:
        if not serve_media:
            raise RuntimeError("PT_MEDIA_SECRET must be set (see deploy/env.example)")
        secret = "dev-only-secret"
    return Settings(
        data_dir=data_dir,
        media_dir=media_dir,
        db_path=Path(os.environ.get("PT_DB", data_dir / "privatube.db")).resolve(),
        media_secret=secret,
        site_url=os.environ.get("PT_SITE_URL", "http://127.0.0.1:8000").rstrip("/"),
        cookie_secure=_bool("PT_COOKIE_SECURE", True),
        serve_media=serve_media,
        media_ttl=_int("PT_MEDIA_TTL", 6 * 3600),
        session_days=_int("PT_SESSION_DAYS", 30),
        max_devices=max(1, _int("PT_MAX_DEVICES", 2)),
        password_max_age_days=_int("PT_PASSWORD_MAX_AGE_DAYS", 182),
        password_length=_int("PT_PASSWORD_LENGTH", 12),
        ytdlp_cookies=Path(os.environ.get("PT_YTDLP_COOKIES") or data_dir / "cookies.txt"),
        ytdlp_format=os.environ.get(
            "PT_YTDLP_FORMAT",
            # Prefer H.264 (plays everywhere incl. older iPhones); {h} = max height (720, 480...).
            "bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/"
            "bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}]/b",
        ),
        ytdlp_ipv4=_bool("PT_YTDLP_IPV4", True),
        title_lang=os.environ.get("PT_TITLE_LANG", ""),
        default_height=_int("PT_DEFAULT_HEIGHT", 720),
        sub_langs=[x.strip().split("-")[0].lower() for x in os.environ.get("PT_SUB_LANGS", "en").split(",") if x.strip()],
        max_comments=_int("PT_MAX_COMMENTS", 300),
        poll_minutes_default=_int("PT_POLL_MINUTES", 60),
        min_free_bytes=int(_float("PT_MIN_FREE_GB", 2.0) * 1024**3),
        backup_hours=_int("PT_BACKUP_HOURS", 24),
        backup_keep=max(1, _int("PT_BACKUP_KEEP", 14)),
        alert_url=os.environ.get("PT_ALERT_URL", ""),
    )


settings = load()
