"""Expiry resolution.

On startup:
  - For each configured pair, fetch close_time and conditionId from both
    Polymarket (Gamma API) and Kalshi (REST). Cache in `pair_resolution` table.

Periodically (default every 5 min):
  - For each pair past close_time on either venue with no recorded result,
    poll the venue for resolution outcome.
  - When BOTH venues have results, resolve all open paper positions for that
    pair via store.resolve_paper_position(), which applies per-leg payouts:
      $1 if the leg's side matches that venue's resolution, else $0.
      Voided/null result on a venue refunds that leg's entry price.

This is the path that converts "Held→Exp" PnL into actual realized PnL.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime

import httpx

from store.sqlite import Store

POLY_GAMMA = "https://gamma-api.polymarket.com/markets"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

INIT_FETCH_DELAY_S = 10.0
SWEEP_INTERVAL_S = float(os.environ.get("RESOLVER_SWEEP_S", "300"))
PER_REQUEST_DELAY_S = 0.25

log = logging.getLogger("resolver")


def parse_iso_to_ns(iso_str: str | None) -> int | None:
    if not iso_str:
        return None
    try:
        s = iso_str.replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp() * 1e9)
    except (ValueError, TypeError):
        return None


def _iso(ts_ns: int | None) -> str:
    if ts_ns is None:
        return "?"
    return datetime.fromtimestamp(ts_ns / 1e9).isoformat(timespec="minutes")


async def fetch_poly_market_by_token(client: httpx.AsyncClient, token_id: str) -> dict | None:
    try:
        r = await client.get(POLY_GAMMA, params={"clob_token_ids": token_id})
        if r.status_code != 200:
            return None
        markets = r.json() or []
        return markets[0] if markets else None
    except httpx.HTTPError as e:
        log.warning("poly lookup failed for token %s: %s", token_id[:16], e)
        return None


async def fetch_kalshi_market(client: httpx.AsyncClient, ticker: str) -> dict | None:
    try:
        r = await client.get(f"{KALSHI_BASE}/markets/{ticker}")
        if r.status_code != 200:
            return None
        return (r.json() or {}).get("market")
    except httpx.HTTPError as e:
        log.warning("kalshi lookup failed for ticker %s: %s", ticker, e)
        return None


def _extract_kalshi_result(market: dict) -> str | None:
    """Returns 'yes', 'no', 'voided', or None (not yet resolved)."""
    result = market.get("result")
    status = (market.get("status") or "").lower()
    if result in ("yes", "no"):
        return result
    if status in ("voided", "cancelled", "canceled"):
        return "voided"
    return None


def _extract_poly_result(market: dict) -> str | None:
    """Returns 'yes', 'no', 'voided', or None (not yet resolved)."""
    if not market.get("closed"):
        return None
    prices = market.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except (ValueError, TypeError):
            return None
    if not isinstance(prices, list) or len(prices) < 2:
        return None
    try:
        y = float(prices[0])
        n = float(prices[1])
    except (ValueError, TypeError):
        return None
    if y >= 0.99 and n <= 0.01:
        return "yes"
    if n >= 0.99 and y <= 0.01:
        return "no"
    # Closed but with indecisive prices — likely voided/canceled
    status = (market.get("umaResolutionStatuses") or "").lower()
    if "void" in status or "canc" in status:
        return "voided"
    return None  # not yet finalized; retry later


async def init_close_times(client: httpx.AsyncClient, pairs, store: Store) -> None:
    """Fetch and cache close_time + conditionId for each pair on both venues."""
    to_fetch = []
    for p in pairs:
        existing = store.get_pair_resolution(p.name) or {}
        if not (existing.get("poly_close_ts_ns") and existing.get("kalshi_close_ts_ns")):
            to_fetch.append(p)
    if not to_fetch:
        log.info("all %d pair(s) already have cached close_times", len(pairs))
        return
    log.info("fetching close_times for %d pair(s) (others cached)", len(to_fetch))

    for p in to_fetch:
        poly_close_ns = None
        kalshi_close_ns = None
        poly_cid = None

        if p.polymarket_yes_token:
            m = await fetch_poly_market_by_token(client, p.polymarket_yes_token)
            if m:
                poly_close_ns = parse_iso_to_ns(m.get("endDate"))
                poly_cid = m.get("conditionId")
            await asyncio.sleep(PER_REQUEST_DELAY_S)

        if p.kalshi_yes_ticker:
            m = await fetch_kalshi_market(client, p.kalshi_yes_ticker)
            if m:
                kalshi_close_ns = parse_iso_to_ns(m.get("close_time"))
            await asyncio.sleep(PER_REQUEST_DELAY_S)

        store.upsert_pair_resolution_meta(
            p.name,
            poly_close_ts_ns=poly_close_ns,
            kalshi_close_ts_ns=kalshi_close_ns,
            poly_condition_id=poly_cid,
        )
        log.info("  %-32s poly_close=%s  kalshi_close=%s",
                 p.name[:32], _iso(poly_close_ns), _iso(kalshi_close_ns))


def resolve_open_positions(store: Store, pair_name: str, poly_result: str, kalshi_result: str) -> int:
    """Close all open positions for a pair using the given per-venue resolutions."""
    n_resolved = 0
    for pos in store.list_open_paper_positions():
        if pos["pair_name"] != pair_name:
            continue
        result = store.resolve_paper_position(pos["id"], poly_result, kalshi_result)
        if result:
            n_resolved += 1
            log.info(
                "PAPER RESOLVED id=%d %s [%s]  pnl=%+.3f  (poly_leg=%.2f kalshi_leg=%.2f, "
                "poly_resolved=%s kalshi_resolved=%s)",
                pos["id"], pair_name, pos["direction"], result["pnl"],
                result["poly_leg_pay"], result["kalshi_leg_pay"],
                poly_result, kalshi_result,
            )
    return n_resolved


async def check_and_resolve(client: httpx.AsyncClient, pairs, store: Store) -> None:
    """For each pair past close_time on EITHER venue, poll both venues for results.

    Polling both venues once either passes its close handles a common asymmetry:
    Polymarket's endDate often reflects the real resolution date (e.g., NBA Finals
    ends June 2026) while Kalshi's close_time may be the *series expiration*
    (e.g., 2028 for NBA series tickers). Once Polymarket starts settling, Kalshi
    typically settles its same-event market shortly after, even if its formal
    close_time is months/years out.
    """
    now_ns = time.time_ns()
    for p in pairs:
        meta = store.get_pair_resolution(p.name) or {}
        closes = [t for t in (meta.get("poly_close_ts_ns"), meta.get("kalshi_close_ts_ns")) if t]
        if not closes:
            continue
        # Skip the pair entirely until at least one venue is past its close
        if now_ns < min(closes):
            continue

        # Try Polymarket
        if not meta.get("poly_result") and p.polymarket_yes_token:
            m = await fetch_poly_market_by_token(client, p.polymarket_yes_token)
            if m:
                result = _extract_poly_result(m)
                if result:
                    store.record_pair_result(p.name, "poly", result, now_ns)
                    log.info("poly resolved: %s -> %s", p.name, result)
                else:
                    store.record_pair_result(p.name, "poly", None, now_ns, "not yet finalized")
            await asyncio.sleep(PER_REQUEST_DELAY_S)

        # Try Kalshi
        if not meta.get("kalshi_result") and p.kalshi_yes_ticker:
            m = await fetch_kalshi_market(client, p.kalshi_yes_ticker)
            if m:
                result = _extract_kalshi_result(m)
                if result:
                    store.record_pair_result(p.name, "kalshi", result, now_ns)
                    log.info("kalshi resolved: %s -> %s", p.name, result)
                else:
                    store.record_pair_result(p.name, "kalshi", None, now_ns, "not yet finalized")
            await asyncio.sleep(PER_REQUEST_DELAY_S)

        # If both results are in, close positions for the pair
        meta = store.get_pair_resolution(p.name) or {}
        if meta.get("poly_result") and meta.get("kalshi_result"):
            resolve_open_positions(store, p.name, meta["poly_result"], meta["kalshi_result"])


async def run(pairs, store: Store) -> None:
    """Main resolver loop."""
    log.info("resolver starting (sweep every %.0fs)", SWEEP_INTERVAL_S)
    await asyncio.sleep(INIT_FETCH_DELAY_S)
    async with httpx.AsyncClient(timeout=20, headers={"Accept": "application/json"}) as client:
        try:
            await init_close_times(client, pairs, store)
        except Exception as e:
            log.exception("init_close_times failed: %s", e)
        while True:
            try:
                await check_and_resolve(client, pairs, store)
            except Exception as e:
                log.exception("resolver sweep error: %s", e)
            await asyncio.sleep(SWEEP_INTERVAL_S)
