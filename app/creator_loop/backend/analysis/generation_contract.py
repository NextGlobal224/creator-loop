"""Portable, strict generation contract shared by desktop and Web. No engines."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from creator_loop.shared.workflow import ResearchMaterial


def response_schema(material_ids: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "observations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "material_id": {"type": "string", "enum": list(material_ids)},
                        "quote": {"type": "string"},
                        "observation": {"type": "string"},
                    },
                    "required": ["material_id", "quote", "observation"],
                    "additionalProperties": False,
                },
            },
            "inferences": {"type": "array", "items": {"type": "string"}},
            "direction": {"type": "string"},
            "draft_body": {"type": "string"},
            "factual_claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "material_ids": {
                            "type": "array",
                            "items": {"type": "string", "enum": list(material_ids)},
                        },
                        "source_quote": {"type": "string"},
                    },
                    "required": ["text", "material_ids", "source_quote"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "observations",
            "inferences",
            "direction",
            "draft_body",
            "factual_claims",
        ],
        "additionalProperties": False,
    }


def validate_response(
    value: Any, materials: Sequence[ResearchMaterial]
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "observations",
        "inferences",
        "direction",
        "draft_body",
        "factual_claims",
    }:
        raise ValueError("LLM response fields invalid; no template fallback")
    sources = {m.material_id: m.text for m in materials}
    for key in ("direction", "draft_body"):
        if (
            not isinstance(value[key], str)
            or not value[key].strip()
            or len(value[key]) > 12000
        ):
            raise ValueError("LLM returned empty/oversized content")
    body = value["draft_body"]
    if re.search(
        r"\[(?:insert|placeholder|điền|tên nguồn)|lorem ipsum|<think>|</think>|```",
        body,
        re.I,
    ):
        raise ValueError("LLM returned placeholder/reasoning markup")
    if any(mid in body for mid in sources):
        raise ValueError("Internal material IDs are not article content")
    if len(body.split()) < 90:
        raise ValueError("LLM returned an incomplete Draft")
    paragraphs = [
        re.sub(r"\s+", " ", p).strip().casefold()
        for p in body.split("\n\n")
        if len(p.strip()) > 40
    ]
    if len(paragraphs) != len(set(paragraphs)):
        raise ValueError("LLM returned repeated paragraphs")
    if (
        not isinstance(value["observations"], list)
        or not 1 <= len(value["observations"]) <= 40
    ):
        raise ValueError("LLM must provide source-bound observations")
    for observation in value["observations"]:
        if not isinstance(observation, dict) or set(observation) != {
            "material_id",
            "quote",
            "observation",
        }:
            raise ValueError("LLM observation malformed")
        text = (
            sources.get(observation["material_id"])
            if isinstance(observation["material_id"], str)
            else None
        )
        quote = observation["quote"]
        if (
            text is None
            or not isinstance(quote, str)
            or not quote.strip()
            or quote not in text
        ):
            raise ValueError("LLM quote does not match the exact input")
        if (
            not isinstance(observation["observation"], str)
            or not observation["observation"].strip()
        ):
            raise ValueError("LLM observation missing")
    if not isinstance(value["inferences"], list) or any(
        not isinstance(s, str) for s in value["inferences"]
    ):
        raise ValueError("LLM inferences malformed")
    if (
        not isinstance(value["factual_claims"], list)
        or len(value["factual_claims"]) > 30
    ):
        raise ValueError("LLM factual claim list malformed")
    for claim in value["factual_claims"]:
        if not isinstance(claim, dict) or set(claim) != {
            "text",
            "material_ids",
            "source_quote",
        }:
            raise ValueError("LLM factual claim malformed")
        if (
            not isinstance(claim["text"], str)
            or not claim["text"].strip()
            or body.count(claim["text"]) != 1
        ):
            raise ValueError("LLM factual claim must locate one exact Draft passage")
        ids = claim["material_ids"]
        if (
            not isinstance(ids, list)
            or not ids
            or any(not isinstance(mid, str) for mid in ids)
            or len(set(ids)) != len(ids)
            or any(mid not in sources for mid in ids)
        ):
            raise ValueError("LLM cited unknown or duplicate input")
        quote = claim["source_quote"]
        if (
            not isinstance(quote, str)
            or not quote.strip()
            or not all(quote in sources[mid] for mid in ids)
        ):
            raise ValueError("LLM factual quote does not match source")
    return value
