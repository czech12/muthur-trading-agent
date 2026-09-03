from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time as dt_time

import yaml

from risk_gates import RiskConfig

DEFAULT_CONFIG_PATH = "/app/config/agent.yaml"


@dataclass
class Config:
    mode: str
    decision_times_et: list[dt_time]
    monitor_interval_seconds: int
    risk: RiskConfig
    stop_loss_pct: float
    take_profit_pct: float
    anthropic_model: str
    trading_enabled: bool = True
    expected_account_number: str | None = None
    premarket_research_time_et: dt_time | None = None

    @property
    def paper(self) -> bool:
        return self.mode == "paper"


def load_config(path: str | None = None) -> Config:
    path = path or os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH)
    with open(path) as f:
        raw = yaml.safe_load(f)

    if raw.get("mode") != "paper":
        raise ValueError("config 'mode' must be 'paper' - this project doesn't support live trading")

    def _parse_time(value: str) -> dt_time:
        hh, mm = (int(part) for part in str(value).split(":"))
        return dt_time(hh, mm)

    decision_times = sorted(_parse_time(t) for t in raw["decision_times_et"])
    if not decision_times:
        raise ValueError("config must list at least one decision_times_et entry")

    premarket_raw = raw.get("premarket_research_time_et")
    premarket_research_time_et = _parse_time(premarket_raw) if premarket_raw else None

    return Config(
        mode=raw["mode"],
        decision_times_et=decision_times,
        monitor_interval_seconds=int(raw.get("monitor_interval_seconds", 300)),
        risk=RiskConfig(**raw["risk"]),
        stop_loss_pct=float(raw["exits"]["stop_loss_pct"]),
        take_profit_pct=float(raw["exits"]["take_profit_pct"]),
        anthropic_model=raw.get("anthropic", {}).get("model", "claude-sonnet-5"),
        trading_enabled=bool(raw.get("trading_enabled", True)),
        # Env var wins if set - lets one shared ConfigMap serve multiple
        # deployments, with the per-deployment value coming from each
        # Deployment's own env (same pattern as STATE_PATH).
        expected_account_number=os.environ.get("EXPECTED_ACCOUNT_NUMBER") or raw.get("expected_account_number") or None,
        premarket_research_time_et=premarket_research_time_et,
    )
