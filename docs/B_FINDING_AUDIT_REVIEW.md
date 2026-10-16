# B: retain editorial findings through source audit

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
