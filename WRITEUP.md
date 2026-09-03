# MU/TH/UR 8400 — Technical Write-Up

Built for the [Alpaca AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon).
Covers AI logic, risk gates, and Alpaca infrastructure implementation, per the hackathon's
submission requirement.

**Pre-event work disclosure:** the agent's core logic, risk gates, and MCP integration, along
with the self-hosted pipeline it deploys through, were built starting Aug 25, ahead of the
official Aug 28 kickoff — permitted under the hackathon's rules for infrastructure/boilerplate
built before the event. All live trading ran during the official window, from a dedicated
$100,000 paper account created for the contest, starting Monday, Aug 31 at 9:30 a.m. ET.

## AI Logic

Two independent loops split the work by how time-sensitive it is. The **Daily Agent Loop** runs
at the 45 minute mark of each trading hour and calls Claude Sonnet 5 to review the market and
every open position, then decides whether to open, hold, or close something. The **Position
Monitor** runs every 5 minutes
with no LLM call at all — it only checks each open position's exit plan against the current
market and closes it if a level has been hit.

Before Claude can act on a position, it has to record a thesis (the actual idea and its
evidence), an entry rationale (why this structure and strike, not just the direction), and an
invalidation condition (what would prove it wrong). That record persists and is read back on
every later cycle, so a position gets re-evaluated against its own original reasoning rather than
judged fresh each time. There is no fixed watchlist — each check-in scans news, movers, and
most-actives for candidates, and a single at-the-money quote is enough to drop one before its
full options chain is ever pulled.

## Risk Gates

A risk gate is a hardcoded checkpoint every order passes through before it reaches Alpaca — plain
Python, not a prompt instruction, so there's no way for the model to talk its way past it. Only
three trade shapes are legal: a long call or put, a debit vertical spread, or a credit vertical
spread. Any short leg must be matched by a long leg on the same underlying, expiration, and
right, of at least equal size — an uncovered short of any kind is rejected outright regardless of
how it's framed. Beyond that structural check, fixed portfolio limits apply: 3% of equity at risk
per trade, 35% of equity in open options positions at once, 5 concurrent positions maximum, and a
7% daily drawdown that halts new entries for the rest of the day.

## Alpaca Infrastructure Implementation

Every account read, market-data lookup, and order placement goes through
[Alpaca's own MCP server](https://github.com/alpacahq/alpaca-mcp-server), run as a local
subprocess — Claude never calls Alpaca's REST API directly. Tool names aren't hardcoded against
one API version; they're discovered from the server's own tool list at connect time and matched
by keyword, so the integration keeps working if Alpaca renames or adds tools upstream. The
order-placing tool is exactly where the risk gate intercepts — an order call goes through the
same MCP surface as every other tool call, it just never reaches Alpaca without clearing the gate
first. The Position Monitor shares this same MCP connection for reading live prices and closing
positions, rather than a second, separate integration. Everything runs against Alpaca's paper
trading environment.

## Lessons Learned

- **The agent traded less than I expected.** I figured it would day trade a lot more; over
  the 4 trading days it was pretty conservative, passing far more often than it opened a
  position. The risk gate's limits were probably a little tighter than they needed to be.
- **Options trading was mostly new to me.** Spreads, strikes, expirations — I picked up a real
  amount of it just from building and watching this agent work.
- **I learned what Claude is actually good for here.** Claude built something workable first,
  then I tweaked and tuned it — the risk limits, the prompts, the rough edges — until it matched
  what I actually wanted. I couldn't have built this myself in the time I had.
- **I had a lot of fun doing this.** This was my first hackathon, but definitely not my last.
  Thanks for putting this event together!
