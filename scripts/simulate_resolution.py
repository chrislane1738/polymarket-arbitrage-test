"""Manually trigger a resolution event on a paired market.

The earliest real-world resolution among our 16 pairs is ~7 days out, so this
harness exists to validate the resolution code path without waiting.

Usage:
    python -m scripts.simulate_resolution "BTC reach $85k in May 2026" yes yes
    python -m scripts.simulate_resolution "Fed Jun26 - No change" yes no   # divergent — wipeout for direction A, jackpot for direction B
    python -m scripts.simulate_resolution "NHL 2026 - Vegas" voided voided  # both venues void the market

Outcomes per venue must be one of: yes, no, voided.

This writes the simulated results into pair_resolution and then calls
resolve_open_positions to close any open paper positions for the pair.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics.resolver import resolve_open_positions  # noqa: E402
from match.markets import load_pairs  # noqa: E402
from store.sqlite import Store  # noqa: E402

VALID_RESULTS = ("yes", "no", "voided")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pair_name", help="exact pair name from config/markets.yaml (use quotes)")
    ap.add_argument("poly_result", choices=VALID_RESULTS, help="Polymarket resolution outcome")
    ap.add_argument("kalshi_result", choices=VALID_RESULTS, help="Kalshi resolution outcome")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would happen without writing to the DB")
    args = ap.parse_args()

    pairs = {p.name: p for p in load_pairs()}
    if args.pair_name not in pairs:
        print(f"No such pair: {args.pair_name!r}")
        print(f"Configured pairs:")
        for name in pairs:
            print(f"  {name}")
        sys.exit(1)

    store = Store()
    try:
        # Snapshot of open positions for this pair
        open_for_pair = [p for p in store.list_open_paper_positions()
                         if p["pair_name"] == args.pair_name]

        print(f"Pair: {args.pair_name}")
        print(f"Simulated resolution: poly={args.poly_result}  kalshi={args.kalshi_result}")
        print(f"Open positions for pair: {len(open_for_pair)}")
        for pos in open_for_pair:
            entry_cost = pos["entry_poly_price"] + pos["entry_kalshi_price"]
            print(f"  id={pos['id']} dir={pos['direction']} size={int(pos['size'])} "
                  f"entry_cost={entry_cost:.4f} fees={pos['entry_fees']:.4f}")

        if not open_for_pair:
            print("\nNo open positions for this pair — nothing to resolve.")
            return

        if args.dry_run:
            print("\n(dry-run) would write resolution + close positions; skipping.")
            return

        # Record the simulated results into pair_resolution
        now_ns = time.time_ns()
        store.record_pair_result(args.pair_name, "poly", args.poly_result, now_ns)
        store.record_pair_result(args.pair_name, "kalshi", args.kalshi_result, now_ns)

        n = resolve_open_positions(store, args.pair_name, args.poly_result, args.kalshi_result)
        print(f"\nResolved {n} position(s).")

        # Show resulting close_pnl for each
        print("\nClose results:")
        for r in store.list_paper_positions():
            if r["pair_name"] != args.pair_name or r["closed_ts_ns"] is None:
                continue
            # Only show positions just resolved by us (close_reason='resolved' with our results)
            if r.get("close_reason") != "resolved":
                continue
            if r.get("resolution_poly") != args.poly_result or r.get("resolution_kalshi") != args.kalshi_result:
                continue
            print(f"  id={r['id']} pnl={r['close_pnl']:+.3f} "
                  f"(poly_leg={r['close_poly_price']:.2f}, kalshi_leg={r['close_kalshi_price']:.2f})")

    finally:
        store.close()


if __name__ == "__main__":
    main()
