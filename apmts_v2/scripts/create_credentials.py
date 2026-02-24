"""
scripts/create_credentials.py
==============================
One-time setup: derive CLOB L1 API credentials from your wallet private key.

Run ONCE before starting APMTS for the first time:
    python scripts/create_credentials.py

This will:
  1. Connect to the Polymarket CLOB API
  2. Sign a derivation message with your private key
  3. Print POLY_API_KEY, POLY_SECRET, POLY_PASSPHRASE
  4. Optionally write them to your .env file

Requires:
    pip install py-clob-client python-dotenv
"""

import os
import sys
from pathlib import Path

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
except ImportError:
    sys.exit(
        "py-clob-client not installed.\n"
        "Run: pip install py-clob-client"
    )

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

CLOB_HOST = "https://clob.polymarket.com"


def main() -> None:
    private_key = os.getenv("POLY_PRIVATE_KEY", "").strip()
    chain_id    = int(os.getenv("POLY_CHAIN_ID", "137"))

    if not private_key:
        sys.exit(
            "POLY_PRIVATE_KEY not set.\n"
            "Add it to your .env file first, then re-run this script."
        )

    print(f"Connecting to CLOB: {CLOB_HOST} (chain {chain_id})")

    # Build an unauthenticated client to derive creds
    client = ClobClient(
        host       = CLOB_HOST,
        key        = private_key,
        chain_id   = POLYGON if chain_id == 137 else chain_id,
    )

    print("Deriving API credentials (requires one on-chain signature)…")
    creds = client.create_or_derive_api_creds()

    print("\n✅ Credentials derived successfully!\n")
    print(f"POLY_API_KEY={creds.api_key}")
    print(f"POLY_SECRET={creds.api_secret}")
    print(f"POLY_PASSPHRASE={creds.api_passphrase}")

    env_file = _PROJECT_ROOT / ".env"
    answer   = input(f"\nWrite these to {env_file}? [y/N] ").strip().lower()

    if answer == "y":
        lines: list[str] = []
        if env_file.exists():
            with env_file.open("r") as f:
                lines = f.readlines()

        def _set(key: str, value: str) -> None:
            for i, line in enumerate(lines):
                if line.startswith(f"{key}="):
                    lines[i] = f"{key}={value}\n"
                    return
            lines.append(f"{key}={value}\n")

        _set("POLY_API_KEY",    creds.api_key)
        _set("POLY_SECRET",     creds.api_secret)
        _set("POLY_PASSPHRASE", creds.api_passphrase)

        with env_file.open("w") as f:
            f.writelines(lines)

        print(f"✅ Written to {env_file}")
    else:
        print("Skipped. Copy the values above into your .env file manually.")


if __name__ == "__main__":
    main()
