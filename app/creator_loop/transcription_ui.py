"""Compatibility alias; implementation lives in frontend.transcription_ui."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("creator_loop.frontend.transcription_ui")
