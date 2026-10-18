"""Backend operations dispatched by thin UI workers."""

from __future__ import annotations

from typing import Any

from creator_loop.backend.publication.packages import PublicationRepository


def run_ObservationWorker(self: Any) -> None:
    try:
        result = PublicationRepository(
            self.root / "creator_loop.sqlite3"
        ).record_manual_observation(
            post_id=self.post_id, observed_at=self.observed_at, metrics=self.metrics
        )
        self.completed.emit(result)
    except Exception as exc:
        self.failed.emit(str(exc))
