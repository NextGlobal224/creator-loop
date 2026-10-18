"""Compatibility alias; implementation lives in backend.knowledge.image_evidence."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("creator_loop.backend.knowledge.image_evidence")
