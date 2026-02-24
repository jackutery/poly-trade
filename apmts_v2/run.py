"""
run.py
======
Entry point for the APMTS trading engine.

Changes vs v1:
  - Logging writes to both console (rich) AND a rotating log file
  - Config loaded with schema validation
  - Kill-file (.kill) polled by engine alongside env var
  - Python version guard
  - Graceful startup messages
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml
from rich.logging import RichHandler

# ── Python version check ───────────────────────────────────────────────────────
if sys.version_info < (3, 10):
    sys.exit("APMTS requires Python 3.10 or higher.")

# ── Paths ──────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent
_CONFIG_FILE  = _PROJECT_ROOT / "config" / "config.yaml"
_LOG_DIR      = _PROJECT_ROOT / "logs"
_LOG_DIR.mkdir(exist_ok=True)

# ── Logging ────────────────────────────────────────────────────────────────────

def _setup_logging(config: dict) -> None:
    log_cfg   = config.get("logging", {})
    log_level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)

    handlers: list[logging.Handler] = [
        RichHandler(rich_tracebacks=True, show_time=True, show_path=False),
    ]

    if log_cfg.get("to_file", True):
        log_file = _LOG_DIR / "apmts.log"
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)-12s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        handlers.append(file_handler)

    logging.basicConfig(
        level=log_level,
        format="%(message)s",
        handlers=handlers,
        force=True,
    )


# ── Config loading & validation ────────────────────────────────────────────────

_REQUIRED_CONFIG_KEYS = [
    "assets",
    "risk.max_usd_per_trade",
    "risk.max_daily_loss_usd",
    "risk.max_open_positions",
    "risk.per_asset_max_exposure_usd",
    "strategy.momentum_threshold",
    "strategy.orderbook_imbalance_threshold",
    "strategy.min_confidence",
    "strategy.cooldown_seconds",
]


def _load_config() -> dict:
    if not _CONFIG_FILE.exists():
        sys.exit(f"Config file not found: {_CONFIG_FILE}")

    with _CONFIG_FILE.open("r") as f:
        config = yaml.safe_load(f)

    # Validate required keys
    missing = []
    for key_path in _REQUIRED_CONFIG_KEYS:
        parts = key_path.split(".")
        node  = config
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                missing.append(key_path)
                break
            node = node[part]

    if missing:
        sys.exit(f"Config validation failed. Missing keys:\n  " + "\n  ".join(missing))

    return config


# ── Kill-file patch (inject into engine loop) ──────────────────────────────────

def _patch_kill_file_check() -> None:
    """
    Monkey-patch os.getenv so that checking APMTS_KILL also checks the .kill file.
    This allows the desktop app to stop the engine by touching .kill,
    even across process boundaries.
    """
    _kill_file = _PROJECT_ROOT / ".kill"
    _original_getenv = os.getenv

    def _patched_getenv(key: str, default=None):
        if key == "APMTS_KILL":
            if _kill_file.exists():
                return "1"
        return _original_getenv(key, default)

    os.getenv = _patched_getenv


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    config = _load_config()
    _setup_logging(config)
    _patch_kill_file_check()

    logger = logging.getLogger("main")
    logger.info("APMTS v2.0 starting…")
    logger.info(f"Config: {_CONFIG_FILE}")
    logger.info(f"Log dir: {_LOG_DIR}")

    # Late import so logging is configured before any module-level loggers fire
    from core.engine import TradingEngine

    engine = TradingEngine(config)
    engine.run()
