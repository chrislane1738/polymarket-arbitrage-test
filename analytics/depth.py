"""Depth-walking primitive.

Given a {price: size} dict for one side of a book and a target quantity,
compute the fill VWAP, filled quantity, and number of levels consumed.

Used by:
  - analytics.arb to compute realistic-fill edge instead of top-of-book
  - analytics.paper to compute liquidation VWAP for mark/exit

Pure function. No I/O. No side effects.
"""
from __future__ import annotations

import math


def walk_levels(
    levels: dict[float, float],
    qty: float,
    side: str,
) -> tuple[float | None, float, int]:
    """Walk one side of a book to fill `qty`.

    Args:
        levels: {price: size} dict for the side being consumed.
                For an ASK walk (buying), this is the asks dict; walk ascending.
                For a BID walk (selling), this is the bids dict; walk descending.
        qty: target quantity to fill.
        side: 'ask' (walk lowest→highest) or 'bid' (walk highest→lowest).

    Returns:
        (vwap, filled_qty, levels_used)
        vwap is None when filled_qty == 0 (empty book or qty <= 0).
        Otherwise vwap = sum(price_i * fill_i) / filled_qty.
        Returns partial fill if liquidity insufficient.
    """
    if side not in ("ask", "bid"):
        raise ValueError(f"side must be 'ask' or 'bid', got {side!r}")
    if qty <= 0 or not levels:
        return (None, 0.0, 0)

    # Snapshot to list first — defensive against concurrent mutation by WS
    # writer task. Sorting also normalizes order regardless of dict insertion.
    # Filter NaN/Inf prices: they would propagate silently into VWAP and
    # corrupt downstream cost/edge calculations (silent corruption is the
    # dangerous failure mode in a trading context).
    sorted_levels = sorted(
        ((p, s) for p, s in levels.items() if math.isfinite(p)),
        key=lambda kv: kv[0],
        reverse=(side == "bid"),
    )

    remaining = qty
    notional = 0.0
    levels_used = 0
    for price, size in sorted_levels:
        if size <= 0:
            continue
        take = min(remaining, size)
        notional += price * take
        remaining -= take
        levels_used += 1
        if remaining <= 0:
            break

    filled = qty - remaining
    if filled <= 0:
        return (None, 0.0, 0)
    return (notional / filled, filled, levels_used)
