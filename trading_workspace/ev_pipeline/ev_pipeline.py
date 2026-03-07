#!/usr/bin/env python3
"""
ev_pipeline.py
===============
Semi-Automated Expected Value (EV) Betting Pipeline
────────────────────────────────────────────────────
Main Orchestrator — the "conductor" of the pipeline orchestra.

Each module is a specialist musician:
  - PolymarketFetcher  → retrieves live market prices (the instrument)
  - OddsFetcher        → retrieves sharp bookmaker lines (the sheet music)
  - EVCalculator       → computes edge via matching + math (the score)
  - TerminalReporter   → formats and presents the analysis (the performance)

This file coordinates all four in sequence and handles errors
at the orchestration level, ensuring one bad data source cannot
silently corrupt the entire run.

Usage:
    python ev_pipeline.py                     # Single run
    python ev_pipeline.py --config my.yaml    # Custom config
    python ev_pipeline.py --dry-run           # Simulate without API calls
    python ev_pipeline.py --sport nba         # Filter to one sport
    python ev_pipeline.py --verbose           # Debug logging

Scheduling (cron example — 60 min before typical evening kickoffs):
    0 17,20 * * * cd /path/to/ev_pipeline && python ev_pipeline.py >> cron.log 2>&1
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml


# ──────────────────────────────────────────────────
# Bootstrap: add project root to sys.path
# ──────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from fetchers.polymarket_fetcher import PolymarketFetcher
from fetchers.odds_fetcher import OddsFetcher
from core.ev_engine import EVCalculator
from output.reporter import TerminalReporter


def load_config(config_path: str) -> dict:
    """Load YAML configuration. Fail loudly on parse errors."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            f"Copy config.yaml.example to config.yaml and fill in your API keys."
        )
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(config: dict, verbose: bool = False) -> None:
    """Configure root logger based on config and CLI flags."""
    level_str = "DEBUG" if verbose else config["output"]["log_level"]
    level = getattr(logging, level_str, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # Suppress noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Semi-Automated EV Betting Pipeline for Polymarket Sports",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python ev_pipeline.py
  python ev_pipeline.py --config config.yaml --verbose
  python ev_pipeline.py --sport basketball_nba
  python ev_pipeline.py --dry-run
        """,
    )
    parser.add_argument(
        "--config", "-c",
        default="config.yaml",
        help="Path to YAML config file (default: config.yaml)",
    )
    parser.add_argument(
        "--sport", "-s",
        default=None,
        help="Run analysis for a single sport key only (e.g. basketball_nba)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load configs but skip live API calls; useful for testing setup",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG level logging",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color codes in terminal output",
    )
    return parser.parse_args()


def run_pipeline(config: dict, args: argparse.Namespace) -> int:
    """
    Execute the full pipeline. Returns exit code (0 = success).
    
    The pipeline is structured as a four-stage assembly line:
    
    Stage 1 [FETCH MARKET DATA]  → Polymarket CLI subprocess calls
    Stage 2 [FETCH ODDS DATA]    → The-Odds-API HTTP requests  
    Stage 3 [COMPUTE EV]         → Match + calculate edge
    Stage 4 [REPORT]             → Format + print to terminal
    
    If Stage 1 or 2 fails, the pipeline aborts early and logs the
    error — never silently producing an empty or misleading report.
    """
    log = logging.getLogger("pipeline")
    run_time = datetime.now(tz=timezone.utc)

    # Override color setting from CLI flag
    if args.no_color:
        config["output"]["colored_terminal"] = False

    # Override sport filter
    if args.sport:
        config["odds_api"]["sports"] = [args.sport]
        log.info("Sport filter applied: %s", args.sport)

    reporter = TerminalReporter(config)

    # ── Stage 1: Polymarket Market Data ─────────────────────────────────────
    log.info("Stage 1/4 — Fetching Polymarket sports markets...")
    t0 = time.perf_counter()

    if args.dry_run:
        log.warning("DRY RUN: Skipping Polymarket CLI calls. Zero markets loaded.")
        poly_markets = []
    else:
        poly_fetcher = PolymarketFetcher(config)
        try:
            poly_markets = poly_fetcher.fetch_all_sports_markets()
        except Exception as e:
            log.error("FATAL: Polymarket fetch failed: %s", e)
            log.error("Ensure `polymarket` binary is installed. See README.")
            return 1

    log.info(
        "Stage 1 complete: %d markets loaded (%.2fs)",
        len(poly_markets), time.perf_counter() - t0,
    )

    if not poly_markets and not args.dry_run:
        log.warning(
            "No Polymarket markets loaded. "
            "Check CLI installation: run `polymarket markets list` manually."
        )

    # ── Stage 2: External Odds Data ─────────────────────────────────────────
    log.info("Stage 2/4 — Fetching bookmaker odds from Odds API...")
    t1 = time.perf_counter()

    if args.dry_run:
        log.warning("DRY RUN: Skipping Odds API calls. Zero events loaded.")
        sport_events = []
    else:
        odds_fetcher = OddsFetcher(config)
        try:
            sport_events = odds_fetcher.fetch_all_events()
        except Exception as e:
            log.error("FATAL: Odds API fetch failed: %s", e)
            return 1

    log.info(
        "Stage 2 complete: %d sport events loaded (%.2fs)",
        len(sport_events), time.perf_counter() - t1,
    )

    if not sport_events and not args.dry_run:
        log.warning(
            "No sport events loaded. Verify ODDS_API_KEY is set "
            "and configured sports have upcoming fixtures."
        )

    # ── Stage 3: EV Calculation ──────────────────────────────────────────────
    log.info("Stage 3/4 — Computing Expected Value for matched markets...")
    t2 = time.perf_counter()

    ev_calc = EVCalculator(config)
    results = ev_calc.evaluate_all(poly_markets, sport_events)

    log.info(
        "Stage 3 complete: %d results produced, %d with positive edge (%.2fs)",
        len(results),
        sum(1 for r in results if r.has_edge),
        time.perf_counter() - t2,
    )

    # ── Stage 4: Report ──────────────────────────────────────────────────────
    log.info("Stage 4/4 — Generating report...")

    reporter.print_report(
        results=results,
        run_time=run_time,
        n_markets_scanned=len(poly_markets),
        n_events_loaded=len(sport_events),
    )

    if config["output"]["save_json_snapshot"]:
        snapshot_path = reporter.save_json_snapshot(results, run_time)
        if snapshot_path:
            log.info("Snapshot: %s", snapshot_path)

    buy_count = sum(1 for r in results if r.has_edge)
    log.info("Pipeline complete. %d actionable recommendations.", buy_count)

    return 0


def main() -> None:
    args = parse_args()

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(f"\n[ERROR] {e}\n", file=sys.stderr)
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"\n[ERROR] Config parse error: {e}\n", file=sys.stderr)
        sys.exit(1)

    setup_logging(config, verbose=args.verbose)

    exit_code = run_pipeline(config, args)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()