# Creator Loop — Bootstrap Gate 1

Local-first Windows desktop foundation. This is a bootstrap, not the full product. The architecture contracts are copied into `docs/`; prototype 0.5/0.6 is not used.

The [V1 acceptance matrix](docs/PRODUCT_ACCEPTANCE.md) tracks required product flows, their evidence, and remaining release conditions.

## Source smoke and tests

Requires Python 3.12 x64. From the repository root:

```bash
python -m unittest discover -s tests -v
PYTHONPATH=app CREATOR_LOOP_DATA_ROOT=/tmp/creator-loop-test python -m creator_loop --smoke
```

On Windows PowerShell:

```powershell
$env:PYTHONPATH = 'app'
$env:CREATOR_LOOP_DATA_ROOT = Join-Path $env:TEMP 'Creator Loop test data'
python -m creator_loop --smoke
```

Desktop UI requires `pip install PySide6==6.10.2`. The app stores user data outside installation by default, under `%LOCALAPPDATA%\CreatorLoop` on Windows. `CREATOR_LOOP_DATA_ROOT` overrides this for test/portable use.

To open the current Library UI, run `python -m creator_loop` with `PYTHONPATH=app`. The three import buttons accept TEXT, MP4 video, and PNG/JPEG images. The app copies bytes into the user-data originals store and lists the imported files. Format detection checks stored signatures, then IMAGE/VIDEO intake decodes real pixels or a video frame with audio samples when present before registration. This preflight passed required CI in PR #44; broad codec coverage and native decoder process isolation remain open.

For a TEXT original, select its row and choose **Tạo Evidence Text**. Enter start/end positions on the NFC text snapshot and inspect the excerpt preview before saving. Select the saved Evidence row and choose **Mở Evidence Text** to verify and display that exact excerpt again. To correct the latest TEXT Evidence Version, select it and choose **Sửa Evidence Text**; enter a new range, actor and reason. The old version remains in the table and can still be reopened. The status shows how many Claim Versions still refer to an older version and need review. The initial create/reopen UI slice passed the required CI checks in PR #10; the correction UI passed the required checks in PR #20.

Select an original and choose **Nguồn của Asset** to inspect its linked Sources. Enter a platform to create a Source, or choose an existing Source to associate it with another Asset. URL, external ID and publisher are optional; unknown rights and relationship stay explicit. The dialog records provenance supplied by the user and does not infer it from the filename.

For a PNG/JPEG original, choose **Tạo Evidence Image**. Set normalized X/Y/width/height coordinates, inspect the cropped preview and enter the observation. Select its Evidence row and choose **Mở Evidence Image** to verify the recorded original digest and reopen that region. Image decoding uses the bundled Qt runtime; images above 40 MiB or 80 million pixels are outside the current local decode budget.

To correct an IMAGE_REGION Evidence Version, select its row and choose **Sửa Evidence Image**. Adjust the region or observation, enter the actor and reason, and inspect the crop preview. The new human version stays on the selected version's verified original or thumbnail anchor; the old version and Claim links remain available for review. Only the latest live version can be corrected.

For an MP4 original with a decodable video track, choose **Tạo Evidence Video**. Set the start/end in milliseconds, play that segment and enter the observation. **Mở Evidence Video** verifies the same original and replays the saved range. For an MP4 with a decodable audio track, choose **Tạo Evidence Audio**, listen to the range, choose speech or other sound, and enter the human observation. **Mở Evidence Audio** verifies the recorded original and replays its saved range. The audio UI passed the required checks in PR #22; transcription is still missing.

To correct a saved TIME_RANGE, select its Evidence row and choose **Sửa Evidence Video** or **Sửa Evidence Audio**. The dialog loads the saved range and observation; audio type stays fixed. Enter the new range/content, actor and reason, then compare the saved original segment before saving. The service appends a human version on the latest verified MP4 original anchor and track with a CORRECT event targeting the old version. It checks recorded/decoded duration and real frames/audio samples, preserves old versions and Claim citations, and leaves the new version pending review. Both versions can be reopened; the status reports Claim Versions needing review.

Select an IMAGE original and choose **Tạo thumbnail Image** to generate a PNG derivative with its own processing run. The task table shows each run's status and derived file path; a failed run stays visible without changing the original. Other media processing tasks and cancellation are later Gate 2 work.

To create Evidence from a generated thumbnail, select its successful task row and choose **Evidence từ thumbnail**. Select a region in the decoded thumbnail; the saved Evidence anchors that derived file. Reopening verifies the thumbnail's own digest, even if the original later becomes unavailable. The service and UI passed required CI in PR #23 and PR #24 respectively.

Select an Evidence Version and use its **Mở Evidence** button to compare the saved source. Then choose **Review Evidence**, select ACCEPT, REJECT, REQUEST_CHANGES or REOPEN, and enter the reviewer and reason when required. The Review column is derived from append-only events. ACCEPT verifies the saved anchor again, and review of an older version after correction is rejected. The service and UI passed required CI in PR #25 and PR #26 respectively.

Choose **Claims** in Library to create a FACTUAL, INTERPRETIVE or EDITORIAL_HYPOTHESIS Claim. Enter the statement and actor, choose exact Evidence Versions and add SUPPORTS, CONTRADICTS or CONTEXT citations, then save. Select a saved Claim Version to read its citations and review history. Correct the latest live version by editing the statement/citations and entering a reason; saving seals a new version and records CORRECT on the old one. Older versions remain read-only. Use **Review phiên bản đã lưu** for ACCEPT, REJECT, REQUEST_CHANGES or REOPEN; ACCEPT verifies every citation anchor. Select a citation and choose **Đóng và mở Evidence trong Library** to reopen that exact source through the existing locator viewer. The support summary keeps STALE citations visible after Evidence correction; Claim acceptance does not approve publication. V1 factual publication requires human ACCEPT on each exact FACTUAL Claim Version referenced by the Package and at least one linked SUPPORTS Evidence Version with current human ACCEPT and a verifiable anchor. Acceptance of a newer version does not satisfy an older Package. PublicationRepository now gates human APPROVED decisions and manual PENDING/PUBLISHED Post records against these exact versions; The Package / Publication UI exposes these decisions and manual records; Final artifact acceptance remains open.

Choose **Projects** in Library to create or select a workspace. Choose Asset, Source or Claim Version and RESEARCH, QUOTE or REUSE_MEDIA, then add the reference. The labels show the exact ID and Claim Version; unknown Source metadata stays explicit. **Lưu trữ Project** preserves the references and switches on the archived view. Archived Projects can be read but cannot receive new references. REUSE_MEDIA records intent and does not grant publication rights. Writes run in a worker; wait for completion before closing the dialog.

Choose **Creator** in Library and select a Project. Write a new alternative, add exact Claim Versions with ASSERTED/INSPIRATION/QUOTE/BACKGROUND, and mark assertion ranges in the **Claim / assertion** tab. Offsets count Unicode codepoints in the current body, with an exclusive end; preview the substring before adding it. Use NEEDS_SOURCE or EDITORIAL explicitly when no Claim backs a span. Editing text that moves or changes a marked span disables saving until that span is removed and marked again. Removing a cited Claim keeps its assertions visible as NEEDS_SOURCE. Save a new alternative, or edit the latest live snapshot with an actor and reason to create a new Version plus CORRECT history. Older and archived snapshots remain read-only.

To author A+B, select at least two same-Project Versions in **Parents A+B**, write the combined body and citations, then choose **Lưu phương án kết hợp A+B**. The app records the exact parents and preserves their snapshots. **Review snapshot đã lưu** appends ACCEPT/REJECT/REQUEST_CHANGES/REOPEN; ACCEPT rechecks exact citation anchors without clearing NEEDS_SOURCE/stale or approving publication. **Đóng và mở Claim** opens the selected exact Claim Version for source inspection. Package repository media items now require a data root, matching physical bytes and explicit OWNED/LICENSED Source declarations; missing or conflicting restrictive rights block sealing. Windows holds read handles through commit to reject concurrent write/delete. The repository recomputes the sealed fingerprint and rechecks current Approval, factual reviews, source anchors and media rights/bytes in a write transaction before preparing a manual Post or recording its publication. REJECT/REVOKE require actor/reason and remain available when media is unavailable. Retry preserves one Post identity; unknown external ID/URL stays NULL, and a publication timestamp needs an explicit timezone. These APIs send no external post.

Choose **Selection** in Library and select a Project. Check the exact A/B/C Draft Versions forming the candidate set, choose one checked Version, inspect its body, and enter the actor and optional reason before **Lưu quyết định**. Select a history row to reopen its exact candidate set, chosen Version, actor and reason; later Draft edits do not rewrite that decision. **Quyết định mới** starts another decision. Archived Projects/Drafts retain readable history. **Đóng và mở Draft được chọn** opens the exact historical Version in Creator, with its Claim/Evidence navigation. Selection does not approve publication.

Choose **Package / Publication** in Library and select a Project. In **Package mới**, choose an exact sealed Draft Version, platform and format, add and order MEDIA/THUMBNAIL files and optional CTA/ALT_TEXT, then close the Package. Caption is copied from the raw sealed Draft; changing it requires a new Draft and Package. In **Snapshot / Approval**, inspect the exact fingerprint, text items and media, open the exact Draft for its Claim/Evidence history, and choose **Kiểm điều kiện hiện tại**. Missing factual ACCEPT/SUPPORTS, unresolved assertions, unavailable bytes or media rights appear as blockers. Enter the human actor, confirm inspection and choose APPROVED; REJECTED/REVOKED require a reason. Every write rechecks the current conditions.

In **Post thủ công**, prepare a PENDING record. Select a text item and copy its exact raw text only after a fresh gated check. Publish manually outside the app, then enter any known external ID/URL and an explicitly zoned publication time, confirm actual publication and choose **Ghi nhận PUBLISHED**. Blank external fields remain NULL. Reopening history preserves exact Package/Post identities; REOPEN or REVOKED blocks later preparation/copy/confirmation. This UI never sends a post to a platform.

Select an original and choose **Tạo Evidence toàn nguồn** only when the complete source is relevant. Inspect the full text, scroll the image at 1:1, or play the full video with its audio. Enter the actor and, for Image/Video, the observation; explicitly check the whole-source confirmation before saving. TEXT stores the complete verified NFC snapshot, including its original line endings. The new version is pending review. **Mở Evidence toàn nguồn** rechecks the exact anchor/digest and displays it again. **Sửa Evidence toàn nguồn** appends an Image/Video observation with an actor, reason and new confirmation; for TEXT it lets you narrow to a verified text range. Old versions and Claim citations remain, and the status reports Claims needing review.

Manual measurement service is available through `PublicationRepository.record_manual_observation(post_id=..., observed_at=..., metrics=tuple[MetricInput, ...])`. It records one immutable MANUAL observation with no fabricated processing run and preserves metric key/scope/unit/definition/raw value. Use an explicitly zoned measurement time at or after publication. Missing values (`numeric_value=None`) create no metric row; `Observation.value_for()` returns None for missing data and 0 for a measured zero. Count values must be whole and nonnegative, all numbers finite. Historical Post measurements remain available after revocation/removal. In **Post thủ công**, select a PUBLISHED/REMOVED Post and choose **Số liệu / Observation của Post đã chọn**. Enter a zoned measurement time and add rows with metric key, scope (POST/ACCOUNT/OTHER), number, unit and definition version. Leave unknown numbers blank; use 0 only for a measured zero. Raw value defaults to the entered numeric text when left blank. Save the new measurement, then select historical measurements to inspect their immutable time/provenance/metrics; **Lần đo mới** creates another observation. Revocation and archive do not erase measurements. Platform definition verification and final artifact acceptance remain open.

The launcher holds `runtime/app-data.lock` for the complete app session, before opening/migrating SQLite. A second app/updater using that data root is refused (CLI exit 3); another data root is independent. The OS releases the held lock on normal exit or crash. Leave the coordination file in place; never delete it or kill a PID to bypass contention. Updater operations must use the same `AppDataLock`. This cooperates across app/updater instances; it does not block someone directly opening SQLite with another tool. With the app closed, `CreatorLoop.exe --backup` (source: `python -m creator_loop --backup`) creates a validated DB-only snapshot with metadata and storage inventory under `backups/<id>/`, holding a SQLite writer reservation too. Media is not included. See [backup instructions](docs/UPDATE_BACKUP.md) for failure handling and retention. Managed launching, explicit recovery and the maintenance UI are available below; final recovery/release acceptance remains open.

## CI and release

For explicitly selected local engine/model files, `--check-components MANIFEST`
checks pinned metadata and physical digest/size without opening the user DB or
executing the components. See [local component preflight](docs/LOCAL_COMPONENTS.md)
for the manifest, timeout command and remaining runtime/8 GB acceptance work.
Choose **Đóng Library để chọn engine/model cục bộ** or launch `--components` to
review the declarations and explicitly confirm a fresh check before saving.
`--inspect-components` shows saved history without claiming current availability
or runtime compatibility. Component files remain externally owned.

Interrupted updates can be inspected with `--inspect-update JOURNAL --installation-root DIR`
and explicitly resumed with `--resume-update JOURNAL --installation-root DIR`. Resume
validates actual SQLite state, backs up current data, retains previous snapshots,
and runs fresh activation/health; it never restores the DB or guesses a candidate.
See [recovery instructions](docs/UPDATE_RECOVERY.md) for refusals and compatibility.

`--inspect-restore BACKUP_ID --restore-candidate DIR --installation-root DIR`
assesses a selected DB-only backup, candidate compatibility and actual media
digests without restoring anything. It reports lost-change/media warnings and
the current-state review proof. See [restore assessment](docs/RESTORE_ASSESSMENT.md);
explicit CLI apply/recovery requires reviewed identity and lost-change confirmation,
as described in [confirmed restore](docs/RESTORE_APPLY.md). In Library, choose
**Đóng Library để sao lưu / cập nhật / khôi phục** to close the app session and
open maintenance. `CreatorLoop.exe --maintenance` opens the same window even
when a restore guard blocks Library. See [maintenance UI](docs/MAINTENANCE_UI.md)
for backup, explicit update/restore consent, recovery, cancel and retained logs.
For an unreadable source, use the separate [damaged-source review](docs/CORRUPT_RESTORE_ASSESSMENT.md),
[raw retention](docs/CORRUPT_SOURCE_PRESERVATION.md), then [confirmed staging](docs/CORRUPT_RESTORE_PREPARATION.md).
The [initial damaged-source UI](docs/DAMAGED_RESTORE_UI.md) in Maintenance provides
review/raw preservation/staging/guarded copy with fresh consent at each step.
This stages a validated migrated backup separately. Use [guarded copy](docs/CORRUPT_RESTORE_COPY.md),
[actual-state inspection](docs/CORRUPT_RESTORE_INSPECTION.md), then
[explicit recovery and fresh health](docs/CORRUPT_COPY_RECOVERY.md) for a validated copy.
Known interrupted-copy states have [consented continuation](docs/CORRUPT_COPY_RESUME.md)
that preserves original/partial bytes and keeps the launch guard. Interrupted
unknown/nonempty partial bundles need separate reviewed retention consent and
complete original evidence. Changed completed copies have a distinct
[fresh review/copy CLI and Maintenance dialog](docs/FRESH_COMPLETED_RESTORE.md),
explicitly selecting the bound backup/candidate and retaining every current bundle.
Maintenance provides [guarded copy review/resume/health-recovery controls](docs/MAINTENANCE_UI.md)
with separate default-off consents. Full frozen product validation, complete
failure recovery and release acceptance remain open.

`--stage-update ZIP --release-manifest JSON --installation-root DIR` verifies a supplied Windows ZIP and its complete file inventory, then stages a separate version directory while retaining existing installations and user data. It does not activate the candidate or migrate the DB. See [staging instructions](docs/INSTALLATION_STAGING.md). CI tests staging and a bounded launcher/schema smoke from the exact ZIP; final updater orchestration and release acceptance remain open.

`--prepare-update ZIP --release-manifest JSON --installation-root DIR` adds a validated DB backup and sequential migration under the same app/SQLite writer locks, checking the packaged SQL and storage references before commit. It leaves an update journal and keeps activation pending. See [update preparation](docs/UPDATE_PREPARATION.md) before running it against an existing DB.

`--health-check` opens an existing DB read-only and returns JSON after schema/integrity/FK, a minimal query and a sample storage-reference check. It never creates or migrates a DB. The updater health helper runs the verified candidate with a native owned-tree deadline; CI exercises the exact staged EXE while the parent holds app/DB locks. See [ownership and health](docs/OWNED_HEALTH.md) for logs and native ownership limits.

MP4 import and TIME_RANGE video/audio Evidence decode use a separate owned Windows worker with a process deadline and read-only inherited original handle. The current local budget is one frame up to 4K and 512 MiB worker committed memory. See [decoder lifecycle](docs/DECODER_LIFECYCLE.md) for timeout/shutdown behavior and the remaining playback/recovery acceptance work.

Normal Library startup records known interrupted thumbnail runs as failed before starting new workers, preserving queued work and terminal history. Smoke modes preserve run history. See [processing recovery](docs/PROCESSING_RECOVERY.md) for retry guidance and tasks whose previous executor remains unverified.

Startup also cleans decoder runtime workspaces whose bound parent and child are proven stopped, preserving live or unbound entries, originals and ownership logs. See [runtime recovery](docs/RUNTIME_RECOVERY.md) for the deletion policy and remaining lifecycle acceptance.

Library can request cancellation of Video intake or media Evidence decoding and waits for owned worker cleanup when closing. See [media cancellation](docs/MEDIA_CANCEL.md) for commit boundaries and remaining playback work.

Video/audio Evidence viewers now run actual playback in a separate owned child, streaming bounded frames/PCM to the UI while retaining the verified original handle. See [playback isolation](docs/PLAYBACK_ISOLATION.md) for pause/cancel/exit deadlines and acceptance limits.

`--activate-update JOURNAL --installation-root DIR` verifies a prepared update and its backup/candidate again, changes the managed installation pointer, and runs bounded readonly health while app/DB locks remain held. Failure retains DB/media/backup/versions; only a verified schema-compatible previous pointer can be selected again. See [activation](docs/UPDATE_ACTIVATION.md). Managed launching from that pointer is available below; the maintenance UI exposes explicit recovery/restore controls.

`--launch-managed --installation-root DIR` launches only the verified ACTIVE pointer. Its child uses `--compatible-only`, takes its own app lock and refuses an incompatible existing schema without migration. UI output is discarded; ownership/exit metadata is retained. See [managed launcher](docs/MANAGED_LAUNCHER.md) for session cleanup and smoke options.

Successful activation records the data-root ID and update/backup IDs in the existing user-data registry while preserving storage/component entries. `COMPLETED_METADATA_PENDING` keeps a health-validated ACTIVE app and requires `--repair-update-metadata JOURNAL --installation-root DIR`; this rechecks the active update/backup and runs fresh readonly health before publishing metadata. See [update metadata](docs/UPDATE_METADATA.md).

The `fast-schema-domain`, `security-dependencies-workflow`, and `windows-artifact` jobs are the intended required PR checks. Repository branch protection or a ruleset must enforce them; workflow YAML alone cannot enforce merging rules. On 2026-10-01, `main` branch protection required all three checks with `strict=true`, as verified through the GitHub API. A tag `v*` reachable from `main` runs the Windows build, extracts and smokes the exact ZIP, then publishes those tested bytes. [PR #5](https://github.com/NextGlobal224/creator-loop/pull/5) ran all three PR jobs successfully on head `99ff244`, including `windows-artifact` on the configured `windows-2022` runner ([CI run](https://github.com/NextGlobal224/creator-loop/actions/runs/36884109471)). The tag release gate remains unverified.

Migrations `0001_initial.sql` through `0005_publication_snapshot_guards.sql` remain unchanged. Schema 4 sealed Selection events and their exact candidate sets. Schema 5 adds `0005_publication_snapshot_guards.sql`: reject INSERT OR REPLACE over historical identities, versions, review events, Package/items, approvals, Posts and observations/metrics, including conflicting unique keys. Claim type remains tied to its identity; changing FACTUAL to another type requires a new Claim. Schema 6 adds `0006_observation_metric_validation.sql`, rejecting invalid historical count/definition values and guarding new inserts without rewriting history. `initialize()` creates a new schema or upgrades supported schema 1/2/3/4/5 databases after making and validating a SQLite backup beside them. Invalid historical decisions abort migration and retain the old database and backup. The maintenance UI provides backup/update/restore controls. Corrupt-source recovery, complete failure recovery and final release migration acceptance remain open under the update contract in `docs/`.

## Boundaries

The Library UI imports and lists originals, links Sources, generates image thumbnails and opens TEXT, IMAGE_REGION and video-track TIME_RANGE Evidence. It supports Package review, manual Post records and observations. The maintenance window provides explicit backup/update/restore controls, and the Component window reviews local selections. External engine/model execution and API metric collection remain open; the app sends no posts to external platforms. The complete packaged workflow and release acceptance remain open.

The public application read path is `open_readonly()`. Publication writes go through `PublicationRepository`; `_connect_write()` is an internal adapter reserved for repositories, migrations and tests. This is an application boundary, not a sandbox against someone opening the SQLite file directly. Direct SQL may create an unsealed staging row, which cannot be approved.

The security job runs Gitleaks v8.30.1 over full Git history (`fetch-depth: 0`, `--log-opts='--all'`) and redacts findings in logs. This is a real secret scan; it still cannot undo a leaked credential, which must be rotated. The `security-dependencies-workflow` job succeeded on PR #5 head `99ff244` in the CI run linked above.
