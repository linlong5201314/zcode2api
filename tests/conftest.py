"""Test-only compatibility shims for optional runtime dependencies.

The gateway's protocol and storage tests do not need a browser or a dotenv
loader.  Keeping the shims here lets the fast unit suite run in a minimal
checkout while production still uses the declared dependencies.
"""

from __future__ import annotations

import sys
import types
import importlib.util


def _missing(module_name: str) -> bool:
    if module_name in sys.modules:
        return False
    try:
        return importlib.util.find_spec(module_name) is None
    except (ImportError, ModuleNotFoundError, ValueError):
        return True


if _missing("dotenv"):
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: False
    sys.modules["dotenv"] = dotenv


if _missing("playwright.async_api"):
    playwright = types.ModuleType("playwright")
    async_api = types.ModuleType("playwright.async_api")
    async_api.async_playwright = lambda: None
    playwright.async_api = async_api
    sys.modules["playwright"] = playwright
    sys.modules["playwright.async_api"] = async_api
