import asyncio
import base64
import json
import logging
import os
import time

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from analytics.book import KalshiBookState

KALSHI_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
KALSHI_WS_PATH = "/trade-api/ws/v2"

log = logging.getLogger("kalshi")


def _load_private_key(path: str):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _sign(private_key, timestamp_ms: int, method: str, path: str) -> str:
    msg = f"{timestamp_ms}{method}{path}".encode()
    sig = private_key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


def _auth_headers() -> dict[str, str] | None:
    key_id = os.environ.get("KALSHI_API_KEY_ID")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not key_path or not os.path.exists(key_path):
        return None
    pk = _load_private_key(key_path)
    ts = int(time.time() * 1000)
    sig = _sign(pk, ts, "GET", KALSHI_WS_PATH)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
    }


async def run(tickers: list[str], store, book_state: KalshiBookState | None = None) -> None:
    """Subscribe to orderbook deltas + trades for the given Kalshi tickers."""
    if not tickers:
        log.info("no tickers configured, skipping")
        return

    headers = _auth_headers()
    if headers is None:
        log.warning(
            "missing KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY_PATH; "
            "Kalshi websocket requires auth — skipping"
        )
        return

    log.info("connecting; subscribing to %d ticker(s)", len(tickers))
    sub_id = 1
    if book_state is None:
        book_state = KalshiBookState()

    # Manual reconnect loop so we can re-sign headers each attempt. The
    # async-for-websockets.connect pattern reuses the original headers on
    # every reconnect — including the timestamp — which Kalshi rejects with
    # HTTP 401 once the timestamp ages out of their freshness window. That
    # 401 used to crash the asyncio event loop and kill the whole feed.
    while True:
        fresh_headers = _auth_headers()
        if fresh_headers is None:
            log.error("auth headers unavailable on reconnect; stopping kalshi feed")
            return
        try:
            async with websockets.connect(
                KALSHI_WS_URL,
                additional_headers=fresh_headers,
                ping_interval=10,
                ping_timeout=20,
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "id": sub_id,
                            "cmd": "subscribe",
                            "params": {
                                "channels": ["orderbook_delta", "trade"],
                                "market_tickers": tickers,
                            },
                        }
                    )
                )
                async for raw in ws:
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        log.warning("non-json message: %r", raw[:120])
                        continue
                    if not isinstance(ev, dict):
                        continue
                    store.record_kalshi(ev)
                    result = book_state.apply(ev)
                    if result is not None:
                        ticker, yes_bid, yes_ask = result
                        store.record_quote("kalshi", ticker, yes_bid, yes_ask)
        except websockets.ConnectionClosed as e:
            log.warning("connection closed (%s); reconnecting", e)
            await asyncio.sleep(1)
            continue
        except Exception as e:
            log.exception("unexpected error: %s; reconnecting", e)
            await asyncio.sleep(2)
            continue
