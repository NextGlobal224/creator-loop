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

To open the current Library UI, run `python -m creator_loop` with `PYTHONPATH=app`. The three import buttons accept TEXT, MP4 video, and PNG/JPEG images. The app copies bytes into the user-data originals store and lists the imported files. Format detection checks stored signatures; broad codec compatibility remains later Gate 2 work.

For a TEXT original, select its row and choose **Tạo Evidence Text**. Enter start/end positions on the NFC text snapshot and inspect the excerpt preview before saving. Select the saved Evidence row and choose **Mở Evidence Text** to verify and display that exact excerpt again. To correct the latest TEXT Evidence Version, select it and choose **Sửa Evidence Text**; enter a new range, actor and reason. The old version remains in the table and can still be reopened. The status shows how many Claim Versions still refer to an older version and need review. The initial create/reopen UI slice passed the required CI checks in PR #10; the correction UI passed the required checks in PR #20.

Select an original and choose **Nguồn của Asset** to inspect its linked Sources. Enter a platform to create a Source, or choose an existing Source to associate it with another Asset. URL, external ID and publisher are optional; unknown rights and relationship stay explicit. The dialog records provenance supplied by the user and does not infer it from the filename.

For a PNG/JPEG original, choose **Tạo Evidence Image**. Set normalized X/Y/width/height coordinates, inspect the cropped preview and enter the observation. Select its Evidence row and choose **Mở Evidence Image** to verify the recorded original digest and reopen that region. Image decoding uses the bundled Qt runtime; images above 40 MiB or 80 million pixels are outside the current local decode budget.

To correct an IMAGE_REGION Evidence Version, select its row and choose **Sửa Evidence Image**. Adjust the region or observation, enter the actor and reason, and inspect the crop preview. The new human version stays on the selected version's verified original or thumbnail anchor; the old version and Claim links remain available for review. Only the latest live version can be corrected.

For an MP4 original with a decodable video track, choose **Tạo Evidence Video**. Set the start/end in milliseconds, play that segment and enter the observation. **Mở Evidence Video** verifies the same original and replays the saved range. For an MP4 with a decodable audio track, choose **Tạo Evidence Audio**, listen to the range, choose speech or other sound, and enter the human observation. **Mở Evidence Audio** verifies the recorded original and replays its saved range. The audio UI passed the required checks in PR #22; transcription is still missing.

To correct a saved TIME_RANGE, select its Evidence row and choose **Sửa Evidence Video** or **Sửa Evidence Audio**. The dialog loads the saved range and observation; audio type stays fixed. Enter the new range/content, actor and reason, then compare the saved original segment before saving. The service appends a human version on the latest verified MP4 original anchor and track with a CORRECT event targeting the old version. It checks recorded/decoded duration and real frames/audio samples, preserves old versions and Claim citations, and leaves the new version pending review. Both versions can be reopened; the status reports Claim Versions needing review.

Select an IMAGE original and choose **Tạo thumbnail Image** to generate a PNG derivative with its own processing run. The task table shows each run's status and derived file path; a failed run stays visible without changing the original. Other media processing tasks and cancellation are later Gate 2 work.

To create Evidence from a generated thumbnail, select its successful task row and choose **Evidence từ thumbnail**. Select a region in the decoded thumbnail; the saved Evidence anchors that derived file. Reopening verifies the thumbnail's own digest, even if the original later becomes unavailable. The service and UI passed required CI in PR #23 and PR #24 respectively.

Select an Evidence Version and use its **Mở Evidence** button to compare the saved source. Then choose **Review Evidence**, select ACCEPT, REJECT, REQUEST_CHANGES or REOPEN, and enter the reviewer and reason when required. The Review column is derived from append-only events. ACCEPT verifies the saved anchor again, and review of an older version after correction is rejected. The service and UI passed required CI in PR #25 and PR #26 respectively.

The Claim service creates Claim Versions with exact SUPPORTS, CONTRADICTS and CONTEXT links to Evidence Versions. A sealed version preserves its statement and citations; corrections append a later version. Its support projection counts current ACCEPT links and stale links after Evidence correction; it does not approve publication. Claim UI and the editorial support threshold are still open.

## CI and release

The `fast-schema-domain`, `security-dependencies-workflow`, and `windows-artifact` jobs are the intended required PR checks. Repository branch protection or a ruleset must enforce them; workflow YAML alone cannot enforce merging rules. On 2026-10-01, `main` branch protection required all three checks with `strict=true`, as verified through the GitHub API. A tag `v*` reachable from `main` runs the Windows build, extracts and smokes the exact ZIP, then publishes those tested bytes. [PR #5](https://github.com/NextGlobal224/creator-loop/pull/5) ran all three PR jobs successfully on head `99ff244`, including `windows-artifact` on the configured `windows-2022` runner ([CI run](https://github.com/NextGlobal224/creator-loop/actions/runs/36884109471)). The tag release gate remains unverified.

`0001_initial.sql` remains unchanged; `0002_claim_citation_seal.sql` seals Claim citation sets. `initialize()` creates a new schema or upgrades a version 1 database after making and validating a SQLite backup beside it. The full updater, recovery UI, and release migration checks remain future gates under the update contract in `docs/`.

## Boundaries

The Library UI imports and lists originals, links Sources, generates image thumbnails and opens TEXT, IMAGE_REGION and video-track TIME_RANGE Evidence. It does not yet launch external media/model engines, publish posts or update installations. Package creation is implemented as a small domain transaction to test atomicity and fingerprint rules; a full publishing workflow is not yet present.

The public application read path is `open_readonly()`. Publication writes go through `PublicationRepository`; `_connect_write()` is an internal adapter reserved for repositories, migrations and tests. This is an application boundary, not a sandbox against someone opening the SQLite file directly. Direct SQL may create an unsealed staging row, which cannot be approved.

The security job runs Gitleaks v8.30.1 over full Git history (`fetch-depth: 0`, `--log-opts='--all'`) and redacts findings in logs. This is a real secret scan; it still cannot undo a leaked credential, which must be rotated. The `security-dependencies-workflow` job succeeded on PR #5 head `99ff244` in the CI run linked above.
