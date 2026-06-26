import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from almanak.framework.market import MarketSnapshot, TokenBalance
from strategy import LendingLoopAaveWBTCUSDCStrategy


@pytest.fixture
def config() -> dict:
    return json.loads((Path(__file__).parent.parent / "config.json").read_text())


@pytest.fixture
def strategy(config: dict) -> LendingLoopAaveWBTCUSDCStrategy:
    return LendingLoopAaveWBTCUSDCStrategy(
        config=config,
        chain="polygon",
        wallet_address="0x" + "1" * 40,
    )


def _market(
    *,
    hf: Decimal,
    collateral_usd: Decimal,
    debt_usd: Decimal,
    wbtc_balance: Decimal,
    usdc_balance: Decimal,
    lltv: Decimal = Decimal("0.7"),
    usdc_price: Decimal = Decimal("1"),
) -> MarketSnapshot:
    market = MarketSnapshot(chain="polygon", wallet_address="0x" + "2" * 40)
    market.set_price("USDC", usdc_price)
    market.set_price("WBTC", Decimal("65000"))
    market.set_balance(
        "WBTC",
        TokenBalance(
            symbol="WBTC",
            balance=wbtc_balance,
            balance_usd=wbtc_balance * Decimal("65000"),
            address="0x1bfd67037b42cf73acf2047067bd4f2c47d9bfd6",
        ),
    )
    market.set_balance(
        "USDC",
        TokenBalance(
            symbol="USDC",
            balance=usdc_balance,
            balance_usd=usdc_balance,
            address="0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
        ),
    )

    health = MagicMock()
    health.health_factor = hf
    health.collateral_value_usd = collateral_usd
    health.debt_value_usd = debt_usd
    health.lltv = lltv
    market.set_position_health("aave_v3", "aave_v3_polygon", health)
    return market


def test_supplies_full_wbtc_on_startup(strategy: LendingLoopAaveWBTCUSDCStrategy) -> None:
    market = _market(
        hf=Decimal("10"),
        collateral_usd=Decimal("0"),
        debt_usd=Decimal("0"),
        wbtc_balance=Decimal("0.25"),
        usdc_balance=Decimal("0"),
    )
    intent = strategy.decide(market)
    assert intent.intent_type.value == "SUPPLY"
    assert intent.token == "WBTC"
    assert intent.amount == Decimal("0.25000000")


def test_holds_when_no_startup_wbtc(strategy: LendingLoopAaveWBTCUSDCStrategy) -> None:
    market = _market(
        hf=Decimal("10"),
        collateral_usd=Decimal("0"),
        debt_usd=Decimal("0"),
        wbtc_balance=Decimal("0"),
        usdc_balance=Decimal("0"),
    )
    intent = strategy.decide(market)
    assert intent.intent_type.value == "HOLD"


def test_borrows_to_target_hf_when_above_upper_band(strategy: LendingLoopAaveWBTCUSDCStrategy) -> None:
    market = _market(
        hf=Decimal("1.45"),
        collateral_usd=Decimal("100000"),
        debt_usd=Decimal("40000"),
        wbtc_balance=Decimal("0"),
        usdc_balance=Decimal("0"),
    )
    intent = strategy.decide(market)
    assert intent.intent_type.value == "BORROW"
    assert intent.borrow_token == "USDC"
    assert intent.borrow_amount > Decimal("0")


def test_repays_when_hf_below_lower_band(strategy: LendingLoopAaveWBTCUSDCStrategy) -> None:
    market = _market(
        hf=Decimal("1.18"),
        collateral_usd=Decimal("100000"),
        debt_usd=Decimal("58000"),
        wbtc_balance=Decimal("0"),
        usdc_balance=Decimal("2000"),
    )
    intent = strategy.decide(market)
    assert intent.intent_type.value == "REPAY"
    assert intent.amount > Decimal("0")


def test_emergency_mode_aggressively_repays(strategy: LendingLoopAaveWBTCUSDCStrategy) -> None:
    market = _market(
        hf=Decimal("1.08"),
        collateral_usd=Decimal("100000"),
        debt_usd=Decimal("62000"),
        wbtc_balance=Decimal("0"),
        usdc_balance=Decimal("500"),
    )
    intent = strategy.decide(market)
    assert intent.intent_type.value == "REPAY"
    assert intent.amount == Decimal("500.00")


def test_emergency_mode_latches_until_resume(strategy: LendingLoopAaveWBTCUSDCStrategy) -> None:
    trigger_market = _market(
        hf=Decimal("1.08"),
        collateral_usd=Decimal("100000"),
        debt_usd=Decimal("62000"),
        wbtc_balance=Decimal("0"),
        usdc_balance=Decimal("500"),
    )
    strategy.decide(trigger_market)

    still_low_market = _market(
        hf=Decimal("1.15"),
        collateral_usd=Decimal("100000"),
        debt_usd=Decimal("61000"),
        wbtc_balance=Decimal("0"),
        usdc_balance=Decimal("0"),
    )
    intent = strategy.decide(still_low_market)
    assert intent.intent_type.value == "HOLD"
    assert strategy._emergency_mode is True


def test_borrow_pauses_under_stop_threshold(strategy: LendingLoopAaveWBTCUSDCStrategy) -> None:
    market = _market(
        hf=Decimal("1.14"),
        collateral_usd=Decimal("100000"),
        debt_usd=Decimal("61000"),
        wbtc_balance=Decimal("0"),
        usdc_balance=Decimal("0"),
    )
    intent = strategy.decide(market)
    assert intent.intent_type.value == "HOLD"
    assert strategy._borrow_paused is True


def test_holds_when_hf_in_band(strategy: LendingLoopAaveWBTCUSDCStrategy) -> None:
    market = _market(
        hf=Decimal("1.30"),
        collateral_usd=Decimal("100000"),
        debt_usd=Decimal("54000"),
        wbtc_balance=Decimal("0"),
        usdc_balance=Decimal("0"),
    )
    intent = strategy.decide(market)
    assert intent.intent_type.value == "HOLD"


def test_data_unavailable_returns_hold(strategy: LendingLoopAaveWBTCUSDCStrategy) -> None:
    market = MagicMock()
    market.position_health.side_effect = ValueError("health unavailable")
    intent = strategy.decide(market)
    assert intent.intent_type.value == "HOLD"


@pytest.mark.parametrize(
    "force_action,expected",
    [
        ("supply", "SUPPLY"),
        ("borrow", "BORROW"),
        ("repay", "REPAY"),
    ],
)
def test_force_actions_emit_non_hold_intents(config: dict, force_action: str, expected: str) -> None:
    cfg = dict(config)
    cfg["force_action"] = force_action
    strat = LendingLoopAaveWBTCUSDCStrategy(
        config=cfg,
        chain="polygon",
        wallet_address="0x" + "3" * 40,
    )
    market = _market(
        hf=Decimal("1.4"),
        collateral_usd=Decimal("120000"),
        debt_usd=Decimal("50000"),
        wbtc_balance=Decimal("0.25"),
        usdc_balance=Decimal("1000"),
    )
    intent = strat.decide(market)
    assert intent.intent_type.value == expected


def test_get_open_positions_reports_supply_and_borrow(strategy: LendingLoopAaveWBTCUSDCStrategy) -> None:
    market = _market(
        hf=Decimal("1.2"),
        collateral_usd=Decimal("100000"),
        debt_usd=Decimal("60000"),
        wbtc_balance=Decimal("0"),
        usdc_balance=Decimal("100"),
    )
    strategy.create_market_snapshot = lambda: market
    summary = strategy.get_open_positions()
    assert len(summary.positions) == 2


def test_teardown_returns_repay_when_debt_and_usdc_available(strategy: LendingLoopAaveWBTCUSDCStrategy) -> None:
    market = _market(
        hf=Decimal("1.2"),
        collateral_usd=Decimal("100000"),
        debt_usd=Decimal("60000"),
        wbtc_balance=Decimal("0"),
        usdc_balance=Decimal("800"),
    )
    intents = strategy.generate_teardown_intents(market=market)
    assert len(intents) == 1
    assert intents[0].intent_type.value == "REPAY"


def test_teardown_returns_empty_without_usdc(strategy: LendingLoopAaveWBTCUSDCStrategy) -> None:
    market = _market(
        hf=Decimal("1.2"),
        collateral_usd=Decimal("100000"),
        debt_usd=Decimal("60000"),
        wbtc_balance=Decimal("0"),
        usdc_balance=Decimal("0"),
    )
    intents = strategy.generate_teardown_intents(market=market)
    assert intents == []
