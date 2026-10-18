"""Compatibility alias; implementation lives in backend.facebook.facebook_sources."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("creator_loop.backend.facebook.facebook_sources")
