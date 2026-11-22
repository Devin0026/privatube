"""Database backups: a gzip'd SQLite snapshot every PT_BACKUP_HOURS into data/backups,
keeping the newest PT_BACKUP_KEEP. Copy that folder off the server now and then (see README);
restore with deploy/restore.sh."""
from __future__ import annotations

import datetime as dt
import gzip
import logging
import os
import sqlite3

from . import db
from .config import settings

log = logging.getLogger("privatube.backup")


def backup_db(conn: sqlite3.Connection, force: bool = False) -> bool:
    t = db.now()
    last = db.kv_get(conn, "backup") or {}
    if not force and t - last.get("at", 0) < settings.backup_hours * 3600:
        return False
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    tmp = settings.data_dir / ".backup.db"
    tmp.unlink(missing_ok=True)
    dst = sqlite3.connect(tmp)
    try:
        conn.backup(dst)   # consistent snapshot even while the web app writes
    finally:
        dst.close()
    data = gzip.compress(tmp.read_bytes())
    tmp.unlink()
    name = "privatube-" + dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y%m%d-%H%M%S") + ".db.gz"
    part = settings.backup_dir / (name + ".part")
    part.write_bytes(data)
    os.replace(part, settings.backup_dir / name)
    for old in sorted(settings.backup_dir.glob("privatube-*.db.gz"))[:-settings.backup_keep]:
        old.unlink(missing_ok=True)
    db.kv_set(conn, "backup", {"at": t, "size": len(data), "file": name})
    log.info("database backup %s (%d KiB)", name, len(data) // 1024)
    return True
