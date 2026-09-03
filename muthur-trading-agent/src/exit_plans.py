from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger(__name__)

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _normalize_expiry(expiration: str) -> str:
    """Claude states expirations as YYYY-MM-DD; parse_occ_symbol (risk_gates.py)
    reports YYMMDD. Normalizes either shape to YYMMDD, the canonical form
    position_monitor.py derives from actual open legs, so both sides key the
    same store entry."""
    expiration = expiration.strip()
    match = _ISO_DATE_RE.match(expiration)
    if match:
        yyyy, mm, dd = match.groups()
        return f"{yyyy[2:]}{mm}{dd}"
    return expiration


class ExitPlanStore:
    """Persisted, PVC-backed store of Claude's stated exit levels per open
    position group (underlying + expiration), letting PositionMonitor enforce
    them in real time between decision cycles.

    Claude writes an entry via the set_exit_plan tool (agent_loop.py) on open
    or when adjusting a plan; PositionMonitor reads it every cycle and clears
    the entry once that group closes.
    """

    def __init__(self, path: str):
        self.path = path
        self._plans: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            with open(self.path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception:
            log.exception(f"failed to load persisted exit plans from {self.path}, starting empty")
            return {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self._plans, f)
        except Exception:
            log.exception(f"failed to persist exit plans to {self.path} - continuing in-memory only")

    @staticmethod
    def _key(underlying: str, expiration: str) -> str:
        return f"{underlying.strip().upper()}|{_normalize_expiry(expiration)}"

    def set(self, underlying: str, expiration: str, plan: dict) -> None:
        self._plans[self._key(underlying, expiration)] = plan
        self._save()

    def get(self, underlying: str, expiration: str) -> dict | None:
        return self._plans.get(self._key(underlying, expiration))

    def clear(self, underlying: str, expiration: str) -> None:
        if self._plans.pop(self._key(underlying, expiration), None) is not None:
            self._save()
