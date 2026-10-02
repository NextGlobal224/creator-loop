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

To open the current Library UI, run `python -m creator_loop` with `PYTHONPATH=app`. The three import buttons accept TEXT, MP4 video, and PNG/JPEG images. The app copies bytes into the user-data originals store and lists the imported files. Format detection checks stored signatures; broad codec compatibility and Evidence review in the UI are later Gate 2 work.

For a TEXT original, select its row and choose **Tạo Evidence Text**. Enter start/end positions on the NFC text snapshot and inspect the excerpt preview before saving. Select the saved Evidence row and choose **Mở Evidence Text** to verify and display that exact excerpt again. To correct the latest TEXT Evidence Version, select it and choose **Sửa Evidence Text**; enter a new range, actor and reason. The old version remains in the table and can still be reopened. The status shows how many Claim Versions still refer to an older version and need review. The initial create/reopen UI slice passed the required CI checks in PR #10; the correction UI passed the required checks in PR #20.

Select an original and choose **Nguồn của Asset** to inspect its linked Sources. Enter a platform to create a Source, or choose an existing Source to associate it with another Asset. URL, external ID and publisher are optional; unknown rights and relationship stay explicit. The dialog records provenance supplied by the user and does not infer it from the filename.

For a PNG/JPEG original, choose **Tạo Evidence Image**. Set normalized X/Y/width/height coordinates, inspect the cropped preview and enter the observation. Select its Evidence row and choose **Mở Evidence Image** to verify the recorded original digest and reopen that region. Image decoding uses the bundled Qt runtime; images above 40 MiB or 80 million pixels are outside the current local decode budget.

For an MP4 original with a decodable video track, choose **Tạo Evidence Video**. Set the start/end in milliseconds, play that segment and enter the observation. **Mở Evidence Video** verifies the same original and replays the saved range. For an MP4 with a decodable audio track, choose **Tạo Evidence Audio**, listen to the range, choose speech or other sound, and enter the human observation. **Mở Evidence Audio** verifies the recorded original and replays its saved range. The audio UI passed the required checks in PR #22; transcription is still missing.

Select an IMAGE original and choose **Tạo thumbnail Image** to generate a PNG derivative with its own processing run. The task table shows each run's status and derived file path; a failed run stays visible without changing the original. Other media processing tasks and cancellation are later Gate 2 work.

To create Evidence from a generated thumbnail, select its successful task row and choose **Evidence từ thumbnail**. Select a region in the decoded thumbnail; the saved Evidence anchors that derived file. Reopening verifies the thumbnail's own digest, even if the original later becomes unavailable. The service and UI passed required CI in PR #23 and PR #24 respectively.

The Evidence review service on the current branch appends ACCEPT, REJECT, REQUEST_CHANGES and REOPEN events for the latest Evidence Version. ACCEPT verifies the saved anchor again; the current review action is derived from event history. The review UI is still missing.

## CI and release

The `fast-schema-domain`, `security-dependencies-workflow`, and `windows-artifact` jobs are the intended required PR checks. Repository branch protection or a ruleset must enforce them; workflow YAML alone cannot enforce merging rules. On 2026-10-01, `main` branch protection required all three checks with `strict=true`, as verified through the GitHub API. A tag `v*` reachable from `main` runs the Windows build, extracts and smokes the exact ZIP, then publishes those tested bytes. [PR #5](https://github.com/NextGlobal224/creator-loop/pull/5) ran all three PR jobs successfully on head `99ff244`, including `windows-artifact` on the configured `windows-2022` runner ([CI run](https://github.com/NextGlobal224/creator-loop/actions/runs/36884109471)). The tag release gate remains unverified.

`0001_initial.sql` is the first migration; never edit it after a release. Current bootstrap `initialize()` supports new DB only. Upgrade runner and restore UI are future gates. The release update contract in `docs/` must be implemented before distribution to users with existing data.

## Boundaries

The Library UI imports and lists originals, links Sources, generates image thumbnails and opens TEXT, IMAGE_REGION and video-track TIME_RANGE Evidence. It does not yet launch external media/model engines, publish posts or update installations. Package creation is implemented as a small domain transaction to test atomicity and fingerprint rules; a full publishing workflow is not yet present.

The public application read path is `open_readonly()`. Publication writes go through `PublicationRepository`; `_connect_write()` is an internal adapter reserved for repositories, migrations and tests. This is an application boundary, not a sandbox against someone opening the SQLite file directly. Direct SQL may create an unsealed staging row, which cannot be approved.

The security job runs Gitleaks v8.30.1 over full Git history (`fetch-depth: 0`, `--log-opts='--all'`) and redacts findings in logs. This is a real secret scan; it still cannot undo a leaked credential, which must be rotated. The `security-dependencies-workflow` job succeeded on PR #5 head `99ff244` in the CI run linked above.
