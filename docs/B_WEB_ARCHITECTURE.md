# Creator Loop B Web — additive deployment baseline

The explicit B Web request of 18 October 2026 adds an authenticated, durable,
Windows-independent deployment. It does not replace Windows beta.2, its data
root, schema compatibility, or A/C acceptance. No principal data import is
permitted until the canonical backup ownership refusal has been resolved.

| Component | Existing runtime | Reuse / smallest required change | Direct source |
| --- | --- | --- | --- |
| UI | PySide6 Windows desktop | New small browser UI; Qt is not a packaged website | `frontend/creator_ui.py` |
| Project, Evidence, Claim, Draft | Python + SQLite | Reuse functions, immutable versions/seals and migrations 1–10 | `backend/creator`, `backend/knowledge`, `migrations` |
| Generation | Native owned subprocess; Workers AI SDK Worker | Extract portable contract/persistence; replace native ownership with durable jobs | `local_generation.py`, `creator_llm.py`, `sdk-stage.js` |
| Media intake | Win32 retained file handles | Cloud immutable byte intake and digest verification | `text_intake.py`, `windows_owned_file.py` |
| DB/files | Windows user root, SQLite/WAL, separate files | Durable canonical SQLite image; ephemeral working projection; separate immutable cloud objects | `database.py`, `storage_paths.py` |

Options checked against current official Cloudflare documentation and account:

* Tunnel keeps Windows as server and fails the requested availability boundary.
* D1 + R2 would require changing the synchronous transaction adapter. Current
  OAuth D1 API returns 401, R2 is not enabled and requires activation. Do not
  activate a subscription or billing to use this option.
* Python Workers + SQLite-backed Durable Objects is selected provisionally:
  real local workerd probe supports Python `sqlite3.serialize()` and DO SQL;
  Free includes SQLite DO. Preserve Python domain rules and reuse the existing
  pinned JavaScript AI SDK through Python's supported JavaScript module FFI.
  One Worker project, no new framework or external service is necessary.

## Storage and consistency

One private owner has one serialized writer Durable Object. The domain remains
one SQLite database, with the unchanged migration/checksum/FK/seal rules.
Its committed bytes reside in durable storage; filesystem copies are disposable
working projections, never an independently writable canonical root. Writes
must validate and commit a new revision durably before acknowledgment. Media
is stored as separate immutable content-addressed objects outside that domain
database, verified by size and SHA-256 before exposing references. No registered
Windows volume is silently remapped. A coordinated backup contains the exact
domain SQLite bytes and every referenced object at the same revision.

Jobs, sessions and idempotency receipts are server coordination state. They do
not replace domain history with JSON. Model work occurs outside a domain writer
transaction. Reserve each stage before sending, retain unknown outcomes and
never retry a possibly submitted inference automatically. Cancel prevents
further stages/commit; it does not promise to revoke an accepted remote call.

Both modes and all three Draft formats retain exact source versions and manual
publication approval. No cloud render/ASR/video/voice quality claim is introduced.
Server credentials never reach browser code. Protect UI/API/media, apply same
origin/CSRF checks and secure expiring sessions. Free limits fail closed; no paid
fallback. New deployment/quality/review/required CI/release evidence is required;
this architecture note is not product acceptance.

Sources checked 18 October 2026:
[Python packages](https://developers.cloudflare.com/workers/languages/python/packages/),
[Python FFI](https://developers.cloudflare.com/workers/languages/python/ffi/),
[DO Python example](https://developers.cloudflare.com/durable-objects/get-started/),
[DO pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/).
