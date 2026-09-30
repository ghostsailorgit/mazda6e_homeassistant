"""Make the integration's HA-free modules importable as ``mazda6e.*``."""

import sys
import types
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "mazda6e"
_pkg = types.ModuleType("mazda6e")
_pkg.__path__ = [str(_PKG_DIR)]
sys.modules.setdefault("mazda6e", _pkg)

try:
    import pytest_homeassistant_custom_component  # noqa: F401
except ImportError:
    # Home Assistant tests need `pip install pytest-homeassistant-custom-component`.
    collect_ignore = ["ha"]
