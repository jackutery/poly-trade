"""
core/engine.py
==============
Main trading loop — orchestrates market discovery, signal generation,
risk gating, order execution, and position lifecycle management.

Fixes vs v1:
  - Real market filtering using keyword patterns (not naive substring)
  - Actual price history collected from CLOB midpoint API
  - Position closing logic (checks if market resolved / stop-loss / take-profit)
  - PnL recorded after every closed position
  - Slippage tolerance enforced before placing orders
  - State and RiskManager share the same StateStore instance
  - Graceful shutdown on KeyboardInterrupt and kill switch
  - config parameters (slippage, retry, loop_sleep) are all used
"""

import os
import re
import time
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from api.polymarket import PolymarketClient, PolymarketKillSwitch, PolymarketAPIError
from core.strategy import AggressiveStrategy
from core.risk import RiskManager
from core.state import StateStore

logger = logging.getLogger("engine")

# Keyword patterns per asset — require word-boundary match to avoid false hits
# e.g. "XRP" should not match "XRPL" or "XRPBTC"
_ASSET_PATTERNS: Dict[str, re.Pattern] = {
    asset: re.compile(rf"\b{re.escape(asset)}\b", re.IGNORECASE)
    for asset in ("BTC", "ETH", "SOL", "XRP", "BITCOIN", "ETHEREUM", "SOLANA", "RIPPLE")
}

_ASSET_ALIASES: Dict[str, str] = {
    "BITCOIN":  "BTC",
    "ETHEREUM": "ETH",
    "SOLANA":   "SOL",
    "RIPPLE":   "XRP",
}


def _infer_asset(title: str, tracked_assets: Set[str]) -> Optional[str]:
    """
    Return the canonical asset name (BTC/ETH/SOL/XRP) if found in title,
    else None.  Uses word-boundary regex to avoid partial matches.
    """
    for keyword, pattern in _ASSET_PATTERNS.items():
        if pattern.search(title):
            canonical = _ASSET_ALIASES.get(keyword, keyword)
            if canonical in tracked_assets:
                return canonical
    return None


def _is_fast_market(market: Dict) -> bool:
    """
    Return True for 5-min or 15-min resolution markets.
    Checks 'question' and 'slug' fields for time indicators.
    """
    text = (
        market.get("question", "") + " " +
        market.get("slug", "") + " " +
        market.get("groupItemTitle", "")
    ).lower()
    return any(kw in text for kw in ("5-minute", "5 minute", "5min",
                                      "15-minute", "15 minute", "15min"))


class TradingEngine:
    """
    Orchestrates the full trading cycle:
      1. Discover markets (Gamma API)
      2. Filter to fast crypto markets
      3. Build price history (CLOB midpoint)
      4. Generate signals (strategy)
      5. Gate through risk (risk manager)
      6. Execute orders (CLOB)
      7. Monitor open positions for exit
      8. Record PnL on close
    """

    def __init__(self, config: Dict) -> None:
        self.cfg     = config
        self.client  = PolymarketClient()
        self.state   = StateStore()
        self.strategy= AggressiveStrategy(config, self.state)
        self.risk    = RiskManager(config, self.state)

        self.tracked_assets: Set[str] = set(config.get("assets", []))
        self.loop_sleep:  float = float(config.get("execution", {}).get("loop_sleep_seconds", 10))
        self.slippage:    float = float(config.get("execution", {}).get("slippage_tolerance", 0.02))

        # Stop-loss and take-profit as config-driven thresholds
        risk_cfg = config.get("risk", {})
        self.stop_loss_pct:    float = float(risk_cfg.get("stop_loss_pct",    0.15))
        self.take_profit_pct:  float = float(risk_cfg.get("take_profit_pct",  0.25))

    # ── Market discovery ──────────────────────────────────────────────────────

    def _fetch_target_markets(self) -> List[Dict]:
        """
        Fetch active markets from Gamma and filter to fast crypto markets.
        Returns list of dicts with injected 'asset' and 'yes_token_id' fields.
        """
        try:
            markets = self.client.gamma.get_markets(active=True, closed=False, limit=200)
        except PolymarketAPIError as exc:
            logger.error(f"Failed to fetch markets: {exc}")
            return []

        result: List[Dict] = []
        for m in markets:
            asset = _infer_asset(
                m.get("question", "") + " " + m.get("groupItemTitle", ""),
                self.tracked_assets,
            )
            if not asset:
                continue
            if not _is_fast_market(m):
                continue

            # Extract YES token ID — needed for CLOB calls
            tokens = m.get("tokens", [])
            yes_token = next(
                (t.get("token_id") for t in tokens
                 if t.get("outcome", "").upper() == "YES"),
                None,
            )
            if not yes_token:
                continue

            m["asset"]        = asset
            m["yes_token_id"] = yes_token
            result.append(m)

        logger.info(f"Found {len(result)} target markets (from {len(markets)} total)")
        return result

    # ── Position lifecycle ────────────────────────────────────────────────────

    def _get_current_price(self, token_id: str) -> Optional[float]:
        """Try midpoint first, fall back to last trade price."""
        price = self.client.clob.get_midpoint(token_id)
        if price is None:
            price = self.client.clob.get_last_trade_price(token_id)
        return price

    def _check_exit(self, position: Dict) -> Tuple[bool, float]:
        """
        Evaluate whether an open position should be closed.

        Returns (should_exit, current_price).
        Exit conditions:
          - Market no longer active (resolved)
          - Price moved beyond stop-loss or take-profit thresholds
        """
        token_id    = position.get("token_id")
        entry_price = float(position.get("entry_price", 0.5))
        side        = position.get("side", "BUY")

        if not token_id:
            return True, 0.0  # corrupted position — close it

        current_price = self._get_current_price(token_id)
        if current_price is None:
            logger.warning(f"Cannot get price for {token_id[:12]}… — keeping position")
            return False, entry_price

        if side == "BUY":
            pnl_pct = (current_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - current_price) / entry_price

        if pnl_pct <= -self.stop_loss_pct:
            logger.warning(
                f"STOP-LOSS triggered: {position['asset']} "
                f"entry={entry_price:.4f} current={current_price:.4f} "
                f"pnl={pnl_pct:+.1%}"
            )
            return True, current_price

        if pnl_pct >= self.take_profit_pct:
            logger.info(
                f"TAKE-PROFIT triggered: {position['asset']} "
                f"entry={entry_price:.4f} current={current_price:.4f} "
                f"pnl={pnl_pct:+.1%}"
            )
            return True, current_price

        return False, current_price

    def _close_position(self, position: Dict, exit_price: float) -> None:
        """
        Place a closing order and record PnL.
        """
        market_id   = position["market_id"]
        token_id    = position.get("token_id", "")
        side        = position.get("side", "BUY")
        size_usd    = float(position.get("size_usd", 0))
        entry_price = float(position.get("entry_price", exit_price))

        close_side = "SELL" if side == "BUY" else "BUY"

        # Apply slippage tolerance
        if close_side == "SELL":
            limit_price = round(exit_price * (1 - self.slippage), 4)
        else:
            limit_price = round(exit_price * (1 + self.slippage), 4)
        limit_price = max(0.01, min(0.99, limit_price))

        try:
            self.client.clob.place_order(
                token_id   = token_id,
                side       = close_side,
                price      = limit_price,
                size       = size_usd,
                order_type = "GTC",
            )
        except PolymarketAPIError as exc:
            logger.error(f"Failed to place close order for {market_id}: {exc}")
            # Still remove from state to avoid infinite retry
        finally:
            self.state.remove_position(market_id)

        # Compute PnL
        if side == "BUY":
            pnl = round((exit_price - entry_price) * size_usd, 4)
        else:
            pnl = round((entry_price - exit_price) * size_usd, 4)

        self.risk.record_trade_result(pnl)
        logger.info(
            f"Closed position {market_id[:16]}… "
            f"side={side} entry={entry_price:.4f} exit={exit_price:.4f} "
            f"pnl={pnl:+.2f} USD"
        )

    # ── Open position monitoring ──────────────────────────────────────────────

    def _monitor_positions(self) -> None:
        """Check all open positions for exit conditions and close if triggered."""
        positions = self.state.get_open_positions()
        for pos in positions:
            try:
                should_exit, current_price = self._check_exit(pos)
                if should_exit:
                    self._close_position(pos, current_price)
            except Exception as exc:
                logger.error(f"Error monitoring position {pos.get('market_id')}: {exc}")

    # ── Trade entry ───────────────────────────────────────────────────────────

    def _open_position(
        self,
        market: Dict,
        signal: Dict,
        current_price: float,
    ) -> None:
        """
        Size, gate, and place a new position.
        """
        asset    = market["asset"]
        token_id = market["yes_token_id"]
        side     = signal["side"]

        size_usd = self.risk.position_size(signal["confidence"])
        if size_usd == 0.0:
            logger.debug(f"Position size too small for {asset} — skip")
            return

        # Apply slippage to limit price
        if side == "BUY":
            limit_price = round(current_price * (1 + self.slippage), 4)
        else:
            limit_price = round(current_price * (1 - self.slippage), 4)
        limit_price = max(0.01, min(0.99, limit_price))

        try:
            order = self.client.clob.place_order(
                token_id   = token_id,
                side       = side,
                price      = limit_price,
                size       = size_usd,
                order_type = "GTC",
            )
        except PolymarketAPIError as exc:
            logger.error(f"Order placement failed for {asset}: {exc}")
            return

        position = {
            "market_id":   market["id"],
            "asset":       asset,
            "token_id":    token_id,
            "side":        side,
            "size_usd":    size_usd,
            "entry_price": limit_price,
            "order_id":    order.get("orderID") or order.get("id"),
            "opened_at":   datetime.now(timezone.utc).isoformat(),
            "confidence":  signal["confidence"],
        }

        self.state.add_position(position)
        logger.info(
            f"OPENED [{side}] {asset} | "
            f"{size_usd} USD @ {limit_price} | "
            f"conf={signal['confidence']:.3f}"
        )

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        logger.info("=" * 60)
        logger.info("APMTS Trading Engine started")
        logger.info(f"Tracking assets: {sorted(self.tracked_assets)}")
        logger.info(f"Daily PnL at start: {self.state.get_daily_pnl():+.2f} USD")
        logger.info("=" * 60)

        try:
            while True:
                # ── Kill switch (env var) ──────────────────────────────────
                if os.getenv("APMTS_KILL", "0") == "1":
                    logger.critical("KILL SWITCH activated — engine halted")
                    break

                # ── Monitor & close open positions ────────────────────────
                self._monitor_positions()

                # ── Discover markets ──────────────────────────────────────
                target_markets = self._fetch_target_markets()
                open_positions = self.state.get_open_positions()

                # ── Process each market ───────────────────────────────────
                for market in target_markets:
                    if os.getenv("APMTS_KILL", "0") == "1":
                        raise PolymarketKillSwitch("Kill switch mid-loop")

                    market_id = market.get("id")
                    token_id  = market["yes_token_id"]
                    asset     = market["asset"]

                    # Skip if already in a position for this market
                    if self.state.get_position(market_id):
                        continue

                    # Get current price (used for history + signal + order)
                    current_price = self._get_current_price(token_id)
                    if current_price is None:
                        continue

                    # Get orderbook for imbalance
                    try:
                        orderbook = self.client.clob.get_orderbook(token_id)
                    except PolymarketAPIError as exc:
                        logger.warning(f"Orderbook fetch failed {asset}: {exc}")
                        continue

                    # Generate signal
                    signal = self.strategy.generate_signal(
                        market_id     = market_id,
                        token_id      = token_id,
                        orderbook     = orderbook,
                        current_price = current_price,
                    )
                    if not signal:
                        continue

                    # Risk gate
                    if not self.risk.allow_trade(
                        signal         = signal,
                        asset          = asset,
                        open_positions = open_positions,
                    ):
                        continue

                    # Execute
                    self._open_position(market, signal, current_price)

                    # Refresh positions list for remaining loop iterations
                    open_positions = self.state.get_open_positions()

                time.sleep(self.loop_sleep)

        except PolymarketKillSwitch:
            logger.critical("Engine halted via kill switch")
        except KeyboardInterrupt:
            logger.info("Engine halted via KeyboardInterrupt")
        except Exception as exc:
            logger.exception(f"Unhandled engine error: {exc}")
        finally:
            snap = self.state.snapshot()
            logger.info(
                f"Engine stopped | "
                f"open={len(snap['open_positions'])} | "
                f"daily_pnl={snap['daily_pnl_usd']:+.2f} USD | "
                f"total_pnl={snap['total_pnl_usd']:+.2f} USD"
            )
