"""Read-only status site for MU/TH/UR 8400 (the Alpaca AI Trading Agents
Hackathon submission). Runs on a different cluster than the agent itself.
Two live data sources, both read-only:

  - Alpaca REST, for current equity, open positions, and the recent equity
    curve (Alpaca's own portfolio-history endpoint).
  - Loki, via Grafana's datasource proxy, for the agent's decision-log
    summaries - both the live tail on the front page and a per-day archive
    of every closed trading day (/api/days, /api/day).

Does not touch the trading pod's PVC. Reaches Grafana over an already-open
internal web path (same one used to reach $HARBOR) with a Viewer-role-only
service-account token - the token, not the network path, is the real access
control here.

A single background thread polls Alpaca/Grafana every POLL_INTERVAL_SECONDS
and publishes into SharedState; every connected browser holds one SSE
connection (/api/stream) that pushes the instant that state changes, so N
open tabs cost one upstream poll cycle, not N.

Pure stdlib (http.server + urllib + threading) - no framework needed for a
handful of GET routes and two outbound HTTPS calls.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")

ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
ALPACA_BASE = "https://paper-api.alpaca.markets"

GRAFANA_API_TOKEN = os.environ["GRAFANA_API_TOKEN"]
GRAFANA_BASE = "https://$GRAFANA"
LOKI_DATASOURCE_UID = "$LOKI_DATASOURCE_UID"
LOKI_CLUSTER_LABEL = "$CLUSTER"
LOKI_NAMESPACE_LABEL = "$NAMESPACE"

# Only show decision-log entries from the real contest window onward - earlier
# entries are dev-account testing noise. Matches the account-cutover time.
LOG_WINDOW_START_UTC = datetime(2026, 8, 31, 12, 45, tzinfo=timezone.utc)  # 8:45am ET

_DECISION_SUMMARY_RE = re.compile(
    r'"decision check-in summary \(orders_approved=(\d+), idle_streak=(\d+)\): (.*)"\s*\}?\s*$',
    re.DOTALL,
)


def _query_loki(start_ns: int, end_ns: int, limit: int) -> list[tuple[int, str]]:
    """Raw (timestamp_ns, line) pairs from Loki for one time window, sorted
    ascending. Shared by the live tail and the per-day archive below."""
    query = f'{{cluster="{LOKI_CLUSTER_LABEL}", namespace="{LOKI_NAMESPACE_LABEL}"}}'
    params = {
        "query": query,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": str(limit),
        "direction": "forward",
    }
    url = (
        f"{GRAFANA_BASE}/api/datasources/proxy/uid/{LOKI_DATASOURCE_UID}"
        f"/loki/api/v1/query_range?{urllib.parse.urlencode(params)}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {GRAFANA_API_TOKEN}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)

    lines: list[tuple[int, str]] = []
    for stream in data.get("data", {}).get("result", []):
        for ts, line in stream.get("values", []):
            lines.append((int(ts), line))
    lines.sort()
    return lines


def _reassemble_entries(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Loki splits a multi-line log message across multiple stored lines, so
    this glues a line starting a new JSON log object back together with every
    line after it up to the next one that starts another."""
    entries: list[tuple[int, str]] = []
    current_ts = None
    current_lines: list[str] = []
    for ts, line in lines:
        if line.startswith('{"time":'):
            if current_lines:
                entries.append((current_ts, "\n".join(current_lines)))
            current_ts, current_lines = ts, [line]
        else:
            current_lines.append(line)
    if current_lines:
        entries.append((current_ts, "\n".join(current_lines)))
    return entries


def _parse_decisions(entries: list[tuple[int, str]]) -> list[dict]:
    """Pulls decision check-in summaries out of reassembled log entries."""
    results = []
    for ts, blob in entries:
        m = _DECISION_SUMMARY_RE.search(blob)
        if not m:
            continue
        orders_approved, idle_streak, summary = m.groups()
        headline = summary.strip().split("\n")[0].lstrip("#* ").strip()
        if not headline:
            headline = "Decision check-in"
        results.append(
            {
                "ts_unix": ts // 1_000_000_000,
                "orders_approved": int(orders_approved),
                "headline": headline[:140],
                "body": summary.strip(),
            }
        )
    return results


def fetch_decision_log(limit: int = 10) -> list[dict]:
    """Most recent decision check-in summaries, for the live terminal."""
    now = datetime.now(timezone.utc)
    if now < LOG_WINDOW_START_UTC:
        # Querying Loki with a start time in the future would just 400.
        return []
    start_ns = int(LOG_WINDOW_START_UTC.timestamp() * 1_000_000_000)
    end_ns = int(now.timestamp() * 1_000_000_000)
    lines = _query_loki(start_ns, end_ns, limit=5000)
    results = _parse_decisions(_reassemble_entries(lines))
    results.sort(key=lambda e: e["ts_unix"], reverse=True)
    return results[:limit]


# The contest week sits entirely inside one fixed UTC offset for US markets,
# so the 4pm close can be a plain UTC hour constant with no timezone library
# involved. A day only shows up in the archive nav once its session is over.
MARKET_CLOSE_UTC_HOUR = 20


def _day_is_complete(day_utc_midnight: datetime) -> bool:
    now = datetime.now(timezone.utc)
    if day_utc_midnight.date() < now.date():
        return True
    return day_utc_midnight.date() == now.date() and now.hour >= MARKET_CLOSE_UTC_HOUR


def _day_label(day_utc_midnight: datetime) -> str:
    # Date only, no weekday name - the nav shows several of these side by
    # side, so the weekday is already implicit from position.
    return day_utc_midnight.strftime("%b %d").replace(" 0", " ")


# Last trading day the score depends on - Friday's snapshot happens at
# market open, before that day's first check-in, so Thursday's close is the
# real end of the contest week here too (same reasoning the agent's own
# scoring-window cutoff uses).
CONTEST_LAST_TRADING_DAY_UTC = datetime(2026, 9, 3, tzinfo=timezone.utc)


def list_archive_days() -> list[dict]:
    """Every weekday of the contest week, in order - not just ones whose
    session has closed, so the nav previews the whole week's schedule from
    day one instead of only growing as sessions complete. `available` is
    true once a day's session has closed; the frontend renders any other
    day as plain text, since there's no real content behind it yet."""
    d = datetime(LOG_WINDOW_START_UTC.year, LOG_WINDOW_START_UTC.month, LOG_WINDOW_START_UTC.day, tzinfo=timezone.utc)
    days = []
    while d.date() <= CONTEST_LAST_TRADING_DAY_UTC.date():
        if d.weekday() < 5:
            days.append({"date": d.strftime("%Y-%m-%d"), "label": _day_label(d), "available": _day_is_complete(d)})
        d += timedelta(days=1)
    return days


def fetch_day_archive(date_str: str) -> dict:
    """One full day's decision check-ins. Unlike fetch_decision_log (which
    re-fetches the whole contest-to-date window every poll) this only
    queries that one day's own window."""
    day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ns = int(day.timestamp() * 1_000_000_000)
    end_ns = int(min(day + timedelta(days=1), datetime.now(timezone.utc)).timestamp() * 1_000_000_000)
    lines = _query_loki(start_ns, end_ns, limit=5000)
    decisions = _parse_decisions(_reassemble_entries(lines))
    decisions.sort(key=lambda e: e["ts_unix"])

    return {
        "date": date_str,
        "label": _day_label(day),
        "decisions": decisions,
    }


def fetch_equity_history() -> list[dict]:
    """Recent equity curve from Alpaca's own portfolio-history endpoint. 1
    week at 15-minute resolution is plenty for a status page."""
    data = fetch_alpaca(
        "/v2/account/portfolio/history"
        "?period=1W&timeframe=15Min&intraday_reporting=extended_hours"
    )
    timestamps = data.get("timestamp") or []
    equity = data.get("equity") or []
    # Alpaca returns 0.0, not null, for every window before the account
    # existed - treat 0 as "no data yet" rather than a real equity value.
    return [{"t": t, "equity": e} for t, e in zip(timestamps, equity) if e]


def fetch_alpaca(path: str) -> dict | list:
    req = urllib.request.Request(
        f"{ALPACA_BASE}{path}",
        headers={
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def build_status() -> dict:
    account = fetch_alpaca("/v2/account")
    positions_raw = fetch_alpaca("/v2/positions")

    equity = float(account["equity"])
    last_equity = float(account["last_equity"])

    groups: dict[tuple[str, str], list[dict]] = {}
    # Reimplements the agent's risk_gates.parse_occ_symbol/group_option_positions
    # grouping here rather than importing it - this is a separate deployment with
    # no shared code with the agent, reading Alpaca directly, not fed data by it.
    occ_re = re.compile(r"^([A-Z]{1,6})(\d{6})[CP]\d{8}$")
    for p in positions_raw:
        m = occ_re.match(p.get("symbol", ""))
        key = (m.group(1), m.group(2)) if m else (p.get("symbol", ""), "")
        groups.setdefault(key, []).append(p)

    positions = []
    for (root, expiry), legs in groups.items():
        net_cost = sum(float(l.get("cost_basis", 0) or 0) for l in legs)
        unrealized = sum(float(l.get("unrealized_pl", 0) or 0) for l in legs)
        positions.append(
            {
                "root": root,
                "expiry": expiry,
                "legs": [
                    {
                        "symbol": l["symbol"],
                        "side": l["side"],
                        "qty": l["qty"],
                        "avgEntry": l["avg_entry_price"],
                        "currentPrice": l["current_price"],
                    }
                    for l in legs
                ],
                "netCostBasis": abs(net_cost),
                "unrealizedPl": unrealized,
            }
        )

    try:
        log = fetch_decision_log()
        log_error = None
    except Exception as exc:  # noqa: BLE001 - status page must not go down over a Loki hiccup
        log = []
        log_error = str(exc)

    try:
        equity_history = fetch_equity_history()
        equity_history_error = None
    except Exception as exc:  # noqa: BLE001 - same rationale as the log fetch above
        equity_history = []
        equity_history_error = str(exc)

    return {
        "refreshedAtUnix": int(datetime.now(timezone.utc).timestamp()),
        "account": {
            "equity": equity,
            "lastEquity": last_equity,
            "cash": float(account["cash"]),
            "buyingPower": float(account["buying_power"]),
            "optionsBuyingPower": float(account["options_buying_power"]),
        },
        "positions": positions,
        "log": log,
        "logError": log_error,
        "equityHistory": equity_history,
        "equityHistoryError": equity_history_error,
    }


POLL_INTERVAL_SECONDS = 20


class SharedState:
    """One upstream poll, fanned out to every connected viewer. SSE waiters
    compare against a version counter, so a waiter behind by more than one
    cycle jumps straight to the latest snapshot."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._version = 0
        self._payload: dict | None = None

    def publish(self, payload: dict) -> None:
        with self._cond:
            self._payload = payload
            self._version += 1
            self._cond.notify_all()

    def snapshot(self) -> tuple[int, dict | None]:
        with self._cond:
            return self._version, self._payload

    def wait_for_update(self, last_seen_version: int | None, timeout: float) -> tuple[int, dict | None]:
        with self._cond:
            if last_seen_version is None or self._version != last_seen_version:
                return self._version, self._payload
            self._cond.wait(timeout)
            return self._version, self._payload


STATE = SharedState()


def _poll_loop() -> None:
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        try:
            STATE.publish(build_status())
        except Exception as exc:  # noqa: BLE001 - keep serving the last known-good snapshot
            print(f"poll cycle failed, keeping last snapshot: {exc}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet, structured-ish access log
        print(f"{self.address_string()} {fmt % args}")

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str, content_type: str) -> None:
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Without this, a browser can cache index.html indefinitely and a
        # deploy stays invisible until a hard refresh. The file is tiny.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _stream_status(self) -> None:
        """One long-lived SSE connection per viewer. Sends the current
        snapshot immediately, then blocks on the shared condition variable; a
        keepalive on timeout holds the connection through idle-timeout
        proxies and surfaces a dropped connection via the next failed write."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")  # defense-in-depth vs proxy buffering
        self.end_headers()
        last_seen_version = None
        try:
            while True:
                version, payload = STATE.wait_for_update(last_seen_version, timeout=15)
                if version != last_seen_version and payload is not None:
                    last_seen_version = version
                    self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # viewer navigated away or the connection dropped - nothing to clean up

    def do_GET(self) -> None:
        # self.path includes the raw query string (e.g. "/?demo=1"), so
        # routing on it directly with exact equality against "/" would never
        # match - every route with a query param 404'd. Strip it once here.
        path = urllib.parse.urlsplit(self.path).path

        if path == "/api/stream":
            self._stream_status()
            return
        if path == "/api/status":
            _, payload = STATE.snapshot()
            if payload is None:
                self._send_json({"error": "still starting up"}, status=503)
            else:
                self._send_json(payload)
            return
        if path == "/api/days":
            try:
                self._send_json({"days": list_archive_days()})
            except Exception as exc:  # noqa: BLE001 - archive nav is a nice-to-have
                self._send_json({"error": str(exc)}, status=502)
            return
        if path == "/api/day":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            date_str = (qs.get("date") or [""])[0]
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
                self._send_json({"error": "date must be YYYY-MM-DD"}, status=400)
                return
            try:
                self._send_json(fetch_day_archive(date_str))
            except Exception as exc:  # noqa: BLE001 - report the failure, don't 500 the process
                self._send_json({"error": str(exc)}, status=502)
            return
        if path in ("/", "/index.html"):
            self._send_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
            return
        if path == "/healthz":
            self._send_json({"status": "ok"})
            return
        self.send_response(404)
        self.end_headers()


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    try:
        STATE.publish(build_status())
    except Exception as exc:  # noqa: BLE001 - don't block startup/readiness over one bad cycle
        print(f"initial poll failed, will retry in the background: {exc}")
    threading.Thread(target=_poll_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    # SSE connections block their handler thread indefinitely - without this,
    # a live viewer would hang graceful shutdown until Kubernetes' SIGKILL.
    server.daemon_threads = True
    print(f"muthur-status-web listening on :{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
