"""Compatibility alias; implementation lives in backend.runtime.processing_witness."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("creator_loop.backend.runtime.processing_witness")
