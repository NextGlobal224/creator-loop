"""Compatibility alias; implementation lives in backend.publication.observations."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("creator_loop.backend.publication.observations")
