from __future__ import annotations

import logging
import math
from functools import reduce

from exit_plans import ExitPlanStore
from mcp_client import AlpacaMcpClient
from risk_gates import group_option_positions, net_group_exposure

log = logging.getLogger(__name__)


class PositionMonitor:
    """Deterministic, LLM-free position management between Claude's decision
    cycles.

    Closes a whole position group (every leg sharing an underlying and
    expiration) at once, never a single leg in isolation. Two ways a group
    can trigger a close, checked per side:

      1. Claude's own exit plan (exit_plans.py) - a price-based take-profit
         and/or stop-loss on the group's net mark, and/or an invalidation
         level on the underlying's price.
      2. A config-driven stop_loss_pct/take_profit_pct fallback, computed
         against the group's net cost basis.

    The fallback applies per-side: if a plan sets take_profit_price but
    leaves stop_loss_price unset, the percentage-based stop-loss still
    protects that side.
    """

    def __init__(
        self,
        mcp: AlpacaMcpClient,
        order_tool_name: str,
        stop_loss_pct: float,
        take_profit_pct: float,
        exit_plan_store: ExitPlanStore,
    ):
        self.mcp = mcp
        self.order_tool_name = order_tool_name
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.exit_plan_store = exit_plan_store

    async def check_positions(self) -> None:
        positions = await self.mcp.get_positions()
        option_legs = [
            p
            for p in positions
            if p.get("asset_class") == "us_option" and abs(float(p.get("qty", 0) or 0)) > 0
        ]

        groups = group_option_positions(option_legs)

        for (root, expiry), legs in groups.items():
            reason = await self._should_close(root, expiry, legs)
            if reason is None:
                continue
            log.info(f"{reason} - closing group root={root} expiry={expiry} legs={len(legs)} together")
            await self._close_group(legs)
            self.exit_plan_store.clear(root, expiry)

    async def _should_close(self, root: str, expiry: str, legs: list[dict]) -> str | None:
        # Net mark: what closing the whole group right now would net. Long leg
        # price is money received selling it; short leg price is money paid
        # buying it back.
        net_mark = sum(
            (1 if leg.get("side") != "short" else -1) * float(leg.get("current_price", 0) or 0)
            for leg in legs
        )

        plan = self.exit_plan_store.get(root, expiry) or {}
        take_profit = plan.get("take_profit_price")
        stop_loss = plan.get("stop_loss_price")

        if take_profit is not None and net_mark >= float(take_profit):
            return f"exit plan take-profit hit (net_mark=${net_mark:.2f} >= ${float(take_profit):.2f})"
        if stop_loss is not None and net_mark <= float(stop_loss):
            return f"exit plan stop-loss hit (net_mark=${net_mark:.2f} <= ${float(stop_loss):.2f})"

        invalidation = plan.get("invalidation_price")
        direction = plan.get("invalidation_direction")
        if invalidation is not None and direction in ("above", "below"):
            last_price = await self.mcp.get_last_trade_price(root)
            if last_price is not None:
                crossed = (
                    last_price >= float(invalidation)
                    if direction == "above"
                    else last_price <= float(invalidation)
                )
                if crossed:
                    return (
                        f"exit plan invalidation hit ({root} last={last_price:.2f} "
                        f"{direction} {float(invalidation):.2f})"
                    )

        # Config-driven percentage fallback, against the group's net cost basis.
        # Applied per-field: only for whichever side Claude's plan left unset,
        # so an incomplete plan never leaves less downside protection than no
        # plan at all.
        net_cost_basis = net_group_exposure(legs)
        total_unrealized_pl = sum(float(leg.get("unrealized_pl", 0) or 0) for leg in legs)
        if net_cost_basis == 0:
            return None

        group_plpc = total_unrealized_pl / net_cost_basis
        if stop_loss is None and group_plpc <= -self.stop_loss_pct:
            return f"stop-loss triggered, fallback (group_plpc={group_plpc:.1%})"
        if take_profit is None and group_plpc >= self.take_profit_pct:
            return f"take-profit triggered, fallback (group_plpc={group_plpc:.1%})"
        return None

    async def _close_group(self, legs: list[dict]) -> None:
        """Closes every leg in one atomic multi-leg order rather than N
        separate orders - separate orders could leave one leg closed and the
        other still open (and uncovered) between fills, exactly the naked-risk
        shape risk_gates.py prevents at entry. A single-leg group uses the
        plain simple-order path instead, since Alpaca's mleg order class is
        documented for multi-leg strategies specifically."""
        # Position `qty` is an unsigned magnitude with a separate
        # `side: "long"/"short"` field, so direction comes from `side` here.
        if len(legs) == 1:
            leg = legs[0]
            qty = abs(float(leg.get("qty", 0) or 0))
            is_long = leg.get("side") != "short"
            await self.mcp.call_tool(
                self.order_tool_name,
                {
                    "symbol": leg["symbol"],
                    "side": "sell" if is_long else "buy",
                    "type": "market",
                    "time_in_force": "day",
                    "qty": str(qty),
                    "position_intent": "sell_to_close" if is_long else "buy_to_close",
                },
            )
            return

        # 2+ legs: one atomic multi-leg close. GCD of the legs' quantities
        # gives the unit count for a ratio spread (a 1x1 vertical reduces to
        # unit_count=1, ratio_qty=1 per leg).
        qtys = [int(abs(float(leg.get("qty", 0) or 0))) for leg in legs]
        unit_count = max(reduce(math.gcd, qtys), 1) if qtys else 1

        leg_payloads = [
            {
                "symbol": leg["symbol"],
                "side": "sell" if leg.get("side") != "short" else "buy",
                "position_intent": "sell_to_close" if leg.get("side") != "short" else "buy_to_close",
                "ratio_qty": str(qty // unit_count),
            }
            for leg, qty in zip(legs, qtys)
        ]

        await self.mcp.call_tool(
            self.order_tool_name,
            {
                "order_class": "mleg",
                "type": "market",
                "time_in_force": "day",
                "qty": str(unit_count),
                "legs": leg_payloads,
            },
        )
