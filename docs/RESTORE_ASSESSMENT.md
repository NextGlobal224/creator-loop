# Review a DB backup before restore

This checkpoint provides **inspection only**. It does not restore a database,
change an installation pointer, grant confirmation, migrate data or delete media.
Restore apply, explicit lost-change/media confirmation and recovery UI remain
under development. Do not copy the backup over an open WAL database.

Close the app. Choose the intended published backup ID and an explicit staged
candidate; never choose an installation by newest directory/time alone:

```powershell
$env:CREATOR_LOOP_DATA_ROOT = 'D:\Dữ liệu Creator Loop'
CreatorLoop.exe --inspect-restore BACKUP_ID --restore-candidate 'D:\Bản cài Creator Loop\VERSION-COMMIT-ID' --installation-root 'D:\Bản cài Creator Loop'
```

Source uses `python -m creator_loop` with `PYTHONPATH=app`. JSON output identifies
the selected backup/time/schema, actual current schema, forward target schema,
table counts, candidate manifest identity and media assessment. Counts describe
rows, not a complete summary of edits; **every database change after the backup
time can be lost** during a later restore, including edits with unchanged counts.
The selected snapshot is DB-only. Newer/untracked files remain on disk; restoring
SQLite cannot recover or remove their bytes.

Inspection holds app and SQLite writer locks, opens and validates the actual
backup's digest/size/integrity/FK/schema/checksum history, and verifies the
candidate's complete file inventory and known packaged migration SQL. The
candidate must support forward migration from the backup and reading the target.
The current source must also be a real valid supported schema (1–6); corrupt,
missing or future-schema source recovery is not handled by this slice.

Media is assessed from the snapshot's actual `asset_files`, using **current**
registered-root/volume identities and safe storage-key resolution. It does not
reapply old root paths or fall back to another volume. For each reference it
reads and hashes the actual file, reporting valid, unavailable/unsafe path,
size mismatch or digest mismatch. Windows read handles deny writes/deletion
while each file is hashed. These handles are released after inspection; evidence
must be rechecked before any later restore. Known missing/changed media requires
separate explicit acknowledgement; absence of a problem is not an ownership or
reuse-rights grant.

The review proof includes a streaming logical fingerprint of current SQLite
schema/rows, including committed WAL-visible data, and current registry identity.
It emits no private row text or media bytes. A new row/edit, registry change,
different backup/candidate or different assessed media changes the proof. This
fingerprint is coordination evidence, not authentication or restore permission.
Any future apply operation must take fresh locks, revalidate evidence, preserve
current state and require the user's explicit lost-change confirmation.

Unresolved runtime records or queued/running processing block inspection; no
recorded PID is killed or cleared. Exit 3 means app-lock contention, exit 4 a
refusal/error type; neither performs a restore. Work has a 60s default checking
budget (maximum600s), SQLite progress cancellation and chunk checks. Blocking OS
I/O also needs a process deadline; tests/CI use the existing bounded runner.

Focused tests cover real backup/media/WAL data, same-size changed bytes, new
references/files, registry changes, offline-volume simulation, unsafe selection,
contention and no-create CLI behavior. Windows has an actual sharing test. The
exact-ZIP CI probe checks valid media and a same-size change of its own private
fixture, then restores that fixture's bytes; it does not exercise destructive DB
restore or certify physical removable volumes, engine/model or release.
