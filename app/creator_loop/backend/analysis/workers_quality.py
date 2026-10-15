"""Fail closed on incomplete or adverse source audit of a Workers article.

The audit is a separate model call, not a factuality certificate or human review.
Every paragraph must be classified; factual passages still need exact citations.
"""

import re
from typing import Any


def validate_quality_audit(content: dict[str, Any], audit: Any) -> None:
    body = content["draft_body"]
    # This provider explicitly commissions an invented opening, not a news report.
    # Reject a lost label; never add one after generation to disguise a real claim.
    if not re.match(r"^(Giả sử\b|Tình huống giả định:)", body.strip(), re.IGNORECASE):
        raise ValueError("Worker hypothetical opening lost its explicit label")
    if (
        not isinstance(audit, dict)
        or set(audit) != {"draft_body", "paragraphs", "issues"}
        or audit["draft_body"] != body
        or not isinstance(audit["issues"], list)
        or audit["issues"]
    ):
        raise ValueError("Worker source audit missing, changed or reported issues")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    checked = audit["paragraphs"]
    if not isinstance(checked, list) or len(checked) != len(paragraphs):
        raise ValueError("Worker source audit omitted article paragraphs")
    claims = content["factual_claims"]
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
