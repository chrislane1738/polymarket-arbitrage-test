"""Paper trading CLI.

Commands:
    python -m scripts.paper list           # all positions (open + closed)
    python -m scripts.paper open           # open positions only
    python -m scripts.paper close <id>     # close at current bids
    python -m scripts.paper pnl            # summary stats

Paper positions are opened automatically by the arb detector when it sees a
candidate above PAPER_MIN_ENTRY_EDGE. There is intentionally no auto-exit.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from store.sqlite import Store  # noqa: E402

DB_PATH = "data/feed.db"


def _age(ts_ns: int | None) -> str:
    if ts_ns is None:
        return "  --  "
    age_s = (time.time_ns() - ts_ns) / 1e9
    if age_s < 60:
        return f"{age_s:4.0f}s"
    if age_s < 3600:
        return f"{age_s/60:4.1f}m"
    return f"{age_s/3600:4.1f}h"


def cmd_list(store: Store, open_only: bool) -> None:
    rows = store.list_paper_positions(open_only=open_only)
    if not rows:
        print("(no positions)")
        return
    hdr = (
        f"{'id':>3} {'status':<6} {'pair':<28} {'direction':<22} "
        f"{'size':>5} {'cost':>7} {'mark':>8} {'MAE':>8} {'MFE':>8} {'expiry':>8} {'age':>6}"
    )
    print(hdr)
    print("-" * len(hdr))
    for p in rows:
        status = "OPEN" if p["closed_ts_ns"] is None else "CLOSED"
        entry_cost = (p["entry_poly_price"] or 0) + (p["entry_kalshi_price"] or 0)
        mark = p.get("mark_pnl")
        held = p.get("held_to_expiry_pnl")
        mae = p.get("max_adverse_pnl")
        mfe = p.get("max_favorable_pnl")
        if status == "CLOSED":
            mark = p.get("close_pnl")
        opened_age = _age(p["opened_ts_ns"])

        def f(v):
            return f"{v:+8.3f}" if isinstance(v, (int, float)) else "       -"

        print(
            f"{p['id']:>3} {status:<6} {p['pair_name'][:28]:<28} {p['direction']:<22} "
            f"{int(p['size']):>5} {entry_cost:>7.4f} {f(mark)} {f(mae)} {f(mfe)} {f(held)} {opened_age:>6}"
        )


def cmd_close(store: Store, pos_id: int) -> None:
    pos_rows = [p for p in store.list_paper_positions() if p["id"] == pos_id]
    if not pos_rows:
        print(f"no position id={pos_id}")
        return
    pos = pos_rows[0]
    if pos["closed_ts_ns"] is not None:
        print(f"position {pos_id} already closed; pnl={pos['close_pnl']}")
        return
    # Pull current best bids on each leg from latest quotes
    from match.markets import load_pairs
    pairs = {p.name: p for p in load_pairs()}
    pair = pairs.get(pos["pair_name"])
    if not pair:
        print(f"pair '{pos['pair_name']}' no longer in config — can't close at current bids")
        return
    d = pos["direction"]
    if d == "poly_yes_kalshi_no":
        _, poly_ask, _ = store.latest_quote("polymarket", pair.polymarket_yes_token)
        poly_bid, _, _ = store.latest_quote("polymarket", pair.polymarket_yes_token)
        kalshi_bid, kalshi_ask, _ = store.latest_quote("kalshi", pair.kalshi_yes_ticker)
        kalshi_no_bid = (1.0 - kalshi_ask) if kalshi_ask is not None else None
        close_poly = poly_bid
        close_kalshi = kalshi_no_bid
    elif d == "kalshi_yes_poly_no":
        _, poly_no_ask, _ = store.latest_quote("polymarket", pair.polymarket_no_token)
        poly_no_bid, _, _ = store.latest_quote("polymarket", pair.polymarket_no_token)
        kalshi_yes_bid, _, _ = store.latest_quote("kalshi", pair.kalshi_yes_ticker)
        close_poly = poly_no_bid
        close_kalshi = kalshi_yes_bid
    else:
        print(f"unknown direction: {d}")
        return
    if close_poly is None or close_kalshi is None:
        print("missing quote on at least one leg; cannot mark close")
        return
    result = store.close_paper_position(pos_id, close_poly, close_kalshi)
    if result:
        print(f"closed id={pos_id}  pnl={result['pnl']:+.3f}  ({result['pnl_per_contract']:+.4f}/contract)")


def cmd_pnl(store: Store) -> None:
    rows = store.list_paper_positions()
    n_open = sum(1 for p in rows if p["closed_ts_ns"] is None)
    n_closed = sum(1 for p in rows if p["closed_ts_ns"] is not None)
    realized = sum((p["close_pnl"] or 0) for p in rows if p["closed_ts_ns"] is not None)
    mark_open = sum((p.get("mark_pnl") or 0) for p in rows if p["closed_ts_ns"] is None)
    held_open = sum((p.get("held_to_expiry_pnl") or 0) for p in rows if p["closed_ts_ns"] is None)
    print(f"Positions  : {len(rows)} total ({n_open} open, {n_closed} closed)")
    print(f"Realized   : {realized:+.3f}")
    print(f"Open MTM   : {mark_open:+.3f}   (liquidation value vs entry cost)")
    print(f"Held-to-exp: {held_open:+.3f}   (assumes one side pays $1 at resolution)")
    print(f"Total      : {realized + mark_open:+.3f}  (realized + MTM)")
    print()
    # Exit-policy review: what would closed positions have netted if held?
    if n_closed > 0:
        closed_held = 0.0
        for p in rows:
            if p["closed_ts_ns"] is None:
                continue
            entry_cost = (p["entry_poly_price"] or 0) + (p["entry_kalshi_price"] or 0)
            held = (1.0 - entry_cost - (p["entry_fees"] or 0.0)) * p["size"]
            closed_held += held
        exit_cost = closed_held - realized
        print(f"Exit-policy review ({n_closed} closed positions):")
        print(f"  realized closes : {realized:+.3f}")
        print(f"  if held to exp  : {closed_held:+.3f}   (assumes binaries resolve, one side = $1)")
        print(f"  exit cost       : {-exit_cost:+.3f}   (negative = exit policy lost vs holding)")
        print()
    # Per-pair breakdown
    by_pair: dict[str, dict] = {}
    for p in rows:
        s = by_pair.setdefault(p["pair_name"], {"n": 0, "realized": 0.0, "mark": 0.0, "held": 0.0})
        s["n"] += 1
        s["realized"] += p["close_pnl"] or 0
        s["mark"] += (p.get("mark_pnl") or 0) if p["closed_ts_ns"] is None else 0
        s["held"] += (p.get("held_to_expiry_pnl") or 0) if p["closed_ts_ns"] is None else 0
    print(f"{'pair':<32} {'n':>3} {'realized':>10} {'mark':>10} {'held_exp':>10}")
    for name, s in sorted(by_pair.items(), key=lambda x: -(x[1]["realized"] + x[1]["mark"])):
        print(f"{name[:32]:<32} {s['n']:>3} {s['realized']:>+10.3f} {s['mark']:>+10.3f} {s['held']:>+10.3f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("open")
    sub.add_parser("pnl")
    c = sub.add_parser("close")
    c.add_argument("id", type=int)
    args = ap.parse_args()

    if not Path(DB_PATH).exists():
        print(f"no database at {DB_PATH}; run `python -m scripts.run_feeds` first")
        return

    store = Store(DB_PATH)
    try:
        if args.cmd == "list":
            cmd_list(store, open_only=False)
        elif args.cmd == "open":
            cmd_list(store, open_only=True)
        elif args.cmd == "close":
            cmd_close(store, args.id)
        elif args.cmd == "pnl":
            cmd_pnl(store)
    finally:
        store.close()


if __name__ == "__main__":
    main()
