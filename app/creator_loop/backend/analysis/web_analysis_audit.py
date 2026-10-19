"""Web finalization boundary; structural checks do not prove semantic support."""

import hashlib
import json
import re
from typing import Any

LABEL = "Giả thuyết chưa xác nhận:"


def validate_analysis_audit(
    receipt: Any,
    analysis: dict[str, Any],
    content: dict[str, Any],
    sources: list[dict[str, Any]],
) -> None:
    """Retain exact analysis and unconfirmed status across the JS/Python boundary."""
    if any(
        content.get(k) != analysis[k]
        for k in ("observations", "inferences", "direction")
    ):
        raise ValueError("Immutable analysis changed")
    fields = [
        *[
            (f"observations.{i}", o["observation"])
            for i, o in enumerate(analysis["observations"])
        ],
        *[(f"inferences.{i}", text) for i, text in enumerate(analysis["inferences"])],
        ("direction", analysis["direction"]),
    ]
    if not analysis["observations"] or any(
        not isinstance(text, str) or not text.strip() for _, text in fields
    ):
        raise ValueError("Empty immutable analysis")
    if any(
        not text.startswith(LABEL) or not text[len(LABEL) :].strip()
        for text in analysis["inferences"]
    ):
        raise ValueError("Substantive unconfirmed inference required")
    if any(
        not any(
            o["material_id"] == s["material_id"]
            and o["quote"].strip()
            and o["quote"] in s["text"]
            for s in sources
        )
        for o in analysis["observations"]
    ):
        raise ValueError("Analysis source identity mismatch")
    expected = hashlib.sha256(
        json.dumps(
            analysis, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {"analysis_sha256", "checks"}
        or receipt["analysis_sha256"] != expected
        or not isinstance(receipt["checks"], list)
        or len(receipt["checks"]) != len(fields)
    ):
        raise ValueError("Explicit immutable analysis audit missing")
    catalog = [
        line.strip() for s in sources for line in s["text"].split("\n") if line.strip()
    ]
    for (field, _), check in zip(fields, receipt["checks"], strict=True):
        hypothesis = isinstance(check, dict) and check.get("verdict") == "HYPOTHESIS"
        keys = {"field", "verdict", "premise_checks", "reason", "issues"}
        if hypothesis:
            keys.add("truth_status")
        if (
            not isinstance(check, dict)
            or set(check) != keys
            or check["field"] != field
            or check["verdict"] not in {"SUPPORTED", "EDITORIAL_ONLY", "HYPOTHESIS"}
            or check["issues"] != []
            or not isinstance(check["reason"], str)
            or not check["reason"].strip()
        ):
            raise ValueError("Analysis audit unresolved or reordered")
        if hypothesis and (
            not field.startswith("inferences.")
            or check["truth_status"] != "UNCONFIRMED"
        ):
            raise ValueError("Invalid unconfirmed hypothesis boundary")
        premises = check["premise_checks"]
        if (
            not isinstance(premises, list)
            or len(premises) > 8
            or (check["verdict"] in {"SUPPORTED", "HYPOTHESIS"} and not premises)
            or (check["verdict"] == "EDITORIAL_ONLY" and premises)
            or (field.startswith("observations.") and check["verdict"] != "SUPPORTED")
        ):
            raise ValueError("Unsupported analysis premise")
        for premise in premises:
            if (
                not isinstance(premise, dict)
                or set(premise) != {"assertion", "quote_ids", "supported"}
                or not isinstance(premise["assertion"], str)
                or not premise["assertion"].strip()
                or premise["supported"] is not True
            ):
                raise ValueError("Unsupported factual background")
            ids = premise["quote_ids"]
            if (
                not isinstance(ids, list)
                or not ids
                or any(type(q) is not int or q < 0 or q >= len(catalog) for q in ids)
                or len(ids) != len(set(ids))
            ):
                raise ValueError("Invalid factual background quote")
            if hypothesis and any(
                re.match(
                    r"^(?:Ghi chú nghiên cứu nhập thủ công\b|Source\s*:|URL\s*:|Hướng biên tập(?: đề xuất)?\s*:)",
                    catalog[q],
                    re.IGNORECASE,
                )
                for q in ids
            ):
                raise ValueError(
                    "Hypothesis background must use factual source excerpts"
                )
