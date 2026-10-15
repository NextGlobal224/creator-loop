# B Workers AI source audit

Opt-in text-only provider for selected bounded research notes. The app's browser,
SQLite and media remain local. This Worker has no Containers or R2 dependency.
It accepts an app-scoped bearer key in the Cloudflare secret store; never put a key
in the repository, config or request logs. Deploying source does not approve
generation costs or switch the app's selected provider.

The v11 real pilot returned valid JSON and preserved the editor's exact article,
but lost the hypothetical label, asserted an unsupported chart origin and efficacy,
and returned an empty factual annotation. `tests/fixtures/workers_b_v11_failed.txt`
preserves that generated article for regression (trailing whitespace normalized). It contains no user session/data.

The v12 protocol makes at most four model calls, with no retry or template fallback:
prose, editing, exact-copy annotation, and a separate source audit. The Worker rejects
an invented opening that loses `Giả sử`, audit issues, missing/reordered paragraphs,
changed article text, and audited factual passages missing a Claim annotation.
The client must also validate the four-stage envelope, exact source quotes and audit
before committing a Workflow/Draft. An old three-stage Worker is incompatible with
the v12 client; pin the exact deployment UUID only after an authorized deployment.

The audit is another model call and can misclassify meaning. Tests and an empty
audit issue list are not a factuality certificate, human review, or product quality
acceptance. Read the complete real pilot against the exact selected notes before
claiming automatic quality PASS or choosing this provider by default. Retain
UNREVIEWED and manual publication approval. A manually edited Draft does not prove
automatic generation quality.

Run the bounded source tests from the project root:

```text
node --test cloudflare/creator-ai/test/worker.test.js
```

No package installation, inference or deployment is needed for those tests. Real
inference requires checking the current plan/free allocation first and explicit
bounded inputs. Do not enable billing, purchase or upgrade to run a pilot.
