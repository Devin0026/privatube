import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="privatube-test-")
os.environ.update(PT_DATA_DIR=_tmp, PT_SERVE_MEDIA="1", PT_COOKIE_SECURE="0",
                  PT_MEDIA_SECRET="test-secret", PT_PAUSE_MIN="0", PT_PAUSE_MAX="0")

import pytest  # noqa: E402

from privatube import db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    if db.settings.db_path.exists():
        for suffix in ("", "-wal", "-shm"):
            p = db.settings.db_path.with_name(db.settings.db_path.name + suffix)
            p.unlink(missing_ok=True)
    db.migrate()
    yield
