"""Bounded local research rules. Search rank and media type are never viral evidence."""

from __future__ import annotations

import json
import re
import unicodedata
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse, urlunparse
from uuid import uuid4

from creator_loop.database import _connect_write, open_readonly
from creator_loop.facebook_sources import post_link, save_capture

MODES = ("COMBINED", "VIRAL", "ANALYSIS")
METRICS = ("likes", "comments", "shares", "views")
DEFAULT_THRESHOLDS = dict(zip(METRICS, (1000, 100, 100, 10000), strict=True))
MAX_SCAN_BUDGETS = {"max_queries": 12, "max_candidates": 300, "max_details": 120}
DEFAULT_SCAN_BUDGETS = {"max_queries": 8, "max_candidates": 160, "max_details": 80}
VIRAL_TARGET = 40


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def fold(value: str) -> str:
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", value.lower().replace("đ", "d"))
        if not unicodedata.combining(c)
    )


def suggested_queries(topic: str, purpose: str = "") -> list[str]:
    topic = topic.strip()[:200]
    return (
        [
            topic,
            (topic + " " + (purpose.strip() or "kinh nghiệm"))[:200],
            (topic + " hướng dẫn")[:200],
        ]
        if topic
        else []
    )


def criteria(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "topic",
        "purpose",
        "since",
        "until",
        "mode",
        "queries",
        "max_queries",
        "max_candidates",
        "max_details",
        "thresholds",
    }:
        raise ValueError("Invalid search criteria")
    result = dict(value)
    for key, maximum in (("topic", 200), ("purpose", 500)):
        if not isinstance(result[key], str) or len(result[key]) > maximum:
            raise ValueError("Invalid topic/purpose")
        result[key] = unicodedata.normalize("NFC", result[key].strip())
    if not result["topic"] or not re.findall(r"\w+", result["topic"]):
        raise ValueError("Topic required")
    if result["mode"] not in MODES:
        raise ValueError("Invalid research mode")
    for key, upper in MAX_SCAN_BUDGETS.items():
        if type(result[key]) is not int or not 1 <= result[key] <= upper:
            raise ValueError("Invalid bounded scan budget")
    for key in ("since", "until"):
        if result[key] is not None:
            if not isinstance(result[key], str):
                raise ValueError("Invalid date")
            date.fromisoformat(result[key])
    if result["since"] and result["until"] and result["since"] > result["until"]:
        raise ValueError("Inverted time window")
    queries = result["queries"]
    if (
        not isinstance(queries, list)
        or not 1 <= len(queries) <= result["max_queries"]
        or any(
            not isinstance(q, str) or not 1 <= len(q.strip()) <= 200 for q in queries
        )
    ):
        raise ValueError("Query budget exceeded")
    result["queries"] = list(dict.fromkeys(q.strip() for q in queries))
    thresholds = result["thresholds"]
    if (
        not isinstance(thresholds, dict)
        or set(thresholds) != set(METRICS)
        or any(
            type(n) is not int or not 1 <= n <= 1000000000 for n in thresholds.values()
        )
    ):
        raise ValueError("Invalid viral thresholds")
    return result


def research_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    """Unique posts; analysis eligibility never contributes to viral counts."""
    unique = {item["external_id"]: item for item in items}
    evaluated = viral = 0
    for item in unique.values():
        assessment = item["assessment"]
        bound = bool(item.get("excerpt") and item.get("publisher"))
        evaluated += int(
            bound
            and assessment["topical"] is not None
            and assessment["time_match"] is not None
            and assessment["viral"] is not None
        )
        viral += int(
            bound
            and assessment["topical"] is True
            and assessment["time_match"] is True
            and assessment["viral"] is True
        )
    return {"candidates": len(unique), "evaluated": evaluated, "viral": viral}


def metric(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str) or not 1 <= len(raw) <= 160:
        return None
    text = fold(raw)
    match = re.search(
        r"(?<!\d)(\d+(?:[.,]\d+)*)(?:\s*(k|m|b|nghin|ngan|trieu|tr|ty))?(?![\d.,a-z])",
        text,
    )
    if not match:
        return None
    number, suffix = match[1], match[2]
    if suffix:
        value = (
            float(number.replace(",", "."))
            if number.count(",") + number.count(".") <= 1
            else None
        )
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", number):
        value = float(re.sub(r"[.,]", "", number))
    else:
        value = float(number) if number.isdigit() else None
    if value is None:
        return None
    scale = {
        "k": 1000,
        "nghin": 1000,
        "ngan": 1000,
        "m": 1000000,
        "trieu": 1000000,
        "tr": 1000000,
        "b": 1000000000,
        "ty": 1000000000,
    }.get(suffix, 1)
    return {
        "raw": raw,
        "value": int(value * scale),
        "approximate": bool(suffix or "+" in text or ">" in text),
    }


def displayed_post_date(label: Any) -> str | None:
    """An explicit displayed calendar date is usable without inventing a time."""
    if not isinstance(label, str):
        return None
    text = fold(label)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text.strip()):
        try:
            return date.fromisoformat(text.strip()).isoformat()
        except ValueError:
            return None
    matches = re.findall(
        r"\b(\d{1,2})\s*thang\s*(\d{1,2})\s*[,\s]*(?:nam\s*)?(\d{4})\b", text
    )
    if len(matches) != 1:
        return None
    day, month, year = map(int, matches[0])
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def assess(item: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    excerpt = item.get("excerpt") or ""
    # Vietnamese accents distinguish words (nhận/nhân, đúng/đứng/dùng).
    # Accent folding here turned unrelated long captions into topic matches.
    tokens = set(
        re.findall(
            r"\w+",
            unicodedata.normalize(
                "NFC", re.sub(r"(?<!\w)#[\w]+", "", excerpt)
            ).casefold(),
        )
    )
    topic = set(
        re.findall(r"\w+", unicodedata.normalize("NFC", settings["topic"]).casefold())
    ) - {
        "về",
        "và",
        "các",
        "của",
        "ở",
        "tại",
    }
    topical = bool(topic) and topic <= tokens if excerpt else None
    published = item.get("published_at")
    dated = (
        published[:10]
        if published
        else displayed_post_date(item.get("published_label"))
    )
    in_time: bool | None = True
    if settings["since"] or settings["until"]:
        in_time = (
            bool(
                (not settings["since"] or dated >= settings["since"])
                and (not settings["until"] or dated <= settings["until"])
            )
            if dated
            else None
        )
    reasons, evidence = [], []
    signals = []
    for name in METRICS:
        reading = item.get("metrics", {}).get(name)
        if reading is not None and reading["value"] >= settings["thresholds"][name]:
            signals.append(
                f"{name}: {reading['raw']} ≥ {settings['thresholds'][name]}"
                + (" (xấp xỉ)" if reading["approximate"] else "")
            )
    viral: bool | None = (
        True
        if signals
        else False
        if all(item.get("metrics", {}).get(n) is not None for n in METRICS)
        else None
    )
    categories = {
        "Vấn đề": ("vấn đề", "rủi ro", "lỗi", "khó khăn", "tại sao"),
        "Hướng dẫn": (
            "hướng dẫn",
            "từng bước",
            "cách làm",
            "cách chọn",
            "cách dùng",
            "cách kiểm tra",
            "mẹo",
        ),
        "Trải nghiệm": ("trải nghiệm", "kinh nghiệm", "tôi đã", "mình đã"),
        "Dữ kiện": ("số liệu", "thống kê", "nghiên cứu", "mức giá"),
        "Tình huống": ("tình huống", "ví dụ", "trường hợp"),
        "Trình bày": ("so sánh", "trước và sau", "minh họa", "checklist"),
    }
    for category, cues in categories.items():
        for cue in cues:
            match = re.search(
                r"(?<!\w)" + re.escape(cue) + r"(?!\w)", excerpt, re.IGNORECASE
            )
            if match:
                reasons.append(category + ": có cụm “" + cue + "” trong phần đọc được")
                evidence.append(
                    excerpt[
                        max(0, match.start() - 70) : min(
                            len(excerpt), match.end() + 160
                        )
                    ]
                )
                break
    analysis: bool | None = bool(reasons) if excerpt else None
    selected_signal = (
        viral
        if settings["mode"] == "VIRAL"
        else analysis
        if settings["mode"] == "ANALYSIS"
        else (viral is True or analysis is True)
    )
    eligible = topical is True and in_time is True and selected_signal is True
    state = (
        "QUALIFIED"
        if eligible
        else "REJECTED"
        if topical is False or in_time is False
        else "NEEDS_REVIEW"
    )
    missing = [
        name for name in ("publisher", "published_at", "excerpt") if not item.get(name)
    ]
    missing += [name for name in METRICS if item.get("metrics", {}).get(name) is None]
    if in_time is None:
        reasons.append("Chưa rõ thời điểm đăng để đối chiếu khoảng thời gian")
    if topical is True:
        evidence.insert(0, excerpt[:350])
    return {
        "topical": topical,
        "time_match": in_time,
        "viral": viral,
        "analysis": analysis,
        "state": state,
        "reasons": signals + reasons,
        "evidence": list(dict.fromkeys(evidence)),
        "missing": missing,
        "method": "LOCAL_RULES_V1_2",
        "purpose": settings["purpose"],
    }


def candidate(
    raw: Any, query: str, settings: dict[str, Any], observed: str | None = None
) -> dict[str, Any]:
    if not isinstance(raw, dict) or (link := post_link(raw.get("url"))) is None:
        raise ValueError("Invalid observed post")
    excerpt = raw.get("excerpt")
    excerpt = excerpt[:8192].strip() or None if isinstance(excerpt, str) else None
    published = raw.get("published_at")
    if published:
        try:
            datetime.fromisoformat(published)
        except (ValueError, TypeError):
            published = None
    publisher_url = raw.get("publisher_url")
    from creator_loop.facebook_cdp import is_facebook_url

    if not isinstance(publisher_url, str) or not is_facebook_url(publisher_url):
        publisher_url = None
    else:
        parsed = urlparse(publisher_url)
        profile = parse_qs(parsed.query).get("id", [])
        query_id = (
            "id=" + profile[0]
            if parsed.path == "/profile.php"
            and len(profile) == 1
            and profile[0].isdigit()
            else ""
        )
        publisher_url = urlunparse(
            ("https", "www.facebook.com", parsed.path.rstrip("/"), "", query_id, "")
        )
        if (
            not re.fullmatch(
                r"/(?:[A-Za-z0-9._-]+|groups/[0-9]+(?:/user/[0-9]+)?|pages/[^/]+/[0-9]+)",
                parsed.path.rstrip("/"),
            )
            or parsed.path in ("/search", "/watch", "/groups", "/reel", "/home.php")
            or (parsed.path == "/profile.php" and not query_id)
        ):
            publisher_url = None
    publisher = raw.get("publisher")
    published_label = raw.get("published_label")
    item = {
        "url": link.url,
        "external_id": link.external_id,
        "publisher": publisher[:200] if isinstance(publisher, str) else None,
        "publisher_url": publisher_url,
        "published_at": published,
        "published_label": published_label[:200]
        if isinstance(published_label, str)
        else None,
        "excerpt": excerpt,
        "scope": "URL_ONLY"
        if not excerpt
        else "CAPTION_ONLY"
        if "/reel/" in link.url
        or "/videos/" in link.url
        or "/watch/" in link.url
        or raw.get("scope") == "CAPTION_ONLY"
        else "VISIBLE_TEXT",
        "hashtags": list(dict.fromkeys(re.findall(r"(?<!\w)#[\w]+", excerpt or "")))[
            :50
        ],
        "metrics": {name: metric(raw.get("metrics", {}).get(name)) for name in METRICS},
        "query": query,
        "observed_at": observed or utc_now(),
    }
    item["assessment"] = assess(item, settings)
    return item


def validate_candidate(item: Any, settings: dict[str, Any]) -> None:
    keys = {
        "url",
        "external_id",
        "publisher",
        "publisher_url",
        "published_at",
        "published_label",
        "excerpt",
        "scope",
        "hashtags",
        "metrics",
        "query",
        "observed_at",
        "assessment",
    }
    if not isinstance(item, dict) or set(item) != keys:
        raise ValueError("Invalid candidate shape")
    link = post_link(item["url"])
    if (
        link is None
        or link.url != item["url"]
        or link.external_id != item["external_id"]
    ):
        raise ValueError("Invalid candidate identity")
    for name, length in (
        ("publisher", 200),
        ("publisher_url", 4096),
        ("published_at", 200),
        ("published_label", 200),
        ("excerpt", 8192),
        ("query", 200),
        ("observed_at", 100),
    ):
        if item[name] is not None and (
            not isinstance(item[name], str) or not 1 <= len(item[name]) <= length
        ):
            raise ValueError("Invalid candidate text")
    datetime.fromisoformat(item["observed_at"])
    if item["published_at"]:
        date.fromisoformat(item["published_at"][:10])
    if (
        item["scope"] not in ("CAPTION_ONLY", "VISIBLE_TEXT", "URL_ONLY")
        or not isinstance(item["metrics"], dict)
        or set(item["metrics"]) != set(METRICS)
    ):
        raise ValueError("Invalid candidate scope/metrics")
    for reading in item["metrics"].values():
        if reading is not None and (
            not isinstance(reading, dict)
            or set(reading) != {"raw", "value", "approximate"}
            or reading != metric(reading.get("raw"))
        ):
            raise ValueError("Metric must match its actual raw reading")
    expected_tags = list(
        dict.fromkeys(re.findall(r"(?<!\w)#[\w]+", item["excerpt"] or ""))
    )[:50]
    if item["hashtags"] != expected_tags or item["assessment"] != assess(
        item, settings
    ):
        raise ValueError("Hashtags/assessment need exact caption/criteria provenance")


def store_search(
    root: Path,
    settings: dict[str, Any],
    queries: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> str:
    settings = criteria(settings)
    if (
        len(items) > settings["max_candidates"]
        or len(queries) > settings["max_queries"]
    ):
        raise ValueError("Search result exceeds budget")
    search_id = uuid4().hex
    with closing(_connect_write(root / "creator_loop.sqlite3")) as db, db:
        db.execute(
            "INSERT INTO facebook_searches VALUES (?,?,?,?)",
            (
                search_id,
                utc_now(),
                json.dumps(settings, ensure_ascii=False),
                json.dumps(queries, ensure_ascii=False),
            ),
        )
        for item in items:
            validate_candidate(item, settings)
            link = post_link(item.get("url"))
            if (
                link is None
                or link.url != item["url"]
                or link.external_id != item["external_id"]
            ):
                raise ValueError("Invalid candidate identity")
            # Recompute from parsed metadata and these exact stored criteria.
            if item["assessment"] != assess(item, settings):
                raise ValueError("Assessment mismatch")
            db.execute(
                "INSERT INTO facebook_candidates VALUES (?,?,?,?,?,?)",
                (
                    uuid4().hex,
                    search_id,
                    link.external_id,
                    link.url,
                    item["observed_at"],
                    json.dumps(item, ensure_ascii=False),
                ),
            )
    return search_id


def candidates(root: Path) -> list[dict[str, Any]]:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        rows = db.execute(
            "SELECT c.candidate_id,c.search_id,c.snapshot_json,s.criteria_json FROM facebook_candidates c JOIN facebook_searches s USING(search_id) ORDER BY c.observed_at DESC,c.rowid DESC LIMIT 500"
        ).fetchall()
        selected = {
            r[0]
            for r in db.execute("SELECT candidate_id FROM facebook_selection_events")
        }
        existing = {
            r[0]
            for r in db.execute("SELECT external_id FROM sources WHERE platform='FB'")
        }
    return [
        {
            **json.loads(raw),
            "candidate_id": cid,
            "search_id": sid,
            "criteria": json.loads(params),
            "saved": cid in selected or json.loads(raw)["external_id"] in existing,
        }
        for cid, sid, raw, params in rows
    ]


def searches(root: Path) -> list[dict[str, Any]]:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        rows = db.execute(
            "SELECT search_id,created_at,criteria_json,queries_json FROM facebook_searches ORDER BY rowid DESC LIMIT 50"
        ).fetchall()
    return [
        {
            "search_id": sid,
            "created_at": created,
            "criteria": json.loads(params),
            "queries": json.loads(queries),
        }
        for sid, created, params, queries in rows
    ]


def save_selected(
    root: Path, candidate_id: str, cancel: Callable[[], bool] = lambda: False
) -> tuple[str, bool]:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        row = db.execute(
            "SELECT snapshot_json FROM facebook_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
    if row is None:
        raise ValueError("Candidate not found")
    item = json.loads(row[0])

    def record(db: Any, source_id: str) -> None:
        if cancel():
            raise InterruptedError()
        db.execute(
            "INSERT INTO facebook_selection_events VALUES (?,?,?,?)",
            (uuid4().hex, candidate_id, source_id, utc_now()),
        )

    source_id, new = save_capture(
        root,
        keyword=item["query"],
        url=item["url"],
        excerpt=item["excerpt"],
        publisher=item["publisher"],
        published_at=item["published_at"],
        observed_at=item["observed_at"],
        cancelled=cancel,
        on_saved=record,
    )
    if not new:
        with closing(_connect_write(root / "creator_loop.sqlite3")) as db, db:
            record(db, source_id)
    return source_id, new


def saved_assessment(root: Path, source_id: str) -> dict[str, Any] | None:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        row = db.execute(
            "SELECT c.snapshot_json,s.criteria_json FROM facebook_selection_events e JOIN facebook_candidates c USING(candidate_id) JOIN facebook_searches s USING(search_id) WHERE e.source_id=? ORDER BY e.rowid DESC LIMIT 1",
            (source_id,),
        ).fetchone()
    return (
        {"observation": json.loads(row[0]), "criteria": json.loads(row[1])}
        if row
        else None
    )


def hashtag_summary(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for item in sorted(items, key=lambda i: i["observed_at"]):
        if (
            item["assessment"]["topical"] is not True
            or item["assessment"]["time_match"] is not True
        ):
            continue
        for tag in item["hashtags"]:
            grouped.setdefault(tag.casefold(), {})[item["external_id"]] = item
    result = []
    for tag, posts in grouped.items():
        observations = list(posts.values())

        def publisher_id(post: dict[str, Any]) -> str | None:
            url = post.get("publisher_url")
            if not url:
                return None
            parsed = urlparse(url)
            if parsed.path == "/profile.php":
                ids = parse_qs(parsed.query).get("id", [])
                return ids[0] if len(ids) == 1 and ids[0].isdigit() else None
            member = re.fullmatch(r"/groups/[0-9]+/user/([0-9]+)", parsed.path)
            if member:
                return member[1]
            match = re.fullmatch(r"/(?:groups|pages/[^/]+)/([0-9]+)", parsed.path)
            return match[1] if match else None

        known = {identity for p in observations if (identity := publisher_id(p))}
        result.append(
            {
                "tag": tag,
                "posts": len(posts),
                "known_independent_sources": len(known),
                "unknown_sources": sum(publisher_id(p) is None for p in observations),
                "observed_from": min(p["observed_at"] for p in observations),
                "observed_to": max(p["observed_at"] for p in observations),
                "source_urls": [p["url"] for p in observations],
                "label": "Được nhắc nhiều trong mẫu quét"
                if len(posts) >= 3
                else "Liên quan",
                "growth": "Chưa rõ — chưa có cửa sổ so sánh tương đương",
            }
        )
    return sorted(result, key=lambda h: (-h["posts"], h["tag"]))
