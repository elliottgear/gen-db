import datetime
import os
import sqlite3
import sys
import tarfile

import pytest

import backup
import seed_batch
import server


@pytest.fixture
def backup_env(db, tmp_path, monkeypatch, uploads_dir):
    backup_dir = tmp_path / 'backups'
    monkeypatch.setattr(backup, 'DB_PATH', server.DB_PATH)
    monkeypatch.setattr(backup, 'UPLOADS_DIR', uploads_dir)
    monkeypatch.setattr(backup, 'BACKUP_DIR', str(backup_dir))
    return backup_dir


def archives(backup_dir):
    return sorted(p for p in os.listdir(backup_dir) if p.endswith('.tar.gz'))


class TestBackup:
    def test_archive_holds_a_consistent_db_snapshot_and_the_uploads(self, backup_env, db, uploads_dir, tmp_path):
        db.execute("INSERT INTO customers (name) VALUES ('Backed Up')")
        db.commit()  # still open (WAL) while the backup runs
        with open(os.path.join(uploads_dir, 'photo.png'), 'wb') as f:
            f.write(b'png-bytes')
        open(os.path.join(uploads_dir, '.incoming-partial'), 'wb').close()
        backup.main()
        [name] = archives(backup_env)
        with tarfile.open(backup_env / name) as tar:
            names = set(tar.getnames())
            assert {'gen_db.sqlite3', 'uploads/photo.png'} <= names
            assert 'uploads/.incoming-partial' not in names
            tar.extract('gen_db.sqlite3', tmp_path / 'restore')
        restored = sqlite3.connect(str(tmp_path / 'restore' / 'gen_db.sqlite3'))
        assert restored.execute("SELECT COUNT(*) FROM customers WHERE name='Backed Up'").fetchone()[0] == 1
        assert restored.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        restored.close()

    def test_keeps_only_the_newest_archives(self, backup_env, monkeypatch):
        backup_env.mkdir()
        for day in range(1, 6):
            (backup_env / f'{backup.PREFIX}202601{day:02d}-000000.tar.gz').write_bytes(b'old')
        (backup_env / 'unrelated.txt').write_text('keep me')
        monkeypatch.setattr(backup, 'BACKUP_KEEP', 3)
        backup.main()
        kept = archives(backup_env)
        assert len(kept) == 3
        assert f'{backup.PREFIX}20260105-000000.tar.gz' in kept and f'{backup.PREFIX}20260103-000000.tar.gz' not in kept
        assert (backup_env / 'unrelated.txt').exists()

    def test_works_without_an_uploads_folder(self, backup_env, monkeypatch, tmp_path):
        monkeypatch.setattr(backup, 'UPLOADS_DIR', str(tmp_path / 'missing'))
        backup.main()
        with tarfile.open(backup_env / archives(backup_env)[0]) as tar:
            assert tar.getnames() == ['gen_db.sqlite3']

    @pytest.mark.xfail(strict=True, reason='BUG (minor): archive names have 1-second resolution, so two backups in '
                                           'the same second overwrite each other.')
    def test_two_backups_in_the_same_second_both_survive(self, backup_env, frozen_clock):
        backup.main()
        backup.main()
        assert len(archives(backup_env)) == 2


class TestSeedBatchScript:
    def test_main_inserts_one_batch_and_reports_it(self, db, capsys, seeded_random):
        before = db.execute('SELECT COUNT(*) FROM customers').fetchone()[0]
        seed_batch.main()
        out = capsys.readouterr().out
        assert f'Inserted {seed_batch.BATCH_SIZE} customers' in out
        assert db.execute('SELECT COUNT(*) FROM customers').fetchone()[0] == before + seed_batch.BATCH_SIZE

    def test_defaults_today_to_the_real_date(self, db, seeded_random):
        seed_batch.insert_batch(db)
        latest = db.execute("SELECT MAX(install_date) FROM generators WHERE created_by=?", (seed_batch.CREATED_BY,)).fetchone()[0]
        assert latest <= datetime.date.today().isoformat()

    def test_serials_never_collide_within_a_batch(self, seeded_random):
        used = set()
        serials = [seed_batch.make_serial('Generac', used) for _ in range(500)]
        assert len(set(serials)) == 500 and all(s.startswith('GEN-') for s in serials)
