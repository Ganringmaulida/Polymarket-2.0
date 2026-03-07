"""
fetchers/polymarket_fetcher.py
===============================
Polymarket CLI Wrapper — the "robotic arm" that reaches into
Polymarket's CLOB and retrieves raw price data via subprocess.

Architecture note: We treat the Polymarket Rust binary like a
local microservice. Each call is stateless, returns JSON, and
is wrapped with timeout + retry logic for production resilience.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class PolymarketToken:
    """Represents one outcome token (e.g. 'Yes' or 'No') on Polymarket."""
    token_id: str
    outcome: str           # "Yes" or "No"
    price: float           # Current midpoint price (0.0–1.0)
    implied_prob: float    # = price (they are identical on CLOB)


@dataclass
class PolymarketMarket:
    """A single binary market on Polymarket with both outcome tokens."""
    market_id: str
    condition_id: str
    question: str
    slug: str
    volume_usd: float
    liquidity_usd: float
    tokens: list[PolymarketToken] = field(default_factory=list)

    @property
    def yes_token(self) -> Optional[PolymarketToken]:
        return next((t for t in self.tokens if t.outcome.lower() == "yes"), None)

    @property
    def no_token(self) -> Optional[PolymarketToken]:
        return next((t for t in self.tokens if t.outcome.lower() == "no"), None)


class PolymarketFetcher:
    """
    Wraps the Polymarket CLI binary via subprocess calls.
    All output is requested in JSON format (`-o json`) for
    deterministic parsing — just as a surgeon prefers a clean
    instrument over a rusty one.
    """

    def __init__(self, config: dict):
        self.binary = config["polymarket"]["cli_binary"]
        self.timeout = config["polymarket"]["cli_timeout_seconds"]
        self.max_markets = config["polymarket"]["max_markets_to_scan"]
        self.sports_tag = config["polymarket"]["sports_tag"]

    def _run(self, args: list[str], retries: int = 2) -> Optional[dict | list]:
        """
        Execute a polymarket CLI command with JSON output.
        Returns parsed JSON or None on failure.
        """
        cmd = [self.binary, "-o", "json"] + args
        logger.debug("Running CLI: %s", " ".join(cmd))

        for attempt in range(retries + 1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
                if result.returncode != 0:
                    err = result.stdout or result.stderr
                    logger.warning("CLI error (attempt %d): %s", attempt + 1, err.strip())
                    if attempt < retries:
                        time.sleep(1.5 ** attempt)
                    continue

                return json.loads(result.stdout)

            except subprocess.TimeoutExpired:
                logger.warning("CLI timeout (attempt %d): %s", attempt + 1, " ".join(cmd))
                if attempt < retries:
                    time.sleep(2)
            except json.JSONDecodeError as e:
                logger.warning("JSON parse error: %s — raw: %s", e, result.stdout[:200])
                return None
            except FileNotFoundError:
                logger.error(
                    "Polymarket binary '%s' not found. "
                    "Install via: cargo install --path . (in polymarket-cli dir)",
                    self.binary,
                )
                return None

        return None

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """
        Fetch the midpoint price for a single token ID.
        """
        data = self._run(["clob", "midpoint", token_id])
        if not data:
            return None
        try:
            return float(data.get("mid", 0))
        except (TypeError, ValueError):
            logger.warning("Unexpected midpoint response: %s", data)
            return None

    def get_batch_midpoints(self, token_ids: list[str]) -> dict[str, float]:
        """
        Fetch midpoints for multiple tokens in one CLI call.
        """
        if not token_ids:
            return {}

        joined = ",".join(token_ids)
        data = self._run(["clob", "midpoints", joined])
        if not data:
            return {}

        result: dict[str, float] = {}

        # DIPERBAIKI: Response adalah dict flat {token_id_str: mid_str}
        if isinstance(data, dict):
            for token_id_str, mid_val in data.items():
                try:
                    result[token_id_str] = float(mid_val)
                except (TypeError, ValueError):
                    logger.warning("Cannot parse midpoint for token %s: %s",
                                   token_id_str, mid_val)
        # FALLBACK: Jika struktur CLI lama terdeteksi (List of Objects)
        elif isinstance(data, list):
            for item in data:
                tid = item.get("token_id") or item.get("tokenId")
                mid = item.get("mid")
                if tid and mid is not None:
                    try:
                        result[str(tid)] = float(mid)
                    except (TypeError, ValueError):
                        pass
        else:
            logger.warning(
                "Unexpected midpoints response type: %s (expected dict or list)",
                type(data).__name__,
            )

        return result

    def search_sports_markets(self, query: str = "") -> list[dict]:
        """
        Search Polymarket for active sports markets.
        """
        # TELAH DIPERBAIKI: Parameter --order dihapus agar diterima oleh API
        args = [
            "markets", "list",
            "--active", "true",
            "--limit", str(self.max_markets),
        ]
        data = self._run(args)
        if not data or not isinstance(data, list):
            return []

        # Filter to sports-tagged markets if possible
        sports_markets = []
        for m in data:
            tags = [t.get("label", "").lower() for t in m.get("tags", [])]
            question = m.get("question", "").lower()
            
            is_sports = (
                self.sports_tag in tags
                or any(kw in question for kw in [
                    "win", "beat", "vs", "match", "game",
                    "nba", "nfl", "nhl", "mlb", "epl", "ufc", "mma",
                    "champion", "league", "cup", "series"
                ])
            )
            if is_sports:
                sports_markets.append(m)

        logger.info("Found %d sports markets out of %d total active markets",
                    len(sports_markets), len(data))
        return sports_markets

    def get_market_with_prices(self, market_raw: dict) -> Optional[PolymarketMarket]:
        """
        Given a raw market dict from the list command, enrich it with
        live midpoint prices for each outcome token.
        """
        try:
            market_id = str(market_raw.get("id", ""))
            condition_id = market_raw.get("conditionId", "")
            question = market_raw.get("question", "Unknown Market")
            slug = market_raw.get("slug", "")
            volume = float(market_raw.get("volumeNum", 0) or 0)
            liquidity = float(market_raw.get("liquidityNum", 0) or 0)

            # Extract token IDs
            clob_token_ids: list[str] = market_raw.get("clobTokenIds", [])
            outcomes: list[str] = market_raw.get("outcomes", ["Yes", "No"])

            if not clob_token_ids:
                logger.debug("No clobTokenIds for market: %s", question)
                return None

            # Batch fetch all token midpoints
            midpoints = self.get_batch_midpoints(clob_token_ids)

            tokens: list[PolymarketToken] = []
            for i, token_id in enumerate(clob_token_ids):
                outcome_label = outcomes[i] if i < len(outcomes) else f"Outcome {i}"
                price = midpoints.get(token_id)
                if price is None:
                    # Fallback: parse from outcomePrices if available
                    outcome_prices = market_raw.get("outcomePrices", [])
                    if i < len(outcome_prices):
                        try:
                            price = float(outcome_prices[i])
                        except (TypeError, ValueError):
                            price = 0.0
                    else:
                        price = 0.0

                tokens.append(PolymarketToken(
                    token_id=token_id,
                    outcome=outcome_label,
                    price=price,
                    implied_prob=price,
                ))

            return PolymarketMarket(
                market_id=market_id,
                condition_id=condition_id,
                question=question,
                slug=slug,
                volume_usd=volume,
                liquidity_usd=liquidity,
                tokens=tokens,
            )

        except Exception as e:
            logger.warning("Failed to build PolymarketMarket: %s — %s", e,
                           market_raw.get("question", "?"))
            return None

    def fetch_all_sports_markets(self) -> list[PolymarketMarket]:
        """
        Full pipeline: search → enrich with live prices.
        """
        raw_markets = self.search_sports_markets()
        if not raw_markets:
            logger.warning("No sports markets found from Polymarket CLI.")
            return []

        enriched: list[PolymarketMarket] = []
        for raw in raw_markets:
            market = self.get_market_with_prices(raw)
            if market:
                enriched.append(market)

        logger.info("Successfully enriched %d / %d markets with live prices",
                    len(enriched), len(raw_markets))
        return enriched