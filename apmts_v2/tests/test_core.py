"""
tests/test_core.py
==================
Unit tests for the APMTS core modules.

Run with:
    pytest tests/ -v
"""

import json
import math
import tempfile
import time
from pathlib import Path
from typing import Dict
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _minimal_config() -> Dict:
    return {
        "assets": ["BTC", "ETH", "SOL", "XRP"],
        "risk": {
            "max_usd_per_trade": 50,
            "min_usd_per_trade": 2,
            "max_daily_loss_usd": 200,
            "max_open_positions": 4,
            "per_asset_max_exposure_usd": {"BTC": 100, "ETH": 100, "SOL": 75, "XRP": 75},
            "stop_loss_pct": 0.15,
            "take_profit_pct": 0.25,
        },
        "strategy": {
            "momentum_threshold": 0.65,
            "orderbook_imbalance_threshold": 0.60,
            "min_confidence": 0.70,
            "cooldown_seconds": 90,
        },
        "execution": {
            "slippage_tolerance": 0.02,
            "loop_sleep_seconds": 10,
            "retry_attempts": 3,
            "retry_delay_seconds": 2,
        },
        "logging": {"level": "DEBUG", "to_file": False},
    }


def _tmp_state(tmp_path: Path):
    from core.state import StateStore
    return StateStore(path=tmp_path / "state.db")


# ─────────────────────────────────────────────────────────────────────────────
# StateStore
# ─────────────────────────────────────────────────────────────────────────────

class TestStateStore:

    def test_add_and_remove_position(self, tmp_path):
        state = _tmp_state(tmp_path)
        pos = {"market_id": "m1", "asset": "BTC", "size_usd": 20.0,
               "entry_price": 0.72, "side": "BUY", "token_id": "t1",
               "order_id": "o1", "opened_at": "2024-01-01T00:00:00+00:00"}
        state.add_position(pos)
        assert len(state.get_open_positions()) == 1
        removed = state.remove_position("m1")
        assert removed["market_id"] == "m1"
        assert len(state.get_open_positions()) == 0

    def test_daily_pnl_accumulates(self, tmp_path):
        state = _tmp_state(tmp_path)
        state.update_daily_pnl(10.0)
        state.update_daily_pnl(-3.5)
        assert abs(state.get_daily_pnl() - 6.5) < 1e-6

    def test_daily_pnl_persists_across_reload(self, tmp_path):
        from core.state import StateStore
        path = tmp_path / "state.db"
        s1   = StateStore(path=path)
        s1.update_daily_pnl(42.0)
        s2   = StateStore(path=path)
        assert abs(s2.get_daily_pnl() - 42.0) < 1e-6

    def test_atomic_write_does_not_leave_tmp(self, tmp_path):
        state = _tmp_state(tmp_path)
        state.update_daily_pnl(5.0)
        tmp_file = tmp_path / "state.db.tmp"
        assert not tmp_file.exists()

    def test_price_history_ring_buffer(self, tmp_path):
        state = _tmp_state(tmp_path)
        for i in range(35):
            state.append_price("m1", float(i), max_len=10)
        hist = state.get_price_history("m1")
        assert len(hist) == 10
        assert hist[-1] == 34.0

    def test_cooldown_roundtrip(self, tmp_path):
        state = _tmp_state(tmp_path)
        ts = time.time()
        state.set_cooldown("m1", ts)
        assert abs(state.get_cooldown("m1") - ts) < 0.01

    def test_corrupted_state_resets(self, tmp_path):
        from core.state import StateStore
        path = tmp_path / "state.db"
        path.write_text("not valid json{{{")
        s = StateStore(path=path)
        assert s.get_open_positions() == []
        assert (tmp_path / "state.db.bak").exists()

    def test_snapshot_keys(self, tmp_path):
        state = _tmp_state(tmp_path)
        snap  = state.snapshot()
        for key in ("open_positions", "daily_pnl_usd", "total_pnl_usd", "total_trades"):
            assert key in snap


# ─────────────────────────────────────────────────────────────────────────────
# RiskManager
# ─────────────────────────────────────────────────────────────────────────────

class TestRiskManager:

    def _make(self, tmp_path):
        from core.risk import RiskManager
        state = _tmp_state(tmp_path)
        return RiskManager(_minimal_config(), state), state

    def test_allow_trade_basic(self, tmp_path):
        risk, _ = self._make(tmp_path)
        signal  = {"confidence": 0.8}
        assert risk.allow_trade(signal, "BTC", []) is True

    def test_reject_when_daily_loss_exceeded(self, tmp_path):
        risk, state = self._make(tmp_path)
        state.update_daily_pnl(-250.0)   # exceeds 200 limit
        assert risk.allow_trade({"confidence": 0.9}, "BTC", []) is False

    def test_reject_when_max_positions_reached(self, tmp_path):
        risk, _ = self._make(tmp_path)
        positions = [{"asset": "BTC", "size_usd": 10}] * 4
        assert risk.allow_trade({"confidence": 0.9}, "BTC", positions) is False

    def test_reject_when_asset_exposure_full(self, tmp_path):
        risk, _ = self._make(tmp_path)
        # BTC limit is 100 USD; next trade would be 50*0.9=45 → total 95 ≤ 100 ✓
        positions = [{"asset": "BTC", "size_usd": 80.0}]
        # 80 + 45 = 125 > 100 → reject
        assert risk.allow_trade({"confidence": 0.9}, "BTC", positions) is False

    def test_position_size_scales_with_confidence(self, tmp_path):
        risk, _ = self._make(tmp_path)
        assert risk.position_size(1.0) == 50.0
        assert risk.position_size(0.5) == 25.0

    def test_position_size_floored_at_minimum(self, tmp_path):
        risk, _ = self._make(tmp_path)
        # confidence so low result < min_usd_per_trade (2) → returns 0
        assert risk.position_size(0.01) == 0.0

    def test_record_trade_result_updates_state(self, tmp_path):
        risk, state = self._make(tmp_path)
        risk.record_trade_result(-30.0)
        assert abs(state.get_daily_pnl() - (-30.0)) < 1e-6

    def test_config_validation_raises_on_bad_config(self, tmp_path):
        from core.risk import RiskManager
        from core.state import StateStore
        state  = StateStore(path=tmp_path / "state.db")
        bad_cfg = _minimal_config()
        bad_cfg["risk"]["max_usd_per_trade"] = 0.5
        with pytest.raises(ValueError):
            RiskManager(bad_cfg, state)


# ─────────────────────────────────────────────────────────────────────────────
# AggressiveStrategy
# ─────────────────────────────────────────────────────────────────────────────

class TestAggressiveStrategy:

    def _make(self, tmp_path):
        from core.strategy import AggressiveStrategy
        state = _tmp_state(tmp_path)
        return AggressiveStrategy(_minimal_config(), state), state

    def _make_orderbook(self, bid_size: float, ask_size: float) -> Dict:
        return {
            "bids": [{"price": "0.70", "size": str(bid_size)}],
            "asks": [{"price": "0.72", "size": str(ask_size)}],
        }

    def _inject_prices(self, state, market_id: str, prices) -> None:
        for p in prices:
            state.append_price(market_id, p)

    def test_no_signal_without_enough_price_history(self, tmp_path):
        strat, _ = self._make(tmp_path)
        ob = self._make_orderbook(1000, 10)
        # Only 1 price → momentum returns 0.5 (neutral) → below threshold
        sig = strat.generate_signal("m1", "t1", ob, 0.70)
        assert sig is None

    def test_buy_signal_on_strong_uptrend(self, tmp_path):
        strat, state = self._make(tmp_path)
        # Rising prices (strong upward momentum)
        rising = [0.50, 0.54, 0.58, 0.63, 0.68, 0.72, 0.75]
        self._inject_prices(state, "m1", rising)
        # Heavily bid-side orderbook
        ob = self._make_orderbook(bid_size=5000, ask_size=100)
        sig = strat.generate_signal("m1", "t1", ob, 0.75)
        assert sig is not None
        assert sig["side"] == "BUY"
        assert 0 < sig["confidence"] <= 1

    def test_sell_signal_on_strong_downtrend(self, tmp_path):
        strat, state = self._make(tmp_path)
        # Falling prices
        falling = [0.80, 0.75, 0.70, 0.64, 0.58, 0.52, 0.47]
        self._inject_prices(state, "m2", falling)
        # Heavily ask-side orderbook
        ob = self._make_orderbook(bid_size=50, ask_size=5000)
        sig = strat.generate_signal("m2", "t2", ob, 0.47)
        assert sig is not None
        assert sig["side"] == "SELL"

    def test_no_signal_on_conflicting_momentum_and_imbalance(self, tmp_path):
        strat, state = self._make(tmp_path)
        # Rising price but heavy ask side → conflict → no signal
        rising = [0.50, 0.55, 0.60, 0.65, 0.70, 0.74]
        self._inject_prices(state, "m3", rising)
        ob = self._make_orderbook(bid_size=10, ask_size=5000)
        sig = strat.generate_signal("m3", "t3", ob, 0.74)
        assert sig is None

    def test_cooldown_prevents_second_signal(self, tmp_path):
        strat, state = self._make(tmp_path)
        rising = [0.50, 0.55, 0.60, 0.65, 0.70, 0.74, 0.77]
        self._inject_prices(state, "m4", rising)
        ob = self._make_orderbook(5000, 50)
        sig1 = strat.generate_signal("m4", "t4", ob, 0.77)
        sig2 = strat.generate_signal("m4", "t4", ob, 0.77)
        if sig1 is not None:
            assert sig2 is None  # cooldown active

    def test_cooldown_persists_after_state_reload(self, tmp_path):
        from core.strategy import AggressiveStrategy
        from core.state import StateStore

        path  = tmp_path / "state.db"
        state = StateStore(path=path)
        strat = AggressiveStrategy(_minimal_config(), state)

        # Manually set cooldown in state
        state.set_cooldown("m5", time.time())

        # Reload state
        state2 = StateStore(path=path)
        strat2 = AggressiveStrategy(_minimal_config(), state2)
        assert strat2._in_cooldown("m5") is True

    def test_momentum_score_flat_prices(self, tmp_path):
        strat, _ = self._make(tmp_path)
        flat = [0.60] * 10
        score = strat._momentum_score(flat)
        assert abs(score - 0.5) < 1e-6   # neutral

    def test_momentum_score_rising(self, tmp_path):
        strat, _ = self._make(tmp_path)
        rising = [0.50 + i * 0.02 for i in range(10)]
        score  = strat._momentum_score(rising)
        assert score > 0.5   # bullish

    def test_momentum_score_falling(self, tmp_path):
        strat, _ = self._make(tmp_path)
        falling = [0.80 - i * 0.02 for i in range(10)]
        score   = strat._momentum_score(falling)
        assert score < 0.5   # bearish

    def test_orderbook_imbalance_bid_heavy(self, tmp_path):
        strat, _ = self._make(tmp_path)
        ob    = self._make_orderbook(bid_size=1000, ask_size=10)
        score = strat._orderbook_imbalance(ob)
        assert score > 0.9   # strongly bid-side

    def test_orderbook_imbalance_empty_book(self, tmp_path):
        strat, _ = self._make(tmp_path)
        score = strat._orderbook_imbalance({"bids": [], "asks": []})
        assert score == 0.5  # neutral on empty book


# ─────────────────────────────────────────────────────────────────────────────
# Engine — market filter
# ─────────────────────────────────────────────────────────────────────────────

class TestEngineMarketFilter:

    def test_infer_asset_btc(self):
        from core.engine import _infer_asset
        assert _infer_asset("Will BTC exceed $100k?", {"BTC", "ETH"}) == "BTC"

    def test_infer_asset_bitcoin_alias(self):
        from core.engine import _infer_asset
        assert _infer_asset("Bitcoin price above 70k", {"BTC", "ETH"}) == "BTC"

    def test_infer_asset_no_false_xrp_match(self):
        from core.engine import _infer_asset
        # "XRPL" should NOT match "XRP"
        assert _infer_asset("Will XRPL launch new features?", {"XRP"}) is None

    def test_infer_asset_not_in_tracked(self):
        from core.engine import _infer_asset
        assert _infer_asset("Will ETH hit 5000?", {"BTC"}) is None   # ETH not tracked

    def test_is_fast_market_5min(self):
        from core.engine import _is_fast_market
        assert _is_fast_market({"question": "BTC above 70k in 5 minutes?"}) is True

    def test_is_fast_market_15min(self):
        from core.engine import _is_fast_market
        assert _is_fast_market({"slug": "btc-above-70k-15-minute"}) is True

    def test_is_fast_market_daily(self):
        from core.engine import _is_fast_market
        assert _is_fast_market({"question": "Will BTC hit 100k this week?"}) is False
