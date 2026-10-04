# Confirmed DB restore and recovery

Close the app and choose an explicit published backup ID and verified staged
installation. This operation restores SQLite, then applies known forward
migrations to schema 6. It does not downgrade the schema, delete media or restore
media bytes. **Every database change after the backup can be lost**, including
edits that leave table counts unchanged. Preserve the backup and media separately.

First inspect the selected backup and current state:

```powershell
$env:CREATOR_LOOP_DATA_ROOT = 'D:\Dữ liệu Creator Loop'
CreatorLoop.exe --inspect-restore BACKUP_ID --restore-candidate 'D:\Bản cài Creator Loop\VERSION-COMMIT-ID' --installation-root 'D:\Bản cài Creator Loop'
```

Read the backup time, loss warning and media assessment. Copy the returned
`assessment_identity` only after reviewing them. To explicitly accept losing
post-backup database changes:

```powershell
CreatorLoop.exe --apply-restore BACKUP_ID --restore-candidate 'D:\Bản cài Creator Loop\VERSION-COMMIT-ID' --installation-root 'D:\Bản cài Creator Loop' --reviewed-restore ASSESSMENT_IDENTITY --confirm-lost-changes
```

If inspection reports media issues, a separate `--confirm-media-issues` is required
to apply the DB restore. Acknowledgement does not make missing or altered media
healthy: activation remains blocked until all restored references resolve to
their recorded bytes. Newer and unreferenced media remains on disk. A changed DB,
registry, backup, candidate or media makes the review stale; inspect again rather
than reusing old consent. Source invocation uses `python -m creator_loop` with
`PYTHONPATH=app`. The [maintenance UI](MAINTENANCE_UI.md) displays this review and
requires the same separate explicit acknowledgements; it opens without opening
or initializing the DB, including when a pending guard blocks Library.

Before copying, the operation holds app/DB locks and publishes a validated
current-state DB backup under `backups/<id>/`. This particular safety snapshot
records `storage_reference_check: not_checked` so missing current media cannot
prevent retaining current DB data. Ordinary `--backup` still checks storage.
Neither snapshot includes media. All backups and previous installations are
retained; no automatic retention deletion is implemented.

Restore stages and validates a migrated snapshot, verifies media references are
unchanged, then writes a durable journal and runtime guard. The live copy uses
SQLite Backup API under an exclusive SQLite lock, not a filesystem replacement
of an open WAL database. Native Windows media read handles deny writes/deletion
through copying and, after fresh verification, through activation/health. DB and
full media digests are rechecked when locks are reacquired and before clearing
the guard. Fresh bounded health and successful metadata are required.

If interrupted, normal and managed app launches refuse the runtime guard. Keep
DB, backups, installation versions and logs; never delete the guard to bypass
recovery. Use the exact restore journal named in `runtime/restore-in-progress.json`:

```powershell
CreatorLoop.exe --recover-restore 'D:\Dữ liệu Creator Loop\manifests\update-RESTORE_ID.json' --installation-root 'D:\Bản cài Creator Loop'
```

Recovery compares actual DB contents with validated pre-restore and staged
snapshots. It never copies the backup again or assumes a commit from journal
phase. If the original DB remains, it records `RESTORE_NOT_APPLIED` and clears
the guard without launching a candidate. If the restored DB remains, it rechecks
candidate/media and runs fresh activation/health. Unknown or changed state stays
blocked. Fix unavailable media using its registered location and original bytes,
then recover again; no fallback volume or registry repointing is performed.

Exit 3 means app-lock contention, 4 a refused/error operation, and 2 invalid CLI
arguments. Recovery exit 0 means the state is resolved; inspect the journal to
distinguish `RESTORE_NOT_APPLIED` from `COMPLETED`. Failures retain evidence and
do not promise the DB is unchanged after copying begins. Journals/logs report
error types without private row contents. The apply work budget is 120s by
default (maximum 600s); test/CI process deadlines also bound blocking OS calls.

This slice requires an existing valid supported current DB and backup (schemas
1–6). Corrupt, missing and future-schema current DB recovery,
physical removable-volume acceptance, real engine/model and final release
acceptance remain open. CI fixture tests are not proof for those requirements.

SQLite behavior used here is documented in [Backup API](https://www.sqlite.org/c3ref/backup_finish.html)
and [exclusive locking mode](https://www.sqlite.org/pragma.html#pragma_locking_mode).
