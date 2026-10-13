"""Compatibility alias; implementation lives in backend.library.originals."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("creator_loop.backend.library.originals")
