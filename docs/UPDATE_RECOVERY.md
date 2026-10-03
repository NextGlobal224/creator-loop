# Interrupted update recovery

Close the app before inspecting or resuming an update. Use an updater whose
known migration history matches the candidate and the actual database. Keep
the journal, every backup, previous installation and logs. Do not delete worker
records, kill a recorded PID or choose the newest staging directory to bypass
a refusal.

```powershell
$env:CREATOR_LOOP_DATA_ROOT = 'D:\Dữ liệu Creator Loop'
CreatorLoop.exe --inspect-update 'D:\Dữ liệu Creator Loop\manifests\update-ID.json' --installation-root 'D:\Bản cài Creator Loop'
CreatorLoop.exe --resume-update 'D:\Dữ liệu Creator Loop\manifests\update-ID.json' --installation-root 'D:\Bản cài Creator Loop'
```

Replace `ID` with the update ID from the operation you intend to recover. Source
commands use `python -m creator_loop` with `PYTHONPATH=app`. Inspection acquires
the app and SQLite writer locks, checks actual schema/history/integrity/FK,
candidate inventory and manifest binding, backup and storage references, and
reports metadata as JSON. It creates no database, migrates nothing, changes no
journal and does not run health. `recovery_ready` describes those checks at that
instant; resume takes fresh locks and repeats them.

A journal phase or `migration_committed` flag can lag a real COMMIT. Resume
uses the actual validated SQLite state, records the prior coordination state
and creates a **new current-state DB-only backup**, retaining the old one. This
preserves a snapshot of changes made since the original backup. With the same
app/writer locks held, it applies sequential known migrations when needed and
checks storage references. If the database is already at the target, it
validates that state and completes its own transaction without inferring the
historical commit outcome. Its own pre-commit failure rolls back; its own
post-commit failure is recorded separately. Neither path restores data.

Fresh activation then takes locks again, rechecks the candidate/backup/database
and runs bounded readonly native health. An app or writer winning the gap is
refused or detected by these fresh checks. Only a fully verified previous
pointer compatible with the actual schema can be selected after health
failure, including a previous pointer retained in an interrupted switch's
journal. An incompatible previous installation remains on disk but cannot be
launched against the new schema.

Candidate manifests are bound to preparation journals by a canonical SHA-256
identity. A changed candidate, missing binding from an older journal, or a
crash before recording a candidate requires a new explicit preparation from
the intended ZIP/manifest. Resume never guesses a candidate or silently trusts
replacement metadata. This binding detects changes; it does not authenticate
the publisher. Completed updates use [metadata recheck/repair](UPDATE_METADATA.md)
instead of being resumed again. Attempts are retained (maximum 100 per journal),
as are backups; there is no automatic retention deletion.

Storage checks verify references, availability and size, not a full media
backup. Missing storage, invalid history/schema, queued/running processing or
unknown runtime records block resume. Worker recovery and DB restore with
explicit lost-change confirmation/media evaluation remain separate work.
These commands do not restore DB/media, erase newer files or promise downgrade.

Exit 3 means data-root lock contention; exit 4 reports a refusal/error type
without embedding private asset content. Retain logs and journal after either
failure. A healthy core activation with pending metadata uses the repair path;
it is not a reason to restore a database.

Tests exercise actual child-process exits before/after migration COMMIT and
after recovery's own COMMIT, supported actual schemas 1–6, post-backup data,
lock continuity/gap refusal, altered candidate/backup, pending pointers and
failure retention. Health orchestration fixtures use real readonly SQLite
checks; native process behavior has its own tests. CI also probes inspect/resume
and fresh health from the exact ZIP, simulating a lagging coordination journal.
That artifact probe is not a claim of engine/model or full V1 release acceptance.
