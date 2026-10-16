"""Fail closed on incomplete or adverse source audit of a Workers article.

The audit is a separate model call, not a factuality certificate or human review.
Every paragraph must be classified; factual passages still need exact citations.
"""

import re
from typing import Any


def validate_quality_audit(
    content: dict[str, Any], audit: Any, editor_findings: Any = ()
) -> None:
    body = content["draft_body"]
    # This provider explicitly commissions an invented opening, not a news report.
    # Reject a lost label; never add one after generation to disguise a real claim.
    if not re.match(r"^(Giả sử\b|Tình huống giả định:)", body.strip(), re.IGNORECASE):
        raise ValueError("Worker hypothetical opening lost its explicit label")
    if (
        not isinstance(audit, dict)
        or set(audit) != {"draft_body", "paragraphs", "issues", "finding_checks"}
        or audit["draft_body"] != body
        or not isinstance(audit["issues"], list)
        or audit["issues"]
    ):
        raise ValueError("Worker source audit missing, changed or reported issues")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    # Bind the receipt to the actual editing stage, including false-positive
    # leads. An empty global issues list cannot stand in for resolving them.
    checks = audit["finding_checks"]
    if (
        not isinstance(editor_findings, (list, tuple))
        or any(not isinstance(f, str) or not f.strip() for f in editor_findings)
        or not isinstance(checks, list)
        or len(checks) != len(editor_findings)
    ):
        raise ValueError("Worker editorial finding coverage missing")
    for finding, check in zip(editor_findings, checks, strict=True):
        if (
            not isinstance(check, dict)
            or set(check) != {"finding", "verdict", "reason", "segments"}
            or check["finding"] != finding
            or check["verdict"] not in {"ADDRESSED", "NOT_APPLICABLE"}
            or not isinstance(check["reason"], str)
            or not check["reason"].strip()
            or not isinstance(check["segments"], list)
            or not check["segments"]
        ):
            raise ValueError("Worker editorial finding unresolved or changed")
        seen: set[int] = set()
        for segment in check["segments"]:
            if (
                not isinstance(segment, dict)
                or set(segment) != {"index", "text"}
                or type(segment["index"]) is not int
                or segment["index"] < 0
                or segment["index"] in seen
                or not isinstance(segment["text"], str)
                or not segment["text"].strip()
                or body.count(segment["text"]) != 1
            ):
                raise ValueError("Worker editorial finding lost exact passage")
            seen.add(segment["index"])
    checked = audit["paragraphs"]
    if not isinstance(checked, list) or len(checked) != len(paragraphs):
        raise ValueError("Worker source audit omitted article paragraphs")
    claims = content["factual_claims"]
    # Recheck known source-audit misses on the app boundary too, including
    # old/cached responses. Keep immutable original output; never repair it here.
    uncited = body
    for claim in sorted(claims, key=lambda item: len(item["text"]), reverse=True):
        uncited = uncited.replace(claim["text"], " ")
    if re.search(
        r"khả năng[^.!?\n]{0,100}(?:cao|thấp) hơn"
        r"|luôn[^.!?\n]{0,100}(?:an toàn|tốt|hiệu quả) nhất",
        uncited,
        re.IGNORECASE,
    ):
        raise ValueError("Worker uncited comparative or absolute assertion")
    for position, (text, item) in enumerate(zip(paragraphs, checked, strict=True)):
        if (
            not isinstance(item, dict)
            or set(item) != {"text", "kind", "factual_passages"}
            or item["text"] != text
            or item["kind"] not in {"HYPOTHETICAL", "ADVICE", "SOURCE_FACT"}
            or not isinstance(item["factual_passages"], list)
        ):
            raise ValueError("Worker source audit coverage/classification invalid")
        if position == 0 and item["kind"] != "HYPOTHETICAL":
            raise ValueError("Worker opening was not audited as hypothetical")
        facts = item["factual_passages"]
        if item["kind"] == "SOURCE_FACT" and not facts:
            raise ValueError("Worker factual paragraph has no audited passages")
        if any(
            not isinstance(fact, str)
            or not fact.strip()
            or text.count(fact) != 1
            or not any(fact in claim["text"] for claim in claims)
            for fact in facts
        ):
            raise ValueError("Worker factual passage missing exact Claim annotation")
