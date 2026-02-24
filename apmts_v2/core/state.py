"""
core/state.py
=============
Persistent, thread-safe local state store.

Fixes vs v1:
  - Atomic writes (write-then-rename) to prevent corruption on crash
  - Absolute path so desktop app and engine always share the same file
  - Auto-reset of daily PnL at midnight
  - Cooldown state persisted across restarts
  - Price history persisted per-market for momentum calculation
  - Explicit schema with defaults for forward-compatibility
"""

import json
import os
import threading
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

# ── Path: resolve relative to project root regardless of CWD ──────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE    = _PROJECT_ROOT / "state.db"

_LOCK = threading.Lock()

_DEFAULT_STATE: Dict = {
    "schema_version":  2,
    "open_positions":  [],
    "daily_pnl_usd":   0.0,
    "pnl_date":        str(date.today()),
    "cooldowns":       {},          # market_id → last_signal_unix_ts
    "price_history":   {},          # market_id → [float, ...]
    "total_trades":    0,
    "total_pnl_usd":   0.0,
}


class StateStore:
    """
    Lightweight JSON-based state persistence.

    - Thread-safe: all mutations hold _LOCK
    - Crash-safe:  writes to .tmp then renames atomically
    - Restart-safe: daily PnL resets automatically on date change
    """

    def __init__(self, path: Path = STATE_FILE) -> None:
        self.path = Path(path)
        self._state: Dict = dict(_DEFAULT_STATE)
        self._load()
        self._maybe_reset_daily_pnl()

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if self.path.exists():
            try:
                with self.path.open("r") as f:
                    loaded = json.load(f)
                # Merge with defaults so new keys are always present
                merged = dict(_DEFAULT_STATE)
                merged.update(loaded)
                self._state = merged
            except (json.JSONDecodeError, OSError) as exc:
                # Corrupted state — start fresh but preserve a backup
                backup = self.path.with_suffix(".db.bak")
                self.path.rename(backup)
                import logging
                logging.getLogger("state").error(
                    f"Corrupted state file — reset to defaults. Backup: {backup}. Error: {exc}"
                )
                self._state = dict(_DEFAULT_STATE)

    def _save(self) -> None:
        """Atomic write: write to .tmp then rename to avoid partial writes."""
        tmp = self.path.with_suffix(".db.tmp")
        try:
            with tmp.open("w") as f:
                json.dump(self._state, f, indent=2, default=str)
            tmp.replace(self.path)
        except OSError as exc:
            import logging
            logging.getLogger("state").error(f"State save failed: {exc}")
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    def _maybe_reset_daily_pnl(self) -> None:
        """Reset daily PnL if the stored date is not today."""
        today = str(date.today())
        if self._state.get("pnl_date") != today:
            with _LOCK:
                self._state["daily_pnl_usd"] = 0.0
                self._state["pnl_date"]       = today
                self._save()

    # ── Positions ──────────────────────────────────────────────────────────────

    def get_open_positions(self) -> List[Dict]:
        return list(self._state.get("open_positions", []))

    def add_position(self, position: Dict) -> None:
        """
        Add a new open position.

        Expected keys:
          market_id, asset, token_id, size_usd, entry_price, side, order_id,
          opened_at (unix timestamp)
        """
        with _LOCK:
            self._state["open_positions"].append(position)
            self._state["total_trades"] = self._state.get("total_trades", 0) + 1
            self._save()

    def remove_position(self, market_id: str) -> Optional[Dict]:
        """Remove and return the position for market_id, or None if not found."""
        with _LOCK:
            positions = self._state.get("open_positions", [])
            removed   = next((p for p in positions if p["market_id"] == market_id), None)
            self._state["open_positions"] = [
                p for p in positions if p["market_id"] != market_id
            ]
            self._save()
        return removed

    def get_position(self, market_id: str) -> Optional[Dict]:
        """Return open position for market_id or None."""
        return next(
            (p for p in self._state.get("open_positions", [])
             if p["market_id"] == market_id),
            None,
        )

    # ── PnL ────────────────────────────────────────────────────────────────────

    def update_daily_pnl(self, pnl_usd: float) -> None:
        with _LOCK:
            self._maybe_reset_daily_pnl()
            self._state["daily_pnl_usd"]  = round(
                self._state.get("daily_pnl_usd", 0.0) + pnl_usd, 4
            )
            self._state["total_pnl_usd"]  = round(
                self._state.get("total_pnl_usd", 0.0) + pnl_usd, 4
            )
            self._save()

    def get_daily_pnl(self) -> float:
        self._maybe_reset_daily_pnl()
        return self._state.get("daily_pnl_usd", 0.0)

    def get_total_pnl(self) -> float:
        return self._state.get("total_pnl_usd", 0.0)

    # ── Cooldowns ──────────────────────────────────────────────────────────────

    def set_cooldown(self, market_id: str, timestamp: float) -> None:
        with _LOCK:
            self._state.setdefault("cooldowns", {})[market_id] = timestamp
            self._save()

    def get_cooldown(self, market_id: str) -> Optional[float]:
        return self._state.get("cooldowns", {}).get(market_id)

    # ── Price history ──────────────────────────────────────────────────────────

    def append_price(self, market_id: str, price: float, max_len: int = 30) -> None:
        """
        Append a price sample to the market's history ring-buffer.
        Persists to disk so momentum survives restarts.
        """
        with _LOCK:
            history = self._state.setdefault("price_history", {})
            bucket  = history.setdefault(market_id, [])
            bucket.append(round(price, 6))
            if len(bucket) > max_len:
                bucket[:] = bucket[-max_len:]
            self._save()

    def get_price_history(self, market_id: str) -> List[float]:
        return list(self._state.get("price_history", {}).get(market_id, []))

    # ── Stats snapshot ─────────────────────────────────────────────────────────

    def snapshot(self) -> Dict:
        """Return a read-only dict snapshot of the full state."""
        return {
            "open_positions": self.get_open_positions(),
            "daily_pnl_usd":  self.get_daily_pnl(),
            "total_pnl_usd":  self.get_total_pnl(),
            "total_trades":   self._state.get("total_trades", 0),
            "pnl_date":       self._state.get("pnl_date"),
        }
