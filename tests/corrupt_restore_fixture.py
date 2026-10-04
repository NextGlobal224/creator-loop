"""Clone an isolated closed schema1 backup/candidate seed, not live user data.

The seed is produced once by the actual backup/stage/migration API. Every test
receives its own files/installation; changes never reach the cached seed.
Real corrupt preparation/copy/inspection still run in each behavior test, and
the supported schema1-6 matrix retains fresh actual backup/update preparation.
"""

import atexit
import copy
import json
import shutil
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from creator_loop.database import SCHEMA_VERSION, validate
from test_update_preparation import UpdatePreparationTests


@lru_cache(maxsize=1)
def _seed():
    fixture = UpdatePreparationTests()
    fixture.setUp()
    atexit.register(fixture.doCleanups)
    journal = fixture._prepare()
    record = json.loads(journal.read_text())
    # No live-WAL main-file copy. Only our closed, fully validated synthetic
    # fixture is cloned; raw damage/archive/root binding are created afresh.
    if any(
        Path(str(fixture.path) + suffix).exists()
        for suffix in ("-wal", "-shm", "-journal")
    ):
        raise RuntimeError("Closed fixture has unexpected SQLite sidecars")
    with closing(
        sqlite3.connect(
            fixture.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
        )
    ) as db:
        db.execute("PRAGMA foreign_keys=ON")
        validate(db, expected_version=SCHEMA_VERSION)
    return fixture, journal, record


def clone_prepared_fixture(fixture):
    """Caller owns its TemporaryDirectory; clone only explicit seed entries."""
    seed, seed_journal, record = _seed()
    backup = fixture.root / "backups" / record["backup_id"]
    backup.mkdir()
    source_backup = seed.root / "backups" / record["backup_id"]
    for name in ("creator_loop.sqlite3", "backup-manifest.json"):
        shutil.copyfile(source_backup / name, backup / name)
    source_candidate = Path(record["candidate_directory"])
    candidate = fixture.installation / source_candidate.name
    candidate.mkdir()
    for name in ("release-manifest.json", *seed.manifest["files"]):
        source = source_candidate / name
        target = candidate / name
        if source.is_symlink() or source.is_junction() or not source.is_file():
            raise ValueError("Fixture seed cannot contain linked/missing entries")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    shutil.copyfile(seed.path, fixture.path)
    shutil.copyfile(seed.archive, fixture.archive)
    shutil.copyfile(seed.manifest_path, fixture.manifest_path)
    fixture.manifest = copy.deepcopy(seed.manifest)
    journal = fixture.root / "manifests" / seed_journal.name
    journal.write_text(
        json.dumps({**record, "candidate_directory": str(candidate)}), encoding="utf-8"
    )
    return journal
