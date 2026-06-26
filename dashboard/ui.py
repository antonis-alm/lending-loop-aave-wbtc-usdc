"""Dashboard UI for Lending-Loop-Aave-WBTC-USDC."""

from typing import Any


STRATEGY_DISPLAY_NAME = "Lending-Loop-Aave-WBTC-USDC"


def render_custom_dashboard(
    deployment_id: str,
    strategy_config: dict[str, Any],
    api_client: Any,
    session_state: dict[str, Any],
) -> None:
    import streamlit as st
    from almanak.framework.dashboard.templates import get_aave_v3_config, render_lending_dashboard

    st.title(STRATEGY_DISPLAY_NAME)

    config = get_aave_v3_config(
        collateral_token=str(strategy_config.get("collateral_token", "WBTC")),
        borrow_token=str(strategy_config.get("borrow_token", "USDC")),
        chain=str(strategy_config.get("chain", "polygon")),
    )

    render_lending_dashboard(deployment_id, strategy_config, session_state, config)
