"""Append manual Post measurements; absent numeric values stay unknown, never zero."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from creator_loop.claims import _timestamp
from creator_loop.transactions import atomic_transaction


@dataclass(frozen=True)
class MetricInput:
    metric_key: str
    metric_scope: str
    numeric_value: float | None
    unit: str
    definition_version: str
    raw_value: str | None = None


@dataclass(frozen=True)
class Observation:
    observation_id: str
    post_id: str
    platform: str
    observed_at: str
    collector_type: str
    processing_run_id: str | None
    created_at: str
    metrics: tuple[MetricInput, ...]

    def value_for(self, key: str, scope: str = "POST") -> float | None:
        """Missing metric rows mean unknown; a stored zero remains zero."""
        return next(
            (
                m.numeric_value
                for m in self.metrics
                if m.metric_key == key and m.metric_scope == scope
            ),
            None,
        )


def list_observations(db: sqlite3.Connection, post_id: str) -> tuple[Observation, ...]:
    """Read a coherent history in the caller's read transaction."""
    rows = db.execute(
        """SELECT o.observation_id,o.post_id,p.platform,o.observed_at,
        o.collector_type,o.processing_run_id,o.created_at
        FROM observations o JOIN posts p ON p.post_id=o.post_id
        WHERE o.post_id=? ORDER BY o.rowid""",
        (post_id,),
    ).fetchall()
    return tuple(
        Observation(
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            tuple(
                MetricInput(*metric)
                for metric in db.execute(
                    """SELECT metric_key,metric_scope,numeric_value,unit,
                    definition_version,raw_value FROM observation_metrics
                    WHERE observation_id=? ORDER BY metric_key,metric_scope""",
                    (row[0],),
                ).fetchall()
            ),
        )
        for row in rows
    )


def _record_manual_observation(
    db: sqlite3.Connection,
    *,
    post_id: str,
    observed_at: str,
    metrics: tuple[MetricInput, ...],
) -> Observation:
    """One owned transaction; no API/run provenance is fabricated for manual input."""
    if db.in_transaction:
        raise ValueError("Observation requires its own transaction")
    try:
        instant = datetime.fromisoformat(observed_at.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Observed time must be ISO 8601 with a timezone") from exc
    if instant.utcoffset() is None:
        raise ValueError("Observed time requires an explicit timezone")
    normalized = instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    seen: set[tuple[str, str]] = set()
    known: list[MetricInput] = []
    for metric in metrics:
        if (
            not metric.metric_key.strip()
            or metric.metric_key != metric.metric_key.strip()
        ):
            raise ValueError("Metric key must be nonempty without outer whitespace")
        if metric.metric_scope not in ("POST", "ACCOUNT", "OTHER"):
            raise ValueError("Metric scope must be POST, ACCOUNT or OTHER")
        if not metric.unit.strip() or not metric.definition_version.strip():
            raise ValueError("Metric unit and definition version are required")
        identity = (metric.metric_key, metric.metric_scope)
        if identity in seen:
            raise ValueError("Duplicate metric key/scope")
        seen.add(identity)
        value = metric.numeric_value
        if value is None:
            if metric.raw_value is not None:
                raise ValueError("Unknown metric cannot claim a raw numeric value")
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Metric value must be a finite number or unknown")
        if not math.isfinite(value):
            raise ValueError("Metric value must be finite")
        if isinstance(value, int) and int(float(value)) != value:
            raise ValueError(
                "Metric integer cannot be represented exactly in SQLite REAL"
            )
        if metric.unit.strip().casefold() == "count" and (
            value < 0 or not float(value).is_integer()
        ):
            raise ValueError("Count metrics must be nonnegative whole numbers")
        known.append(metric)
    with atomic_transaction(db):
        post = db.execute(
            "SELECT platform,published_at,status FROM posts WHERE post_id=?", (post_id,)
        ).fetchone()
        if post is None or post[1] is None or post[2] not in ("PUBLISHED", "REMOVED"):
            raise ValueError("A previously published Post is required for measurement")
        published = datetime.fromisoformat(post[1].replace("Z", "+00:00"))
        if published.utcoffset() is None or instant < published:
            raise ValueError("Measurement time must not precede publication")
        observation = Observation(
            uuid4().hex,
            post_id,
            post[0],
            normalized,
            "MANUAL",
            None,
            _timestamp(),
            tuple(sorted(known, key=lambda m: (m.metric_key, m.metric_scope))),
        )
        db.execute(
            """INSERT INTO observations(observation_id,post_id,observed_at,
            collector_type,processing_run_id,created_at) VALUES(?,?,?,?,?,?)""",
            (
                observation.observation_id,
                post_id,
                normalized,
                "MANUAL",
                None,
                observation.created_at,
            ),
        )
        for metric in observation.metrics:
            db.execute(
                """INSERT INTO observation_metrics(observation_id,metric_key,
                metric_scope,numeric_value,unit,definition_version,raw_value)
                VALUES(?,?,?,?,?,?,?)""",
                (
                    observation.observation_id,
                    metric.metric_key,
                    metric.metric_scope,
                    metric.numeric_value,
                    metric.unit,
                    metric.definition_version,
                    metric.raw_value,
                ),
            )
    return observation
