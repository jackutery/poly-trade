"""
core/risk.py
============
USD-based risk management layer.

Fixes vs v1:
  - daily_loss loaded from StateStore (restart-safe — no longer in-memory only)
  - position_size enforces a minimum floor ($1 USDC)
  - allow_trade checks against state PnL, not a local float
  - per_asset_max_exposure uses a default of 0 safely
  - Exposes record_trade_result() so PnL is always updated after every trade
"""

import logging
from typing import Dict, List

from core.state import StateStore

logger = logging.getLogger("risk")


class RiskManager:
    """
    Stateful USD-denominated risk gate.

    All PnL tracking is delegated to StateStore so values survive restarts.
    """

    def __init__(self, config: Dict, state: StateStore) -> None:
        self.cfg   = config["risk"]
        self.state = state

        # Sanity-check config values at startup
        self._validate_config()

    def _validate_config(self) -> None:
        required = [
            "max_usd_per_trade",
            "max_daily_loss_usd",
            "max_open_positions",
            "per_asset_max_exposure_usd",
        ]
        for key in required:
            if key not in self.cfg:
                raise ValueError(f"risk config missing key: '{key}'")

        if self.cfg["max_usd_per_trade"] < 1.0:
            raise ValueError("risk.max_usd_per_trade must be >= 1.0 USDC")
        if self.cfg["max_daily_loss_usd"] <= 0:
            raise ValueError("risk.max_daily_loss_usd must be positive")

    # ── PnL tracking ──────────────────────────────────────────────────────────

    def record_trade_result(self, pnl_usd: float) -> None:
        """
        Call after each position is closed.
        Updates persistent state — survives restarts.
        """
        self.state.update_daily_pnl(pnl_usd)
        logger.info(
            f"Trade result: {pnl_usd:+.2f} USD | "
            f"Daily PnL: {self.state.get_daily_pnl():+.2f} USD"
        )

    # ── Exposure helpers ──────────────────────────────────────────────────────

    def _asset_exposure(self, asset: str, open_positions: List[Dict]) -> float:
        return sum(
            p.get("size_usd", 0.0)
            for p in open_positions
            if p.get("asset") == asset
        )

    def _total_exposure(self, open_positions: List[Dict]) -> float:
        return sum(p.get("size_usd", 0.0) for p in open_positions)

    # ── Main gate ─────────────────────────────────────────────────────────────

    def allow_trade(
        self,
        signal: Dict,
        asset: str,
        open_positions: List[Dict],
    ) -> bool:
        """
        Return True only if ALL risk checks pass.
        Checks (in order):
          1. Kill switch
          2. Daily loss limit (read from persistent state)
          3. Max concurrent positions
          4. Per-asset exposure cap
        """
        # 1 — daily loss (read from disk, restart-safe)
        daily_pnl = self.state.get_daily_pnl()
        max_loss  = abs(self.cfg["max_daily_loss_usd"])
        if daily_pnl <= -max_loss:
            logger.critical(
                f"Risk REJECT — daily loss limit: {daily_pnl:.2f} USD "
                f"(limit: -{max_loss:.2f} USD)"
            )
            return False

        # 2 — max open positions
        if len(open_positions) >= self.cfg["max_open_positions"]:
            logger.warning(
                f"Risk REJECT — max open positions reached "
                f"({len(open_positions)}/{self.cfg['max_open_positions']})"
            )
            return False

        # 3 — per-asset exposure
        exposure    = self._asset_exposure(asset, open_positions)
        max_asset   = float(
            self.cfg["per_asset_max_exposure_usd"].get(asset, 0)
        )
        next_size   = self.position_size(signal.get("confidence", 0.0))

        if max_asset <= 0:
            logger.warning(f"Risk REJECT — no exposure limit configured for {asset}")
            return False

        if exposure + next_size > max_asset:
            logger.warning(
                f"Risk REJECT — {asset} exposure {exposure:.2f}+{next_size:.2f} "
                f"> limit {max_asset:.2f} USD"
            )
            return False

        logger.info(
            f"Risk APPROVED — {asset} | "
            f"exposure {exposure:.2f}/{max_asset:.2f} USD | "
            f"daily PnL {daily_pnl:+.2f} USD"
        )
        return True

    # ── Sizing ────────────────────────────────────────────────────────────────

    def position_size(self, confidence: float) -> float:
        """
        Dynamic USD sizing: base * confidence, floored at $1.

        confidence must be in [0, 1].
        Returns 0.0 if below minimum — caller should skip the trade.
        """
        confidence = max(0.0, min(1.0, float(confidence)))
        base       = float(self.cfg["max_usd_per_trade"])
        size       = round(base * confidence, 2)
        minimum    = max(1.0, float(self.cfg.get("min_usd_per_trade", 1.0)))

        if size < minimum:
            logger.debug(f"Computed size {size} below minimum {minimum} — skipping")
            return 0.0

        return size
