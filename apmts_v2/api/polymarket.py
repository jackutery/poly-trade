"""
api/polymarket.py
=================
Polymarket dual-API client.

Architecture:
  - GammaClient  → gamma-api.polymarket.com  (market discovery & metadata)
  - CLOBClient   → clob.polymarket.com        (orderbook, orders, balances)

Authentication:
  Polymarket CLOB requires L1 API credentials (api_key, secret, passphrase)
  generated from your wallet via py-clob-client's create_or_derive_api_creds().
  These are DIFFERENT from a simple "API key" — they are derived from your
  private key through an on-chain signature.

  See: https://docs.polymarket.com/#authentication

Required env vars:
  POLY_API_KEY        - CLOB L1 API key
  POLY_SECRET         - CLOB L1 secret
  POLY_PASSPHRASE     - CLOB L1 passphrase
  POLY_PRIVATE_KEY    - Wallet private key (for order signing)
  POLY_CHAIN_ID       - 137 (Polygon mainnet) or 80002 (Amoy testnet)
"""

import os
import time
import logging
import hmac
import hashlib
import base64
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("polymarket")

# ── Constants ──────────────────────────────────────────────────────────────────
GAMMA_BASE   = "https://gamma-api.polymarket.com"
CLOB_BASE    = "https://clob.polymarket.com"
DEFAULT_TIMEOUT = 15
MAX_RETRIES     = 3
RETRY_BACKOFF   = 2.0   # seconds, multiplied by attempt number


# ── Exceptions ─────────────────────────────────────────────────────────────────
class PolymarketAPIError(Exception):
    """Raised when any Polymarket API returns a non-2xx response."""


class PolymarketAuthError(Exception):
    """Raised when credentials are missing or invalid."""


class PolymarketKillSwitch(Exception):
    """Raised when APMTS_KILL=1 is detected — stops the engine immediately."""


# ── Internal helpers ───────────────────────────────────────────────────────────

def _check_kill() -> None:
    if os.getenv("APMTS_KILL", "0") == "1":
        raise PolymarketKillSwitch("Kill switch activated — halting.")


def _load_credentials() -> Dict[str, str]:
    """Load and validate all required env vars at startup."""
    required = {
        "api_key":    "POLY_API_KEY",
        "secret":     "POLY_SECRET",
        "passphrase": "POLY_PASSPHRASE",
        "private_key":"POLY_PRIVATE_KEY",
        "chain_id":   "POLY_CHAIN_ID",
    }
    creds: Dict[str, str] = {}
    missing = []
    for field, env in required.items():
        val = os.getenv(env, "").strip()
        if not val:
            missing.append(env)
        creds[field] = val
    if missing:
        raise PolymarketAuthError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            "See .env.example for setup instructions."
        )
    return creds


def _build_clob_headers(
    creds: Dict[str, str],
    method: str,
    path: str,
    body: str = "",
) -> Dict[str, str]:
    """
    Build CLOB L1 authentication headers.

    HMAC-SHA256 signature over: timestamp + method + path + body
    See: https://docs.polymarket.com/#l1-authentication
    """
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    message   = timestamp + method.upper() + path + body

    secret_bytes = base64.b64decode(creds["secret"])
    sig = hmac.new(secret_bytes, message.encode(), hashlib.sha256)
    signature = base64.b64encode(sig.digest()).decode()

    return {
        "POLY-API-KEY":    creds["api_key"],
        "POLY-SIGNATURE":  signature,
        "POLY-TIMESTAMP":  timestamp,
        "POLY-PASSPHRASE": creds["passphrase"],
        "Content-Type":    "application/json",
        "Accept":          "application/json",
        "User-Agent":      "APMTS/2.0",
    }


# ── GammaClient ────────────────────────────────────────────────────────────────

class GammaClient:
    """
    Read-only client for the Polymarket Gamma API.
    Used for market discovery and metadata — no auth required.
    """

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "Accept":     "application/json",
            "User-Agent": "APMTS/2.0",
        })

    def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        _check_kill()
        url = GAMMA_BASE + path

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
                if resp.status_code == 429:
                    wait = RETRY_BACKOFF * attempt
                    logger.warning(f"Gamma rate-limited — waiting {wait}s")
                    time.sleep(wait)
                    continue
                if resp.status_code >= 400:
                    raise PolymarketAPIError(f"Gamma HTTP {resp.status_code}: {resp.text[:300]}")
                return resp.json()
            except (requests.RequestException, PolymarketAPIError) as exc:
                logger.error(f"Gamma request failed (attempt {attempt}/{MAX_RETRIES}): {exc}")
                if attempt == MAX_RETRIES:
                    raise
                time.sleep(RETRY_BACKOFF * attempt)

        raise PolymarketAPIError("Gamma: max retries exceeded")

    def get_markets(
        self,
        active: bool = True,
        closed: bool = False,
        limit: int = 200,
        offset: int = 0,
    ) -> List[Dict]:
        """
        Fetch markets from Gamma API.

        Returns a list of market dicts. Each market contains:
          id, question, conditionId, slug, endDate, active, closed,
          tokens (YES/NO token_ids), volume, etc.
        """
        params: Dict[str, Any] = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit":  limit,
            "offset": offset,
        }
        result = self._get("/markets", params=params)
        # Gamma returns {"markets": [...], "count": N}
        if isinstance(result, dict):
            return result.get("markets", [])
        if isinstance(result, list):
            return result
        return []

    def get_market_by_slug(self, slug: str) -> Optional[Dict]:
        """Fetch a single market by its slug."""
        result = self._get(f"/markets/{slug}")
        return result if isinstance(result, dict) else None

    def get_events(self, active: bool = True, limit: int = 100) -> List[Dict]:
        """Fetch events (groups of related markets)."""
        params = {"active": str(active).lower(), "limit": limit}
        result = self._get("/events", params=params)
        if isinstance(result, dict):
            return result.get("events", [])
        return result if isinstance(result, list) else []


# ── CLOBClient ─────────────────────────────────────────────────────────────────

class CLOBClient:
    """
    Authenticated client for the Polymarket CLOB API.
    Handles orderbook queries, order placement, and account data.
    """

    def __init__(self) -> None:
        self.creds = _load_credentials()
        self.session = requests.Session()

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        payload: Optional[Dict] = None,
        retries: int = MAX_RETRIES,
    ) -> Any:
        _check_kill()

        import json as _json
        body_str = _json.dumps(payload) if payload else ""
        url      = CLOB_BASE + path
        headers  = _build_clob_headers(self.creds, method, path, body_str)

        for attempt in range(1, retries + 1):
            try:
                resp = self.session.request(
                    method  = method.upper(),
                    url     = url,
                    headers = headers,
                    params  = params,
                    data    = body_str if payload else None,
                    timeout = DEFAULT_TIMEOUT,
                )

                if resp.status_code == 401:
                    raise PolymarketAuthError(
                        "CLOB auth failed — check POLY_API_KEY / POLY_SECRET / POLY_PASSPHRASE"
                    )
                if resp.status_code == 429:
                    wait = RETRY_BACKOFF * attempt
                    logger.warning(f"CLOB rate-limited — waiting {wait}s")
                    time.sleep(wait)
                    # Rebuild headers with new timestamp for retry
                    headers = _build_clob_headers(self.creds, method, path, body_str)
                    continue
                if resp.status_code >= 400:
                    raise PolymarketAPIError(
                        f"CLOB HTTP {resp.status_code}: {resp.text[:300]}"
                    )

                return resp.json()

            except PolymarketAuthError:
                raise   # never retry auth errors
            except (requests.RequestException, PolymarketAPIError) as exc:
                logger.error(f"CLOB request failed (attempt {attempt}/{retries}): {exc}")
                if attempt == retries:
                    raise
                time.sleep(RETRY_BACKOFF * attempt)

        raise PolymarketAPIError("CLOB: max retries exceeded")

    # ── Orderbook ──────────────────────────────────────────────────────────────

    def get_orderbook(self, token_id: str) -> Dict:
        """
        Fetch the full orderbook for a token (YES or NO).

        Args:
            token_id: The CLOB token ID (from Gamma market.tokens[n].token_id)

        Returns:
            {
              "market": str,
              "asset_id": str,
              "bids": [{"price": str, "size": str}, ...],   # sorted desc
              "asks": [{"price": str, "size": str}, ...],   # sorted asc
              "hash": str
            }
        """
        return self._request("GET", "/book", params={"token_id": token_id})

    def get_spread(self, token_id: str) -> Dict:
        """Lightweight mid/spread info for a token."""
        return self._request("GET", "/spread", params={"token_id": token_id})

    def get_price(self, token_id: str, side: str) -> Dict:
        """
        Get best bid or ask price.
        side: "buy" or "sell"
        """
        return self._request(
            "GET", "/price",
            params={"token_id": token_id, "side": side}
        )

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """
        Convenience: return midpoint price as float or None.
        """
        try:
            data = self._request("GET", "/midpoint", params={"token_id": token_id})
            mid = data.get("mid")
            return float(mid) if mid is not None else None
        except PolymarketAPIError:
            return None

    # ── Orders ─────────────────────────────────────────────────────────────────

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "GTC",
    ) -> Dict:
        """
        Place a limit order via the CLOB.

        Args:
            token_id:   CLOB token ID for the YES or NO token
            side:       "BUY" or "SELL"
            price:      Limit price in [0.01, 0.99]
            size:       Order size in USDC (e.g. 10.0 = $10)
            order_type: "GTC" (Good Till Cancelled) or "FOK" (Fill or Kill)

        Returns:
            {"orderID": str, "status": str, ...}

        Note:
            Orders must be signed with your private key (EIP-712).
            This implementation uses the simplified signed-order endpoint.
            For production, integrate py-clob-client for full signing support.
        """
        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise ValueError(f"Invalid side '{side}' — must be BUY or SELL")

        if not (0.01 <= price <= 0.99):
            raise ValueError(f"Price {price} out of range [0.01, 0.99]")

        if size < 1.0:
            raise ValueError(f"Minimum order size is $1.00 USDC, got {size}")

        payload = {
            "order": {
                "tokenID":    token_id,
                "side":       side,
                "price":      str(round(price, 4)),
                "size":       str(round(size, 2)),
                "orderType":  order_type,
                "feeRateBps": "0",
            },
            "owner":    self.creds["api_key"],
            "orderType": order_type,
        }

        logger.info(f"Placing order: token={token_id} {side} {size}USDC @ {price}")
        return self._request("POST", "/order", payload=payload)

    def cancel_order(self, order_id: str) -> Dict:
        """Cancel a specific open order."""
        return self._request("DELETE", f"/order/{order_id}")

    def cancel_all_orders(self) -> Dict:
        """Cancel all open orders for the authenticated account."""
        return self._request("DELETE", "/orders")

    def get_orders(self, status: str = "LIVE") -> List[Dict]:
        """
        Fetch orders filtered by status.
        status: "LIVE", "MATCHED", "DELAYED", "UNMATCHED", "CANCELED"
        """
        return self._request("GET", "/orders", params={"status": status})

    def get_order(self, order_id: str) -> Dict:
        """Fetch a single order by ID."""
        return self._request("GET", f"/order/{order_id}")

    # ── Account ────────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        """
        Return available USDC balance as a float.
        """
        data = self._request("GET", "/balance")
        return float(data.get("balance", 0))

    def get_positions(self) -> List[Dict]:
        """
        Fetch open token positions for the authenticated account.

        Returns list of:
          {"asset": str, "size": str, "price": str, "outcome": str, ...}
        """
        return self._request("GET", "/positions")

    def get_trades(self, limit: int = 50) -> List[Dict]:
        """Fetch recent trade history."""
        return self._request("GET", "/trades", params={"limit": limit})

    # ── Sampling ───────────────────────────────────────────────────────────────

    def get_last_trade_price(self, token_id: str) -> Optional[float]:
        """Return the last matched trade price for a token."""
        try:
            trades = self._request(
                "GET", "/last-trade-price", params={"token_id": token_id}
            )
            price = trades.get("price")
            return float(price) if price is not None else None
        except PolymarketAPIError:
            return None


# ── Unified facade ─────────────────────────────────────────────────────────────

class PolymarketClient:
    """
    Single entry point that combines GammaClient (market discovery)
    and CLOBClient (execution) into one object.

    Usage:
        client = PolymarketClient()
        markets = client.gamma.get_markets()
        book    = client.clob.get_orderbook(token_id)
        order   = client.clob.place_order(token_id, "BUY", 0.72, 25.0)
    """

    def __init__(self) -> None:
        self.gamma = GammaClient()
        self.clob  = CLOBClient()
        logger.info("PolymarketClient initialised (Gamma + CLOB)")
