import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import dashboard.ui as ui


def _mock_modules() -> tuple[ModuleType, ModuleType, MagicMock, MagicMock, MagicMock]:
    streamlit_module = ModuleType("streamlit")
    streamlit_title = MagicMock()
    streamlit_module.title = streamlit_title

    templates_module = ModuleType("almanak.framework.dashboard.templates")
    get_aave_v3_config = MagicMock(return_value=object())
    render_lending_dashboard = MagicMock()
    templates_module.get_aave_v3_config = get_aave_v3_config
    templates_module.render_lending_dashboard = render_lending_dashboard

    return (
        streamlit_module,
        templates_module,
        streamlit_title,
        get_aave_v3_config,
        render_lending_dashboard,
    )


def test_dashboard_imports() -> None:
    assert callable(ui.render_custom_dashboard)


def test_render_custom_dashboard_uses_aave_template_with_strategy_config() -> None:
    strategy_config = {
        "collateral_token": "WBTC",
        "borrow_token": "USDC",
        "chain": "polygon",
    }
    session_state = {"last_hf": "1.32"}

    streamlit_module, templates_module, mock_title, mock_get_config, mock_render = _mock_modules()

    with patch.dict(
        sys.modules,
        {
            "streamlit": streamlit_module,
            "almanak.framework.dashboard.templates": templates_module,
        },
    ):
        ui.render_custom_dashboard("dep-123", strategy_config, api_client=None, session_state=session_state)

    mock_title.assert_called_once_with(ui.STRATEGY_DISPLAY_NAME)
    mock_get_config.assert_called_once_with(
        collateral_token="WBTC",
        borrow_token="USDC",
        chain="polygon",
    )
    mock_render.assert_called_once_with("dep-123", strategy_config, session_state, mock_get_config.return_value)


def test_render_custom_dashboard_uses_expected_defaults() -> None:
    strategy_config = {}

    streamlit_module, templates_module, _mock_title, mock_get_config, _mock_render = _mock_modules()

    with patch.dict(
        sys.modules,
        {
            "streamlit": streamlit_module,
            "almanak.framework.dashboard.templates": templates_module,
        },
    ):
        ui.render_custom_dashboard("dep-456", strategy_config, api_client=None, session_state={})

    mock_get_config.assert_called_once_with(
        collateral_token="WBTC",
        borrow_token="USDC",
        chain="polygon",
    )
