"""Recognise YouTube URLs without importing yt-dlp (keeps the web process small)."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}


def parse(url: str) -> tuple[str, str] | None:
    """Return ("video", id) or ("channel", canonical_url), or None if unrecognised."""
    url = url.strip()
    if VIDEO_ID.match(url):
        return "video", url
    if url.startswith("@"):
        url = "https://www.youtube.com/" + url
    if "://" not in url:
        url = "https://" + url
    u = urlparse(url)
    host = (u.hostname or "").lower()
    parts = [p for p in u.path.split("/") if p]
    if host == "youtu.be" and parts and VIDEO_ID.match(parts[0]):
        return "video", parts[0]
    if host not in YT_HOSTS or not parts:
        return None
    if parts[0] == "watch":
        v = parse_qs(u.query).get("v", [""])[0]
        return ("video", v) if VIDEO_ID.match(v) else None
    if parts[0] in ("shorts", "live", "embed", "v") and len(parts) > 1 and VIDEO_ID.match(parts[1]):
        return "video", parts[1]
    if parts[0].startswith("@"):
        return "channel", f"https://www.youtube.com/{parts[0]}"
    if parts[0] == "channel" and len(parts) > 1 and CHANNEL_ID.match(parts[1]):
        return "channel", f"https://www.youtube.com/channel/{parts[1]}"
    if parts[0] in ("c", "user") and len(parts) > 1:
        return "channel", f"https://www.youtube.com/{parts[0]}/{parts[1]}"
    return None


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"
