from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from anthropic import AsyncAnthropic

from exit_plans import ExitPlanStore
from mcp_client import AlpacaMcpClient
from risk_gates import RiskGate

log = logging.getLogger(__name__)

# A local tool, handled entirely in-process and never routed to the MCP
# subprocess. Turns Claude's stated exit levels into a rule PositionMonitor
# enforces in real time. Each call replaces any prior plan for that
# (underlying, expiration) - Claude must include every field it wants active.
SET_EXIT_PLAN_TOOL = {
    "name": "set_exit_plan",
    "description": (
        "Register or update the concrete exit plan for one open option position "
        "group (all legs sharing the same underlying and expiration) so the fast "
        "position monitor - checked every few minutes, no LLM call - can act on it "
        "immediately, instead of your plan only being re-evaluated at your next "
        "hourly check-in. Call this whenever you open a position, or want to "
        "tighten/loosen an existing plan's levels. This replaces any prior plan for "
        "the same underlying+expiration, so include every level you want active."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "underlying": {"type": "string", "description": "Root ticker, e.g. NVDA"},
            "expiration": {
                "type": "string",
                "description": "Option expiration date, YYYY-MM-DD, matching the position's OCC symbol.",
            },
            "take_profit_price": {
                "type": "number",
                "description": (
                    "Close the whole group once its net mark (sum of each leg's own "
                    "price, long legs positive and short legs negative - i.e. what "
                    "you'd net closing right now) reaches this level. Omit if you "
                    "don't want a price-based take-profit for this group."
                ),
            },
            "stop_loss_price": {
                "type": "number",
                "description": "Close the group once its net mark falls to this level. Omit to skip.",
            },
            "invalidation_price": {
                "type": "number",
                "description": (
                    "Close the group if the underlying's own last trade price "
                    "crosses this level (e.g. a stated breakeven or support/"
                    "resistance level being broken) - independent of the group's "
                    "option pricing. Requires invalidation_direction. Omit to skip."
                ),
            },
            "invalidation_direction": {
                "type": "string",
                "enum": ["above", "below"],
                "description": (
                    "Whether crossing invalidation_price means trading ABOVE it "
                    "(e.g. a bearish thesis broken by a rally) or BELOW it (a "
                    "bullish thesis broken by a breakdown). Required if "
                    "invalidation_price is set."
                ),
            },
            "note": {
                "type": "string",
                "description": "Short reason, logged when this plan is set and again if it triggers a close.",
            },
        },
        "required": ["underlying", "expiration"],
    },
}

# Hard cap on tool-call round trips per decision cycle. Bounds both API cost and how
# long a single cycle can run if Claude keeps retrying rejected orders - same spirit
# as failure_guard.py's hard-exit: don't trust an in-process loop to bound itself,
# bound it from the outside. A typical cycle finishes in well under this many turns;
# this exists to cap the rare long tail, not to constrain normal behavior.
MAX_TURNS = 8

SYSTEM_PROMPT_TEMPLATE = """\
You are a disciplined, experienced options trader running an autonomous agent on an \
Alpaca paper trading account, built for the Alpaca AI Trading Agents Hackathon. Trade \
with the judgment of someone who has run a real book, not a checklist to satisfy - \
skeptical of a good story that isn't a real edge, terse rather than explanatory for its \
own sake, and unwilling to size or hold a position beyond what the actual evidence \
supports. You get a decision check-in during market hours, nominally around once an \
hour - but the real gap since your last check-in varies (a redeploy can trigger one \
early, a slow stretch can leave more time between them) and is stated explicitly at \
the start of every check-in below. Trust that stated number, not an assumption, when \
deciding how far back to look and how much may have changed. Review current account \
state and open positions, then find and evaluate real opportunities across the market \
- not a fixed list of tickers - and decide whether to open, adjust, or close any \
positions right now.

This account's total equity is measured as of market OPEN on Friday, September 4, \
2026 (9:30am ET) - not market close. That means Friday's own check-ins (the first is \
9:45am, after the snapshot already happened) have zero effect on the scored outcome - \
Thursday, September 3's close is the real deadline for anything to matter, a full \
trading day earlier than it might first appear. This is a planning constraint, not a \
target to chase: weigh whether a thesis realistically has time to play out given a \
position's expiration and the trading days actually left before Thursday's close, the \
same way you'd weigh it against any other deadline. It's not a reason to force a \
trade, take on more risk, or abandon the same research standard as any other day - a \
bad trade taken to "do something" before the window closes is still a bad trade, and \
there is specifically no reason to do anything different on Friday itself.

RESEARCH AND DECISION QUALITY ARE THE MOST IMPORTANT PART OF THIS AGENT - more \
important than trading frequently. Do not pick a trade first and rationalize it \
afterward. For every position you open, work through and state:
- THESIS: what specific, checkable thing do you believe about this underlying, and \
what evidence (price action, news, relative volume, options positioning) supports it?
- ENTRY: why this structure (long call/put, debit spread, or credit spread), this \
strike/width, this expiration - not just "a call because it's going up."
- INVALIDATION: what would prove this thesis wrong, and therefore ends the trade? A \
plan without a stated invalidation condition isn't a plan.
The code-level stop-loss/take-profit percentages (config-driven, checked every few \
minutes regardless of what you decide) are a backstop for positions with no explicit \
plan, not a substitute for having your own exit logic in mind.

There are exactly three trade shapes available here, and none of them is a default \
over the others - pick whichever one actually fits this trade:
- Long call or long put: one leg, no short. Max loss is the premium paid.
- Debit vertical spread: a covered short leg plus a long leg, net debit paid. Max \
loss is that debit.
- Credit vertical spread: a covered short leg plus a long leg, net credit received. \
Max loss is the strike width between the two legs, not the credit received - a \
conservative worst case, so size accordingly.

Whenever you open a position, or want to change the exit levels for one you're \
already holding, call set_exit_plan with the concrete price levels - this makes the \
fast, LLM-free position monitor enforce your plan in real time between check-ins, \
instead of it just sitting in this summary until you happen to notice it again next \
hour. State the plan, then register it - don't just describe it in prose. When \
you're opening a new position, call the order tool and set_exit_plan together in the \
same turn (both as tool calls in one response) rather than spacing them across two - \
set_exit_plan doesn't need the order's fill result, it only needs the levels you \
already decided on, so there's no reason to spend a separate turn on it.

A plain single-leg long call or put must be submitted as a limit order with an \
explicit limit_price, not a market order - the risk gate has no way to bound a \
market order's cost before it fills and will reject it. For a real conditional \
entry instead of an immediate one, use order type stop or stop_limit with a \
stop_price - it will only trigger if and when the market actually reaches that \
level. Multi-leg (spread) orders don't support conditional entry at Alpaca - type \
must be market or limit for those, so a spread entry always executes at current \
market pricing, immediately, when you place it.

You are given a log of your own recent check-ins below (Recent decision history). \
Use it: if you have an open position, check whether the thesis you stated for it \
still holds rather than re-deriving your view on that underlying from scratch. If a \
prior check-in was looking at a name but didn't act, it's fine to revisit it - but \
say so explicitly rather than presenting it as a brand new idea.

There is no watchlist. Use the market-scanning tools available to you (movers, most \
active stocks, news) to find real candidates each check-in, rather than reasoning \
about the same handful of names every time. Prefer underlyings with real liquidity \
and tight options spreads - a technically-correct trade on a thinly-traded name can \
still be a bad trade once the bid/ask spread eats the edge - but don't limit yourself \
to a fixed set of mega-caps either; the point is genuine opportunity discovery.

The movers/most-active lists are frequently dominated by sub-$5 stocks, warrants, and \
other illiquid names with no real options market - skip anything trading under $5/share \
without spending a turn drilling into it, and don't spend output re-listing which \
illiquid names you skipped each check-in - a one-line "movers list was mostly \
illiquid, skipped" is enough. get_news has historically surfaced better candidates \
than the movers/most-active tools; lean on it first for discovery, and use \
movers/actives as a secondary check.

A normal-priced stock can still have a dead options market - price alone doesn't \
tell you that, only checking does. For any candidate that clears the price filter, \
check a single at-the-money quote first as a fast liquidity gate before pulling a \
full multi-strike chain or building out a specific structure - if that one quote \
shows a wide spread or no real market, that's enough to pass on the name without \
spending more turns mapping out its whole chain.

Timestamps inside market-data and news tool results (bars, quotes, trades, \
articles) are in UTC, not Eastern - convert before quoting a specific time of day \
in your reasoning or summary. Subtract 4 hours for Eastern Daylight Time (in \
effect for the entire contest week), state it in 24-hour time, and label it EDT, \
not ET (e.g. a 13:35 UTC bar is "09:35 EDT", not "13:30 ET" or "9:35 AM ET"). The \
current_time_et given at the start of this check-in is already Eastern and needs \
no conversion.

This is a competition judged on your account's total equity during the official \
scoring window, plus the creativity, autonomy, and robustness of your trading \
workflow - not on how cautious you are. An agent that never trades has nothing to \
show on either measure, so treat "no workable setup was found" as a real finding you \
should only reach after actually looking - scanning for real candidates, considering \
both calls and puts, and considering both a simple long option and a vertical spread \
- not a default you fall back to when nothing looks obviously great. \
Never place a trade purely to break a quiet streak or to have something to show, \
though - an unresearched trade you can't defend hurts both of those measures worse \
than a well-reasoned pass does, precisely because research quality is the point.

Hard rules, enforced in code - not just guidance:
- Every short (sell_to_open) leg must be fully covered by a long (buy_to_open) leg \
on the same underlying, expiration, and right - a naked short leg of any kind is \
rejected automatically before it ever reaches Alpaca, no exceptions regardless of \
how it's framed. This account never holds the underlying stock, so covered calls \
and cash-secured puts are not actually possible here even though the account's \
options approval level lists them - don't spend a turn attempting either.
- Every order is checked against per-trade and portfolio-wide risk caps before \
submission. If an order is rejected, the tool result tells you why - adjust size or \
structure and try again, or move on to a different idea.
- You have {max_turns} tool-call turns this check-in. Use them efficiently: check \
account/market state across a few symbols, decide, act. Don't loop indefinitely \
re-checking the same data. You'll get an explicit reminder once only a few turns \
remain - treat that as a hard signal to wrap up, not a suggestion.

End your final turn with a short plain-text summary covering: what you decided, the \
thesis/invalidation for anything opened or still held, and what (if anything) you \
looked at but passed on and why. This doubles as the trading log entry for this \
check-in, and is fed back to you as context on your next one.
"""


@dataclass
class CycleResult:
    summary: str
    orders_approved: int
    # Sum of estimated_max_loss for orders approved this cycle. main.py folds
    # this into its exposure baseline, since a resting order isn't in
    # get_positions() yet.
    exposure_added: float = 0.0


class DailyAgentLoop:
    """Drives one decision cycle: builds context, calls Claude, and dispatches
    tool_use blocks to the MCP session, routing order-placing calls through
    RiskGate first. A manual agentic loop (not the SDK's Tool Runner) because
    tool execution is delegated to the MCP subprocess, not local functions."""

    def __init__(
        self,
        anthropic_client: AsyncAnthropic,
        model: str,
        mcp: AlpacaMcpClient,
        risk_gate: RiskGate,
        order_tool_name: str,
        exit_plan_store: ExitPlanStore,
    ):
        self.client = anthropic_client
        self.model = model
        self.mcp = mcp
        self.risk_gate = risk_gate
        self.order_tool_name = order_tool_name
        self.exit_plan_store = exit_plan_store

    async def run_cycle(
        self,
        *,
        equity: float,
        day_start_equity: float,
        open_position_count: int,
        current_options_exposure: float,
        idle_streak: int,
        current_time_et: str,
        recent_history: list[str] = (),
        minutes_since_last_checkin: int | None = None,
        trading_allowed: bool = True,
    ) -> CycleResult:
        if idle_streak <= 0:
            streak_note = "No idle streak right now - the last check-in placed a trade."
        else:
            streak_note = (
                f"Heads up: the last {idle_streak} check-in(s) in a row ended with no trade. "
                "That's fine if the market genuinely has nothing workable, but make sure "
                "you're actually scanning for candidates (movers/actives, not just "
                "reasoning from memory) and considering both call/put and spread "
                "structures before concluding that again."
            )

        # Tool list and system prompt are identical across turns of a cycle -
        # cache_control means only the first turn pays full price for them.
        # streak_note changes cycle-to-cycle, so it stays out of the cached
        # `system` block (goes in the first user message instead) to avoid
        # invalidating the cache every cycle.
        system = SYSTEM_PROMPT_TEMPLATE.format(max_turns=MAX_TURNS)
        tools = self.mcp.anthropic_tools() + [SET_EXIT_PLAN_TOOL]
        if not trading_allowed:
            # Pre-market research check-in: the order tool is structurally
            # removed, not just discouraged in the prompt, so no real order can
            # be placed before the market opens regardless of what Claude decides.
            tools = [t for t in tools if t["name"] != self.order_tool_name]
        tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
        if recent_history:
            history_block = "Recent decision history (oldest first, most recent last):\n" + "\n---\n".join(
                recent_history
            )
        else:
            history_block = "Recent decision history: none yet - this is the first check-in this run."

        if minutes_since_last_checkin is None:
            gap_note = "This is the first check-in this run - no prior gap to report."
        elif minutes_since_last_checkin < 45:
            gap_note = (
                f"Only {minutes_since_last_checkin} minutes since your last check-in "
                f"(shorter than the nominal hourly gap - likely a restart/redeploy "
                f"triggered this one early). Don't re-run a full session analysis you "
                f"already just did minutes ago; focus on what's actually new since then."
            )
        else:
            gap_note = f"{minutes_since_last_checkin} minutes since your last check-in."

        if trading_allowed:
            opening_note = (
                f"Begin this decision check-in. Current date/time: {current_time_et} "
                f"(America/New_York) - use this, not an assumption, for anything "
                f"expiration-date-related. {gap_note} {streak_note}"
            )
        else:
            opening_note = (
                f"Pre-market research check-in. Current date/time: {current_time_et} "
                f"(America/New_York) - regular trading hours haven't started yet, so the "
                f"order tool isn't available this check-in and nothing you decide here can "
                f"place a trade. Use this time well: check overnight news, pre-market movers "
                f"and quotes, anything that changed since yesterday, and note candidates and a "
                f"tentative plan (thesis, structure, invalidation) for the first trading "
                f"check-in after the open. Your notes here carry forward into that check-in's "
                f"recent decision history, same as any other check-in's."
            )

        messages: list[dict] = [
            {
                "role": "user",
                "content": f"{opening_note}\n\n{history_block}",
            }
        ]

        summary = ""
        orders_approved = 0
        exposure_added = 0.0
        for turn in range(MAX_TURNS):
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=8096,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=tools,
                thinking={"type": "adaptive"},
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})

            usage = response.usage
            log.info(
                f"turn={turn} input_tokens={usage.input_tokens} "
                f"cache_read_input_tokens={getattr(usage, 'cache_read_input_tokens', 0)} "
                f"cache_creation_input_tokens={getattr(usage, 'cache_creation_input_tokens', 0)} "
                f"output_tokens={usage.output_tokens}"
            )

            text_blocks = [block.text for block in response.content if block.type == "text"]
            if text_blocks:
                summary = "\n".join(text_blocks)

            if response.stop_reason != "tool_use":
                if response.stop_reason not in ("end_turn", "stop_sequence"):
                    # max_tokens or refusal - the summary about to be persisted
                    # may be incomplete. Log it rather than treating this the
                    # same as a normal finish.
                    log.warning(
                        f"decision cycle ended with unusual stop_reason={response.stop_reason!r} "
                        f"- summary may be incomplete"
                    )
                break

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result_text, approved, added = await self._dispatch_tool(
                    block.name,
                    block.input,
                    equity=equity,
                    day_start_equity=day_start_equity,
                    # Each approval raises the count/exposure the next tool call
                    # in this cycle is evaluated against, so a second order can't
                    # sneak past caps the first already pushed past.
                    open_position_count=open_position_count + orders_approved,
                    current_options_exposure=current_options_exposure + exposure_added,
                )
                if approved:
                    orders_approved += 1
                    exposure_added += added
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": result_text}
                )

            # Claude knows the total turn budget from the system prompt but not how
            # much is already spent - inject a countdown once turns get scarce
            # (last 3), rather than every turn.
            turns_left_after_this = MAX_TURNS - (turn + 1)
            if turns_left_after_this <= 3:
                tool_results.append(
                    {
                        "type": "text",
                        "text": (
                            f"[{turns_left_after_this} tool-call turn(s) left after this "
                            f"one, out of {MAX_TURNS} total. Converge: finish evaluating "
                            f"what you've already started rather than opening a new line "
                            f"of research, and end with your summary before you run out.]"
                        ),
                    }
                )

            messages.append({"role": "user", "content": tool_results})
        else:
            log.warning(f"decision cycle hit MAX_TURNS={MAX_TURNS} without concluding")

        return CycleResult(summary=summary, orders_approved=orders_approved, exposure_added=exposure_added)

    async def _dispatch_tool(
        self,
        name: str,
        arguments: dict,
        *,
        equity: float,
        day_start_equity: float,
        open_position_count: int,
        current_options_exposure: float,
    ) -> tuple[str, bool, float]:
        if name == "set_exit_plan":
            return self._handle_set_exit_plan(arguments), False, 0.0

        if name == self.order_tool_name:
            gate_result = self.risk_gate.evaluate(
                arguments,
                equity=equity,
                day_start_equity=day_start_equity,
                open_position_count=open_position_count,
                current_options_exposure=current_options_exposure,
            )
            if not gate_result.allowed:
                log.warning(f"order rejected by risk gate: {gate_result.reason} arguments={arguments}")
                error = json.dumps({"error": f"order rejected by risk gate: {gate_result.reason}"})
                return error, False, 0.0
            log.info(
                f"order approved by risk gate estimated_max_loss=${gate_result.estimated_max_loss:,.0f} "
                f"arguments={arguments}"
            )
            result_text = await self.mcp.call_tool(name, arguments)
            return result_text, True, gate_result.estimated_max_loss

        return await self.mcp.call_tool(name, arguments), False, 0.0

    def _handle_set_exit_plan(self, arguments: dict) -> str:
        """In-process only - never touches Alpaca or the MCP subprocess. Light
        validation to catch a nonsensical plan (e.g. stop above take-profit)
        that would otherwise silently never trigger."""
        underlying = arguments.get("underlying")
        expiration = arguments.get("expiration")
        if not underlying or not expiration:
            return json.dumps({"error": "underlying and expiration are required"})

        take_profit = arguments.get("take_profit_price")
        stop_loss = arguments.get("stop_loss_price")
        invalidation = arguments.get("invalidation_price")
        direction = arguments.get("invalidation_direction")

        if stop_loss is not None and take_profit is not None and float(stop_loss) >= float(take_profit):
            return json.dumps(
                {"error": "stop_loss_price must be lower than take_profit_price - plan not saved"}
            )
        if invalidation is not None and direction not in ("above", "below"):
            return json.dumps(
                {"error": "invalidation_direction ('above' or 'below') is required when invalidation_price is set - plan not saved"}
            )

        plan = {
            "take_profit_price": take_profit,
            "stop_loss_price": stop_loss,
            "invalidation_price": invalidation,
            "invalidation_direction": direction,
            "note": arguments.get("note", ""),
        }
        self.exit_plan_store.set(underlying, expiration, plan)
        log.info(f"exit plan set underlying={underlying} expiration={expiration} plan={plan}")
        return json.dumps({"status": "plan saved", "underlying": underlying, "expiration": expiration})
