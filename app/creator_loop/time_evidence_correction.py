"""Compatibility alias; implementation lives in backend.knowledge.time_evidence_correction."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module(
    "creator_loop.backend.knowledge.time_evidence_correction"
)
