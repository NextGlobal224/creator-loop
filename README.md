# Creator Loop — Bootstrap Gate 1

Local-first Windows desktop foundation. This is a bootstrap, not the full product. The architecture contracts are copied into `docs/`; prototype 0.5/0.6 is not used.

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

## CI and release

The `fast-schema-domain`, `security-dependencies-workflow`, and `windows-artifact` jobs are the intended required PR checks. An administrator must enable them in the repository ruleset; workflow YAML alone cannot enforce merging rules. A tag `v*` reachable from `main` runs the Windows build, extracts and smokes the exact ZIP, then publishes those tested bytes. On 2026-10-01, [PR #5](https://github.com/NextGlobal224/creator-loop/pull/5) ran all three PR jobs successfully on head `99ff244`, including `windows-artifact` on the configured `windows-2022` runner ([CI run](https://github.com/NextGlobal224/creator-loop/actions/runs/36884109471)). Required-check ruleset settings and the tag release gate remain unverified.

`0001_initial.sql` is the first migration; never edit it after a release. Current bootstrap `initialize()` supports new DB only. Upgrade runner and restore UI are future gates. The release update contract in `docs/` must be implemented before distribution to users with existing data.

## Boundaries

The minimal UI reports schema readiness. It does not ingest media, launch engines, publish posts or update installations. Package creation is implemented as a small domain transaction to test atomicity and fingerprint rules; a full publishing workflow is not yet present.

The public application read path is `open_readonly()`. Publication writes go through `PublicationRepository`; `_connect_write()` is an internal adapter reserved for repositories, migrations and tests. This is an application boundary, not a sandbox against someone opening the SQLite file directly. Direct SQL may create an unsealed staging row, which cannot be approved.

The security job runs Gitleaks v8.30.1 over full Git history (`fetch-depth: 0`, `--log-opts='--all'`) and redacts findings in logs. This is a real secret scan; it still cannot undo a leaked credential, which must be rotated. The `security-dependencies-workflow` job succeeded on PR #5 head `99ff244` in the CI run linked above.
