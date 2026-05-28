"""Kalshi REST poller.

Kalshi's market-data REST endpoints are publicly accessible (no API key),
so this is the cold-start path for the Kalshi side. WebSocket is faster but
requires auth — see feeds/kalshi_ws.py.

For each configured ticker we poll:
  - GET /markets/{ticker}/orderbook  -> top-of-book quote
  - GET /markets/trades?ticker=X     -> recent trades since last seen
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from store.sqlite import Store

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLL_INTERVAL = 5.0
PER_REQUEST_DELAY = 0.18

log = logging.getLogger("kalshi-rest")


async def _get(client: httpx.AsyncClient, url: str, params: dict | None = None) -> dict | None:
    try:
        r = await client.get(url, params=params)
    except httpx.HTTPError as e:
        log.warning("request failed %s: %s", url, e)
        return None
    if r.status_code == 429:
        log.info("429 from %s; backing off", url)
        await asyncio.sleep(1.5)
        return None
    if r.status_code >= 400:
        log.warning("%s -> %s", url, r.status_code)
        return None
    try:
        return r.json()
    except Exception:
        return None


async def run(tickers: list[str], store: Store) -> None:
    if not tickers:
        log.info("no tickers configured, kalshi REST poller idle")
        return
    log.info("polling %d kalshi ticker(s) every %.1fs", len(tickers), POLL_INTERVAL)
    # Track the most recent trade we've seen per ticker to avoid duplicates
    last_trade_ts: dict[str, int] = {t: int(time.time()) - 60 for t in tickers}

    async with httpx.AsyncClient(timeout=15, headers={"Accept": "application/json"}) as client:
        while True:
            sweep_start = time.monotonic()
            for ticker in tickers:
                ob = await _get(client, f"{KALSHI_BASE}/markets/{ticker}/orderbook", {"depth": 5})
                if ob:
                    store.record_kalshi_orderbook_rest(ticker, ob)
                await asyncio.sleep(PER_REQUEST_DELAY)

                trades_resp = await _get(
                    client,
                    f"{KALSHI_BASE}/markets/trades",
                    {"ticker": ticker, "limit": 50, "min_ts": last_trade_ts[ticker]},
                )
                if trades_resp:
                    trades = trades_resp.get("trades") or []
                    max_ts = last_trade_ts[ticker]
                    for t in trades:
                        ts_epoch = t.get("created_time_epoch") or 0
                        # Newer schema uses ISO created_time; fall back to that
                        if not ts_epoch and t.get("created_time"):
                            try:
                                from datetime import datetime
                                ts_epoch = int(
                                    datetime.fromisoformat(
                                        t["created_time"].replace("Z", "+00:00")
                                    ).timestamp()
                                )
                            except Exception:
                                ts_epoch = 0
                        if ts_epoch > max_ts:
                            max_ts = ts_epoch
                        store.record_kalshi_trade_rest(ticker, t)
                    if max_ts > last_trade_ts[ticker]:
                        last_trade_ts[ticker] = max_ts
                await asyncio.sleep(PER_REQUEST_DELAY)

            elapsed = time.monotonic() - sweep_start
            if elapsed < POLL_INTERVAL:
                await asyncio.sleep(POLL_INTERVAL - elapsed)
