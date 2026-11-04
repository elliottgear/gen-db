#!/usr/bin/env python3
"""
Snapshot the Watt Watch database and uploads into one .tar.gz.

Uses SQLite's online backup API, so it's safe to run while the server is up.
Keeps the newest BACKUP_KEEP archives (default 14) and deletes older ones.

Usage:  python3 backup.py                 (writes to BACKUP_DIR, default <db folder>/backups)
        railway run python3 backup.py     (against the production volume)

A backup on the same volume protects against bad edits, not against losing the
volume — copy the archives somewhere else too.
"""
import datetime
import os
import re
import sqlite3
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import DB_PATH, UPLOADS_DIR  # noqa: E402

BACKUP_DIR = os.environ.get('BACKUP_DIR') or os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), 'backups')
BACKUP_KEEP = int(os.environ.get('BACKUP_KEEP', 14))
PREFIX = 'watt-watch-'
# watt-watch-YYYYmmdd-HHMMSS[-micro][-n].tar.gz (older archives have no microseconds)
ARCHIVE_RE = re.compile(r'^watt-watch-(\d{8}-\d{6})(?:-(\d{6}))?(?:-(\d+))?\.tar\.gz$')


def _archive_path(stamp):
    path = os.path.join(BACKUP_DIR, f'{PREFIX}{stamp}.tar.gz')
    n = 1
    while os.path.exists(path):  # same microsecond (e.g. a frozen clock): never overwrite an archive
        path = os.path.join(BACKUP_DIR, f'{PREFIX}{stamp}-{n}.tar.gz')
        n += 1
    return path


def _age_key(name):
    m = ARCHIVE_RE.match(name)
    return m.group(1), m.group(2) or '000000', int(m.group(3) or 0)


def main():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    archive = _archive_path(datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = os.path.join(tmp, 'gen_db.sqlite3')
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(snapshot)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        with tarfile.open(archive, 'w:gz') as tar:
            tar.add(snapshot, arcname='gen_db.sqlite3')
            if os.path.isdir(UPLOADS_DIR):
                tar.add(UPLOADS_DIR, arcname='uploads',
                        filter=lambda info: None if os.path.basename(info.name).startswith('.incoming-') else info)
    print(f'Backup written: {archive}')

    archives = sorted((n for n in os.listdir(BACKUP_DIR) if ARCHIVE_RE.match(n)), key=_age_key)
    for old in archives[:-BACKUP_KEEP]:
        os.remove(os.path.join(BACKUP_DIR, old))
        print(f'Removed old backup: {old}')


if __name__ == '__main__':
    main()
