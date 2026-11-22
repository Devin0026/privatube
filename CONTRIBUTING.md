# Contributing to PrivaTube

Thanks for helping! Bug reports, fixes and small focused features are very welcome. For
anything big, please open an issue first so we can agree on the approach.

## Getting started

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

See the README's *Development* section to run the site locally. By contributing you agree
that your work is licensed under the AGPL-3.0, like the rest of the project.

## Rules of thumb

These keep PrivaTube small, safe and easy to run on a 1 GB server.

### Simplicity

1. **Boring tech, few moving parts.** One language (Python), one database file (SQLite),
   nginx and systemd. Every new service, runtime or build step must justify the RAM,
   disk, updates and attack surface it adds.
2. **Server-rendered pages first.** Use JavaScript only for what HTML can't do (the
   player, infinite scroll, saving the playback position). No frontend build pipeline.
3. **Configuration lives in `/etc/privatube/env`.** Code has safe defaults; nothing is
   hard-coded per server.
4. **Design for the size you have.** A small group of viewers means `LIKE` search, one
   uvicorn process and in-memory rate limits are fine. Don't add Redis, Postgres or
   queues "for scale".

### Security (private content, internet-facing)

5. **Deny by default.** Every page requires a login (`need_login`), every admin route
   uses `need_admin`, and every video query goes through `Principal.scope_sql()`. A
   route that lists videos without the scope filter is a bug.
6. **Never trust input.** Parameterized SQL only. Templates auto-escape (never use `|safe`
   on user or YouTube text). URLs are validated with `youtube.parse()`, and redirect
   targets go through `safe_next()`.
7. **Every state-changing request is a POST with a CSRF token.** Keep the strict CSP:
   no inline scripts or styles.
8. **Store secrets only as hashes.** Passwords use argon2id; session tokens and access
   codes use SHA-256. Show a generated secret once and never log it.
9. **Every file URL is signed and expires.** Nothing under the media folder is reachable
   without a valid `secure_link` signature. Never hand out stable links.
10. **Least privilege at runtime.** The services run as user `privatube` in systemd's
    read-only sandbox; only `/var/lib/privatube` is writable.

### Reliability

11. **Every background action is a job in the database.** Jobs retry with backoff and
    their errors show up in the admin panel. Nothing is fire-and-forget.
12. **Every job is idempotent.** Running a download or sync twice gives the same result:
    downloads go to a temp folder and are moved into place at the end.
13. **Separate "try later" from "give up".** `Defer` covers low disk, upcoming live
    streams and YouTube's bot check. `Permanent` covers private or removed videos.
    Everything else retries.
14. **Watch the disk.** Every write path checks free space; a full disk takes down
    SQLite, nginx logs and the OS.
15. **Expect yt-dlp to break.** It updates daily, failures stay visible, and yt-dlp code
    is kept in `worker.py`, so a YouTube change touches one file.

### Process

16. **Tests for every rule above that can regress:** access scope, CSRF, throttling,
    signatures, job state changes, ingest merging. Run `pytest` before sending a change.
17. **Keep yt-dlp out of unit tests.** Monkeypatch `worker.download` and
    `worker.list_channel`; test against real YouTube by hand.
18. **Migrations only go forward.** Add a new entry to `db.MIGRATIONS`; never edit one
    that has been released.
19. **The installer stays idempotent.** Re-running `deploy/install.sh` must update an
    existing server without losing data.
