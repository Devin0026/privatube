#!/usr/bin/env bash
# Is YouTube access working? Run on the server:  sudo bash ./deploy/check-youtube.sh
# Prints cookie NAMES and expiry dates only (never values), then tries one video.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
runuser -u privatube -- bash -c 'set -a; . /etc/privatube/env; set +a; cd /tmp
export PATH=/usr/local/bin:$PATH
exec /opt/privatube/venv/bin/python - <<"PY"
import sqlite3, time
from privatube.config import settings
from privatube import worker
f = settings.ytdlp_cookies
print("cookie file:", f, "exists" if f.is_file() else "MISSING")
if f.is_file():
    rows = [l.split("\t") for l in f.read_text(errors="replace").splitlines()
            if l.strip() and (not l.startswith("#") or l.startswith("#HttpOnly_"))]
    rows = [r for r in rows if len(r) >= 7]
    yt = [r for r in rows if r[0].removeprefix("#HttpOnly_").endswith("youtube.com")]
    print(f"  {len(rows)} cookies, {len(yt)} for youtube.com; modified {time.ctime(f.stat().st_mtime)}")
    names = {r[5] for r in yt}
    for k in ("SID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO", "SAPISID"):
        print(f"  {k:16}", "present" if k in names else "-- missing")
    exp = [int(r[4]) for r in yt if r[4].isdigit() and int(r[4]) > 0]
    if exp:
        print("  earliest expiry:", time.strftime("%Y-%m-%d", time.gmtime(min(exp))))
    print("  owner/mode:", oct(f.stat().st_mode & 0o777))
try:
    print("YouTube test: OK ->", worker.probe())
except Exception as e:
    print("YouTube test: FAILED ->", str(e)[:300])
c = sqlite3.connect(settings.db_path)
for st, n in c.execute("SELECT status, COUNT(*) FROM videos GROUP BY status"):
    print(f"videos {st}: {n}")
for kind, err in c.execute("SELECT kind, substr(error,1,120) FROM jobs WHERE status=? ORDER BY updated_at DESC LIMIT 3", ("failed",)):
    print("recent failure:", kind, "|", err)
PY'
