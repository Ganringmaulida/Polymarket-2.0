#!/usr/bin/env python3
"""
run_scheduler.py
=================
Lightweight Event-Driven Scheduler — the "alarm clock" that wakes
the pipeline at strategically optimal moments.

Two trigger modes (configurable in config.yaml):

Mode A — Time-to-Kickoff:
    Monitors upcoming fixture kickoff times from the Odds API.
    Fires the pipeline exactly N minutes before each kickoff to
    capture maximum CLOB liquidity and post-lineup-announcement odds.
    Think of it as setting your alarm for the exact moment the
    market is most informative.

Mode B — Fixed UTC Schedule:
    Fires at predetermined UTC times regardless of fixtures.
    Simpler and more predictable — like a news broadcast on a
    fixed schedule.

Usage:
    python run_scheduler.py                  # Run continuously
    python run_scheduler.py --mode kickoff   # Only kickoff-based triggers
    python run_scheduler.py --mode fixed     # Only fixed-time triggers
    python run_scheduler.py --once           # One immediate run then exit
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

log = logging.getLogger("scheduler")


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def run_pipeline_now(config_path: str = "config.yaml") -> None:
    """Invoke the main pipeline as a subprocess."""
    script = Path(__file__).parent / "ev_pipeline.py"
    cmd = [sys.executable, str(script), "--config", config_path]
    log.info("Triggering pipeline: %s", " ".join(cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        log.warning("Pipeline exited with code %d", result.returncode)


def get_upcoming_kickoffs(config: dict) -> list[datetime]:
    """
    Fetch all kickoff times from Odds API for scheduling purposes.
    We use a lightweight call here — just event listings, no full odds.
    """
    try:
        import requests
        import os

        api_key = os.environ.get("ODDS_API_KEY") or config["odds_api"]["api_key"]
        if api_key == "YOUR_ODDS_API_KEY_HERE":
            return []

        kickoffs: list[datetime] = []
        for sport in config["odds_api"]["sports"]:
            resp = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{sport}/events",
                params={"apiKey": api_key},
                timeout=10,
            )
            if resp.status_code == 200:
                for event in resp.json():
                    ct = event.get("commence_time", "")
                    try:
                        dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                        kickoffs.append(dt)
                    except ValueError:
                        pass
            time.sleep(0.2)  # rate limiting

        return sorted(kickoffs)

    except Exception as e:
        log.warning("Could not fetch kickoff times: %s", e)
        return []


def get_next_fixed_run(config: dict, now: datetime) -> datetime:
    """Return the next fixed UTC run time after `now`."""
    run_times_str: list[str] = config["scheduler"]["fixed_run_times_utc"]
    candidates: list[datetime] = []

    for t_str in run_times_str:
        hh, mm = map(int, t_str.split(":"))
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        candidates.append(candidate)

    return min(candidates)


def scheduler_loop(config: dict, mode: str, config_path: str) -> None:
    """
    Main scheduling loop. Runs indefinitely until interrupted.
    Like a professional timer — always watching, never forgetting.
    """
    lead_minutes = config["scheduler"]["minutes_before_kickoff"]
    notified_kickoffs: set[str] = set()   # Prevent duplicate triggers

    log.info("Scheduler started. Mode: %s | Lead time: %d min", mode, lead_minutes)

    while True:
        now = datetime.now(tz=timezone.utc)

        triggered = False

        # ── Mode A: Kickoff-based triggers ──────────────────────────────────
        if mode in ("kickoff", "both"):
            kickoffs = get_upcoming_kickoffs(config)

            for kickoff in kickoffs:
                key = kickoff.isoformat()
                if key in notified_kickoffs:
                    continue

                time_to_kickoff = (kickoff - now).total_seconds() / 60  # minutes
                target_window = (lead_minutes - 2, lead_minutes + 2)    # ±2 min window

                if target_window[0] <= time_to_kickoff <= target_window[1]:
                    log.info(
                        "Kickoff trigger: %s (T-%dm)",
                        kickoff.strftime("%Y-%m-%d %H:%M UTC"), int(time_to_kickoff),
                    )
                    run_pipeline_now(config_path)
                    notified_kickoffs.add(key)
                    triggered = True

        # ── Mode B: Fixed-time triggers ──────────────────────────────────────
        if mode in ("fixed", "both"):
            next_fixed = get_next_fixed_run(config, now)
            delta_sec = (next_fixed - now).total_seconds()

            if delta_sec <= 30:  # within 30 seconds of scheduled time
                log.info("Fixed trigger: %s", next_fixed.strftime("%H:%M UTC"))
                run_pipeline_now(config_path)
                triggered = True
                time.sleep(60)  # Prevent double-trigger within same minute

        if not triggered:
            # Sleep 60s between checks. Low overhead, sufficient granularity.
            time.sleep(60)


def main() -> None:
    parser = argparse.ArgumentParser(description="EV Pipeline Scheduler")
    parser.add_argument("--config", "-c", default="config.yaml")
    parser.add_argument(
        "--mode",
        choices=["kickoff", "fixed", "both"],
        default="both",
        help="Trigger mode (default: both)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run pipeline once immediately and exit",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  [SCHEDULER]  %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config(args.config)

    if args.once:
        log.info("--once flag: running pipeline immediately.")
        run_pipeline_now(args.config)
        return

    try:
        scheduler_loop(config, args.mode, args.config)
    except KeyboardInterrupt:
        log.info("Scheduler stopped by operator (Ctrl+C).")


if __name__ == "__main__":
    main()