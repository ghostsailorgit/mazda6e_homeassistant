from unittest.mock import AsyncMock, patch

import pytest

CLIENT = "custom_components.mazda6e.api.Mazda6eClient"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def no_optional_backend_calls():
    """Function config and preheat plan: unknown / none unless a test says so."""
    with (
        patch(f"{CLIENT}.get_functions", AsyncMock(return_value=set())),
        patch(f"{CLIENT}.get_battery_preheat_plan", AsyncMock(return_value=None)),
    ):
        yield
