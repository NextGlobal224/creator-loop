"""Compatibility alias; implementation lives in backend.runtime.maintenance_process."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module(
    "creator_loop.backend.runtime.maintenance_process"
)
