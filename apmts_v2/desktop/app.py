"""
desktop/app.py
==============
Local monitoring dashboard for APMTS.

Fixes vs v1:
  - Kill switch writes a flag FILE instead of os.environ — works across processes
  - Engine launched with sys.executable (not hardcoded "python")
  - State file path resolved from project root (absolute, not CWD-relative)
  - load_state errors are surfaced in the UI, not silently swallowed
  - GUI updates scheduled via after() — never from background thread directly
  - "Total PnL" and "Total Trades" added to dashboard
  - Log tail widget shows last N lines of the log file
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import customtkinter as ctk

# ── Paths (resolved from this file's location) ─────────────────────────────────
_HERE         = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
_STATE_FILE   = _PROJECT_ROOT / "state.db"
_KILL_FILE    = _PROJECT_ROOT / ".kill"          # engine polls this file
_RUN_SCRIPT   = _PROJECT_ROOT / "run.py"
_LOG_DIR      = _PROJECT_ROOT / "logs"

REFRESH_MS    = 2000   # dashboard refresh interval


class APMTSApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        self.title("APMTS — Polymarket Trading System v2")
        self.geometry("580x560")
        self.resizable(False, False)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self._engine_process: Optional[subprocess.Popen] = None

        self._build_ui()
        self._schedule_refresh()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Header
        ctk.CTkLabel(
            self, text="APMTS — Polymarket Trading System",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(pady=(14, 4))

        # Status row
        status_frame = ctk.CTkFrame(self, fg_color="transparent")
        status_frame.pack(fill="x", padx=20, pady=4)

        ctk.CTkLabel(status_frame, text="Engine:").pack(side="left")
        self._status_label = ctk.CTkLabel(
            status_frame, text="STOPPED",
            text_color="tomato",
            font=ctk.CTkFont(weight="bold"),
        )
        self._status_label.pack(side="left", padx=8)

        # PnL row
        pnl_frame = ctk.CTkFrame(self, fg_color="transparent")
        pnl_frame.pack(fill="x", padx=20, pady=2)

        self._pnl_daily = ctk.CTkLabel(pnl_frame, text="Daily PnL:  $0.00")
        self._pnl_daily.pack(side="left", padx=(0, 20))

        self._pnl_total = ctk.CTkLabel(pnl_frame, text="Total PnL:  $0.00")
        self._pnl_total.pack(side="left")

        self._trades_label = ctk.CTkLabel(pnl_frame, text="Trades: 0")
        self._trades_label.pack(side="right")

        # Positions box
        ctk.CTkLabel(self, text="Open Positions", anchor="w").pack(
            fill="x", padx=20, pady=(8, 2)
        )
        self._positions_box = ctk.CTkTextbox(self, height=120, width=540)
        self._positions_box.pack(padx=20)

        # Log tail
        ctk.CTkLabel(self, text="Recent Log", anchor="w").pack(
            fill="x", padx=20, pady=(8, 2)
        )
        self._log_box = ctk.CTkTextbox(self, height=130, width=540)
        self._log_box.pack(padx=20)

        # Buttons
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(pady=14)

        self._start_btn = ctk.CTkButton(
            btn_frame, text="▶  START ENGINE",
            width=180, command=self._start_engine,
        )
        self._start_btn.pack(side="left", padx=8)

        self._kill_btn = ctk.CTkButton(
            btn_frame, text="⬛  KILL SWITCH",
            width=180, fg_color="#c0392b", hover_color="#922b21",
            command=self._kill_engine,
        )
        self._kill_btn.pack(side="left", padx=8)

        # Error label
        self._error_label = ctk.CTkLabel(
            self, text="", text_color="tomato", wraplength=540
        )
        self._error_label.pack(pady=4)

    # ── Engine control ─────────────────────────────────────────────────────────

    def _start_engine(self) -> None:
        if self._engine_process is not None:
            return  # already running

        # Clear kill file
        if _KILL_FILE.exists():
            _KILL_FILE.unlink()

        try:
            self._engine_process = subprocess.Popen(
                [sys.executable, str(_RUN_SCRIPT)],
                cwd=str(_PROJECT_ROOT),
            )
            self._set_status("RUNNING", "lime green")
            self._error_label.configure(text="")
        except Exception as exc:
            self._error_label.configure(text=f"Failed to start engine: {exc}")

    def _kill_engine(self) -> None:
        # Write kill file — engine polls this on every loop iteration
        _KILL_FILE.touch()

        # Also terminate the subprocess if we have a handle
        if self._engine_process is not None:
            try:
                self._engine_process.terminate()
                self._engine_process.wait(timeout=5)
            except Exception:
                try:
                    self._engine_process.kill()
                except Exception:
                    pass
            self._engine_process = None

        self._set_status("STOPPED", "tomato")

    def _set_status(self, text: str, color: str) -> None:
        self._status_label.configure(text=text, text_color=color)

    # ── State polling ──────────────────────────────────────────────────────────

    def _schedule_refresh(self) -> None:
        self._load_state()
        self._poll_process()
        self.after(REFRESH_MS, self._schedule_refresh)

    def _poll_process(self) -> None:
        """Check if the engine subprocess has exited unexpectedly."""
        if self._engine_process is not None:
            ret = self._engine_process.poll()
            if ret is not None:
                self._engine_process = None
                self._set_status("STOPPED (exited)", "tomato")

    def _load_state(self) -> None:
        if not _STATE_FILE.exists():
            return

        try:
            with _STATE_FILE.open("r") as f:
                state = json.load(f)

            daily_pnl  = state.get("daily_pnl_usd", 0.0)
            total_pnl  = state.get("total_pnl_usd", 0.0)
            total_tr   = state.get("total_trades", 0)
            positions  = state.get("open_positions", [])

            # PnL colours
            daily_color = "lime green" if daily_pnl >= 0 else "tomato"
            total_color = "lime green" if total_pnl >= 0 else "tomato"

            self._pnl_daily.configure(
                text=f"Daily PnL:  ${daily_pnl:+.2f}",
                text_color=daily_color,
            )
            self._pnl_total.configure(
                text=f"Total PnL:  ${total_pnl:+.2f}",
                text_color=total_color,
            )
            self._trades_label.configure(text=f"Trades: {total_tr}")

            # Positions
            self._positions_box.configure(state="normal")
            self._positions_box.delete("1.0", "end")
            if positions:
                for p in positions:
                    side  = p.get("side", "?")
                    asset = p.get("asset", "?")
                    size  = p.get("size_usd", 0)
                    price = p.get("entry_price", 0)
                    ts    = p.get("opened_at", "")[:19]
                    line  = f"[{side}] {asset} | ${size:.2f} @ {price:.4f}  {ts}\n"
                    self._positions_box.insert("end", line)
            else:
                self._positions_box.insert("end", "No open positions\n")
            self._positions_box.configure(state="disabled")

            self._error_label.configure(text="")

        except (json.JSONDecodeError, OSError) as exc:
            self._error_label.configure(text=f"State read error: {exc}")

        # Log tail
        self._refresh_log_tail()

    def _refresh_log_tail(self, lines: int = 12) -> None:
        """Show the last N lines from the most recent log file."""
        if not _LOG_DIR.exists():
            return
        log_files = sorted(_LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime)
        if not log_files:
            return
        latest = log_files[-1]
        try:
            with latest.open("r") as f:
                all_lines = f.readlines()
            tail = "".join(all_lines[-lines:])
            self._log_box.configure(state="normal")
            self._log_box.delete("1.0", "end")
            self._log_box.insert("end", tail)
            self._log_box.configure(state="disabled")
            self._log_box.see("end")
        except OSError:
            pass

    # ── Cleanup ────────────────────────────────────────────────────────────────

    def destroy(self) -> None:
        # Do not kill the engine when the UI closes — let it run headlessly
        super().destroy()


# Silence the Optional import warning (used in type hint above)
from typing import Optional  # noqa: E402


if __name__ == "__main__":
    app = APMTSApp()
    app.mainloop()
