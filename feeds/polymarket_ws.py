import asyncio
import json
import logging

import websockets

from analytics.book import PolymarketBookState
from store.sqlite import Store

POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

log = logging.getLogger("polymarket")


async def run(asset_ids: list[str], store: Store, book_state: PolymarketBookState | None = None) -> None:
    """Subscribe to L2 book + trade updates and write quotes + events.

    If book_state is provided, mutate that shared instance (so other tasks
    can read depth from the same object). Otherwise create a private one.
    """
    if not asset_ids:
        log.info("no asset_ids configured, skipping")
        return

    if book_state is None:
        book_state = PolymarketBookState()
    sub_msg = json.dumps({"assets_ids": asset_ids, "type": "market"})
    log.info("connecting; subscribing to %d token(s)", len(asset_ids))

    async for ws in websockets.connect(POLY_WS_URL, ping_interval=10, ping_timeout=20):
        try:
            await ws.send(sub_msg)
            async for raw in ws:
                if raw == "PONG" or raw == b"PONG":
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("non-json message: %r", raw[:120])
                    continue
                events = payload if isinstance(payload, list) else [payload]
                for ev in events:
                    if not isinstance(ev, dict):
                        continue
                    store.record_polymarket(ev)
                    result = book_state.apply(ev)
                    if result is not None:
                        asset, best_bid, best_ask = result
                        store.record_quote("polymarket", asset, best_bid, best_ask)
        except websockets.ConnectionClosed as e:
            log.warning("connection closed (%s); reconnecting", e)
            await asyncio.sleep(1)
            continue
        except Exception as e:
            log.exception("unexpected error: %s; reconnecting", e)
            await asyncio.sleep(2)
            continue
