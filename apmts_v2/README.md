# APMTS v2 — Aggressive Polymarket Trading System

Fully automated, locally-executed trading system for Polymarket fast-resolution markets.  
Designed for **passive operation**, **strict risk control**, and **complete user privacy**.

---

## What's New in v2

| Area | v1 (broken) | v2 (fixed) |
|---|---|---|
| **API** | Wrong endpoints (`api.polymarket.com`) | Correct **Gamma** + **CLOB** dual-API |
| **Auth** | `Bearer <key>` (invalid) | HMAC-SHA256 L1 auth headers |
| **Momentum** | `[price] * 5` placeholder | Real persisted price history per market |
| **Signals** | Only ever generated BUY | Bidirectional **BUY + SELL** |
| **PnL tracking** | In-memory only (lost on restart) | Persisted in `state.db`, date-aware auto-reset |
| **Position close** | Never closed positions | Stop-loss + take-profit exit logic |
| **Kill switch** | env var not forwarded to child process | Kill **file** (`.kill`) polled by engine |
| **State writes** | Direct write (crash = corruption) | Atomic write-then-rename |
| **Config params** | Many params ignored | All params wired and validated |
| **Tests** | None | 25 unit tests covering all modules |
| **Logging** | Console only | Console + rotating file handler |
| **Credentials** | `create_credentials.py` setup script | Included |

---

## Strategy Overview

Each loop iteration:

1. **Market discovery** — Gamma API returns active markets; filtered to 5-min/15-min crypto markets (BTC/ETH/SOL/XRP) using word-boundary regex.
2. **Price history** — CLOB midpoint API updates a per-market ring buffer (max 30 samples) persisted to `state.db`.
3. **Momentum score** — Weighted log-returns → `tanh` squash → [0, 1].  `> 0.5` = upward, `< 0.5` = downward.
4. **Order-book imbalance** — Depth-weighted bid vs ask volume across top-5 levels → [0, 1].  `> 0.5` = buy pressure.
5. **Direction** — Both scores must agree (momentum and imbalance point the same way).
6. **Confidence** — Weighted average (momentum 40%, imbalance 60%).
7. **Risk gate** — Daily PnL, max positions, per-asset exposure.
8. **Execution** — Limit order with slippage applied.
9. **Position monitoring** — Stop-loss (default 15%) and take-profit (default 25%) checked every loop.

---

## Risk Controls

- Maximum USD per trade
- Minimum USD per trade (prevents dust orders)
- Maximum daily loss (USD) — **persists across restarts**
- Maximum concurrent open positions
- Per-asset exposure caps
- Stop-loss and take-profit per position
- Emergency kill switch (file-based, works across processes)

---

## Architecture

```
Polymarket Gamma API          Polymarket CLOB API
(market discovery)            (orderbook / orders / account)
       │                               │
       └──────────┬────────────────────┘
                  │
           PolymarketClient
                  │
           TradingEngine (core/engine.py)
          ┌───────┼───────┐
          │       │       │
    Strategy   RiskMgr  StateStore
   (signals)  (gates)  (persistence)
                  │
           Desktop Dashboard
           (desktop/app.py)
```

---

## Installation

Python 3.10+ required.

```bash
git clone https://github.com/your-username/apmts.git
cd apmts
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

---

## Setup: CLOB Credentials

Polymarket uses wallet-derived API credentials — **not a simple API key**.  
Run the one-time setup script:

```bash
# 1. Add your wallet private key to .env
cp .env.example .env
# Edit .env → set POLY_PRIVATE_KEY=0x...

# 2. Derive CLOB credentials
python scripts/create_credentials.py
```

This signs a derivation message with your private key and writes  
`POLY_API_KEY`, `POLY_SECRET`, and `POLY_PASSPHRASE` to your `.env`.

---

## Configuration

All settings are in `config/config.yaml`:

```yaml
risk:
  max_usd_per_trade:   50      # max size per order
  min_usd_per_trade:    2      # orders below this are skipped
  max_daily_loss_usd: 200      # engine stops when daily PnL < -200
  max_open_positions:   4
  stop_loss_pct:      0.15     # close if down 15%
  take_profit_pct:    0.25     # close if up 25%

strategy:
  momentum_threshold:            0.65
  orderbook_imbalance_threshold: 0.60
  min_confidence:                0.70
  cooldown_seconds:              90

execution:
  slippage_tolerance:   0.02   # 2% slippage on limit price
  loop_sleep_seconds:   10
```

---

## Running

```bash
# Start the trading engine
python run.py

# Start the desktop dashboard (in a separate terminal)
python desktop/app.py

# Emergency stop (any method works)
touch .kill               # file-based kill
export APMTS_KILL=1       # env-based kill
# Or use the KILL SWITCH button in the dashboard
```

---

## Tests

```bash
pip install pytest
pytest tests/ -v
```

25 tests covering:
- `StateStore`: persistence, atomic writes, ring buffer, cooldown, corruption recovery
- `RiskManager`: all gate conditions, PnL tracking, position sizing
- `AggressiveStrategy`: buy/sell signals, cooldown, momentum, imbalance
- `TradingEngine`: market filter, asset inference, fast-market detection

---

## Repository Structure

```
apmts/
├── run.py                      # Entry point
├── requirements.txt
├── .env.example                # Credential template
├── conftest.py                 # pytest path setup
├── config/
│   └── config.yaml             # All strategy & risk parameters
├── api/
│   └── polymarket.py           # GammaClient + CLOBClient
├── core/
│   ├── engine.py               # Main trading loop
│   ├── strategy.py             # Signal generation
│   ├── risk.py                 # Risk management
│   └── state.py                # Persistent state store
├── desktop/
│   └── app.py                  # Local monitoring dashboard
├── scripts/
│   └── create_credentials.py   # One-time CLOB credential setup
└── tests/
    └── test_core.py            # Unit tests
```

---

## Security

- API credentials stored only in local `.env` (never committed)
- Private key only used by `scripts/create_credentials.py` (one-time setup)
- No cloud services, no telemetry, no outbound connections except Polymarket API
- All state stored locally in `state.db`

---

## Disclaimer

This software executes trades using **real money**.

You are solely responsible for capital allocation, risk parameter selection,
API usage compliance, and financial outcomes.

**Test thoroughly on Amoy testnet (`POLY_CHAIN_ID=80002`) before using mainnet.**

Polymarket Terms of Service prohibit participation from certain jurisdictions
including the United States. Ensure compliance before use.
