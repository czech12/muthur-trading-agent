from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic

from agent_loop import DailyAgentLoop
from config import load_config
from exit_plans import ExitPlanStore
from failure_guard import RapidFailureGuard
from mcp_client import AlpacaMcpClient
from position_monitor import PositionMonitor
from risk_gates import RiskGate, group_option_positions, net_group_exposure

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    stream=sys.stdout,
)
logging.getLogger().addHandler(RapidFailureGuard())
log = logging.getLogger("alpaca-hackathon-agent")

HEARTBEAT_PATH = os.environ.get("HEARTBEAT_PATH", "/tmp/heartbeat")
BUILD_INFO_PATH = os.environ.get("BUILD_INFO_PATH", "/app/BUILD_INFO")
# Independent of monitor_interval_seconds - the Dockerfile HEALTHCHECK expects a
# heartbeat at least every 90s regardless of how far apart real work is spaced out.
HEARTBEAT_INTERVAL_SECONDS = 30

# Persisted research journal (recent_history), mounted on a small PVC so it
# survives a pod restart across the multi-day contest. Load/save below degrade
# to in-memory-only on failure rather than crashing.
STATE_PATH = os.environ.get("STATE_PATH", "/app/state/history.json")
# Claude's stated exit levels per open position group, enforced in real time by
# PositionMonitor. Set explicitly per environment so dev/contest never share state.
EXIT_PLANS_PATH = os.environ.get("EXIT_PLANS_PATH", "/app/state/exit_plans.json")
# ~1 trading day of hourly check-ins - keeps the prompt bounded over a multi-day
# run. This is research continuity (thesis/invalidation), not a full audit log.
MAX_HISTORY_ENTRIES = 8

_ET = ZoneInfo("America/New_York")

# Official P&L measurement window ends at market OPEN on Friday Sep 4 2026, 9:30am
# ET, per Alpaca's FAQ - every decision slot on that day fires after this snapshot
# and has zero effect on score. Skip decision cycles past this point; position_monitor
# (no LLM call) keeps running so open positions stay protected.
SCORING_WINDOW_END_ET = datetime(2026, 9, 4, 9, 30, tzinfo=_ET)

# Accepts both ET and EDT since persisted history entries may carry either
# label depending on when they were written.
_HISTORY_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) (?:ET|EDT)")


def minutes_since_last_checkin(recent_history: list[str], now_et: datetime) -> int | None:
    """Real gaps vary (restarts, holidays, uneven decision_times_et slots), so
    this is handed to the model directly rather than left for it to infer
    from raw timestamps."""
    if not recent_history:
        return None
    match = _HISTORY_TS_RE.match(recent_history[-1])
    if not match:
        return None
    try:
        last_dt = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=_ET)
    except ValueError:
        return None
    return max(0, int((now_et - last_dt).total_seconds() // 60))


async def _heartbeat_loop() -> None:
    """Touches the heartbeat file on a fixed cadence, independent of the main
    loop - a decision cycle can itself run for minutes. See the call site in
    run_forever() for why this matters."""
    while True:
        touch_heartbeat()
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


def touch_heartbeat() -> None:
    with open(HEARTBEAT_PATH, "w") as f:
        f.write(str(time.time()))


def read_build_info() -> str:
    try:
        with open(BUILD_INFO_PATH) as f:
            return f.read().strip()
    except OSError:
        return "unknown"


def load_history(path: str) -> list[str]:
    try:
        with open(path) as f:
            data = json.load(f)
        return [str(entry) for entry in data] if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception:
        log.exception(f"failed to load persisted history from {path}, starting empty")
        return []


def save_history(path: str, history: list[str]) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(history, f)
    except Exception:
        log.exception(f"failed to persist history to {path} - continuing in-memory only")


def due_slots(
    now_et: datetime, decision_times: list[dt_time], fired_today: set[dt_time]
) -> list[dt_time]:
    return [t for t in decision_times if now_et.time() >= t and t not in fired_today]


async def run_premarket_research(
    mcp: AlpacaMcpClient,
    agent: DailyAgentLoop,
    now_et: datetime,
    equity: float,
    day_start_equity: float | None,
    pending_exposure_today: float,
    idle_streak: int,
    recent_history: list[str],
) -> None:
    """Research-only check-in before the open (news, pre-market movers/quotes).
    The order tool is unavailable this cycle (trading_allowed=False), so this
    can never place a trade - it just seeds recent_history for the first real
    check-in after the open. Mutates `recent_history` in place; the caller
    persists it afterward."""
    try:
        clock = await mcp.get_clock()
    except Exception:
        log.exception("failed to fetch market clock for premarket research, skipping")
        return

    # is_open is always False before 9:30 ET, so use next_open instead: if it
    # falls later today, today is a trading day; otherwise it isn't.
    next_open_raw = clock.get("next_open")
    is_trading_day_today = False
    if next_open_raw:
        try:
            is_trading_day_today = datetime.fromisoformat(next_open_raw).astimezone(_ET).date() == now_et.date()
        except ValueError:
            log.warning(f"could not parse clock next_open={next_open_raw!r}")

    if not is_trading_day_today or clock.get("is_open"):
        log.info("skipping premarket research - not a trading day today, or market already open")
        return

    try:
        positions = await mcp.get_positions()
        options_positions = [p for p in positions if p.get("asset_class") == "us_option"]
        groups = group_option_positions(options_positions)
        options_exposure = pending_exposure_today + sum(
            net_group_exposure(legs) for legs in groups.values()
        )
        result = await agent.run_cycle(
            equity=equity,
            day_start_equity=day_start_equity or equity,
            open_position_count=len(groups),
            current_options_exposure=options_exposure,
            idle_streak=idle_streak,
            recent_history=recent_history,
            current_time_et=now_et.strftime("%Y-%m-%d %H:%M %A"),
            minutes_since_last_checkin=minutes_since_last_checkin(recent_history, now_et),
            trading_allowed=False,
        )
        log.info(f"premarket research summary: {result.summary}")
        recent_history.append(
            f"[{now_et.strftime('%Y-%m-%d %H:%M')} EDT, pre-market research] {result.summary}"
        )
        del recent_history[:-MAX_HISTORY_ENTRIES]
    except Exception:
        log.exception("premarket research check-in failed")


async def run_forever() -> None:
    log.info(f"Running alpaca-hackathon-agent v{read_build_info()}")

    api_key = os.environ["ALPACA_API_KEY"]
    secret_key = os.environ["ALPACA_SECRET_KEY"]

    config = load_config()
    # ANTHROPIC_API_KEY is picked up automatically if set; otherwise the SDK
    # falls back to an `ant auth login` OAuth profile if present.
    anthropic_client = AsyncAnthropic()

    log.info(f"trading_enabled={config.trading_enabled}")
    log.info(f"decision_times_et={config.decision_times_et}")

    async with AlpacaMcpClient(api_key, secret_key, paper=config.paper) as mcp:
        # Fail fast if the connected account doesn't match config: the contest
        # rules disqualify a submission run on a reused account, and this process
        # runs unattended for days. Unset expected_account_number (the default)
        # skips this check - it's the dev/test posture.
        startup_account = await mcp.get_account()
        connected_account_number = startup_account.get("account_number")
        log.info(f"connected_account_number={connected_account_number}")
        if config.expected_account_number and connected_account_number != config.expected_account_number:
            raise RuntimeError(
                f"SAFETY HALT: connected to account_number={connected_account_number!r} but "
                f"config expects expected_account_number={config.expected_account_number!r} - "
                "refusing to trade against the wrong Alpaca account. Fix the ALPACA_API_KEY/"
                "ALPACA_SECRET_KEY env vars or the expected_account_number in config to match."
            )

        # The MCP server exposes separate order tools per asset class
        # (place_stock_order / place_crypto_order / place_option_order), so
        # both keywords are needed to find the options one specifically.
        order_tool = mcp.find_tool(["option", "order"])
        if order_tool is None:
            raise RuntimeError(
                "could not find an options order-placing tool on alpaca-mcp-server - "
                f"available tools: {[t.name for t in mcp.tools]}"
            )
        log.info(f"using order_tool={order_tool}")

        exit_plan_store = ExitPlanStore(EXIT_PLANS_PATH)
        risk_gate = RiskGate(config.risk)
        agent = DailyAgentLoop(
            anthropic_client,
            config.anthropic_model,
            mcp,
            risk_gate,
            order_tool,
            exit_plan_store,
        )
        monitor = PositionMonitor(
            mcp, order_tool, config.stop_loss_pct, config.take_profit_pct, exit_plan_store
        )

        fired_slots_date: date | None = None
        fired_slots_today: set[dt_time] = set()
        premarket_fired_today = False
        last_equity_date: date | None = None
        last_work_ts = 0.0
        day_start_equity: float | None = None
        # Consecutive check-ins with zero approved trades. In-memory only - losing
        # this on restart just resets the "keep looking harder" nudge.
        idle_streak = 0
        # Resting/unfilled orders approved today but not yet in get_positions(),
        # folded into the exposure baseline so several same-day orders can't each
        # individually clear caps while collectively blowing past them. Resets at
        # day rollover, same as day_start_equity.
        pending_exposure_today = 0.0
        scoring_window_end_logged = False

        recent_history: list[str] = load_history(STATE_PATH)
        log.info(f"loaded {len(recent_history)} persisted history entries from {STATE_PATH}")

        # Runs concurrently with the main loop, not once per iteration - a decision
        # cycle with many tool-call turns can run for minutes, which would otherwise
        # leave the heartbeat stale past the HEALTHCHECK's 90s threshold and risk a
        # mid-cycle restart (e.g. between placing an order and registering its exit
        # plan). asyncio's cooperative scheduling lets this task run during any
        # `await` in the main loop, regardless of how long that work takes.
        heartbeat_task = asyncio.create_task(_heartbeat_loop())

        while True:
            # Ticks on the short heartbeat interval rather than sleeping for the
            # full monitor_interval_seconds directly, so a shutdown signal during
            # `asyncio.sleep` is never blocked behind a multi-minute wait - the
            # actual work below still only runs once per monitor_interval_seconds.
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

            if time.time() - last_work_ts < config.monitor_interval_seconds:
                continue
            last_work_ts = time.time()

            try:
                trading_enabled = load_config().trading_enabled
            except Exception:
                log.exception("failed to reload config for trading_enabled check, keeping previous value")
                trading_enabled = config.trading_enabled

            try:
                account = await mcp.get_account()
                equity = float(account["equity"])
            except Exception:
                log.exception("failed to fetch account state this cycle, skipping")
                continue

            now_et = datetime.now(_ET)
            if last_equity_date != now_et.date():
                day_start_equity = equity
                pending_exposure_today = 0.0
                last_equity_date = now_et.date()
            if fired_slots_date != now_et.date():
                fired_slots_today = set()
                premarket_fired_today = False
                fired_slots_date = now_et.date()

            try:
                await monitor.check_positions()
            except Exception:
                log.exception("position monitor cycle failed")

            if not trading_enabled:
                log.info("trading paused, skipping decision cycle")
                continue

            if now_et >= SCORING_WINDOW_END_ET:
                if not scoring_window_end_logged:
                    log.info(
                        f"past scoring window end ({SCORING_WINDOW_END_ET.isoformat()}) - "
                        "skipping all further decision cycles to conserve API budget; "
                        "position monitor keeps running"
                    )
                    scoring_window_end_logged = True
                continue

            if (
                config.premarket_research_time_et is not None
                and not premarket_fired_today
                and now_et.time() >= config.premarket_research_time_et
            ):
                premarket_fired_today = True
                await run_premarket_research(
                    mcp, agent, now_et, equity, day_start_equity, pending_exposure_today,
                    idle_streak, recent_history,
                )
                save_history(STATE_PATH, recent_history)

            due = due_slots(now_et, config.decision_times_et, fired_slots_today)
            if not due:
                continue
            due_slot = due[-1]
            if len(due) > 1:
                # A restart/redeploy mid-day can leave several slots due at once -
                # only run the most recent, with current data, instead of replaying
                # each missed slot back to back.
                log.info(f"catch-up: collapsing {len(due) - 1} earlier due slot(s) into {due_slot}")

            # Real market-open check, not a weekday guess - catches holidays (e.g.
            # Labor Day, inside this contest's week) that weekday() < 5 would miss.
            try:
                clock = await mcp.get_clock()
            except Exception:
                log.exception("failed to fetch market clock this cycle, skipping")
                continue
            if not clock.get("is_open"):
                fired_slots_today.update(due)
                log.info(f"market closed at due_slot={due_slot}, skipping check-in")
                continue

            fired_slots_today.update(due)
            try:
                positions = await mcp.get_positions()
                options_positions = [p for p in positions if p.get("asset_class") == "us_option"]
                groups = group_option_positions(options_positions)
                options_exposure = pending_exposure_today + sum(
                    net_group_exposure(legs) for legs in groups.values()
                )
                result = await agent.run_cycle(
                    equity=equity,
                    day_start_equity=day_start_equity or equity,
                    open_position_count=len(groups),
                    current_options_exposure=options_exposure,
                    idle_streak=idle_streak,
                    recent_history=recent_history,
                    current_time_et=now_et.strftime("%Y-%m-%d %H:%M %A"),
                    minutes_since_last_checkin=minutes_since_last_checkin(recent_history, now_et),
                )
                pending_exposure_today += result.exposure_added
                idle_streak = 0 if result.orders_approved else idle_streak + 1
                log.info(
                    f"decision check-in summary (orders_approved={result.orders_approved}, "
                    f"idle_streak={idle_streak}): {result.summary}"
                )
                recent_history.append(f"[{now_et.strftime('%Y-%m-%d %H:%M')} EDT] {result.summary}")
                del recent_history[:-MAX_HISTORY_ENTRIES]
                save_history(STATE_PATH, recent_history)
            except Exception:
                log.exception("decision check-in failed")


def main() -> None:
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
