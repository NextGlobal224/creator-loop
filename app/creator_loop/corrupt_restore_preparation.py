"""Compatibility alias; implementation lives in backend.runtime.corrupt_restore_preparation."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module(
    "creator_loop.backend.runtime.corrupt_restore_preparation"
)
