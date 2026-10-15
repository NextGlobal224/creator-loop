# B Workers AI source-first generation

Opt-in TEXT-only provider for selected bounded research notes. Browser, GUI and
canonical SQLite remain local; this Worker has no Containers or R2 dependency.
Keep its app-scoped bearer key in the Cloudflare secret store and local secrets
directory with an endpoint/digest binding, never in Git, argv or request logs.

The v21 protocol makes at most four model calls with no retry or template fallback:

1. Analyze exact selected source notes, distinguishing direct quotes, the note
   author's paraphrases/proposals and tentative editorial inferences.
2. Write a complete article from that analysis. The typed fictional opening gets
   one visible label; four editorial paragraphs and any factual Claims remain
   distinct. Inline text segments carry a quote-catalog ID (-1 for advice/fiction); the app
   composes the exact same text into both the article and Claim annotation, rejecting
   invalid IDs and ambiguous repeated cited passages. Exact Unicode quotes come
   directly from selected source lines, avoiding observed model-copy punctuation
   drift. Choosing a catalog quote does not prove semantic support; audit and
   complete output QA still check that separately. This fixes the
   real v18 failure where separately generated Claims no longer appeared in the body.
   Unsupported real-world assertions cannot be disguised as fiction.
3. Edit the first article once against the exact notes, reviewing risky assertions
   and removing unsupported efficacy/source claims. This declared model step is
   automatic editing, not a manually repaired artifact or a retry loop.
4. Audit the exact final article AND analysis against the notes, covering every paragraph.
   Any reported issue, missing coverage, changed body or audited factual passage
   without a Claim annotation is rejected. Audit output identifies the immutable
   article by ASCII SHA-256 and paragraphs by index; the app reattaches original
   Vietnamese text. The model never regenerates the article through a Unicode
   schema `const`, which produced malformed/truncated output in a real pilot.
   The client additionally validates exact
   source quotes, selected material IDs, stage identity and deployment UUID before
   committing a Workflow/Draft. These are four sequential HTTP requests, with
   the existing socket90s/owned-process100s deadline per request. Completed model
   steps are passed forward once; cancellation prevents the next request. Model,
   deployment and step order are checked at every response. Requests each remain
   capped64KiB. Old monolithic/three/four-stage protocols are incompatible.

Real v11 output lost its hypothetical label, asserted unsupported chart origins
and efficacy, and omitted Claims. Its generated article is a regression fixture
(trailing whitespace normalized). Later live diagnostics found prose-only writing
instructions had been reused in a JSON editing stage. The stages now receive only
selected Project/source JSON and their own task. The AI.run binding uses the
documented messages/max_tokens/response_format fields, preserving system/user roles
and accepting its JSON-object response. Earlier Responses endpoint fields are no
longer sent to this binding. See the official model and JSON Mode documentation:
https://developers.cloudflare.com/workers-ai/models/gpt-oss-120b/ and
https://developers.cloudflare.com/workers-ai/features/json-mode/. Source analysis precedes writing, so unsupported prose is not used as the
starting point for another draft. Rejected outputs remain FAIL and never become
successful Drafts. Bounded HTTP422/502 diagnostics retain selected generated output
and public error codes locally, with credential redaction, without raw exceptions.

The model is explicitly configured, never selected as an error fallback. GPT-OSS
120B failed earlier source-quality pilots; SEA-LION27B also failed the documented
trial. GPT-OSS is explicitly selected for evaluation on the new source-first,
Vietnamese, bounded editing/audit flow; this is not a fallback. The app's native
Codex provider remains the default until a
real cloud quality pilot qualifies. Both model IDs and exact deployment UUIDs are
bound into cache/provenance; no model weights or credential manifests are shipped.

Automated audit can misclassify meaning. An empty issue list, a test or a successful
JSON/save/reopen check does not certify factuality or product quality. Read the
complete real output against exact sources before claiming automatic quality PASS
or choosing cloud by default. A manually edited Draft does not prove automatic
generation quality. Keep UNREVIEWED and manual publication approval.

Run the bounded fixture tests from the project root:

```text
node --test cloudflare/creator-ai/test/worker.test.js
```

Tests need no model call, credential or deployment. Real inference requires checking
the existing plan/free allowance first. No billing activation, purchase or upgrade
is authorized by this component. Pin the exact deployed UUID in the app config;
configuration change does not require switching the default provider.
