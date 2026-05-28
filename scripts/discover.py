"""Search for matching markets across Polymarket and Kalshi.

Usage:
    python -m scripts.discover "fed rate"
    python -m scripts.discover trump --limit 20
"""
import argparse
import asyncio
import sys
from pathlib import Path

import httpx

KALSHI_PAGE_DELAY = 0.25  # seconds between paged requests; Kalshi rate-limits aggressively

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

POLY_GAMMA = "https://gamma-api.polymarket.com/markets"
KALSHI_EVENTS = "https://api.elections.kalshi.com/trade-api/v2/events"
KALSHI_MARKETS = "https://api.elections.kalshi.com/trade-api/v2/markets"


async def search_polymarket(client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
    out: list[dict] = []
    q_lower = query.lower()
    # Pull a few pages of top-volume open markets, then filter client-side.
    for offset in (0, 100, 200, 300, 400):
        r = await client.get(
            POLY_GAMMA,
            params={
                "closed": "false",
                "limit": 100,
                "offset": offset,
                "order": "volumeNum",
                "ascending": "false",
            },
        )
        r.raise_for_status()
        rows = r.json() or []
        if not rows:
            break
        for m in rows:
            text = (m.get("question") or "") + " " + (m.get("description") or "")
            if q_lower in text.lower():
                out.append(
                    {
                        "question": m.get("question"),
                        "condition_id": m.get("conditionId"),
                        "clob_token_ids": m.get("clobTokenIds"),
                        "volume": m.get("volumeNum"),
                        "end_date": m.get("endDate"),
                    }
                )
                if len(out) >= limit:
                    return out
    return out


async def search_kalshi(
    client: httpx.AsyncClient, query: str, limit: int, include_all: bool = False
) -> list[dict]:
    """Search Kalshi by event (groups of related markets), then list each
    event's markets.

    Searching /events (vs. flat /markets) is faster, less noisy, and matches
    Kalshi's own taxonomy. Multi-leg parlay series (KXMVE*) are skipped by
    default; pass include_all=True to keep them.
    """
    matching_events: list[dict] = []
    q_lower = query.lower()
    cursor: str | None = None
    pages = 0
    max_pages = 20

    while pages < max_pages and len(matching_events) < limit:
        params: dict[str, str | int] = {"limit": 200, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        r = await client.get(KALSHI_EVENTS, params=params)
        if r.status_code == 429:
            await asyncio.sleep(1.5)
            break
        r.raise_for_status()
        data = r.json() or {}
        for e in data.get("events", []) or []:
            event_ticker = str(e.get("event_ticker", "") or "")
            series = str(e.get("series_ticker", "") or "")
            if not include_all and (event_ticker.startswith("KXMVE") or series.startswith("KXMVE")):
                continue
            text = (
                (e.get("title") or "")
                + " "
                + (e.get("sub_title") or "")
                + " "
                + event_ticker
                + " "
                + series
            )
            if q_lower in text.lower():
                matching_events.append(e)
                if len(matching_events) >= limit:
                    break
        cursor = data.get("cursor")
        if not cursor:
            break
        pages += 1
        await asyncio.sleep(KALSHI_PAGE_DELAY)

    # For each matching event, fetch its markets
    out: list[dict] = []
    for e in matching_events:
        event_ticker = e.get("event_ticker")
        mr = await client.get(KALSHI_MARKETS, params={"event_ticker": event_ticker, "limit": 50})
        if mr.status_code == 429:
            await asyncio.sleep(1.5)
            continue
        mr.raise_for_status()
        markets = (mr.json() or {}).get("markets", []) or []
        out.append(
            {
                "event_ticker": event_ticker,
                "event_title": e.get("title"),
                "event_subtitle": e.get("sub_title"),
                "series_ticker": e.get("series_ticker"),
                "markets": [
                    {
                        "ticker": m.get("ticker"),
                        "yes_sub_title": m.get("yes_sub_title"),
                        "yes_bid": m.get("yes_bid"),
                        "yes_ask": m.get("yes_ask"),
                        "volume": m.get("volume"),
                        "close_time": m.get("close_time"),
                    }
                    for m in markets
                ],
            }
        )
        await asyncio.sleep(KALSHI_PAGE_DELAY)
    return out


def _print_poly(rows: list[dict]) -> None:
    print(f"\n=== Polymarket ({len(rows)}) ===")
    for m in rows:
        print(f"  Q: {m['question']}")
        print(f"     condition_id : {m['condition_id']}")
        print(f"     clob_tokens  : {m['clob_token_ids']}")
        print(f"     volume       : {m['volume']}  ends: {m['end_date']}")
        print()


def _print_kalshi(events: list[dict]) -> None:
    total_markets = sum(len(e["markets"]) for e in events)
    print(f"\n=== Kalshi ({len(events)} event(s), {total_markets} market(s)) ===")
    for e in events:
        print(f"  E: {e['event_title']}")
        if e["event_subtitle"]:
            print(f"     {e['event_subtitle']}")
        print(f"     event_ticker : {e['event_ticker']}   series: {e['series_ticker']}")
        for m in e["markets"]:
            bid = m["yes_bid"]
            ask = m["yes_ask"]
            label = m["yes_sub_title"] or m["ticker"]
            print(f"       - {m['ticker']:<40} {label}")
            print(f"           yes bid/ask: {bid}/{ask}  vol: {m['volume']}  closes: {m['close_time']}")
        print()


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("query", help="case-insensitive substring to match")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument(
        "--all-kalshi",
        action="store_true",
        help="include Kalshi parlay/multi-leg series (KXMVE*) in results",
    )
    args = ap.parse_args()

    async with httpx.AsyncClient(timeout=30) as client:
        poly, kalshi = await asyncio.gather(
            search_polymarket(client, args.query, args.limit),
            search_kalshi(client, args.query, args.limit, include_all=args.all_kalshi),
        )

    _print_poly(poly)
    _print_kalshi(kalshi)


if __name__ == "__main__":
    asyncio.run(main())
