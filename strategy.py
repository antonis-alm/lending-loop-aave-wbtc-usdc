from __future__ import annotations

import logging
from decimal import Decimal, ROUND_DOWN
from typing import Any

from almanak.framework.data import MarketSnapshotError, PriceUnavailableError
from almanak.framework.intents import Intent
from almanak.framework.market import MarketSnapshot
from almanak.framework.strategies import IntentStrategy, almanak_strategy

logger = logging.getLogger(__name__)

_DATA_UNAVAILABLE_ERRORS = (
    PriceUnavailableError,
    MarketSnapshotError,
    ValueError,
    KeyError,
)


@almanak_strategy(
    name="Lending-Loop-Aave-WBTC-USDC",
    description="HF-band Aave V3 lending manager for WBTC collateral and USDC debt",
    version="1.0.0",
    author="Almanak",
    tags=["lending", "aave_v3", "polygon", "wbtc", "usdc"],
    supported_chains=["polygon"],
    supported_protocols=["aave_v3"],
    intent_types=["SUPPLY", "BORROW", "REPAY", "HOLD"],
    default_chain="polygon",
    quote_asset="USD",
)
class LendingLoopAaveWBTCUSDCStrategy(IntentStrategy):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        def cfg(key: str, default: Any) -> Any:
            return self.config.get(key, default) if isinstance(self.config, dict) else getattr(self.config, key, default)

        self.protocol = str(cfg("protocol", "aave_v3"))
        self.market_id = str(cfg("market_id", "aave_v3_polygon"))

        self.collateral_token = str(cfg("collateral_token", "WBTC"))
        self.borrow_token = str(cfg("borrow_token", "USDC"))

        self.initial_supply_fraction_of_wbtc_balance = Decimal(
            str(cfg("initial_supply_fraction_of_wbtc_balance", "1.0"))
        )
        self.initial_borrow_target_hf = Decimal(str(cfg("initial_borrow_target_hf", "1.3")))

        self.health_factor_lower_bound = Decimal(str(cfg("health_factor_lower_bound", "1.2")))
        self.health_factor_target = Decimal(str(cfg("health_factor_target", "1.3")))
        self.health_factor_upper_bound = Decimal(str(cfg("health_factor_upper_bound", "1.4")))

        self.borrow_if_hf_above = Decimal(str(cfg("borrow_if_hf_above", "1.4")))
        self.repay_if_hf_below = Decimal(str(cfg("repay_if_hf_below", "1.2")))
        self.borrow_to_target_hf = Decimal(str(cfg("borrow_to_target_hf", "1.3")))
        self.repay_to_target_hf = Decimal(str(cfg("repay_to_target_hf", "1.3")))
        self.min_hf_after_borrow = Decimal(str(cfg("min_hf_after_borrow", "1.3")))

        self.borrow_stop_threshold = Decimal(str(cfg("borrow_stop_threshold", "1.15")))
        self.emergency_mode_trigger = Decimal(str(cfg("emergency_mode_trigger", "1.10")))
        self.resume_threshold = Decimal(str(cfg("resume_threshold", "1.2")))
        self.aggressive_repay_in_emergency_mode = bool(cfg("aggressive_repay_in_emergency_mode", True))

        self.min_action_usdc = Decimal(str(cfg("min_action_usdc", "5")))
        self.min_action_wbtc = Decimal(str(cfg("min_action_wbtc", "0.00001")))
        self.min_collateral_usd_for_active_position = Decimal(
            str(cfg("min_collateral_usd_for_active_position", "10"))
        )

        self.force_action = str(cfg("force_action", "")).strip().lower()

        self._emergency_mode = False
        self._borrow_paused = False
        self._last_hf = Decimal("0")
        self._last_action = "HOLD"

    def _quantize_usdc(self, amount: Decimal) -> Decimal:
        return amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    def _read_market_state(self, market: MarketSnapshot) -> dict[str, Decimal]:
        health = market.position_health(protocol=self.protocol, market_id=self.market_id)
        hf = Decimal(str(health.health_factor))
        collateral_usd = Decimal(str(getattr(health, "collateral_value_usd", Decimal("0"))))
        debt_usd = Decimal(str(getattr(health, "debt_value_usd", Decimal("0"))))
        lltv = Decimal(str(getattr(health, "lltv", Decimal("0.8"))))

        wbtc_balance = Decimal(str(market.balance(self.collateral_token).balance))
        usdc_balance = Decimal(str(market.balance(self.borrow_token).balance))
        usdc_price = Decimal(str(market.price(self.borrow_token)))

        return {
            "hf": hf,
            "collateral_usd": collateral_usd,
            "debt_usd": debt_usd,
            "lltv": lltv,
            "wbtc_balance": wbtc_balance,
            "usdc_balance": usdc_balance,
            "usdc_price": usdc_price,
        }

    def _supply_intent(self, wbtc_balance: Decimal) -> Intent:
        amount = (wbtc_balance * self.initial_supply_fraction_of_wbtc_balance).quantize(
            Decimal("0.00000001"), rounding=ROUND_DOWN
        )
        return Intent.supply(
            protocol=self.protocol,
            token=self.collateral_token,
            amount=amount,
            use_as_collateral=True,
            chain=self.chain,
        )

    def _borrow_tokens_to_target_hf(
        self, collateral_usd: Decimal, debt_usd: Decimal, lltv: Decimal, usdc_price: Decimal, target_hf: Decimal
    ) -> Decimal:
        if usdc_price <= 0 or target_hf <= 0:
            return Decimal("0")
        k_value = collateral_usd * lltv
        target_debt_usd = k_value / target_hf
        borrow_usd = target_debt_usd - debt_usd
        if borrow_usd <= 0:
            return Decimal("0")
        return self._quantize_usdc(borrow_usd / usdc_price)

    def _repay_tokens_to_target_hf(
        self, collateral_usd: Decimal, debt_usd: Decimal, lltv: Decimal, usdc_price: Decimal, target_hf: Decimal
    ) -> Decimal:
        if usdc_price <= 0 or target_hf <= 0:
            return Decimal("0")
        k_value = collateral_usd * lltv
        target_debt_usd = k_value / target_hf
        repay_usd = debt_usd - target_debt_usd
        if repay_usd <= 0:
            return Decimal("0")
        return self._quantize_usdc(repay_usd / usdc_price)

    def _forced_intent(self, market: MarketSnapshot) -> Intent:
        data = self._read_market_state(market)

        if self.force_action == "supply":
            amount = max(data["wbtc_balance"], self.min_action_wbtc)
            return Intent.supply(
                protocol=self.protocol,
                token=self.collateral_token,
                amount=amount,
                use_as_collateral=True,
                chain=self.chain,
            )

        if self.force_action == "borrow":
            borrow_amount = self._borrow_tokens_to_target_hf(
                collateral_usd=data["collateral_usd"],
                debt_usd=data["debt_usd"],
                lltv=data["lltv"],
                usdc_price=data["usdc_price"],
                target_hf=self.borrow_to_target_hf,
            )
            borrow_amount = max(borrow_amount, self.min_action_usdc)
            return Intent.borrow(
                protocol=self.protocol,
                collateral_token=self.collateral_token,
                collateral_amount=Decimal("0"),
                borrow_token=self.borrow_token,
                borrow_amount=borrow_amount,
                interest_rate_mode="variable",
                chain=self.chain,
            )

        if self.force_action == "repay":
            repay_amount = max(min(data["usdc_balance"], data["debt_usd"] / data["usdc_price"]), self.min_action_usdc)
            return Intent.repay(
                protocol=self.protocol,
                token=self.borrow_token,
                amount=self._quantize_usdc(repay_amount),
                interest_rate_mode="variable",
                chain=self.chain,
            )

        raise ValueError(f"Unknown force_action: {self.force_action!r}")

    def decide(self, market: MarketSnapshot) -> Intent:
        if self.force_action:
            try:
                return self._forced_intent(market)
            except _DATA_UNAVAILABLE_ERRORS as exc:
                return Intent.hold(reason=f"force_action data unavailable: {exc}")

        try:
            data = self._read_market_state(market)
        except _DATA_UNAVAILABLE_ERRORS as exc:
            return Intent.hold(reason=f"market data unavailable: {exc}")

        hf = data["hf"]
        collateral_usd = data["collateral_usd"]
        debt_usd = data["debt_usd"]
        lltv = data["lltv"]
        wbtc_balance = data["wbtc_balance"]
        usdc_balance = data["usdc_balance"]
        usdc_price = data["usdc_price"]

        self._last_hf = hf

        if collateral_usd <= self.min_collateral_usd_for_active_position:
            if wbtc_balance >= self.min_action_wbtc:
                self._last_action = "SUPPLY"
                return self._supply_intent(wbtc_balance)
            self._last_action = "HOLD"
            return Intent.hold(reason="No active collateral and insufficient WBTC to supply")

        if hf < self.emergency_mode_trigger:
            self._emergency_mode = True
            self._borrow_paused = True

        if self._emergency_mode and hf > self.resume_threshold:
            self._emergency_mode = False

        if hf < self.borrow_stop_threshold:
            self._borrow_paused = True
        elif self._borrow_paused and not self._emergency_mode and hf >= self.resume_threshold:
            self._borrow_paused = False

        if self._emergency_mode:
            if usdc_balance < self.min_action_usdc:
                self._last_action = "HOLD"
                return Intent.hold(reason="Emergency mode active, waiting for USDC to repay")

            repay_amount = usdc_balance
            if not self.aggressive_repay_in_emergency_mode:
                repay_amount = min(
                    usdc_balance,
                    self._repay_tokens_to_target_hf(
                        collateral_usd=collateral_usd,
                        debt_usd=debt_usd,
                        lltv=lltv,
                        usdc_price=usdc_price,
                        target_hf=self.resume_threshold,
                    ),
                )

            repay_amount = self._quantize_usdc(repay_amount)
            if repay_amount < self.min_action_usdc:
                self._last_action = "HOLD"
                return Intent.hold(reason="Emergency mode active but repay size below minimum")

            self._last_action = "REPAY"
            return Intent.repay(
                protocol=self.protocol,
                token=self.borrow_token,
                amount=repay_amount,
                interest_rate_mode="variable",
                chain=self.chain,
            )

        if hf < self.repay_if_hf_below or hf < self.health_factor_lower_bound:
            if usdc_balance < self.min_action_usdc:
                self._last_action = "HOLD"
                return Intent.hold(reason="HF below lower bound but no USDC available to repay")

            repay_needed = self._repay_tokens_to_target_hf(
                collateral_usd=collateral_usd,
                debt_usd=debt_usd,
                lltv=lltv,
                usdc_price=usdc_price,
                target_hf=self.repay_to_target_hf,
            )
            repay_amount = self._quantize_usdc(min(repay_needed, usdc_balance))

            if repay_amount < self.min_action_usdc:
                self._last_action = "HOLD"
                return Intent.hold(reason="HF below lower bound but repay size is below minimum")

            self._last_action = "REPAY"
            return Intent.repay(
                protocol=self.protocol,
                token=self.borrow_token,
                amount=repay_amount,
                interest_rate_mode="variable",
                chain=self.chain,
            )

        borrow_trigger = hf > self.borrow_if_hf_above or hf > self.health_factor_upper_bound or debt_usd <= 0
        if borrow_trigger and not self._borrow_paused:
            target_hf = self.initial_borrow_target_hf if debt_usd <= 0 else self.borrow_to_target_hf
            borrow_amount = self._borrow_tokens_to_target_hf(
                collateral_usd=collateral_usd,
                debt_usd=debt_usd,
                lltv=lltv,
                usdc_price=usdc_price,
                target_hf=target_hf,
            )

            if borrow_amount >= self.min_action_usdc:
                projected_debt_usd = debt_usd + (borrow_amount * usdc_price)
                if projected_debt_usd > 0:
                    projected_hf = (collateral_usd * lltv) / projected_debt_usd
                    if projected_hf < self.min_hf_after_borrow or projected_hf < self.health_factor_target:
                        self._last_action = "HOLD"
                        return Intent.hold(reason=f"Projected HF {projected_hf:.3f} below safety threshold")

                self._last_action = "BORROW"
                return Intent.borrow(
                    protocol=self.protocol,
                    collateral_token=self.collateral_token,
                    collateral_amount=Decimal("0"),
                    borrow_token=self.borrow_token,
                    borrow_amount=borrow_amount,
                    interest_rate_mode="variable",
                    chain=self.chain,
                )

        self._last_action = "HOLD"
        return Intent.hold(reason=f"HF {hf:.3f} inside policy band or borrowing paused")

    def on_intent_executed(self, intent: Intent, success: bool, result: Any) -> None:
        if not success:
            return
        self._last_action = getattr(getattr(intent, "intent_type", None), "value", self._last_action)

    def get_status(self) -> dict[str, Any]:
        return {
            "strategy": self.STRATEGY_NAME,
            "chain": self.chain,
            "protocol": self.protocol,
            "collateral_token": self.collateral_token,
            "borrow_token": self.borrow_token,
            "emergency_mode": self._emergency_mode,
            "borrow_paused": self._borrow_paused,
            "last_hf": str(self._last_hf),
            "last_action": self._last_action,
        }

    def get_persistent_state(self) -> dict[str, Any]:
        return {
            "emergency_mode": self._emergency_mode,
            "borrow_paused": self._borrow_paused,
            "last_hf": str(self._last_hf),
            "last_action": self._last_action,
        }

    def load_persistent_state(self, state: dict[str, Any]) -> None:
        if not state:
            return
        self._emergency_mode = bool(state.get("emergency_mode", False))
        self._borrow_paused = bool(state.get("borrow_paused", False))
        self._last_hf = Decimal(str(state.get("last_hf", "0")))
        self._last_action = str(state.get("last_action", "HOLD"))

    def get_open_positions(self):
        from datetime import UTC, datetime

        from almanak.framework.teardown import PositionInfo, PositionType, TeardownPositionSummary

        snapshot = self.create_market_snapshot()
        health = snapshot.position_health(protocol=self.protocol, market_id=self.market_id)
        collateral_usd = Decimal(str(getattr(health, "collateral_value_usd", Decimal("0"))))
        debt_usd = Decimal(str(getattr(health, "debt_value_usd", Decimal("0"))))

        positions: list[PositionInfo] = []
        if debt_usd > 0:
            positions.append(
                PositionInfo(
                    position_type=PositionType.BORROW,
                    position_id="aave-borrow-usdc",
                    chain=self.chain,
                    protocol=self.protocol,
                    value_usd=debt_usd,
                    details={"token": self.borrow_token, "market_id": self.market_id},
                )
            )
        if collateral_usd > 0:
            positions.append(
                PositionInfo(
                    position_type=PositionType.SUPPLY,
                    position_id="aave-supply-wbtc",
                    chain=self.chain,
                    protocol=self.protocol,
                    value_usd=collateral_usd,
                    details={"token": self.collateral_token, "market_id": self.market_id},
                )
            )

        return TeardownPositionSummary(
            deployment_id=getattr(self, "deployment_id", self.STRATEGY_NAME),
            timestamp=datetime.now(UTC),
            positions=positions,
        )

    def generate_teardown_intents(self, mode=None, market=None) -> list[Intent]:
        snapshot = market if market is not None else self.create_market_snapshot()
        health = snapshot.position_health(protocol=self.protocol, market_id=self.market_id)
        debt_usd = Decimal(str(getattr(health, "debt_value_usd", Decimal("0"))))
        if debt_usd <= 0:
            return []

        usdc_price = Decimal(str(snapshot.price(self.borrow_token)))
        usdc_wallet = Decimal(str(snapshot.balance(self.borrow_token).balance))
        if usdc_price <= 0 or usdc_wallet < self.min_action_usdc:
            return []

        debt_tokens = debt_usd / usdc_price
        if usdc_wallet >= debt_tokens:
            return [
                Intent.repay(
                    protocol=self.protocol,
                    token=self.borrow_token,
                    repay_full=True,
                    interest_rate_mode="variable",
                    chain=self.chain,
                )
            ]

        return [
            Intent.repay(
                protocol=self.protocol,
                token=self.borrow_token,
                amount=self._quantize_usdc(usdc_wallet),
                interest_rate_mode="variable",
                chain=self.chain,
            )
        ]
