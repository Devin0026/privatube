"""Turn a pasted/uploaded YouTube cookie export into the Netscape cookies.txt yt-dlp reads.

Accepted:
  * Netscape cookies.txt ("# Netscape HTTP Cookie File" ...), kept as is
  * JSON from Cookie-Editor / EditThisCookie: a list of {domain, name, value, path,
    secure, expirationDate}, or {"cookies": [...]}
  * a Cookie header string, "SID=...; HSID=...; ..." (optionally starting with "Cookie:"),
    as copied from the browser's developer tools on youtube.com
"""
from __future__ import annotations

import json
import time

MAX_LEN = 200_000
HEADER = "# Netscape HTTP Cookie File\n"


def _line(domain: str, name: str, value: str, path: str = "/", secure: bool = True,
          expires: int | None = None) -> str:
    domain = domain.strip() or ".youtube.com"
    sub = "TRUE" if domain.startswith(".") else "FALSE"
    exp = int(expires) if expires else int(time.time()) + 365 * 86400
    for part in (domain, name, value, path):
        if any(c in part for c in "\t\r\n"):
            raise ValueError("bad character in cookie")
    return "\t".join([domain, sub, path or "/", "TRUE" if secure else "FALSE", str(exp), name, value])


def to_netscape(text: str) -> tuple[str, str]:
    """Returns (cookies.txt contents, detected format). Raises ValueError if unusable."""
    text = text.strip().lstrip("﻿")
    if not text or len(text) > MAX_LEN:
        raise ValueError("empty or too large")
    if text.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
        out, fmt = text + "\n", "Netscape"
    elif text[0] in "[{":
        try:
            data = json.loads(text)
        except ValueError as e:
            raise ValueError("invalid JSON") from e
        items = data.get("cookies", []) if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ValueError("JSON is not a list of cookies")
        lines = [_line(str(c.get("domain", "")), str(c["name"]), str(c.get("value", "")),
                       str(c.get("path", "/")), bool(c.get("secure", True)),
                       c.get("expirationDate") or c.get("expires") or None)
                 for c in items if isinstance(c, dict) and c.get("name")]
        out, fmt = HEADER + "\n".join(lines) + "\n", "JSON"
    else:
        h = text[7:] if text.lower().startswith("cookie:") else text
        lines = []
        for pair in h.replace("\n", ";").split(";"):
            name, eq, value = pair.strip().partition("=")
            if eq and name:
                lines.append(_line(".youtube.com", name.strip(), value.strip()))
        out, fmt = HEADER + "\n".join(lines) + "\n", "header string"
    rows = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith("#")]
    if not any(ln.split("\t")[0].endswith("youtube.com") for ln in rows):
        raise ValueError("no youtube.com cookies in it")
    return out, fmt
