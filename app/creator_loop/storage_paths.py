"""Compatibility alias; implementation lives in backend.library.storage_paths."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("creator_loop.backend.library.storage_paths")
