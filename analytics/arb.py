"""Cross-venue arbitrage detector.

Model: each paired market is a binary outcome that resolves YES or NO on both
Polymarket and Kalshi. Cross-venue, riskless arb is "buy YES on cheaper venue
+ buy NO on more expensive venue" — one side wins, payout = $1 per contract.

For a pair with:
    poly_yes_ask  = best price to BUY YES on Polymarket   (in [0, 1])
    poly_no_ask   = best price to BUY NO  on Polymarket
    kalshi_yes_ask = best price to BUY YES on Kalshi
    kalshi_no_ask  = best price to BUY NO  on Kalshi

Direction A — long YES on Poly, long NO on Kalshi:
    cost   = poly_yes_ask + kalshi_no_ask
    gross  = 1 - cost
Direction B — long YES on Kalshi, long NO on Poly:
    cost   = kalshi_yes_ask + poly_no_ask
    gross  = 1 - cost

Fees:
  Polymarket: 0% protocol, but ~0.5% effective for USDC/gas/slippage (configurable)
  Kalshi    : taker fee = 0.07 * n * p * (1-p)  per contract  (their published formula)
             We compute it per leg using that leg's fill price.

Net edge = gross_edge - poly_fee_yes_side - poly_fee_no_side - kalshi_fee_per_leg

We log every direction where net_edge >= MIN_LOG_EDGE so you can review the
false-positive rate even on tiny edges.
"""
from __future__ import annotations

import asyncio
import logging
import os as _os
import time
from dataclasses import dataclass

from store.sqlite import Store

POLY_TAKER_FEE_BPS = 50  # 0.50% on notional per leg (rough, configurable)
KALSHI_FEE_RATE = 0.07   # Kalshi published taker fee coefficient

MIN_LOG_EDGE = -0.05     # log anything with net edge >= -5 cents per $1 (so we see near-misses)
MIN_PRINT_EDGE = 0.005   # only print to stdout when net edge >= 0.5¢ per $1
MAX_QUOTE_AGE_S = 30     # skip pair if either side's latest quote is older than this
EDGE_CHANGE_EPSILON = 1e-4  # only persist a new row when net edge moves by >1bp

# Depth-walked fill behavior. When enabled, evaluate() walks the supplied
# depth dicts on each leg up to FILL_SIZE_CONTRACTS contracts and uses the
# resulting VWAP as the fill price (instead of top-of-book ask). When
# depth dicts are absent or the gate is off, falls back to top-of-book.
FILL_DEPTH_ENABLED = _os.environ.get("FILL_DEPTH_ENABLED", "0") not in ("0", "false", "False", "")
FILL_SIZE_CONTRACTS = float(_os.environ.get("FILL_SIZE_CONTRACTS", "100"))

log = logging.getLogger("arb")


def kalshi_fee(price: float) -> float:
    """Per-contract Kalshi taker fee, expressed as fraction of $1 contract."""
    return KALSHI_FEE_RATE * price * (1.0 - price)


def poly_fee(price: float) -> float:
    """Per-contract Polymarket effective taker fee, as fraction of $1."""
    # Fee is on notional ≈ price * size; per-$1-of-payout it's just bps * price
    return (POLY_TAKER_FEE_BPS / 10_000.0) * price


@dataclass
class PairQuote:
    pair_name: str
    poly_yes_bid: float | None = None
    poly_yes_ask: float | None = None
    poly_yes_ts_ns: int | None = None
    poly_no_bid: float | None = None
    poly_no_ask: float | None = None
    poly_no_ts_ns: int | None = None
    kalshi_yes_bid: float | None = None
    kalshi_yes_ask: float | None = None
    kalshi_ts_ns: int | None = None
    kalshi_ticker: str | None = None

    # Depth dicts: {price: size}. None means caller didn't supply L2 (use
    # top-of-book fallback). Polymarket exposes bids+asks directly per asset.
    # Kalshi books store BID-side liquidity on both yes and no — the YES ask
    # depth is reconstructed from the NO bid depth (price flipped to 1 - p).
    poly_yes_bids: dict[float, float] | None = None
    poly_yes_asks: dict[float, float] | None = None
    poly_no_bids: dict[float, float] | None = None
    poly_no_asks: dict[float, float] | None = None
    kalshi_yes_bids: dict[float, float] | None = None
    kalshi_no_bids: dict[float, float] | None = None

    def is_stale(self, now_ns: int, max_age_s: float = MAX_QUOTE_AGE_S) -> dict[str, bool]:
        cutoff = now_ns - int(max_age_s * 1e9)
        return {
            "poly_yes": self.poly_yes_ts_ns is None or self.poly_yes_ts_ns < cutoff,
            "poly_no":  self.poly_no_ts_ns  is None or self.poly_no_ts_ns  < cutoff,
            "kalshi":   self.kalshi_ts_ns   is None or self.kalshi_ts_ns   < cutoff,
        }

    @property
    def kalshi_no_bid(self) -> float | None:
        if self.kalshi_yes_ask is None:
            return None
        return 1.0 - self.kalshi_yes_ask

    @property
    def kalshi_no_ask(self) -> float | None:
        if self.kalshi_yes_bid is None:
            return None
        return 1.0 - self.kalshi_yes_bid

    @property
    def kalshi_yes_asks(self) -> dict[float, float] | None:
        """Reconstructed YES-ask depth: each NO bid at price p becomes
        a YES ask at price (1 - p) with the same size."""
        if self.kalshi_no_bids is None:
            return None
        return {1.0 - p: s for p, s in self.kalshi_no_bids.items()}

    @property
    def kalshi_no_asks(self) -> dict[float, float] | None:
        if self.kalshi_yes_bids is None:
            return None
        return {1.0 - p: s for p, s in self.kalshi_yes_bids.items()}


def evaluate(pq: PairQuote, now_ns: int | None = None) -> list[dict]:
    """Return list of candidate trades with edge breakdown.

    Skips any direction whose required quote leg is stale (older than
    MAX_QUOTE_AGE_S). When FILL_DEPTH_ENABLED and depth dicts are present
    on pq, fills are computed by walking up to FILL_SIZE_CONTRACTS contracts
    of the relevant side and using the VWAP as the leg price. Otherwise
    falls back to top-of-book ask.

    Each candidate dict now also contains:
      fill_vwap_poly, fill_vwap_kalshi : float — actual per-contract fill price
      fill_qty_poly, fill_qty_kalshi   : float — contracts that would be filled
      levels_consumed_poly, levels_consumed_kalshi : int — book levels walked
                                                          (0 = top-of-book fallback)
      partial_fill : bool — True if either leg's filled qty < requested
    """
    from analytics.depth import walk_levels

    import time as _time
    if now_ns is None:
        now_ns = _time.time_ns()

    # Re-read env per-call so tests can monkeypatch.setenv() between invocations.
    gate_on = _os.environ.get("FILL_DEPTH_ENABLED", "0") not in ("0", "false", "False", "")
    fill_size = float(_os.environ.get("FILL_SIZE_CONTRACTS", "100"))

    stale = pq.is_stale(now_ns)
    results: list[dict] = []

    def _resolve_leg(top_of_book: float | None, depth: dict | None, side: str):
        """Return (price_used, fill_qty, levels_consumed) for one leg."""
        if gate_on and depth:
            vwap, filled, levels_used = walk_levels(depth, fill_size, side)
            if vwap is not None:
                return (vwap, filled, levels_used)
        if top_of_book is None:
            return (None, 0.0, 0)
        return (top_of_book, fill_size, 0)

    # Direction A: buy YES on Poly, buy NO on Kalshi
    if (
        pq.poly_yes_ask is not None
        and pq.kalshi_no_ask is not None
        and not stale["poly_yes"]
        and not stale["kalshi"]
    ):
        poly_vwap, poly_qty, poly_levels = _resolve_leg(pq.poly_yes_ask, pq.poly_yes_asks, "ask")
        kalshi_vwap, kalshi_qty, kalshi_levels = _resolve_leg(pq.kalshi_no_ask, pq.kalshi_no_asks, "ask")
        if poly_vwap is not None and kalshi_vwap is not None:
            cost = poly_vwap + kalshi_vwap
            gross = 1.0 - cost
            fees = poly_fee(poly_vwap) + kalshi_fee(kalshi_vwap)
            net = gross - fees
            partial = (poly_qty < fill_size) or (kalshi_qty < fill_size)
            results.append({
                "direction": "poly_yes_kalshi_no",
                "gross_edge": gross,
                "net_edge": net,
                "cost": cost,
                "poly_leg_price": poly_vwap,
                "kalshi_leg_price": kalshi_vwap,
                "fees": fees,
                "fill_vwap_poly": poly_vwap,
                "fill_vwap_kalshi": kalshi_vwap,
                "fill_qty_poly": poly_qty,
                "fill_qty_kalshi": kalshi_qty,
                "levels_consumed_poly": poly_levels,
                "levels_consumed_kalshi": kalshi_levels,
                "partial_fill": partial,
            })

    # Direction B: buy YES on Kalshi, buy NO on Poly
    if (
        pq.kalshi_yes_ask is not None
        and pq.poly_no_ask is not None
        and not stale["poly_no"]
        and not stale["kalshi"]
    ):
        poly_vwap, poly_qty, poly_levels = _resolve_leg(pq.poly_no_ask, pq.poly_no_asks, "ask")
        kalshi_vwap, kalshi_qty, kalshi_levels = _resolve_leg(pq.kalshi_yes_ask, pq.kalshi_yes_asks, "ask")
        if poly_vwap is not None and kalshi_vwap is not None:
            cost = poly_vwap + kalshi_vwap
            gross = 1.0 - cost
            fees = poly_fee(poly_vwap) + kalshi_fee(kalshi_vwap)
            net = gross - fees
            partial = (poly_qty < fill_size) or (kalshi_qty < fill_size)
            results.append({
                "direction": "kalshi_yes_poly_no",
                "gross_edge": gross,
                "net_edge": net,
                "cost": cost,
                "poly_leg_price": poly_vwap,
                "kalshi_leg_price": kalshi_vwap,
                "fees": fees,
                "fill_vwap_poly": poly_vwap,
                "fill_vwap_kalshi": kalshi_vwap,
                "fill_qty_poly": poly_qty,
                "fill_qty_kalshi": kalshi_qty,
                "levels_consumed_poly": poly_levels,
                "levels_consumed_kalshi": kalshi_levels,
                "partial_fill": partial,
            })

    return results


async def run(store: Store, build_pair_quotes, paper=None, interval: float = 2.0) -> None:
    """Periodically read latest quotes and evaluate arb candidates.

    `build_pair_quotes` is a callable returning list[PairQuote] from current
    store state — passed in to keep this module pure.

    `paper` is an optional analytics.paper.PaperTrader. When supplied, qualifying
    candidates auto-open paper positions and all open positions are marked-to-market
    on each tick.
    """
    log.info("arb detector running every %.1fs", interval)
    last_logged: dict[tuple[str, str], float] = {}  # (pair, direction) -> last persisted net_edge
    while True:
        loop_start = time.monotonic()
        try:
            now_ns = time.time_ns()
            pair_quotes = build_pair_quotes()
            if paper is not None:
                paper.mark_all(pair_quotes)
                paper.maybe_exit(pair_quotes)
            for pq in pair_quotes:
                for cand in evaluate(pq, now_ns=now_ns):
                    if cand["net_edge"] < MIN_LOG_EDGE:
                        continue
                    key = (pq.pair_name, cand["direction"])
                    prev = last_logged.get(key)
                    if prev is None or abs(cand["net_edge"] - prev) >= EDGE_CHANGE_EPSILON:
                        store.record_arb_candidate(pq, cand)
                        last_logged[key] = cand["net_edge"]
                    if paper is not None:
                        paper.maybe_enter(pq, cand)
                    if cand["net_edge"] >= MIN_PRINT_EDGE:
                        log.info(
                            "ARB %s [%s] net=%.4f gross=%.4f cost=%.4f (poly=%.4f kalshi=%.4f fees=%.4f)",
                            pq.pair_name,
                            cand["direction"],
                            cand["net_edge"],
                            cand["gross_edge"],
                            cand["cost"],
                            cand["poly_leg_price"],
                            cand["kalshi_leg_price"],
                            cand["fees"],
                        )
        except Exception as e:
            log.exception("arb eval error: %s", e)

        elapsed = time.monotonic() - loop_start
        await asyncio.sleep(max(0.0, interval - elapsed))
