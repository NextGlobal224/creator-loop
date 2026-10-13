"""Compatibility alias; implementation lives in backend.analysis.transcript_review_task."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module(
    "creator_loop.backend.analysis.transcript_review_task"
)
