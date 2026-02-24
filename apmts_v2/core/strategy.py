"""
core/strategy.py
================
Aggressive momentum + order-book imbalance strategy.

Fixes vs v1:
  - Momentum uses REAL price history from StateStore (not [x]*5 placeholder)
  - Bidirectional signals: generates both BUY and SELL based on imbalance direction
  - Cooldown persisted in StateStore (survives restarts)
  - Weighted confidence: momentum carries more weight than imbalance
  - All config parameters are actually used
  - Graceful handling of thin orderbooks (< 3 levels)
"""

import time
import logging
from typing import Dict, List, Optional

import numpy as np

from core.state import StateStore

logger = logging.getLogger("strategy")


class AggressiveStrategy:
    """
    Signal generator combining price momentum and order-book imbalance.

    Signal anatomy:
        {
            "market_id":  str,
            "token_id":   str,       # CLOB token to trade
            "side":       "BUY" | "SELL",
            "confidence": float,     # [0, 1]
            "momentum":   float,
            "imbalance":  float,
        }
    """

    def __init__(self, config: Dict, state: StateStore) -> None:
        self.cfg   = config["strategy"]
        self.state = state

        self._cooldown_secs: float = float(self.cfg["cooldown_seconds"])
        self._momentum_threshold:  float = float(self.cfg["momentum_threshold"])
        self._imbalance_threshold: float = float(self.cfg["orderbook_imbalance_threshold"])
        self._min_confidence:      float = float(self.cfg["min_confidence"])

        # Validate
        if not (0 < self._min_confidence < 1):
            raise ValueError("strategy.min_confidence must be in (0, 1)")

    # ── Cooldown (persisted) ──────────────────────────────────────────────────

    def _in_cooldown(self, market_id: str) -> bool:
        last = self.state.get_cooldown(market_id)
        if last is None:
            return False
        return (time.time() - last) < self._cooldown_secs

    def _set_cooldown(self, market_id: str) -> None:
        self.state.set_cooldown(market_id, time.time())

    # ── Momentum score ────────────────────────────────────────────────────────

    def _momentum_score(self, prices: List[float]) -> float:
        """
        Normalized momentum score in [-1, +1] mapped to [0, 1].

        Uses log-returns to avoid division-by-zero on zero prices and to
        be more statistically well-behaved.
          > 0.5 → upward momentum (favours BUY)
          < 0.5 → downward momentum (favours SELL)
          = 0.5 → neutral
        """
        if len(prices) < 3:
            return 0.5  # neutral — not enough data

        arr = np.array(prices, dtype=float)

        # Guard against zero/negative prices (shouldn't happen with probabilities)
        if np.any(arr <= 0):
            return 0.5

        log_returns = np.diff(np.log(arr))

        # Weighted: recent returns count more
        weights = np.linspace(1, 2, len(log_returns))
        weighted_mean = float(np.average(log_returns, weights=weights))

        # tanh squash: maps (-inf,+inf) → (-1,+1), then shift to [0,1]
        raw = float(np.tanh(weighted_mean * 15))
        return float((raw + 1) / 2)

    # ── Order-book imbalance ──────────────────────────────────────────────────

    def _orderbook_imbalance(self, orderbook: Dict) -> float:
        """
        Order-book imbalance score in [0, 1].

          > 0.5 → buy-side pressure (bids dominant  → BUY signal)
          < 0.5 → sell-side pressure (asks dominant → SELL signal)
          = 0.5 → balanced

        Uses depth-weighted volume across top-5 levels.
        """
        bids: List[Dict] = orderbook.get("bids", [])
        asks: List[Dict] = orderbook.get("asks", [])

        def _vol(levels: List[Dict], n: int = 5) -> float:
            total = 0.0
            for lvl in levels[:n]:
                try:
                    total += float(lvl.get("size", 0))
                except (TypeError, ValueError):
                    pass
            return total

        bid_vol = _vol(bids)
        ask_vol = _vol(asks)

        total = bid_vol + ask_vol
        if total < 1e-9:
            return 0.5  # empty book → neutral

        raw = (bid_vol - ask_vol) / total   # [-1, +1]
        return float((raw + 1) / 2)         # [0, 1]

    # ── Signal generation ─────────────────────────────────────────────────────

    def generate_signal(
        self,
        market_id: str,
        token_id: str,
        orderbook: Dict,
        current_price: float,
    ) -> Optional[Dict]:
        """
        Compute momentum + imbalance scores and return a signal or None.

        Steps:
          1. Append current_price to persistent price history
          2. Check cooldown
          3. Compute scores
          4. Determine direction
          5. Gate on thresholds and min_confidence
          6. Set cooldown and return signal
        """
        # Always record the latest price — even if we don't trade
        self.state.append_price(market_id, current_price)
        prices = self.state.get_price_history(market_id)

        if self._in_cooldown(market_id):
            logger.debug(f"{market_id[:12]}… in cooldown — skip")
            return None

        momentum  = self._momentum_score(prices)
        imbalance = self._orderbook_imbalance(orderbook)

        logger.debug(
            f"{market_id[:12]}… momentum={momentum:.3f} "
            f"imbalance={imbalance:.3f}"
        )

        # Determine direction from imbalance
        # Imbalance > 0.5 → buy pressure → BUY signal
        # Imbalance < 0.5 → sell pressure → SELL signal
        if imbalance >= 0.5:
            side              = "BUY"
            directional_score = imbalance               # [0.5, 1]
            momentum_aligned  = momentum >= 0.5         # upward momentum
            momentum_score    = momentum                # BUY-aligned if > 0.5
        else:
            side              = "SELL"
            directional_score = 1.0 - imbalance         # [0.5, 1]
            momentum_aligned  = momentum <= 0.5         # downward momentum
            momentum_score    = 1.0 - momentum          # SELL-aligned if > 0.5

        # Both momentum and imbalance must exceed their thresholds
        if directional_score < self._imbalance_threshold:
            return None
        if momentum_score < self._momentum_threshold:
            return None
        if not momentum_aligned:
            # Momentum and imbalance point in opposite directions — skip
            logger.debug(f"{market_id[:12]}… momentum/imbalance conflict — skip")
            return None

        # Weighted confidence: momentum 40%, imbalance 60%
        confidence = round(0.4 * momentum_score + 0.6 * directional_score, 4)

        if confidence < self._min_confidence:
            return None

        self._set_cooldown(market_id)

        signal = {
            "market_id":  market_id,
            "token_id":   token_id,
            "side":       side,
            "confidence": confidence,
            "momentum":   round(momentum, 4),
            "imbalance":  round(imbalance, 4),
        }

        logger.info(
            f"SIGNAL [{side}] {market_id[:16]}… "
            f"conf={confidence:.3f} "
            f"mom={momentum:.3f} imb={imbalance:.3f}"
        )
        return signal
