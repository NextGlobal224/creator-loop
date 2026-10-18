"""Compatibility alias; implementation lives in backend.analysis.whisper_runtime_probe."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module(
    "creator_loop.backend.analysis.whisper_runtime_probe"
)
