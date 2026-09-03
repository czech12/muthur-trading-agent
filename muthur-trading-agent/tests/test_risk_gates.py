"""Standalone assertion-based checks for risk_gates.py - no pytest, no new
dependency. Run directly:

    python3 tests/test_risk_gates.py

Exits non-zero on any failure so it's usable in a CI step later, but today
its job is to make the risk gate's real guarantees checkable in seconds
rather than only trusted by reading the code - and to lock in regressions on
bugs that were once live (see comments on each test).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from risk_gates import (  # noqa: E402
    RiskConfig,
    RiskGate,
    group_option_positions,
    net_group_exposure,
    parse_occ_symbol,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILURES.append(name)


def make_gate(**overrides) -> RiskGate:
    defaults = dict(
        max_risk_per_trade_pct=0.03,
        max_options_exposure_pct=0.35,
        max_concurrent_positions=5,
        max_daily_loss_pct=0.07,
    )
    defaults.update(overrides)
    return RiskGate(RiskConfig(**defaults))


def base_call_kwargs(**overrides) -> dict:
    defaults = dict(
        equity=100_000.0,
        day_start_equity=100_000.0,
        open_position_count=0,
        current_options_exposure=0.0,
    )
    defaults.update(overrides)
    return defaults


def test_naked_short_rejected() -> None:
    """The one guarantee everything else is built on: a sell_to_open leg with
    no covering buy_to_open on the same underlying/expiry/right must be
    refused, regardless of how the order is otherwise framed."""
    gate = make_gate()
    order = {
        "order_class": "simple",
        "symbol": "AAPL260918C00200000",
        "side": "sell",
        "position_intent": "sell_to_open",
        "qty": "1",
    }
    result = gate.evaluate(order, **base_call_kwargs())
    check("naked short option is rejected", result.allowed is False)
    check(
        "naked short rejection reason mentions unbounded risk",
        result.reason is not None and "naked short" in result.reason,
        result.reason,
    )


def test_covered_credit_spread_uses_strike_width() -> None:
    """A real vertical credit spread (short 200C / long 205C, net credit -
    no limit_price given here to hit the credit fallback path) must be
    allowed, with max loss bounded by strike width x qty x 100, not treated
    as unbounded just because one leg is short."""
    gate = make_gate()
    order = {
        "order_class": "mleg",
        "qty": "2",
        "legs": [
            {"symbol": "AAPL260918C00200000", "position_intent": "sell_to_open", "ratio_qty": "1"},
            {"symbol": "AAPL260918C00205000", "position_intent": "buy_to_open", "ratio_qty": "1"},
        ],
    }
    result = gate.evaluate(order, **base_call_kwargs())
    check("covered credit spread is allowed", result.allowed is True, result.reason)
    check(
        "credit spread max loss = width x qty x 100",
        result.estimated_max_loss == 5 * 2 * 100,
        result.estimated_max_loss,
    )


def test_debit_spread_uses_limit_price_not_width() -> None:
    """Regression test for a real fixed bug: a covered bull call spread
    priced as a $3.80 net debit has a true max loss of $380/contract (what
    was paid), not the much larger strike-width-based figure. Pricing this
    by width would reject perfectly safe debit spreads."""
    gate = make_gate()
    order = {
        "order_class": "mleg",
        "qty": "1",
        "limit_price": "3.80",
        "legs": [
            {"symbol": "AAPL260918C00200000", "position_intent": "buy_to_open", "ratio_qty": "1"},
            {"symbol": "AAPL260918C00210000", "position_intent": "sell_to_open", "ratio_qty": "1"},
        ],
    }
    result = gate.evaluate(order, **base_call_kwargs())
    check("debit spread is allowed", result.allowed is True, result.reason)
    check(
        "debit spread max loss = limit_price x qty x 100, not strike width",
        result.estimated_max_loss == 380.0,
        result.estimated_max_loss,
    )


def test_plain_long_market_order_rejected_with_actionable_error() -> None:
    """Regression test for a real fixed bug: a single long call/put with no
    limit_price (a market order) has nothing to bound its cost against and
    must be rejected with a clear, actionable message - not the confusing
    "could not determine spread width for a covered short order" error that
    doesn't even apply when there's no short leg at all."""
    gate = make_gate()
    order = {
        "order_class": "simple",
        "symbol": "AAPL260918C00200000",
        "side": "buy",
        "position_intent": "buy_to_open",
        "qty": "1",
    }
    result = gate.evaluate(order, **base_call_kwargs())
    check("plain long market order is rejected", result.allowed is False)
    check(
        "rejection reason tells the caller to use a limit order",
        result.reason is not None and "limit" in result.reason.lower(),
        result.reason,
    )


def test_oversized_trade_rejected_by_per_trade_cap() -> None:
    """A debit spread costing more than max_risk_per_trade_pct of equity
    must be rejected on that basis, independent of the defined-risk check
    (which it otherwise passes cleanly)."""
    gate = make_gate(max_risk_per_trade_pct=0.03)
    order = {
        "order_class": "mleg",
        "qty": "10",
        "limit_price": "5.00",  # 10 * 5.00 * 100 = $5,000 = 5% of $100k equity
        "legs": [
            {"symbol": "AAPL260918C00200000", "position_intent": "buy_to_open", "ratio_qty": "1"},
            {"symbol": "AAPL260918C00210000", "position_intent": "sell_to_open", "ratio_qty": "1"},
        ],
    }
    result = gate.evaluate(order, **base_call_kwargs())
    check("trade over the per-trade cap is rejected", result.allowed is False)
    check(
        "rejection reason cites the per-trade cap",
        result.reason is not None and "per-trade cap" in result.reason,
        result.reason,
    )


def test_daily_loss_halt() -> None:
    """Once today's drawdown from session-start equity hits the configured
    cap, even an otherwise-safe order must be refused."""
    gate = make_gate(max_daily_loss_pct=0.07)
    order = {
        "order_class": "simple",
        "symbol": "AAPL260918C00200000",
        "side": "buy",
        "position_intent": "buy_to_open",
        "qty": "1",
        "limit_price": "1.00",
    }
    result = gate.evaluate(
        order,
        **base_call_kwargs(equity=92_000.0, day_start_equity=100_000.0),  # -8% today
    )
    check("daily loss halt blocks new entries once the cap is breached", result.allowed is False)
    check(
        "rejection reason cites the daily loss cap",
        result.reason is not None and "daily loss cap" in result.reason,
        result.reason,
    )


def test_closing_order_bypasses_daily_loss_halt() -> None:
    """Regression test for a real bug found while writing this suite: a pure
    closing order was being blocked by the daily-loss halt because that
    check ran before the order was ever identified as a close - exactly the
    wrong moment to refuse a close (the daily halt is precisely when you
    most need to be able to de-risk)."""
    gate = make_gate(max_daily_loss_pct=0.07)
    order = {
        "order_class": "simple",
        "symbol": "AAPL260918C00200000",
        "side": "sell",
        "position_intent": "sell_to_close",
        "qty": "1",
    }
    result = gate.evaluate(
        order,
        **base_call_kwargs(equity=80_000.0, day_start_equity=100_000.0),  # already past halt
    )
    check("a closing order is allowed even during a daily loss halt", result.allowed is True, result.reason)


def test_closing_order_bypasses_position_count_cap() -> None:
    """Regression test for the same bug class: a close was also being
    blocked by max_concurrent_positions - most dangerous exactly when
    already AT the cap and trying to close one of those positions."""
    gate = make_gate(max_concurrent_positions=5)
    order = {
        "order_class": "simple",
        "symbol": "AAPL260918C00200000",
        "side": "sell",
        "position_intent": "sell_to_close",
        "qty": "1",
    }
    result = gate.evaluate(order, **base_call_kwargs(open_position_count=5))
    check("a closing order is allowed even at the position-count cap", result.allowed is True, result.reason)


def test_position_grouping_treats_a_spread_as_one_position() -> None:
    """Regression test for a real fixed bug: a single 2-leg vertical spread
    must count as ONE position group, not two - counting raw legs silently
    choked headroom under max_concurrent_positions for no real reason."""
    positions = [
        {"symbol": "AAPL260918P00200000", "cost_basis": "-460.0"},
        {"symbol": "AAPL260918P00195000", "cost_basis": "350.0"},
        {"symbol": "NVDA260918C00185000", "cost_basis": "620.0"},
    ]
    groups = group_option_positions(positions)
    check("a 2-leg AAPL spread groups into exactly one entry", len(groups) == 2, groups.keys())
    aapl_group = groups[("AAPL", "260918")]
    check("the AAPL group contains both legs", len(aapl_group) == 2)


def test_net_group_exposure_nets_not_sums_abs() -> None:
    """Regression test for a real fixed bug: summing abs(cost_basis) per leg
    independently overstates a credit-financed spread's real risk (e.g.
    abs(910) + abs(-530) = 1440 instead of the real abs(910 - 530) = 380)."""
    legs = [
        {"cost_basis": "910.0"},
        {"cost_basis": "-530.0"},
    ]
    exposure = net_group_exposure(legs)
    check("net exposure nets signed values, not sum-of-abs", exposure == 380.0, exposure)


def test_parse_occ_symbol_rejects_non_option_input() -> None:
    """A plain equity symbol (or anything malformed) must parse to None so
    callers can treat "not an option" as a normal case, not an error."""
    check("a plain equity symbol parses to None", parse_occ_symbol("AAPL") is None)
    check(
        "a well-formed OCC symbol parses correctly",
        parse_occ_symbol("AAPL260918C00200000")
        == parse_occ_symbol("AAPL260918C00200000"),
    )


def main() -> int:
    tests = [
        test_naked_short_rejected,
        test_covered_credit_spread_uses_strike_width,
        test_debit_spread_uses_limit_price_not_width,
        test_plain_long_market_order_rejected_with_actionable_error,
        test_oversized_trade_rejected_by_per_trade_cap,
        test_daily_loss_halt,
        test_closing_order_bypasses_daily_loss_halt,
        test_closing_order_bypasses_position_count_cap,
        test_position_grouping_treats_a_spread_as_one_position,
        test_net_group_exposure_nets_not_sums_abs,
        test_parse_occ_symbol_rejects_non_option_input,
    ]
    for t in tests:
        print(f"{t.__name__}:")
        t()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
