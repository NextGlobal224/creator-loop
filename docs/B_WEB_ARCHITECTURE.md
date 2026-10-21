# Creator Loop B Web — additive deployment baseline

The explicit20October goal now releases MANUAL TEXT/IMAGE_CAPTION B beta first,
then moves A entirely to Cloudflare. C implementation and every dependency on C
are cancelled; retain existing VIDEO script/assets/timing without new development.
Preserve source/history/shared data. The explicit21October resume removes local
cleanup as a completion condition; do not delete local data for this goal.
Guarded migration/cloud restore and A/B operation without local runtime remain
required. This amendment does not waive canonical backup/access protections.

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

Deployment/rollback can run in the manual `B Web verified cloud deploy or rollback`
GitHub workflow on protected main. It checks the exact successful main CI and
artifact digest, approved account/target, expected live version, idle jobs and
included resource headroom. Deployment uploads an inactive version, then checks
main/version/bindings/idle jobs again immediately before one traffic activation.
It reuses the cloud-built runtime and never calls AI. Cloudflare has no atomic
compare-and-swap/quiescence guarantee here: a detected change aborts activation;
an ambiguous activation is unconfirmed, with no automatic retry or rollback.
Actual official module bytes, every static asset and runtime settings must match
the trusted CI artifact before success. Rollback requires independent successful
GitHub deployment receipts for both exact UUIDs/account/target, matching CI bytes,
identical complete Python/domain dependency trees and migration/runtime settings,
plus unchanged DO namespace/model. Arbitrary historical UUIDs and schema downgrade
are refused; no DB restore occurs. Receipt artifacts must still be retained.
Credentials stay in GitHub/Cloudflare secrets, not source or a Windows runtime.
Missing/expired credentials fail closed; rotate them through the owning cloud
account, not a laptop backend. Actual workflow/deployment/rollback evidence must
still be collected before claiming those operations passed.

The 20 October scope requires B operations entirely on Cloudflare. Windows is
only a browser client; no local backend, canonical SQLite/media, scheduler,
watcher, Tunnel or VS Code is needed for Web operations. Source/build tools on a
developer machine are not part of the serving path. Python DO alarms own job
wakeups after a browser closes; ephemeral Worker filesystem projections are not
Windows data. Login/operator secrets remain Cloudflare bindings; entering the
operator credential on Web does not require a local credential file.

Named manual cloud backups pin the committed DB image and immutable object
manifest in the same DO. At most three snapshots are retained, with no silent
deletion; referenced media are never garbage-collected. Download reconstructs
the existing backup format. Restore uses the existing explicit confirmation,
revision check and atomic validation path. These snapshots protect against
application-state replacement, not loss of the Cloudflare account/namespace.
Their isolated restore must be verified on the deployed version before claiming
acceptance. They do not prove migration or backup of the shared Windows DB.

Principal migration remains guarded: inventory shared A/B/C ownership first,
then obtain a consistent full backup including media, stage an ID-preserving
merge with a durable import manifest, verify relations/hashes by isolated
restore, cut over reads/writes and fence old B writers before deleting only
proven B-exclusive migrated data. Whole-DB restore must not overwrite newer
cloud records. Shared or unresolved local data are retained; a local rename is
not retirement. No principal import, deletion or fence is implied by this note.

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

The Web analysis profile reads the pinned provider's final JSON channels
(`choices[0].message.content` before `response`), with complete single-choice
checks. It validates the immutable analysis and separates only the explicit
analysis audit receipt from the fourth response. Hidden reasoning is never
exported. A rejected parsed JSON response and fixed diagnostic fields remain
in the authenticated job for diagnosis; they do not authorize a retry or a
Draft commit. Fixture channel coverage does not prove live output quality.
An inference must contain a proposition after its explicit unconfirmed label;
an empty array is preferable to a bare label. Web stage instructions distinguish
source content from tool applicability and require an actual textual change for
REWRITE. These instructions preserve the existing fail-closed edit/audit guards;
they do not repair a model response or establish semantic acceptance.
The Web analysis binding requests medium reasoning from the same pinned Worker
model, with the existing token cap and one-call boundary. Live low-effort output
had repeatedly invented efficacy/confirmation-count rules; a full immutable
audit rejected it. This stage setting does not alter the Codex agent model or
effort, the Windows Worker deployment, or the remaining stage settings.

The contract permits explicitly unconfirmed editorial hypotheses. The separate
analysis audit can classify only `inferences.N` as `HYPOTHESIS`, retaining
`truth_status:UNCONFIRMED`, checked factual background and no open issue. It
does not establish the hypothesized effect or create a factual citation/review.
Missing or invented background, tool applicability and institutional attribution
remain unresolved; observations and direction cannot use this category.
Analysis schema descriptions request an editorial content plan rather than a
reader verification procedure. Audit leads must be compared with the actual
immutable final text: a voluntary invitation without a promised result does
not require proof of success, while hidden capability/benefit assertions do.
The fifth live TEXT chain exposed both an unsupported direction and audit false
positives; it remains failed. Offline category/schema tests do not establish
native acceptance or new output quality.

The sixth live attempt still invented a tool-based direction. Exact request-hash
replay showed that the SDK's textual prompt schema lacked the Web field
descriptions present in the native decoding schema, while accumulated system
instructions still asked for a practical reader direction. Web analysis now uses
one coherent content-planning instruction and synchronizes the SDK prompt's
schema suffix with the native schema, preserving all selected source bytes.
No response is rewritten. The auditor also distinguishes a later reference to
an explicitly fictional opening from a claim about a real observed event;
real-world premises and full article development remain checked separately.
Only the opening can retain the HYPOTHETICAL segment classification.

Sources checked 18 October 2026:
[Python packages](https://developers.cloudflare.com/workers/languages/python/packages/),
[Python FFI](https://developers.cloudflare.com/workers/languages/python/ffi/),
[DO Python example](https://developers.cloudflare.com/durable-objects/get-started/),
[DO pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/).
# Extractive source observations and bounded audit (Web)

Web analysis selects relevant exact factual passages for `observations`: both
`quote` and `observation` preserve the selected passage, excluding catalog
metadata/editorial instructions. This is an extractive source ledger, not a
claim that a paraphrase or the entire source has been verified. The model still
generates its editorial plan and optional unconfirmed hypotheses; their full
premises remain subject to the separate immutable analysis audit. The plan has
a 1,200-character output bound; observations are not limited to three items.
Writing and editing remain generative and must meet the full quality contract.

The Web fourth-call ceiling is 6,000 output tokens, retaining one call and all
article/analysis checks. Final-channel diagnostics record only a recognized
finish reason, UTF-8 content length and strict JSON parse status, never final
text or hidden reasoning. Incomplete JSON remains a failure. These changes are
not quality or release acceptance; the failed pilot history is retained.

Web editing additionally constrains each expanded native REWRITE replacement
against the exact original segment with JSON Schema `not`/`const`, and binds the
same constraint into the textual SDK schema. Original receipts, mandatory edits,
source checks, whitespace-normalized no-op rejection and final audit remain.
This prevents an exact-copy rewrite in the declared schema, without repairing
an output. Remote decoding support for the new constraint still needs live
verification; offline schema validation alone cannot establish compatibility.

The ninth live run verified native execution of the exact-copy exclusion; it
still failed article quality and created no Draft. Its exact input capture
showed 26,946/49,712 writing message bytes and 74,869/110,028 editing message
bytes were duplicated literal schema text. Web TEXT writing and all Web editing
now replace only that recognized textual suffix with a clearly labelled readable
shape contract. The full authoritative native `response_format` schema, source
pack, SDK/domain validation, call/output caps and 5,000-Neuron forecast remain.
Analysis/audit schemas and caption/VIDEO writing stay unchanged. The readable
writing contract preserves the existing role purposes and asks for brief-bound
development without a fixed segment or word minimum. Less duplicate text is
not evidence of better prose or proportional token savings; live quality remains
unverified when Free quota is below the unchanged complete-chain forecast.
