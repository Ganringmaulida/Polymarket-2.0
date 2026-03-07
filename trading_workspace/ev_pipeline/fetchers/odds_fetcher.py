"""
fetchers/odds_fetcher.py
=========================
The-Odds-API Integration Layer — the "intelligence desk" that
aggregates sharp-money bookmaker lines and converts them into
a consensus True Probability estimate.

Design principle: We never trust a single bookmaker.
Like a navigator using multiple GPS satellites for a precise fix,
we average across several "sharp" books to triangulate the
market's best estimate of true outcome probability.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class BookmakerOdds:
    """Raw decimal odds from a single bookmaker for one outcome."""
    bookmaker: str
    outcome: str          # e.g. "Arsenal", "Chelsea", "Draw"
    decimal_odds: float   # e.g. 2.10 means $1 returns $2.10 total
    implied_raw: float    # 1 / decimal_odds (includes vig)


@dataclass
class SportEvent:
    """
    A sporting event with aggregated odds from multiple bookmakers.
    This is the "True Probability" data source — the counterpart
    to Polymarket's Implied Probability.
    """
    event_id: str
    sport_key: str
    home_team: str
    away_team: str
    commence_time: datetime
    bookmaker_odds: list[BookmakerOdds] = field(default_factory=list)

    # Computed after vig removal
    true_prob_home: Optional[float] = None
    true_prob_away: Optional[float] = None
    true_prob_draw: Optional[float] = None  # Only for soccer

    @property
    def match_label(self) -> str:
        return f"{self.away_team} @ {self.home_team}"

    @property
    def hours_until_kickoff(self) -> float:
        now = datetime.now(tz=timezone.utc)
        delta = self.commence_time - now
        return delta.total_seconds() / 3600


class OddsFetcher:
    """
    Fetches H2H (moneyline) odds from The-Odds-API and removes
    the bookmaker's vig to derive True Probabilities.

    The vig is like a market maker's spread — it's how books profit
    regardless of outcome. Removing it reveals the pure probability
    estimate embedded in the odds.
    """

    BASE_URL = "https://api.the-odds-api.com/v4"

    def __init__(self, config: dict):
        self.api_key = (
            os.environ.get("ODDS_API_KEY")
            or config["odds_api"]["api_key"]
        )
        self.sports = config["odds_api"]["sports"]
        self.preferred_bookmakers = config["odds_api"]["bookmakers"]
        self.vig_method = config["ev"]["vig_removal_method"]

        if self.api_key == "YOUR_ODDS_API_KEY_HERE":
            logger.warning(
                "No Odds API key configured. Set ODDS_API_KEY env var "
                "or update config.yaml. Odds data will be unavailable."
            )

        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._requests_remaining: Optional[int] = None

    def _get(self, endpoint: str, params: dict) -> Optional[list | dict]:
        """HTTP GET with rate limit awareness and error handling."""
        params["apiKey"] = self.api_key
        url = f"{self.BASE_URL}{endpoint}"

        try:
            resp = self._session.get(url, params=params, timeout=10)

            # Track API quota from response headers
            remaining = resp.headers.get("x-requests-remaining")
            if remaining:
                self._requests_remaining = int(remaining)
                if self._requests_remaining < 10:
                    logger.warning(
                        "Odds API quota nearly exhausted: %d requests remaining",
                        self._requests_remaining,
                    )

            if resp.status_code == 401:
                logger.error("Odds API: Invalid API key.")
                return None
            if resp.status_code == 422:
                logger.warning("Odds API: Unprocessable request — %s", resp.text)
                return None
            if resp.status_code == 429:
                logger.warning("Odds API: Rate limited. Sleeping 60s.")
                time.sleep(60)
                return None

            resp.raise_for_status()
            return resp.json()

        except requests.RequestException as e:
            logger.error("Odds API request failed: %s", e)
            return None

    def _american_to_decimal(self, american: int) -> float:
        """
        Convert American odds to decimal odds.
        Like converting Fahrenheit to Celsius — same information,
        different scale.
        """
        if american > 0:
            return (american / 100) + 1.0
        else:
            return (100 / abs(american)) + 1.0

    def _remove_vig_multiplicative(self, raw_probs: list[float]) -> list[float]:
        """
        Remove vig via multiplicative method: divide each raw implied
        probability by the total overround (sum > 1.0).

        Example: [0.55, 0.52] sums to 1.07 (7% vig).
        After removal: [0.514, 0.486] — sums to 1.0.
        """
        total = sum(raw_probs)
        if total <= 0:
            return raw_probs
        return [p / total for p in raw_probs]

    def _remove_vig_power(self, raw_probs: list[float]) -> list[float]:
        """
        Power method (Shin method approximation) — more accurate
        for extreme probabilities (heavy favorites/underdogs).
        """
        import scipy.optimize as opt  # type: ignore

        def overround_error(k: float) -> float:
            return sum(p ** k for p in raw_probs) - 1.0

        try:
            k = opt.brentq(overround_error, 0.5, 2.0)
            return [p ** k for p in raw_probs]
        except Exception:
            # Fallback to multiplicative if scipy unavailable
            return self._remove_vig_multiplicative(raw_probs)

    def _compute_consensus_probs(
        self, outcomes: list[str], all_bookmaker_lines: list[BookmakerOdds]
    ) -> dict[str, float]:
        """
        Aggregate odds across multiple bookmakers, remove vig,
        and return a consensus True Probability per outcome.

        Process (4 steps):
        1. Group raw implied probs per outcome per book
        2. Remove vig per book
        3. Average de-vigged probs across books
        4. Normalize to sum to 1.0 (sanity check)
        """
        # Step 1: Group by bookmaker, then compute no-vig probs per book
        books_data: dict[str, dict[str, float]] = {}  # {book: {outcome: raw_prob}}
        for line in all_bookmaker_lines:
            if line.bookmaker not in books_data:
                books_data[line.bookmaker] = {}
            books_data[line.bookmaker][line.outcome] = line.implied_raw

        # Step 2 & 3: Remove vig per book, then average
        outcome_devigged_probs: dict[str, list[float]] = {o: [] for o in outcomes}

        for book, outcome_probs in books_data.items():
            probs_in_order = [outcome_probs.get(o, 0.0) for o in outcomes]
            if all(p == 0 for p in probs_in_order):
                continue

            if self.vig_method == "power":
                devigged = self._remove_vig_power(probs_in_order)
            else:
                devigged = self._remove_vig_multiplicative(probs_in_order)

            for i, outcome in enumerate(outcomes):
                outcome_devigged_probs[outcome].append(devigged[i])

        # Step 4: Average + normalize
        consensus: dict[str, float] = {}
        for outcome in outcomes:
            probs = outcome_devigged_probs[outcome]
            consensus[outcome] = sum(probs) / len(probs) if probs else 0.0

        total = sum(consensus.values())
        if total > 0:
            consensus = {k: v / total for k, v in consensus.items()}

        return consensus

    def fetch_events_for_sport(self, sport_key: str) -> list[SportEvent]:
        """
        Fetch all upcoming events + bookmaker odds for one sport.
        """
        bookmakers_str = ",".join(self.preferred_bookmakers)
        data = self._get(
            f"/sports/{sport_key}/odds",
            params={
                "regions": "us,eu",
                "markets": "h2h",
                "oddsFormat": "american",
                "bookmakers": bookmakers_str,
            },
        )

        if not data or not isinstance(data, list):
            logger.info("No odds data returned for sport: %s", sport_key)
            return []

        events: list[SportEvent] = []
        for raw in data:
            try:
                event = self._parse_event(sport_key, raw)
                if event:
                    events.append(event)
            except Exception as e:
                logger.debug("Failed to parse event: %s", e)

        logger.info("Fetched %d events for %s", len(events), sport_key)
        return events

    def _parse_event(self, sport_key: str, raw: dict) -> Optional[SportEvent]:
        """Parse a single event from API response and compute true probs."""
        commence_raw = raw.get("commence_time", "")
        try:
            commence_dt = datetime.fromisoformat(
                commence_raw.replace("Z", "+00:00")
            )
        except ValueError:
            commence_dt = datetime.now(tz=timezone.utc)

        event = SportEvent(
            event_id=raw["id"],
            sport_key=sport_key,
            home_team=raw["home_team"],
            away_team=raw["away_team"],
            commence_time=commence_dt,
        )

        outcomes_set: set[str] = set()
        all_lines: list[BookmakerOdds] = []

        for bookmaker in raw.get("bookmakers", []):
            bk_name = bookmaker["key"]
            for market in bookmaker.get("markets", []):
                if market["key"] != "h2h":
                    continue
                for outcome in market.get("outcomes", []):
                    name = outcome["name"]
                    price = outcome["price"]  # American odds integer

                    decimal = self._american_to_decimal(int(price))
                    raw_implied = 1.0 / decimal if decimal > 0 else 0.0

                    outcomes_set.add(name)
                    all_lines.append(BookmakerOdds(
                        bookmaker=bk_name,
                        outcome=name,
                        decimal_odds=decimal,
                        implied_raw=raw_implied,
                    ))

        if not all_lines:
            return None

        event.bookmaker_odds = all_lines
        outcomes_list = sorted(outcomes_set)  # deterministic ordering

        # Compute consensus true probabilities
        consensus = self._compute_consensus_probs(outcomes_list, all_lines)
        event.true_prob_home = consensus.get(event.home_team, 0.0)
        event.true_prob_away = consensus.get(event.away_team, 0.0)
        event.true_prob_draw = consensus.get("Draw")

        return event

    def fetch_all_events(self) -> list[SportEvent]:
        """
        Fetch events across all configured sports.
        This is the primary entry point for this module.
        """
        all_events: list[SportEvent] = []
        for sport in self.sports:
            events = self.fetch_events_for_sport(sport)
            all_events.extend(events)
            # Polite rate limiting — like waiting your turn in a queue
            time.sleep(0.25)

        logger.info("Total events fetched across all sports: %d", len(all_events))
        return all_events