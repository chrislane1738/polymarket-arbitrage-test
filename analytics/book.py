"""In-memory orderbook state.

Polymarket WS sends:
  - "book"          : full snapshot (replaces state)
  - "price_change"  : per-level deltas (size=0 means level removed)

Kalshi WS sends:
  - "orderbook_snapshot" : full state per side ('yes'/'no' are BID-side levels)
  - "orderbook_delta"    : signed change to a level at one price on one side

We maintain per-key state and expose best_bid/best_ask after each update so
the arb detector can read fresh top-of-book without re-parsing raw events.
"""
from __future__ import annotations


class PolymarketBookState:
    def __init__(self) -> None:
        self.books: dict[str, dict[str, dict[float, float]]] = {}

    def _ensure(self, asset_id: str) -> dict[str, dict[float, float]]:
        return self.books.setdefault(asset_id, {"bids": {}, "asks": {}})

    def apply(self, ev: dict) -> tuple[str, float | None, float | None] | None:
        """Update book state from a single WS event.

        Returns (asset_id, best_bid, best_ask) if anything changed, else None.
        """
        asset = ev.get("asset_id")
        if not asset:
            return None
        et = ev.get("event_type")
        book = self._ensure(asset)

        if et == "book":
            book["bids"] = {
                float(b["price"]): float(b["size"])
                for b in (ev.get("bids") or [])
                if float(b.get("size") or 0) > 0
            }
            book["asks"] = {
                float(a["price"]): float(a["size"])
                for a in (ev.get("asks") or [])
                if float(a.get("size") or 0) > 0
            }
        elif et == "price_change":
            for change in ev.get("changes") or []:
                side_raw = (change.get("side") or "").upper()
                side = "bids" if side_raw == "BUY" else "asks"
                try:
                    price = float(change["price"])
                    size = float(change["size"])
                except (KeyError, ValueError, TypeError):
                    continue
                if size <= 0:
                    book[side].pop(price, None)
                else:
                    book[side][price] = size
        else:
            return None

        best_bid = max(book["bids"]) if book["bids"] else None
        best_ask = min(book["asks"]) if book["asks"] else None
        return (asset, best_bid, best_ask)

    def get_levels(self, asset_id: str, side: str) -> dict[float, float]:
        """Return a shallow copy of {price: size} for the requested side.

        side: 'bids' or 'asks'. Returns {} for unknown asset or empty side.
        Copy is defensive: callers can mutate without affecting internal state.
        """
        if side not in ("bids", "asks"):
            raise ValueError(f"side must be 'bids' or 'asks', got {side!r}")
        book = self.books.get(asset_id)
        if book is None:
            return {}
        return dict(book.get(side, {}))


class KalshiBookState:
    """Per-ticker YES-side top-of-book derived from Kalshi WS.

    Kalshi's book stores BIDS on both YES and NO. The implied YES ASK is
    1 - best_no_bid (selling YES at price p == buying NO at 1 - p).

    Prices arrive in cents (1-99). We normalize to dollars [0, 1] internally.
    """

    def __init__(self) -> None:
        # ticker -> {"yes": {price_dollars: size}, "no": {price_dollars: size}}
        self.books: dict[str, dict[str, dict[float, float]]] = {}

    def _ensure(self, ticker: str) -> dict[str, dict[float, float]]:
        return self.books.setdefault(ticker, {"yes": {}, "no": {}})

    @staticmethod
    def _to_dollars(p) -> float | None:
        try:
            v = float(p)
        except (TypeError, ValueError):
            return None
        # Newer schema sometimes returns dollars already; legacy is cents.
        return v / 100.0 if v > 1.0 else v

    def apply(self, ev: dict) -> tuple[str, float | None, float | None] | None:
        """Update book state from a single WS event.

        Returns (ticker, best_yes_bid, best_yes_ask) if anything changed.
        """
        msg_type = ev.get("type")
        msg = ev.get("msg") or {}
        ticker = msg.get("market_ticker")
        if not ticker:
            return None
        book = self._ensure(ticker)

        if msg_type == "orderbook_snapshot":
            # Newer schema uses *_dollars_fp (price string already in dollars);
            # legacy used "yes"/"no" with cents-int prices.
            for side_key in ("yes", "no"):
                levels = msg.get(f"{side_key}_dollars_fp") or msg.get(side_key) or []
                new = {}
                for entry in levels:
                    try:
                        p = self._to_dollars(entry[0])
                        s = float(entry[1])
                    except (TypeError, ValueError, IndexError):
                        continue
                    if p is not None and s > 0:
                        new[p] = s
                book[side_key] = new

        elif msg_type == "orderbook_delta":
            side = msg.get("side")
            if side not in ("yes", "no"):
                return None
            # Newer: price_dollars (str dollars), delta_fp (str size).
            # Legacy: price (int cents), delta (int size).
            raw_price = msg.get("price_dollars")
            if raw_price is None:
                raw_price = msg.get("price")
            p = self._to_dollars(raw_price)
            if p is None:
                return None
            raw_delta = msg.get("delta_fp")
            if raw_delta is None:
                raw_delta = msg.get("delta", 0)
            try:
                delta = float(raw_delta)
            except (TypeError, ValueError):
                return None
            current = book[side].get(p, 0.0)
            new_size = current + delta
            if new_size <= 0:
                book[side].pop(p, None)
            else:
                book[side][p] = new_size
        else:
            return None

        best_yes_bid = max(book["yes"]) if book["yes"] else None
        best_no_bid = max(book["no"]) if book["no"] else None
        best_yes_ask = (1.0 - best_no_bid) if best_no_bid is not None else None
        return (ticker, best_yes_bid, best_yes_ask)

    def get_levels(self, ticker: str, side: str) -> dict[float, float]:
        """Return a shallow copy of {price_dollars: size} for the requested side.

        side: 'yes' or 'no' — both store BID-side liquidity for that outcome.
        Copy is defensive: callers can mutate without affecting internal state.
        """
        if side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no', got {side!r}")
        book = self.books.get(ticker)
        if book is None:
            return {}
        return dict(book.get(side, {}))
