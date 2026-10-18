"""Fail closed on incomplete or adverse source audit of a Workers article.

The audit is a separate model call, not a factuality certificate or human review.
Every paragraph must be classified; factual passages still need exact citations.
"""

import re
import unicodedata
from typing import Any

ARTICLE_REQUIREMENT_IDS = [
    "PROJECT_NO_UNSOURCED_RULE",
    "PROJECT_CLOSING_MESSAGE",
    "PROJECT_NONREPETITIVE_DEVELOPMENT",
]


def requires_hypothetical_opening(purpose: str, direction: str = "") -> bool:
    """Compatibility profile for the exact historical brief, not a topic guess."""
    return not direction.strip() and unicodedata.normalize(
        "NFC", purpose
    ).strip().lower() == (
        "giúp người đọc nhận thức đúng về sự thật trước khi chia sẻ ảnh chụp màn hình"
    )


def worker_audit_findings(edit_stage: Any) -> list[str]:
    """Bind app checks to original edit operations and mandatory task coverage.

    These are checklist leads, not a declaration that every article violates them.
    Keep model findings, including insufficient notes omitted from its summary.
    """
    if (
        not isinstance(edit_stage, dict)
        or edit_stage.get("stage") != "editing"
        or edit_stage.get("task_requirements") != ARTICLE_REQUIREMENT_IDS
        or not isinstance(edit_stage.get("value"), dict)
    ):
        raise ValueError("Worker article task coverage missing or changed")
    value = edit_stage["value"]
    findings, edits = value.get("source_issues"), value.get("edits")
    if (
        not isinstance(findings, list)
        or any(not isinstance(f, str) or not f.strip() for f in findings)
        or not isinstance(edits, list)
        or any(not isinstance(e, dict) for e in edits)
    ):
        raise ValueError("Worker original edit findings missing")
    notes = [e.get("note") for e in edits if e.get("support") == "INSUFFICIENT"]
    if any(not isinstance(note, str) or not note.strip() for note in notes):
        raise ValueError("Worker insufficient-support explanation missing")
    return list(dict.fromkeys([*findings, *notes, *ARTICLE_REQUIREMENT_IDS]))


def validate_quality_audit(
    content: dict[str, Any],
    audit: Any,
    editor_findings: Any = (),
    *,
    require_hypothetical: bool = True,
) -> None:
    body = content["draft_body"]
    # This provider explicitly commissions an invented opening, not a news report.
    # Reject a lost label; never add one after generation to disguise a real claim.
    if require_hypothetical and not re.match(
        r"^(Giả sử\b|Tình huống giả định:)", body.strip(), re.IGNORECASE
    ):
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
        if finding.startswith("PROJECT_") and check["verdict"] != "ADDRESSED":
            raise ValueError("Worker Project requirement unresolved or waived")
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
        if finding in {
            "PROJECT_NO_UNSOURCED_RULE",
            "PROJECT_NONREPETITIVE_DEVELOPMENT",
        }:
            ordered = sorted(check["segments"], key=lambda segment: segment["index"])
            if [segment["index"] for segment in ordered] != list(
                range(len(ordered))
            ) or re.sub(
                r"\s+", " ", " ".join(segment["text"] for segment in ordered)
            ).strip() != re.sub(r"\s+", " ", body).strip():
                raise ValueError("Worker global audit omitted immutable segments")
        if finding == "PROJECT_NONREPETITIVE_DEVELOPMENT" and (
            check["verdict"] != "ADDRESSED"
            or any(
                not any(segment["text"] in paragraph for segment in check["segments"])
                for paragraph in paragraphs[1:]
            )
        ):
            raise ValueError("Worker development check omitted editorial paragraphs")
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
        labelled = bool(
            re.match(r"^(Giả sử\b|Tình huống giả định:)", text, re.IGNORECASE)
        )
        if (item["kind"] == "HYPOTHETICAL" and (position != 0 or not labelled)) or (
            labelled and item["kind"] != "HYPOTHETICAL"
        ):
            raise ValueError("Worker hypothetical passage lost label or classification")
        if position == 0 and require_hypothetical and item["kind"] != "HYPOTHETICAL":
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
