"""Compatibility alias; implementation lives in frontend.product_flow_smoke."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("creator_loop.integration.product_flow_smoke")
