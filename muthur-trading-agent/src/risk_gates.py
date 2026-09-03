from __future__ import annotations

import re
from dataclasses import dataclass

OPTION_MULTIPLIER = 100  # one US equity option contract = 100 shares

_OCC_SYMBOL_RE = re.compile(
    r"^(?P<root>[A-Z]{1,6})(?P<expiry>\d{6})(?P<right>[CP])(?P<strike>\d{8})$"
)


@dataclass(frozen=True)
class ParsedOption:
    root: str
    expiry: str  # YYMMDD
    right: str  # "C" or "P"
    strike: float


def parse_occ_symbol(symbol: str) -> ParsedOption | None:
    """Parse a standard OCC option symbol (e.g. AAPL241213C00250000).
    Returns None for equities or malformed input, so callers can treat
    "not an option" as a normal case rather than an error."""
    match = _OCC_SYMBOL_RE.match(symbol)
    if not match:
        return None
    return ParsedOption(
        root=match["root"],
        expiry=match["expiry"],
        right=match["right"],
        strike=int(match["strike"]) / 1000,
    )


def group_option_positions(positions: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Group option-leg positions by (underlying root, expiry), so a
    multi-leg spread counts as one position rather than N. Used for caps
    like max_concurrent_positions, which count positions, not legs."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for p in positions:
        parsed = parse_occ_symbol(p.get("symbol", ""))
        key = (parsed.root, parsed.expiry) if parsed else (p.get("symbol", ""), "")
        groups.setdefault(key, []).append(p)
    return groups


def net_group_exposure(legs: list[dict]) -> float:
    """Net cost basis of a position group: long premium paid minus short
    premium received. Summing each leg's cost basis by absolute value
    instead would overstate a credit-financed spread's real risk."""
    return abs(sum(float(leg.get("cost_basis", 0) or 0) for leg in legs))


@dataclass
class GateResult:
    allowed: bool
    reason: str | None = None
    estimated_max_loss: float = 0.0


@dataclass
class RiskConfig:
    max_risk_per_trade_pct: float
    max_options_exposure_pct: float
    max_concurrent_positions: int
    max_daily_loss_pct: float


class RiskGate:
    """Guardrails around every order Claude places. Claude sees only the
    allow/deny result, never a way to bypass this.

    1. Defined-risk enforcement: reject any order with unbounded worst-case
       loss (a naked short option), checked against the order's actual legs.
    2. Portfolio caps: per-trade risk, total options exposure, position
       count, and a same-day loss halt.
    """

    def __init__(self, config: RiskConfig):
        self.config = config

    def evaluate(
        self,
        order: dict,
        *,
        equity: float,
        day_start_equity: float,
        open_position_count: int,
        current_options_exposure: float,
    ) -> GateResult:
        # Closing orders only reduce exposure, so they bypass every cap below -
        # the daily-loss halt and position-count cap exist to stop new risk,
        # not to block a position from being closed.
        if not self._opens(order):
            return GateResult(True, estimated_max_loss=0.0)

        if day_start_equity > 0:
            daily_loss_pct = (day_start_equity - equity) / day_start_equity
            if daily_loss_pct >= self.config.max_daily_loss_pct:
                return GateResult(False, f"daily loss cap reached ({daily_loss_pct:.1%})")

        if open_position_count >= self.config.max_concurrent_positions:
            return GateResult(False, f"max concurrent positions reached ({open_position_count})")

        try:
            max_loss = self._estimate_max_loss(order)
        except ValueError as exc:
            return GateResult(False, f"could not evaluate risk of order: {exc}")

        if max_loss is None:
            return GateResult(
                False, "order has undefined/unbounded risk (naked short option) - rejected"
            )

        if equity > 0 and max_loss / equity > self.config.max_risk_per_trade_pct:
            return GateResult(
                False,
                f"estimated max loss ${max_loss:,.0f} exceeds per-trade cap "
                f"({self.config.max_risk_per_trade_pct:.1%} of ${equity:,.0f} equity)",
            )

        projected_exposure = current_options_exposure + max_loss
        if equity > 0 and projected_exposure / equity > self.config.max_options_exposure_pct:
            return GateResult(
                False,
                f"would push total options exposure to ${projected_exposure:,.0f}, over the "
                f"{self.config.max_options_exposure_pct:.1%} portfolio cap",
            )

        return GateResult(True, estimated_max_loss=max_loss)

    @staticmethod
    def _normalize_legs(order: dict) -> list[dict]:
        """Normalize both order shapes (single-leg, top-level fields; or
        multi-leg, legs[]) into a flat legs list."""
        legs = order.get("legs")
        if legs:
            return legs
        return [
            {
                "symbol": order.get("symbol"),
                "side": order.get("side"),
                "position_intent": order.get("position_intent"),
                "ratio_qty": order.get("qty", "1"),
            }
        ]

    def _opens(self, order: dict) -> list[dict]:
        """Legs that open new exposure (position_intent ending in
        "_to_open"). Empty for a pure closing order."""
        legs = self._normalize_legs(order)
        return [leg for leg in legs if (leg.get("position_intent") or "").endswith("_to_open")]

    def _estimate_max_loss(self, order: dict) -> float | None:
        """Worst-case dollar loss for `order`, or None if unbounded (a short
        leg with no covering long leg on the same underlying, expiration,
        and right). Only called for orders that open new exposure -
        `evaluate()` handles pure closes before this runs."""
        qty = float(order.get("qty") or 1)
        limit_price = order.get("limit_price")

        opens = self._opens(order)
        shorts = [leg for leg in opens if leg.get("position_intent") == "sell_to_open"]
        longs = [leg for leg in opens if leg.get("position_intent") == "buy_to_open"]

        for short_leg in shorts:
            parsed_short = parse_occ_symbol(short_leg["symbol"])
            if parsed_short is None:
                raise ValueError(f"unrecognized option symbol {short_leg['symbol']!r}")
            # "Covered" means a long leg on the same underlying, expiration, and
            # right (a short call needs a long call, not a put or a different
            # expiration) with quantity at least matching the short - a smaller
            # long position still leaves the excess short quantity naked.
            covered = any(
                (parsed_long := parse_occ_symbol(long_leg["symbol"])) is not None
                and parsed_long.root == parsed_short.root
                and parsed_long.expiry == parsed_short.expiry
                and parsed_long.right == parsed_short.right
                and float(long_leg.get("ratio_qty", 0)) >= float(short_leg.get("ratio_qty", 0))
                for long_leg in longs
            )
            if not covered:
                return None  # naked short - unbounded risk, hard reject

        if limit_price is not None and float(limit_price) >= 0:
            # Net debit: max loss is capped at what was paid. Every short leg
            # is already confirmed covered above, so this bound holds
            # regardless of leg count.
            return float(limit_price) * qty * OPTION_MULTIPLIER

        if not shorts:
            raise ValueError(
                "a plain long call/put needs an explicit limit_price to evaluate risk - "
                "submit it as a limit order, not a market order"
            )

        # Net credit (or no limit_price given): fall back to strike width x
        # qty as a conservative upper bound on max loss. The true max loss of
        # a credit spread is width minus credit received, but width alone is
        # always >= that and is cheap to compute.
        #
        # Matches on root only here, not expiration/right like the coverage
        # check above - deliberately looser, so a multi-leg order's actual
        # covering leg is never missed, and taking the max across every
        # same-root pairing keeps this a worst-case (never-too-low) bound
        # rather than tied to one specific pairing. The coverage check already
        # guarantees at least one same-root match per short leg, so `widths`
        # can't end up empty here - the ValueError below is unreached in
        # practice, kept as a defensive fallback.
        widths = []
        for short_leg in shorts:
            parsed_short = parse_occ_symbol(short_leg["symbol"])
            for long_leg in longs:
                parsed_long = parse_occ_symbol(long_leg["symbol"])
                if parsed_long and parsed_long.root == parsed_short.root:
                    widths.append(abs(parsed_long.strike - parsed_short.strike))
        if not widths:
            raise ValueError("could not determine spread width for a covered short order")
        return max(widths) * qty * OPTION_MULTIPLIER
