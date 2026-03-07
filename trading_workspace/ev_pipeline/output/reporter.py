"""
output/reporter.py
===================
Terminal Reporter — the "mission debrief" module.

Transforms raw EVResult objects into a structured, human-readable
terminal report that an operator can scan in under 60 seconds
and immediately know what to execute.

Design philosophy: Like a well-formatted briefing document given
to a general before battle — essential facts upfront, supporting
detail behind. Signal, not noise.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.ev_engine import EVResult, Recommendation

logger = logging.getLogger(__name__)

# ANSI color codes for terminal output
class Color:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    RED     = "\033[91m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    BLUE    = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN    = "\033[96m"
    WHITE   = "\033[97m"
    DIM     = "\033[2m"
    BG_GREEN  = "\033[42m"
    BG_RED    = "\033[41m"
    BG_YELLOW = "\033[43m"
    BG_BLUE   = "\033[44m"


def _c(text: str, *codes: str, use_color: bool = True) -> str:
    if not use_color:
        return text
    return "".join(codes) + text + Color.RESET


class TerminalReporter:
    """
    Formats and prints the EV pipeline results to stdout and optionally
    saves to log file and JSON snapshot.
    """

    SEPARATOR     = "─" * 100
    THICK_SEP     = "═" * 100
    SECTION_WIDTH = 100

    def __init__(self, config: dict):
        self.colored       = config["output"]["colored_terminal"]
        self.log_file      = config["output"]["log_file"]
        self.save_json     = config["output"]["save_json_snapshot"]
        self.snapshot_dir  = Path(config["output"]["json_snapshot_dir"])
        self.margin        = config["ev"]["margin_of_safety"]

        # Setup file logger if configured
        if self.log_file:
            fh = logging.FileHandler(self.log_file, mode="a", encoding="utf-8")
            fh.setLevel(logging.INFO)
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logging.getLogger().addHandler(fh)

    def _c(self, text: str, *codes: str) -> str:
        return _c(text, *codes, use_color=self.colored)

    def _header(self, run_time: datetime, n_markets_scanned: int, n_events_loaded: int) -> str:
        ts = run_time.strftime("%Y-%m-%d %H:%M:%S UTC")
        lines = [
            "",
            self._c(self.THICK_SEP, Color.CYAN, Color.BOLD),
            self._c(
                "  SEMI-AUTOMATED EV BETTING PIPELINE  ─  ANALYSIS REPORT",
                Color.CYAN, Color.BOLD,
            ),
            self._c(f"  Run Time    : {ts}", Color.WHITE),
            self._c(f"  Markets Scanned  : {n_markets_scanned}", Color.WHITE),
            self._c(f"  Sport Events Loaded : {n_events_loaded}", Color.WHITE),
            self._c(f"  Edge Threshold  : {self.margin*100:.1f}%  (Margin of Safety)", Color.WHITE),
            self._c(self.THICK_SEP, Color.CYAN, Color.BOLD),
            "",
        ]
        return "\n".join(lines)

    def _format_result_block(self, result: EVResult, rank: int) -> str:
        """
        Format one EVResult as a structured multi-line block.
        Like a trading desk "ticket" — all essential info at a glance.
        """
        c = self._c

        # Recommendation badge
        rec = result.recommendation
        if rec == Recommendation.BUY_YES:
            badge = c(f" {rec.value} ", Color.BOLD, Color.BG_GREEN, Color.WHITE)
        elif rec == Recommendation.BUY_NO:
            badge = c(f" {rec.value} ", Color.BOLD, Color.BG_GREEN, Color.WHITE)
        elif rec == Recommendation.NO_EDGE:
            badge = c(f" {rec.value} ", Color.BG_YELLOW, Color.WHITE)
        else:
            badge = c(f" {rec.value} ", Color.DIM)

        edge_pct = max(result.edge_yes, result.edge_no) * 100
        edge_color = Color.GREEN if edge_pct > 0 else Color.RED
        edge_str = c(f"{edge_pct:+.2f}%", edge_color, Color.BOLD)

        ev_color = Color.GREEN if result.ev_per_dollar > 0 else Color.RED
        ev_str = c(f"${result.ev_per_dollar:+.4f}", ev_color, Color.BOLD)

        t_minus = f"T-{result.hours_until_kickoff:.1f}h" if result.hours_until_kickoff > 0 else "LIVE"
        t_color = Color.YELLOW if result.hours_until_kickoff < 2 else Color.WHITE

        lines = [
            c(self.SEPARATOR, Color.DIM),
            f"  #{rank:<3}  {badge}  {c(result.polymarket_question, Color.BOLD, Color.WHITE)}",
            f"       {'Odds Event':<16}: {c(result.sport_event_label, Color.CYAN)} "
            f"({result.sport_key})  │  Kickoff: {c(t_minus, t_color)}  "
            f"│  Match Confidence: {result.match_confidence:.0%}",
            "",
            f"       {'TRUE PROBABILITY':<16}  "
            f"Yes: {c(f'{result.true_prob_yes:.2%}', Color.CYAN, Color.BOLD)}  │  "
            f"No: {c(f'{result.true_prob_no:.2%}', Color.CYAN, Color.BOLD)}",
            f"       {'MARKET (POLYMARKET)':<16}  "
            f"Yes: {c(f'{result.implied_prob_yes:.2%}', Color.MAGENTA, Color.BOLD)}  │  "
            f"No: {c(f'{result.implied_prob_no:.2%}', Color.MAGENTA, Color.BOLD)}",
            "",
            f"       {'EDGE':<16}: {edge_str}  │  "
            f"EV per $1: {ev_str}  │  "
            f"Volume: ${result.polymarket_volume_usd:>12,.0f}  │  "
            f"Liquidity: ${result.polymarket_liquidity_usd:>10,.0f}",
            "",
            f"       {'MARKET URL':<16}: https://polymarket.com/event/{result.polymarket_slug}",
        ]

        if result.has_edge and result.recommended_token_id:
            action = c(
                f"  ▶  ACTION: {rec.value}  │  TOKEN ID: {result.recommended_token_id}  ",
                Color.BOLD, Color.GREEN,
            )
            lines.append("")
            lines.append(action)

        return "\n".join(lines)

    def _summary_table(self, results: list[EVResult]) -> str:
        """
        Compact summary table of all BUY recommendations.
        The "quick-glance" view for the operator's pre-execution checklist.
        """
        buys = [r for r in results if r.has_edge]
        if not buys:
            return self._c(
                "\n  ⚑  No actionable edges found in this run. "
                "All markets within margin of safety.\n",
                Color.YELLOW,
            )

        lines = [
            "",
            self._c("  ╔══ ACTIONABLE OPPORTUNITIES  ══╗", Color.GREEN, Color.BOLD),
            "",
            self._c(
                f"  {'#':<4} {'MARKET':<45} {'SIDE':<8} "
                f"{'TRUE':>7} {'MKT':>7} {'EDGE':>7} {'EV/$':>8}  T-",
                Color.BOLD, Color.WHITE,
            ),
            "  " + "─" * 95,
        ]

        for i, r in enumerate(buys, 1):
            side = "YES" if r.recommendation == Recommendation.BUY_YES else "NO"
            true_p = r.true_prob_yes if side == "YES" else r.true_prob_no
            mkt_p  = r.implied_prob_yes if side == "YES" else r.implied_prob_no
            edge   = r.ev_per_dollar
            q_short = r.polymarket_question[:44]

            line = (
                f"  {i:<4} {q_short:<45} {side:<8} "
                f"{true_p:>6.1%}  {mkt_p:>6.1%}  "
                f"{edge:>+6.1%}  {r.ev_per_dollar:>+7.4f}  "
                f"T-{r.hours_until_kickoff:.1f}h"
            )
            lines.append(self._c(line, Color.GREEN))

        lines.append("")
        lines.append(self._c(f"  Total actionable bets: {len(buys)}", Color.GREEN, Color.BOLD))
        return "\n".join(lines)

    def _footer(self, results: list[EVResult]) -> str:
        n_buy  = sum(1 for r in results if r.has_edge)
        n_skip = sum(1 for r in results if not r.has_edge)
        ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S UTC")

        lines = [
            "",
            self._c(self.THICK_SEP, Color.CYAN),
            self._c(
                f"  PIPELINE COMPLETE  ─  {ts}  │  "
                f"BUY Recommendations: {n_buy}  │  "
                f"Ignored: {n_skip}  │  "
                f"Total Evaluated: {len(results)}",
                Color.CYAN,
            ),
            self._c(
                "  ⚠  This system NEVER executes orders. "
                "All trades require manual confirmation by the operator.",
                Color.YELLOW,
            ),
            self._c(self.THICK_SEP, Color.CYAN),
            "",
        ]
        return "\n".join(lines)

    def print_report(
        self,
        results: list[EVResult],
        run_time: datetime,
        n_markets_scanned: int,
        n_events_loaded: int,
    ) -> None:
        """
        Main entry point: print the full report to terminal.
        """
        print(self._header(run_time, n_markets_scanned, n_events_loaded))
        print(self._summary_table(results))
        print("")
        print(self._c("  ─── DETAILED ANALYSIS ───", Color.BOLD, Color.WHITE))
        print("")

        if not results:
            print(self._c("  No results to display.", Color.DIM))
        else:
            for i, result in enumerate(results, 1):
                print(self._format_result_block(result, i))

        print(self._footer(results))

    def save_json_snapshot(
        self, results: list[EVResult], run_time: datetime
    ) -> Optional[Path]:
        """
        Persist results as a JSON snapshot file for audit trail,
        backtesting, and performance tracking.
        Like a black-box flight recorder — always running silently.
        """
        if not self.save_json:
            return None

        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        fname = run_time.strftime("snapshot_%Y%m%d_%H%M%S.json")
        fpath = self.snapshot_dir / fname

        payload = {
            "run_time": run_time.isoformat(),
            "total_results": len(results),
            "buy_recommendations": sum(1 for r in results if r.has_edge),
            "results": [
                {
                    "question": r.polymarket_question,
                    "market_id": r.polymarket_market_id,
                    "slug": r.polymarket_slug,
                    "sport_event": r.sport_event_label,
                    "sport_key": r.sport_key,
                    "hours_until_kickoff": r.hours_until_kickoff,
                    "true_prob_yes": r.true_prob_yes,
                    "true_prob_no": r.true_prob_no,
                    "implied_prob_yes": r.implied_prob_yes,
                    "implied_prob_no": r.implied_prob_no,
                    "edge_yes": r.edge_yes,
                    "edge_no": r.edge_no,
                    "ev_per_dollar": r.ev_per_dollar,
                    "recommendation": r.recommendation.value,
                    "recommended_token_id": r.recommended_token_id,
                    "volume_usd": r.polymarket_volume_usd,
                    "liquidity_usd": r.polymarket_liquidity_usd,
                    "match_confidence": r.match_confidence,
                    "polymarket_url": f"https://polymarket.com/event/{r.polymarket_slug}",
                    "computed_at": r.computed_at.isoformat(),
                }
                for r in results
            ],
        }

        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        logger.info("JSON snapshot saved: %s", fpath)
        return fpath