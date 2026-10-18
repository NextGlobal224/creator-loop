"""Compatibility alias; implementation lives in backend.runtime.windows_owned_file."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("creator_loop.backend.runtime.windows_owned_file")
