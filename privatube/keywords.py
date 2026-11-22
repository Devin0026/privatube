"""Per-channel title filters that decide which new uploads are auto-downloaded."""
from __future__ import annotations

import re

_SPLIT = re.compile(r"[,\n，、;；]+")


def parse(text: str | None) -> list[str]:
    """'news, 直播\\nQ&A' -> ['news', '直播', 'q&a'] (case-insensitive substrings)."""
    return [k.strip().casefold() for k in _SPLIT.split(text or "") if k.strip()]


def titles(title: str | None, alt_title: str | None = None) -> str:
    """Both language versions of a title as one text, so a keyword matches either."""
    return f"{title or ''}\n{alt_title or ''}"


def matches(title: str | None, include: str | None, exclude: str | None) -> bool:
    """True if the title has any include keyword (or none are set) and no exclude keyword.
    Pass titles(title, alt_title) to check the Chinese and English titles together."""
    t = (title or "").casefold()
    if any(k in t for k in parse(exclude)):
        return False
    inc = parse(include)
    return not inc or any(k in t for k in inc)
