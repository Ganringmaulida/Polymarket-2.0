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
            proc = None
            stdout = ""  
            try:
                # KOMPUTASI: Menggunakan Popen untuk kontrol proses level sistem operasi
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",       # Memaksa UTF-8 mencegah crash di Windows
                    errors="replace",       
                )
                stdout, stderr = proc.communicate(timeout=self.timeout)

                if proc.returncode != 0:
                    err = stdout or stderr
                    logger.warning("CLI error (attempt %d): %s", attempt + 1, err.strip())
                    if attempt < retries:
                        time.sleep(1.5 ** attempt)
                    continue

                if not stdout.strip():
                    return None

                return json.loads(stdout)

            except subprocess.TimeoutExpired:
                # KOMPUTASI: Pembersihan eksplisit (Kill & Drain) untuk mencegah Zombie Process
                if proc:
                    proc.kill()
                    proc.communicate()  
                logger.warning("CLI timeout (attempt %d): %s", attempt + 1, " ".join(cmd))
                if attempt < retries:
                    time.sleep(2)
            except json.JSONDecodeError as e:
                logger.warning("JSON parse error: %s — raw: %s", e, stdout[:200])
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
            # FIX A2: key "midpoint" sesuai output Rust CLI
            # (src/output/clob.rs:100 → json!({"midpoint": result.mid.to_string()}))
            return float(data.get("midpoint", 0))
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
        Search Polymarket for active sports markets using pagination (--offset)
        to bypass the 500 API hard-cap and reach daily H2H matches.
        """
        sports_markets = []
        limit_per_request = 500
        total_fetched = 0

        while total_fetched < self.max_markets:
            current_limit = min(limit_per_request, self.max_markets - total_fetched)
            
            args = [
                "markets", "list",
                "--active", "true",
                "--limit", str(current_limit),
                "--offset", str(total_fetched)
            ]
            
            logger.debug("Fetching markets with offset %d", total_fetched)
            data = self._run(args)
            
            if not data or not isinstance(data, list) or len(data) == 0:
                break  

            for m in data:
                if not isinstance(m, dict):
                    continue
                
                # KOMPUTASI 1: Ekstraksi Tags menjadi Set untuk O(1) Lookup
                raw_tags = m.get("tags", [])
                tags_set = set()
                if isinstance(raw_tags, list):
                    for t in raw_tags:
                        if isinstance(t, dict):
                            tags_set.add(t.get("label", "").lower())
                        elif isinstance(t, str):
                            tags_set.add(t.lower())

                # KOMPUTASI 2: Short-Circuit Rejection (Membuang pasar Non-Sports secara instan)
                # Jauh lebih hemat CPU karena tidak perlu membaca string pertanyaan sama sekali
                blacklisted_tags = {"politics", "crypto", "pop culture", "business", "science", "mentions"}
                if tags_set.intersection(blacklisted_tags):
                    continue

                question = m.get("question", "").lower()
                
                # KOMPUTASI 3: Negative Text Filter (Membuang pasar Sports Jangka Panjang)
                futures_keywords = [
                    "win the 202", "win the 203", "finals", "championship", 
                    "stanley cup", "world cup", "mvp", "rookie", "award", 
                    "finish in", "draft", "relegated"
                ]
                if any(f_kw in question for f_kw in futures_keywords):
                    continue 
                
                # KOMPUTASI 4: Strict Positive Filter & H2H Validation
                strict_leagues = [
                    "nba", "nfl", "nhl", "mlb", "epl", "premier league", 
                    "champions league", "la liga", "bundesliga"
                ]
                
                has_valid_league = any(lg in question for lg in strict_leagues) or (self.sports_tag in tags_set)
                
                # Spasi di sekitar "vs" sangat penting untuk mencegah false positive pada kata yang mengandung "vs"
                is_daily_h2h = any(kw in question for kw in [" vs ", " vs. ", " beat "])
                
                if (has_valid_league or is_daily_h2h) and not tags_set.intersection(blacklisted_tags):
                    sports_markets.append(m)

            total_fetched += len(data)
            
            if len(data) < current_limit:
                break

        logger.info("Found %d short-term sports markets out of %d total active markets scanned",
                    len(sports_markets), total_fetched)
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

            # KOMPUTASI 1: Type Guard & Fallback untuk clobTokenIds
            # Menangani anomali di mana API mengembalikan Stringified JSON
            clob_token_ids = market_raw.get("clobTokenIds", [])
            if isinstance(clob_token_ids, str):
                try: clob_token_ids = json.loads(clob_token_ids)
                except json.JSONDecodeError: clob_token_ids = []

            # Fallback 2: Jika properti clobTokenIds tidak ada, cari di dalam list of objects 'tokens'
            if not clob_token_ids:
                tokens_data = market_raw.get("tokens", [])
                if isinstance(tokens_data, list):
                    clob_token_ids = [str(t.get("token_id")) for t in tokens_data if isinstance(t, dict) and t.get("token_id")]

            # KOMPUTASI 2: Type Guard & Fallback untuk outcomes
            outcomes = market_raw.get("outcomes", ["Yes", "No"])
            if isinstance(outcomes, str):
                try: outcomes = json.loads(outcomes)
                except json.JSONDecodeError: outcomes = ["Yes", "No"]

            if not clob_token_ids:
                logger.debug("No clobTokenIds for market: %s", question)
                return None

            midpoints = self.get_batch_midpoints(clob_token_ids)

            # KOMPUTASI 3: Type Guard & Fallback untuk outcomePrices
            outcome_prices = market_raw.get("outcomePrices", [])
            if isinstance(outcome_prices, str):
                try: outcome_prices = json.loads(outcome_prices)
                except json.JSONDecodeError: outcome_prices = []

            tokens: list[PolymarketToken] = []
            for i, token_id in enumerate(clob_token_ids):
                outcome_label = outcomes[i] if i < len(outcomes) else f"Outcome {i}"
                price = midpoints.get(token_id)
                
                # Jika harga live gagal ditarik, gunakan harga bawaan dari payload list
                if price is None:
                    if i < len(outcome_prices):
                        try: price = float(outcome_prices[i])
                        except (TypeError, ValueError): price = 0.0
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
            logger.warning("Failed to build PolymarketMarket: %s — %s", e, market_raw.get("question", "?"))
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