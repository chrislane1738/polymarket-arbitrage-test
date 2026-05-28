"""Main runner: starts all feeds + the arb detector.

By default uses:
  - Polymarket WS (no auth)
  - Kalshi REST poller (no auth)
  - Arb detector (every 2s)

Set KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH to also enable the Kalshi WS
feed (faster than REST polling).

Usage:
    python -m scripts.run_feeds
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load .env from project root before importing modules that read env vars
from dotenv import load_dotenv  # noqa: E402
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from analytics import arb, resolver  # noqa: E402
from analytics.book import KalshiBookState, PolymarketBookState  # noqa: E402
from analytics.paper import PaperTrader  # noqa: E402
from feeds import kalshi_rest, kalshi_ws, polymarket_ws  # noqa: E402
from match.markets import MarketPair, load_pairs  # noqa: E402
from store.sqlite import Store  # noqa: E402


def build_pair_quote_factory(
    pairs: list[MarketPair],
    store: Store,
    poly_book: PolymarketBookState,
    kalshi_book: KalshiBookState,
):
    """Returns a function that snapshots current quotes + depth into PairQuote objects."""

    def _factory():
        out = []
        for p in pairs:
            pq = arb.PairQuote(pair_name=p.name, kalshi_ticker=p.kalshi_yes_ticker)
            if p.polymarket_yes_token:
                bid, ask, ts = store.latest_quote("polymarket", p.polymarket_yes_token)
                pq.poly_yes_bid, pq.poly_yes_ask, pq.poly_yes_ts_ns = bid, ask, ts
                pq.poly_yes_bids = poly_book.get_levels(p.polymarket_yes_token, "bids") or None
                pq.poly_yes_asks = poly_book.get_levels(p.polymarket_yes_token, "asks") or None
            if p.polymarket_no_token:
                bid, ask, ts = store.latest_quote("polymarket", p.polymarket_no_token)
                pq.poly_no_bid, pq.poly_no_ask, pq.poly_no_ts_ns = bid, ask, ts
                pq.poly_no_bids = poly_book.get_levels(p.polymarket_no_token, "bids") or None
                pq.poly_no_asks = poly_book.get_levels(p.polymarket_no_token, "asks") or None
            if p.kalshi_yes_ticker:
                bid, ask, ts = store.latest_quote("kalshi", p.kalshi_yes_ticker)
                pq.kalshi_yes_bid, pq.kalshi_yes_ask, pq.kalshi_ts_ns = bid, ask, ts
                pq.kalshi_yes_bids = kalshi_book.get_levels(p.kalshi_yes_ticker, "yes") or None
                pq.kalshi_no_bids = kalshi_book.get_levels(p.kalshi_yes_ticker, "no") or None
            out.append(pq)
        return out

    return _factory


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    log = logging.getLogger("run_feeds")
    pairs = load_pairs()
    poly_tokens: list[str] = []
    kalshi_tickers: list[str] = []
    for p in pairs:
        poly_tokens.extend(p.polymarket_token_ids)
        kalshi_tickers.extend(p.kalshi_tickers)

    log.info(
        "Loaded %d pair(s): %d Polymarket token(s), %d Kalshi ticker(s)",
        len(pairs), len(poly_tokens), len(kalshi_tickers),
    )
    if not pairs:
        log.warning("No pairs configured. Run `python -m scripts.discover <query>` and edit config/markets.yaml.")
        return

    store = Store()
    use_kalshi_ws = bool(os.environ.get("KALSHI_API_KEY_ID")) and bool(
        os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    )

    paper = PaperTrader(store)
    poly_book = PolymarketBookState()
    kalshi_book = KalshiBookState()
    tasks = [
        asyncio.create_task(
            polymarket_ws.run(poly_tokens, store, book_state=poly_book),
            name="polymarket_ws",
        ),
        asyncio.create_task(
            arb.run(store, build_pair_quote_factory(pairs, store, poly_book, kalshi_book), paper=paper),
            name="arb",
        ),
        asyncio.create_task(resolver.run(pairs, store), name="resolver"),
    ]
    if use_kalshi_ws:
        log.info("Kalshi feed: WebSocket (auth detected)")
        tasks.append(asyncio.create_task(
            kalshi_ws.run(kalshi_tickers, store, book_state=kalshi_book),
            name="kalshi_ws",
        ))
    else:
        # Be explicit about why so people debugging KALSHI_API_KEY_ID = empty issues see it
        kid = os.environ.get("KALSHI_API_KEY_ID")
        pth = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        reason = []
        if not kid:
            reason.append("KALSHI_API_KEY_ID is empty or unset")
        if not pth:
            reason.append("KALSHI_PRIVATE_KEY_PATH is empty or unset")
        log.warning("Kalshi feed: REST polling (no WS — %s; depth-walking disabled for kalshi)",
                    ", ".join(reason) or "auth unset")
        tasks.append(asyncio.create_task(kalshi_rest.run(kalshi_tickers, store), name="kalshi_rest"))

    try:
        await asyncio.gather(*tasks)
    finally:
        store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nshutting down")
