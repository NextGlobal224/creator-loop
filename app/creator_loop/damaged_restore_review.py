"""Compatibility alias; implementation lives in backend.runtime.damaged_restore_review."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module(
    "creator_loop.backend.runtime.damaged_restore_review"
)
