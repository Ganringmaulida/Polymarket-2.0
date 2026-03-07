"""
core/ev_engine.py
==================
Expected Value Calculation Engine — the analytical core of the
entire pipeline.

Conceptual model: Think of a poker player who folds 70% of hands
but wins disproportionately when they play. This engine is the
"hand evaluator" — it tells you when the cards (odds) in your hand
are genuinely better than what the table (market) believes.

EV Formula:
    EV = (True_Prob × Profit_If_Win) - ((1 - True_Prob) × Stake)
    On Polymarket (binary, $1 contracts):
    EV_per_dollar = True_Prob - Implied_Prob

Edge = True_Prob - Implied_Prob
Recommendation: BUY if Edge > Margin_of_Safety
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from enum import Enum
from typing import Optional

from fetchers.odds_fetcher import SportEvent
from fetchers.polymarket_fetcher import PolymarketMarket

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────
# Data Contracts
# ──────────────────────────────────────────────────

class Recommendation(str, Enum):
    BUY_YES  = "BUY YES  ✅"
    BUY_NO   = "BUY NO   ✅"
    IGNORE   = "IGNORE   ⏭"
    NO_EDGE  = "NO EDGE  ➖"


@dataclass
class EVResult:
    """
    The complete analytical output for one matched market pairing.
    This is the final product delivered to the operator.
    """
    # Identifiers
    polymarket_question: str
    polymarket_market_id: str
    polymarket_slug: str
    sport_event_label: str
    sport_key: str
    hours_until_kickoff: float

    # Probabilities
    true_prob_yes: float      # De-vigged bookmaker consensus (home win or "Yes" side)
    true_prob_no: float       # De-vigged bookmaker consensus (away win or "No" side)
    implied_prob_yes: float   # Polymarket midpoint for Yes token
    implied_prob_no: float    # Polymarket midpoint for No token

    # Edge metrics
    edge_yes: float           # true_prob_yes - implied_prob_yes
    edge_no: float            # true_prob_no  - implied_prob_no

    # Output
    recommendation: Recommendation
    recommended_token_id: Optional[str]
    ev_per_dollar: float      # Expected profit per $1 deployed

    # Context
    polymarket_volume_usd: float
    polymarket_liquidity_usd: float
    match_confidence: float    # 0–1, how confident the fuzzy matcher is
    computed_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    @property
    def has_edge(self) -> bool:
        return self.recommendation in (Recommendation.BUY_YES, Recommendation.BUY_NO)

    def to_log_line(self) -> str:
        """Compact single-line summary for terminal log scanning."""
        edge_str = f"EDGE {max(self.edge_yes, self.edge_no)*100:+.1f}%"
        return (
            f"[{self.recommendation.value}] {self.sport_event_label:<40} | "
            f"True: Y={self.true_prob_yes:.1%} N={self.true_prob_no:.1%} | "
            f"Mkt: Y={self.implied_prob_yes:.1%} N={self.implied_prob_no:.1%} | "
            f"{edge_str} | EV=${self.ev_per_dollar:+.3f}/$ | "
            f"Vol=${self.polymarket_volume_usd:,.0f} | "
            f"T-{self.hours_until_kickoff:.1f}h"
        )


# ──────────────────────────────────────────────────
# Matching Engine
# ──────────────────────────────────────────────────

class MarketMatcher:
    """
    Fuzzy-matches Polymarket questions to Odds-API sport events.

    The challenge: Polymarket asks "Will Arsenal beat Chelsea?"
    while Odds-API lists "Chelsea @ Arsenal". They describe the
    same event but with different vocabulary — like two people
    giving directions to the same building.

    Strategy:
    1. Extract team names from Polymarket question using regex patterns.
    2. Compare extracted names to odds event home/away teams.
    3. Score using string similarity (SequenceMatcher).
    4. Accept match only above configured confidence threshold.
    """

    # Common patterns in Polymarket sports questions
    QUESTION_PATTERNS = [
        r"will (.+?) (?:beat|defeat|win against|win over|vs\.?) (.+?)\??$",
        r"(.+?) (?:vs|v\.?|versus) (.+?)\??$",
        r"will (.+?) win.*?(?:against|vs\.?) (.+?)\??$",
        r"(.+?) to win.*?(?:vs|against|over) (.+?)\??$",
    ]

    def __init__(self, config: dict):
        self.min_similarity = config["matching"]["team_name_similarity_threshold"]
        self.max_kickoff_offset_hours = config["matching"]["max_kickoff_offset_hours"]

    def _normalize(self, s: str) -> str:
        """Lowercase, strip punctuation, collapse whitespace."""
        s = s.lower().strip()
        s = re.sub(r"[^\w\s]", "", s)
        s = re.sub(r"\s+", " ", s)
        return s

    def _similarity(self, a: str, b: str) -> float:
        """Token-level Jaccard-augmented SequenceMatcher similarity."""
        a_norm, b_norm = self._normalize(a), self._normalize(b)
        seq_score = SequenceMatcher(None, a_norm, b_norm).ratio()

        # Bonus: check if all tokens of shorter string appear in longer
        a_tokens = set(a_norm.split())
        b_tokens = set(b_norm.split())
        shorter = a_tokens if len(a_tokens) <= len(b_tokens) else b_tokens
        longer  = b_tokens if len(a_tokens) <= len(b_tokens) else a_tokens
        overlap = len(shorter & longer) / len(shorter) if shorter else 0.0

        # Weighted blend: sequence score + token overlap
        return 0.6 * seq_score + 0.4 * overlap

    def _extract_teams_from_question(self, question: str) -> Optional[tuple[str, str]]:
        """
        Attempt to extract two team names from a Polymarket question.
        Returns (team_a, team_b) or None if no pattern matches.
        """
        for pattern in self.QUESTION_PATTERNS:
            m = re.search(pattern, question, re.IGNORECASE)
            if m:
                return m.group(1).strip(), m.group(2).strip()
        return None

    def _score_match(
        self,
        question: str,
        event: SportEvent,
    ) -> float:
        """
        Compute a match confidence score between a Polymarket question
        and a SportEvent. Returns 0.0–1.0.
        """
        teams = self._extract_teams_from_question(question)
        if not teams:
            # Fallback: check if both team names appear anywhere in question
            q_norm = self._normalize(question)
            home_in_q = self._similarity(event.home_team, question)
            away_in_q = self._similarity(event.away_team, question)
            return (home_in_q + away_in_q) / 2

        team_a, team_b = teams

        # Best-case: (team_a ≈ home AND team_b ≈ away) OR vice versa
        score_direct  = (
            self._similarity(team_a, event.home_team) * 0.5 +
            self._similarity(team_b, event.away_team) * 0.5
        )
        score_flipped = (
            self._similarity(team_a, event.away_team) * 0.5 +
            self._similarity(team_b, event.home_team) * 0.5
        )
        return max(score_direct, score_flipped)

    def find_best_match(
        self,
        polymarket: PolymarketMarket,
        sport_events: list[SportEvent],
    ) -> Optional[tuple[SportEvent, float]]:
        """
        Find the best-matching SportEvent for a given PolymarketMarket.
        Returns (event, confidence_score) or None if no acceptable match.
        """
        now = datetime.now(tz=timezone.utc)
        best_event: Optional[SportEvent] = None
        best_score = 0.0

        for event in sport_events:
            # Time filter: only consider events within kickoff window
            hours = event.hours_until_kickoff
            if hours < -2 or hours > 168:  # between 2h ago and 1 week ahead
                continue

            score = self._score_match(polymarket.question, event)
            if score > best_score:
                best_score = score
                best_event = event

        if best_score >= self.min_similarity and best_event is not None:
            logger.debug(
                "Matched '%s' → '%s' (score=%.2f)",
                polymarket.question, best_event.match_label, best_score,
            )
            return best_event, best_score

        logger.debug(
            "No match for '%s' (best_score=%.2f < threshold=%.2f)",
            polymarket.question, best_score, self.min_similarity,
        )
        return None


# ──────────────────────────────────────────────────
# EV Calculator
# ──────────────────────────────────────────────────

class EVCalculator:
    """
    Computes Expected Value for each matched market pair and
    issues a binary BUY / IGNORE recommendation.

    The core logic is intentionally simple and transparent —
    like a clear rulebook vs. a black-box algorithm.
    No hidden adjustments, no "feelings". Pure math.
    """

    def __init__(self, config: dict):
        self.margin_of_safety = config["ev"]["margin_of_safety"]
        self.min_implied_prob  = config["ev"]["min_implied_prob"]
        self.max_implied_prob  = config["ev"]["max_implied_prob"]
        self.min_volume        = config["ev"]["min_polymarket_volume_usd"]
        self.matcher           = MarketMatcher(config)

    def _is_market_tradeable(self, market: PolymarketMarket) -> tuple[bool, str]:
        """
        Pre-flight checks before performing EV calculation.
        Returns (is_tradeable, reason_if_not).
        """
        if market.volume_usd < self.min_volume:
            return False, f"volume ${market.volume_usd:,.0f} < min ${self.min_volume:,.0f}"

        yes = market.yes_token
        no  = market.no_token

        if not yes or not no:
            return False, "missing Yes/No token pair"

        if not (self.min_implied_prob <= yes.implied_prob <= self.max_implied_prob):
            return False, f"Yes prob {yes.implied_prob:.2%} out of tradeable range"

        if not (self.min_implied_prob <= no.implied_prob <= self.max_implied_prob):
            return False, f"No prob {no.implied_prob:.2%} out of tradeable range"

        return True, ""

    def _determine_recommendation(
        self,
        edge_yes: float,
        edge_no: float,
        yes_token_id: Optional[str],
        no_token_id: Optional[str],
    ) -> tuple[Recommendation, Optional[str], float]:
        """
        Apply the edge filter and return final recommendation.
        Returns (recommendation, token_id_to_buy, ev_per_dollar).
        """
        if edge_yes > edge_no and edge_yes > self.margin_of_safety:
            return Recommendation.BUY_YES, yes_token_id, edge_yes
        elif edge_no > edge_yes and edge_no > self.margin_of_safety:
            return Recommendation.BUY_NO, no_token_id, edge_no
        elif max(edge_yes, edge_no) > 0:
            return Recommendation.NO_EDGE, None, max(edge_yes, edge_no)
        else:
            return Recommendation.IGNORE, None, min(edge_yes, edge_no)

    def evaluate(
        self,
        market: PolymarketMarket,
        sport_events: list[SportEvent],
    ) -> Optional[EVResult]:
        """
        Core evaluation function. Given a Polymarket market and the
        full list of sport events, attempt to match and compute EV.
        Returns EVResult or None if market cannot be evaluated.
        """
        tradeable, reason = self._is_market_tradeable(market)
        if not tradeable:
            logger.debug("Skipping '%s': %s", market.question, reason)
            return None

        match = self.matcher.find_best_match(market, sport_events)
        if not match:
            return None

        event, confidence = match
        yes_token = market.yes_token
        no_token  = market.no_token

        # On Polymarket: "Yes" = the stated outcome occurring.
        # We map this to the team/outcome the question is about.
        # By convention: Polymarket sports questions are typically
        # phrased as "Will [Team A] win?" where Yes = Team A wins.
        # We identify which team is Team A via the matcher extraction.
        extracted = self.matcher._extract_teams_from_question(market.question)

        if extracted:
            # team_a is the "Yes" outcome candidate
            team_a = extracted[0]
            # Check which event team best matches team_a
            home_sim = self.matcher._similarity(team_a, event.home_team)
            away_sim = self.matcher._similarity(team_a, event.away_team)

            if home_sim >= away_sim:
                true_prob_yes = event.true_prob_home or 0.0
                true_prob_no  = event.true_prob_away or 0.0
            else:
                true_prob_yes = event.true_prob_away or 0.0
                true_prob_no  = event.true_prob_home or 0.0
        else:
            # Default: assume question is about home team winning
            true_prob_yes = event.true_prob_home or 0.0
            true_prob_no  = event.true_prob_away or 0.0

        implied_yes = yes_token.implied_prob if yes_token else 0.5
        implied_no  = no_token.implied_prob  if no_token  else 0.5

        # EV per dollar = True Prob - Implied Prob
        # (On Polymarket, buying Yes at 0.40 and winning returns $1,
        #  so profit = 1 - 0.40 = 0.60. Expected profit = TP * 0.60 - (1-TP)*0.40
        #  which simplifies to TP - 0.40 = edge.)
        edge_yes = true_prob_yes - implied_yes
        edge_no  = true_prob_no  - implied_no

        recommendation, token_id, ev_per_dollar = self._determine_recommendation(
            edge_yes, edge_no,
            yes_token.token_id if yes_token else None,
            no_token.token_id  if no_token  else None,
        )

        return EVResult(
            polymarket_question=market.question,
            polymarket_market_id=market.market_id,
            polymarket_slug=market.slug,
            sport_event_label=event.match_label,
            sport_key=event.sport_key,
            hours_until_kickoff=event.hours_until_kickoff,
            true_prob_yes=true_prob_yes,
            true_prob_no=true_prob_no,
            implied_prob_yes=implied_yes,
            implied_prob_no=implied_no,
            edge_yes=edge_yes,
            edge_no=edge_no,
            recommendation=recommendation,
            recommended_token_id=token_id,
            ev_per_dollar=ev_per_dollar,
            polymarket_volume_usd=market.volume_usd,
            polymarket_liquidity_usd=market.liquidity_usd,
            match_confidence=confidence,
        )

    def evaluate_all(
        self,
        markets: list[PolymarketMarket],
        sport_events: list[SportEvent],
    ) -> list[EVResult]:
        """
        Evaluate all markets. Returns sorted results:
        positive-edge markets first, ordered by EV descending.
        """
        results: list[EVResult] = []
        skipped = 0

        for market in markets:
            result = self.evaluate(market, sport_events)
            if result:
                results.append(result)
            else:
                skipped += 1

        logger.info(
            "EV evaluation: %d results computed, %d markets skipped",
            len(results), skipped,
        )

        # Sort: BUY recommendations first, then by EV descending
        def sort_key(r: EVResult) -> tuple:
            priority = 0 if r.has_edge else 1
            return (priority, -r.ev_per_dollar)

        return sorted(results, key=sort_key)