"""Command line helpers.

    python -m privatube.cli init-db
    python -m privatube.cli create-user <username> [--admin]
    python -m privatube.cli reset-password <username>
    python -m privatube.cli reset-mfa <username>
    python -m privatube.cli check-backup <file.db.gz>   # is this backup usable? (read-only)
    python -m privatube.cli restore <file.db.gz>        # replace the database (services stopped!)
    python -m privatube.cli reconcile                   # mark videos whose file is missing as evicted
"""
from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from . import db, security
from .config import settings


def check_backup(path: Path) -> dict:
    """Unpack a backup to a temp file and check it; returns a summary or raises ValueError."""
    fd, name = tempfile.mkstemp(suffix=".db", dir=settings.data_dir)
    os.close(fd)
    tmp = Path(name)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst)
    try:
        c = sqlite3.connect(tmp)
        ok = c.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise ValueError(f"integrity check failed: {ok}")
        info = {"schema": c.execute("PRAGMA user_version").fetchone()[0],
                **{t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                   for t in ("users", "channels", "videos", "comments", "access_codes")}}
        c.close()
    except (sqlite3.DatabaseError, ValueError):
        tmp.unlink(missing_ok=True)
        raise
    return info | {"tmp": tmp}


def reconcile() -> dict:
    """After a restore the database may be older than the files: mark 'ready' videos whose
    file is gone as evicted (they stay listed and can be downloaded again)."""
    fixed = {"evicted": 0}
    with db.get() as conn:
        for v in conn.execute("SELECT id, path FROM videos WHERE status='ready' AND path IS NOT NULL").fetchall():
            if not (settings.media_dir / v["path"]).is_file():
                conn.execute("UPDATE videos SET status='evicted' WHERE id=?", (v["id"],))
                fixed["evicted"] += 1
    return fixed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="privatube")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    c = sub.add_parser("create-user")
    c.add_argument("username")
    c.add_argument("--admin", action="store_true")
    r = sub.add_parser("reset-password")
    r.add_argument("username")
    m = sub.add_parser("reset-mfa")
    m.add_argument("username")
    for name in ("check-backup", "restore"):
        sub.add_parser(name).add_argument("file", type=Path)
    sub.add_parser("reconcile")
    args = ap.parse_args(argv)

    if args.cmd in ("check-backup", "restore"):
        try:
            info = check_backup(args.file)
        except (OSError, sqlite3.DatabaseError, ValueError) as e:
            print(f"backup NOT usable: {e}", file=sys.stderr)
            return 1
        tmp = info.pop("tmp")
        print("backup OK: " + ", ".join(f"{k} {v}" for k, v in info.items()))
        if args.cmd == "check-backup":
            tmp.unlink()
            return 0
        dest = settings.db_path
        keep = dest.with_name(f"{dest.name}.before-restore-{time.strftime('%Y%m%d-%H%M%S')}")
        if dest.exists():
            c = sqlite3.connect(dest)            # fold the WAL in, so one file holds everything
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.close()
            dest.rename(keep)
            print(f"previous database kept as {keep}")
        for suffix in ("-wal", "-shm"):
            Path(str(dest) + suffix).unlink(missing_ok=True)
        tmp.rename(dest)
        db.migrate()                              # bring an older backup up to the current schema
        print("restored; checking where the video files are ...")
        print("reconcile: " + ", ".join(f"{k} {v}" for k, v in reconcile().items()))
        return 0

    db.migrate()
    if args.cmd == "reconcile":
        print("reconcile: " + ", ".join(f"{k} {v}" for k, v in reconcile().items()))
        return 0
    if args.cmd == "init-db":
        print("database ready")
        return 0
    with db.get() as conn:
        if args.cmd == "reset-mfa":
            cur = conn.execute("UPDATE users SET totp_secret=NULL, totp_last_step=0 WHERE username=?",
                               (args.username,))
            conn.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)",
                         (args.username,))
            print("MFA removed; set it up again at next sign-in" if cur.rowcount else "no such user")
            return 0 if cur.rowcount else 1
        pw = security.generate_password()
        if args.cmd == "create-user":
            if conn.execute("SELECT 1 FROM users WHERE username=?", (args.username,)).fetchone():
                print("user exists", file=sys.stderr)
                return 1
            conn.execute("INSERT INTO users(username,pw_hash,role,pw_changed_at,created_at) "
                         "VALUES (?,?,?,?,?)", (args.username, security.hash_password(pw),
                                                "admin" if args.admin else "viewer", db.now(), db.now()))
        else:
            cur = conn.execute("UPDATE users SET pw_hash=?, pw_changed_at=?, disabled=0 WHERE username=?",
                               (security.hash_password(pw), db.now(), args.username))
            if cur.rowcount == 0:
                print("no such user", file=sys.stderr)
                return 1
            conn.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)",
                         (args.username,))
    print(f"username: {args.username}\npassword: {pw}\n(shown once - write it down)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
