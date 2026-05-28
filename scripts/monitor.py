"""Live monitor: shows current top-of-book per pair + recent arb candidates.

Usage:
    python -m scripts.monitor              # refresh every 2s
    python -m scripts.monitor --interval 5
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from match.markets import load_pairs  # noqa: E402

DB_PATH = "data/feed.db"


def fmt(v):
    return f"{v:.4f}" if isinstance(v, (int, float)) else "  --  "


def latest_quote(conn, venue: str, market_id: str):
    row = conn.execute(
        "SELECT best_bid, best_ask, ts_ns FROM quotes "
        "WHERE venue = ? AND market_id = ? ORDER BY ts_ns DESC LIMIT 1",
        (venue, market_id),
    ).fetchone()
    return row if row else (None, None, None)


def age_str(ts_ns):
    if ts_ns is None:
        return "      "
    age_s = (time.time_ns() - ts_ns) / 1e9
    if age_s < 60:
        return f"{age_s:5.1f}s"
    if age_s < 3600:
        return f"{age_s/60:5.1f}m"
    return f"{age_s/3600:5.1f}h"


def render(conn) -> None:
    pairs = load_pairs()
    print("\033[2J\033[H", end="")  # clear screen
    print("=" * 100)
    print(f"polymarket-arbitrage-test monitor  |  {time.strftime('%Y-%m-%d %H:%M:%S')}  |  pairs: {len(pairs)}")
    print("=" * 100)

    for p in pairs:
        print(f"\n[{p.name}]")
        print(f"  {'venue/side':<22} {'bid':>10} {'ask':>10} {'mid':>10} {'age':>8}")
        rows = []
        if p.polymarket_yes_token:
            bid, ask, ts = latest_quote(conn, "polymarket", p.polymarket_yes_token)
            mid = (bid + ask) / 2 if bid and ask else None
            rows.append(("polymarket YES", bid, ask, mid, ts))
        if p.polymarket_no_token:
            bid, ask, ts = latest_quote(conn, "polymarket", p.polymarket_no_token)
            mid = (bid + ask) / 2 if bid and ask else None
            rows.append(("polymarket NO ", bid, ask, mid, ts))
        if p.kalshi_yes_ticker:
            bid, ask, ts = latest_quote(conn, "kalshi", p.kalshi_yes_ticker)
            mid = (bid + ask) / 2 if bid and ask else None
            rows.append(("kalshi YES    ", bid, ask, mid, ts))
        for label, bid, ask, mid, ts in rows:
            print(f"  {label:<22} {fmt(bid):>10} {fmt(ask):>10} {fmt(mid):>10} {age_str(ts):>8}")

    print("\n" + "-" * 100)
    print("Most recent arb candidates (net edge >= -0.05):")
    print(f"  {'when':>8}  {'pair':<35} {'direction':<22} {'net':>8} {'gross':>8} {'cost':>8}")
    for ts_ns, pair_name, direction, gross, net, cost in conn.execute(
        "SELECT ts_ns, pair_name, direction, gross_edge, net_edge, cost "
        "FROM arb_candidates ORDER BY ts_ns DESC LIMIT 10"
    ):
        print(
            f"  {age_str(ts_ns):>8}  {pair_name[:35]:<35} {direction:<22} "
            f"{net:>8.4f} {gross:>8.4f} {cost:>8.4f}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--once", action="store_true", help="render once and exit")
    args = ap.parse_args()

    if not Path(DB_PATH).exists():
        print(f"no database at {DB_PATH}; run `python -m scripts.run_feeds` first")
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        if args.once:
            render(conn)
            return
        while True:
            render(conn)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
