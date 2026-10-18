"""Compatibility alias; implementation lives in backend.creator.drafts."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("creator_loop.backend.creator.drafts")
