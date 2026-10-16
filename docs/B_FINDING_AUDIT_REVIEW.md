# B: generation inputs, edit decisions and audit findings

## v28 candidate: change writing/editing, cloud verification not authorized

This is an offline candidate, not a claim that B output quality is fixed. V27
only strengthened audit receipt validation. V28 changes generation inputs and
editing decisions as well; it retains the existing SDK, model and connection.

The original writing/editing HTTP bodies were checked against their saved
SHA256 bindings. The exact frozen v25 index/quality source was recovered from
historical Git content at b76b27a by matching the recorded file hashes (including
the original CRLF bytes); the SDK source still matches its frozen hash. Native
SDK messages were reconstructed from that source and the original input using
a fake AI binding. They are reconstructed messages, not an archived wire capture.

Observed: the quote catalog was concatenated into system instructions. Writing
also received generated observations/inferences alongside the notes. The
analysis proposed handbook applicability without the steps being available.
Writing introduced unsupported comparisons and applicability; editing kept them
despite a free-form finding about handbook scope. The exact original model's
internal reason is unavailable; input authority confusion, inference reuse and
pressure to fill a word target are hypotheses, not proven causal attribution.

V28 addresses these concrete input/contract weaknesses:

- Quote text lives in the user-data evidence pack, preserving exact snapshots,
  catalog IDs, URLs and scope. Source content is absent from system policy.
- Generated observation paraphrases and inferences are excluded from writer and
  editor evidence. The proposed direction is retained only as an explicitly
  unverified editorial plan; a user's selected angle remains separate.
- Writing chooses a quote ID before wording a factual segment. Source scope
  limits and missing information take priority over a word-count target; advice
  cannot silently assert efficacy, applicability or an outcome.
- Editing must first classify **every** original segment, including uncited
  advice, as source-supported, suggestion, fictional opening or insufficient.
  It identifies exact quote IDs and decides KEEP, REWRITE or REMOVE, mapping the
  decision to the final segments. An insufficient assertion cannot be kept.
  A declared rewrite/removal that retains the exact old passage fails closed.
  A replacement may narrow to what an exact quote supports or explicitly leave
  the missing information unknown. Every new segment needs a source decision.

There is no keyword-list addition, fixture-specific rewrite, silent correction,
new model call, retry, fallback, dependency, database change or approval event.
The app cache/prompt identity is v28; original outputs and history are retained.

Offline checks prove evidence placement, input filtering, decision coverage,
exact retained/changed text and source-ID mapping. The original v25 output is
not repaired. Analyst-labelled decision fixtures are not new model outputs;
neither they nor a rejected legacy response prove better writing or correct
support classification. The editor can still misclassify a factual assertion
as a suggestion, or misunderstand a quote. General entailment, natural prose,
ending quality and the larger editing JSON fitting its budget remain unverified.

### Proposed minimum cloud verification — NOT AUTHORIZED

1. Pin the reviewed candidate commit/source hashes, v28 prompt/cache identity,
   workers-ai-provider4.0.0, AI7.0.142 and @cf/openai/gpt-oss-120b. Only after
   explicit additional authority: verify existing Free quota, deploy the pinned
   candidate and bind its actual Worker UUID to the app. No billing changes.
   V27 stays historically cloud-unverified; this is a distinct v28 candidate.
2. Reuse the same two notes (1496 characters), Project purpose/audience/style,
   exact material versions and selected-input hash from the failed SDK pilot.
   Send only those bounded texts; no Facebook session, full DB, scan or media.
3. Propose **one** app-backend end-to-end attempt, **at most4 model calls**:
   analysis, writing, editing, source audit. Existing max-output caps stay
   2600/3000/3000/2200; no retry, fallback or token-cap increase. Four calls are
   the minimum for the unchanged actual app flow; a three-call script reusing
   old analysis would not prove that full flow. This proposal consumes nothing
   and does not reset the spent first-round1/1pilot/4/4calls or hook2/2 budget.
4. Capture inputs, model/Worker identity, all stage outputs, decisions/citations,
   usage and exact Draft save/reopen. Read **every clause** against the notes,
   including uncited advice and the analysis; every factual assertion needs
   adequate exact evidence. Unsupported details must be absent or clearly
   unresolved. No institutional endorsement, efficacy, superiority or practical
   steps may be invented. Check coherent natural Vietnamese, no padding or
   internal QA leakage, and an actual message asking for the original link,
   date/context. No ACCEPT/APPROVE or publication.
5. Stop immediately on quota/capacity/transport/schema/truncation/cancel failure,
   an invalid edit decision or adverse source audit. The app's automated
   failures stop before later stages; semantic review happens on the captured
   output, without claiming an unsupported manual pause between model calls.
   After one attempt, stop on any source/style failure, or after the four-call
   cap even if it passes. No second round or repair call is implied.

A successful future pilot would establish only this frozen case's quality and
app persistence under its actual version. It would not certify every B input,
enable automatic publishing or replace independent GitHub review. Until new
authority is granted, no Cloudflare request, deployment or model call is made.

## v27 historical audit-only correction

16 October 2026. Offline correction only; cloud quality remains FAIL.

The failed SDK pilot used Worker80759b8e, GPT-OSS120B, workers-ai-provider4.0.0
and AI7.0.142. Exact original input/output and Draft44e79e48 remain unchanged.
Analysis proposed using UNESCO handbook exercises despite having only an
introduction, and labelled the proposed efficacy as unconfirmed. Writing then
introduced an unsupported probability comparison, absolute safety comparison
and practical handbook steps, without source quote IDs. Editing retained these
passages and changed the requested ending into an offer by an unspecified
“we”. These are separate source and style failures, not encoding defects.

The audit classified the probability/safety passages as ADVICE and returned no
issues. It checked cited segments as SOURCE_FACT, but that did not force it to
detect factual additions inside uncited advice. Editor findings included both
a real handbook-scope concern and false positives about the commissioned
fictional opening and a supported handbook description. A finding is a lead,
not a verdict. Previously the prompt supplied these leads, while the response
schema allowed an empty audit to discard all of them without explanation.

The small correction requires a disposition for every editor finding in its
original order: ADDRESSED, NOT_APPLICABLE or UNRESOLVED, with a concrete reason
and relevant final segment IDs. The Worker attaches original finding text and
immutable segment text itself. Missing, duplicated, reordered, empty or
out-of-range coverage fails closed; an unresolved finding becomes an audit
issue even if the model's other issue lists are empty. The app compares that
receipt against the separately received editing output, checks exact passages,
and refuses unresolved findings. Provider prompt/cache version is v27; legacy
cached audit envelopes cannot silently qualify under the new boundary.

This adds no calls, retries, fallback, dependencies, keyword filters, database
schema or review events. Existing v26 narrow guards remain separate. The new
regression reproduces the saved v25 edited article and rejects missing finding
coverage before those guards run. Synthetic addressed/false-positive cases
exercise receipt validation only; they are not new model output or evidence
that the real failed Draft was corrected.

This does **not** implement semantic entailment verification. A model can still
miss an assertion or incorrectly mark a finding addressed. It does not repair
the original Draft, prove a better ending, or establish cloud quality. SDK
pilot budget remains spent1/1 and4/4calls. No new cloud calls or deployment are
authorized in this round. All output remains UNREVIEWED; independent GitHub
review and manual publication approval remain separate requirements.

## Bounded GPT Researcher survey

Read four files at commit3582a6841fe7dc801e2a34e34ee408641aea9bce. No clone,
installation, execution or code import was needed.

- [context/compression.py](https://github.com/assafelovic/gpt-researcher/blob/3582a6841fe7dc801e2a34e34ee408641aea9bce/gpt_researcher/context/compression.py):
  the small-input path preserves document content and carries a source/URL in
  document metadata. This helps retain attribution through context preparation.
- [skills/context_manager.py](https://github.com/assafelovic/gpt-researcher/blob/3582a6841fe7dc801e2a34e34ee408641aea9bce/gpt_researcher/skills/context_manager.py):
  selects/retrieves context for report writing. This supplies research context;
  it does not by itself certify support for each generated assertion.
- [skills/writer.py](https://github.com/assafelovic/gpt-researcher/blob/3582a6841fe7dc801e2a34e34ee408641aea9bce/gpt_researcher/skills/writer.py):
  abstains when research context is empty, then passes nonempty context to the
  report generator. B's failing input already contains nonempty selected notes.
- [prompts.py](https://github.com/assafelovic/gpt-researcher/blob/3582a6841fe7dc801e2a34e34ee408641aea9bce/gpt_researcher/prompts.py):
  asks for claim-level inline citations, references and use of supplied sources.
  These are generation instructions, not a deterministic entailment check.

Conclusion limited to the inspected path: attribution/context retention is
useful, but does not close B's gap of unsupported additions hidden inside
advice or silent loss of editor findings. B already binds exact source quotes
to immutable text. Stop the survey here; retain the existing repo/SDK/Cloudflare
connection. No assertion about all other GPT Researcher workflows is made.

REA was not used: saved stage outputs and source answered when the additions
appeared and how the audit accepted them. There is no remaining runtime question
in this bounded correction requiring another tool.
