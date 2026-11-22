"""Subtitles: pick at most one track per language in PT_SUB_LANGS (default "en"), fetch
them as YouTube's json3 and convert to clean WebVTT.

Why json3: YouTube's automatic captions in VTT repeat every line as it "rolls"; json3 has
one event per line, which converts into readable cues. Files live next to the video and
are served by the app itself.
"""
from __future__ import annotations

import json
import re

from .config import settings

# Preferred human-made track per language family, and the auto-translation key to fall back to.
PREFER = {"zh": ("zh-Hans", "zh-CN", "zh-SG", "zh", "zh-Hant", "zh-TW", "zh-HK"),
          "en": ("en", "en-US", "en-GB", "en-CA", "en-AU"),
          "pt": ("pt-BR", "pt", "pt-PT"), "es": ("es", "es-419", "es-ES")}
AUTO_KEY = {"zh": "zh-Hans"}
SAFE_LANG = re.compile(r"[A-Za-z0-9-]{1,20}")


def pick(manual: dict | None, auto: dict | None, langs: list[str] | None = None) -> list[tuple[str, bool]]:
    """[(key, is_auto)] for each language in `langs` (PT_SUB_LANGS): a human-made track if there is one, else
    YouTube's speech recognition in the video's own language (key "<lang>-orig"), else a
    machine translation of it. YouTube rate-limits translations hard (HTTP 429), so the
    worker retries those on later days."""
    manual = {k: v for k, v in (manual or {}).items() if k != "live_chat"}
    auto = auto or {}
    out = []
    for family in langs if langs is not None else settings.sub_langs:
        prefer, fallback = PREFER.get(family, (family,)), AUTO_KEY.get(family, family)
        m = next((k for k in prefer if k in manual), None) or \
            next((k for k in manual if k.split("-")[0] == family), None)
        orig = next((k for k in auto if k.endswith("-orig") and k.split("-")[0] == family), None)
        if m:
            out.append((m, False))
        elif orig:
            out.append((orig, True))
        elif fallback in auto:
            out.append((fallback, True))
    return [(k, a) for k, a in out if SAFE_LANG.fullmatch(k)]


def is_translation(key: str, auto: bool) -> bool:
    return auto and not key.endswith("-orig")


def _ts(ms: int) -> str:
    ms = max(0, int(ms))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02}.{ms:03}"


def json3_to_vtt(data: dict) -> str:
    cues = []
    for e in data.get("events") or []:
        text = "".join(s.get("utf8", "") for s in e.get("segs") or []).strip()
        if not text:
            continue
        start = int(e.get("tStartMs", 0))
        cues.append([start, start + int(e.get("dDurationMs", 0) or 2000), text])
    for a, b in zip(cues, cues[1:]):   # rolling captions overlap: end each where the next begins
        if b[0] < a[1]:
            a[1] = max(a[0] + 300, b[0])
    out = ["WEBVTT", ""]
    for start, end, text in cues:
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        out += [f"{_ts(start)} --> {_ts(end)}", text, ""]
    return "\n".join(out)


def label(lang: str, auto: bool, _) -> str:
    name = _("Chinese") if lang.startswith("zh") else (_("English") if lang.startswith("en") else lang)
    if lang in ("zh-Hant", "zh-TW", "zh-HK"):
        name = _("Chinese (Traditional)")
    return f"{name} ({_('auto')})" if auto else name


def loads(raw: str | None) -> list[dict]:
    try:
        return json.loads(raw) if raw else []
    except ValueError:
        return []
