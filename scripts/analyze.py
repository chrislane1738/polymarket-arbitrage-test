"""Persistence analytics over the arb_candidates table.

With the dedup live, each row in arb_candidates represents an *edge change* for
some (pair, direction). We can derive:

  - How long each distinct edge level persisted (= duration until next change
    for that key, or until end of observation window)
  - Per (pair, direction): total observation time, fraction of time profitable,
    time-weighted mean edge, peak edge, transition count
  - Histogram of session durations across all positive-edge windows

Usage:
    python -m scripts.analyze
    python -m scripts.analyze --min-edge 0.005   # only count edges >= 0.5cents
    python -m scripts.analyze --pair "Fed Jun26 - Cut 25bps"
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = "data/feed.db"


def _bucket(seconds: float) -> str:
    if seconds < 1:    return "<1s"
    if seconds < 5:    return "1-5s"
    if seconds < 15:   return "5-15s"
    if seconds < 60:   return "15-60s"
    if seconds < 300:  return "1-5m"
    if seconds < 1800: return "5-30m"
    if seconds < 7200: return "30m-2h"
    return ">2h"


def analyze(db_path: str, min_edge: float, pair_filter: str | None) -> None:
    conn = sqlite3.connect(db_path)

    # Bracket the full observation window from quotes (proxy for "was the
    # system running"). If no quotes, fall back to arb_candidates.
    obs_start = conn.execute("SELECT MIN(ts_ns) FROM quotes").fetchone()[0]
    obs_end = conn.execute("SELECT MAX(ts_ns) FROM quotes").fetchone()[0]
    if not obs_start:
        obs_start = conn.execute("SELECT MIN(ts_ns) FROM arb_candidates").fetchone()[0]
        obs_end = conn.execute("SELECT MAX(ts_ns) FROM arb_candidates").fetchone()[0]
    if not obs_start:
        print("No data yet — let the feeds run first.")
        return

    window_s = (obs_end - obs_start) / 1e9
    print(f"Observation window: {window_s:,.1f}s ({window_s/60:.1f} min)")
    print(f"  start: {obs_start}  end: {obs_end}")
    print()

    # Pull all rows ordered by (pair, direction, ts) so we can compute
    # per-row "duration until next observation for this key"
    q = (
        "SELECT pair_name, direction, ts_ns, net_edge, gross_edge, "
        "       poly_leg_price, kalshi_leg_price "
        "FROM arb_candidates ORDER BY pair_name, direction, ts_ns"
    )
    rows = list(conn.execute(q))
    if not rows:
        print("No arb_candidates rows yet.")
        return

    # Group rows by (pair, direction)
    by_key: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for r in rows:
        by_key[(r[0], r[1])].append(r)

    print(f"Distinct (pair, direction) keys with logged edges: {len(by_key)}")
    print()

    # Per-key summary
    print(f"{'pair':<30} {'direction':<22} {'rows':>5} {'avg_edge':>10} {'max_edge':>10} {'pct_profit':>11}")
    print("-" * 95)
    key_summary = []
    duration_buckets: dict[str, int] = defaultdict(int)

    for (pair, direction), grouped in by_key.items():
        if pair_filter and pair_filter.lower() not in pair.lower():
            continue
        # Compute duration per row = time until next observation for this key
        # (or window_end - this_ts for the last one)
        time_weighted_sum = 0.0
        profit_time = 0.0
        total_time = 0.0
        max_edge = float("-inf")
        n_profitable_rows = 0
        for i, r in enumerate(grouped):
            ts = r[2]
            net = r[3]
            next_ts = grouped[i + 1][2] if i + 1 < len(grouped) else obs_end
            duration_s = (next_ts - ts) / 1e9
            total_time += duration_s
            time_weighted_sum += net * duration_s
            if net >= min_edge:
                profit_time += duration_s
                n_profitable_rows += 1
                duration_buckets[_bucket(duration_s)] += 1
            if net > max_edge:
                max_edge = net

        avg_edge = time_weighted_sum / total_time if total_time else 0.0
        pct_profit = profit_time / total_time * 100.0 if total_time else 0.0
        key_summary.append((pair, direction, len(grouped), avg_edge, max_edge, pct_profit, profit_time))

    # Sort by pct_profit desc
    key_summary.sort(key=lambda x: x[5], reverse=True)
    for pair, direction, n, avg_e, max_e, pct, _ in key_summary:
        print(f"{pair[:30]:<30} {direction:<22} {n:>5} {avg_e:>10.4f} {max_e:>10.4f} {pct:>10.1f}%")

    print()
    print(f"Sessions with net edge >= {min_edge:+.4f}, bucketed by duration:")
    if not duration_buckets:
        print("  (none)")
    else:
        order = ["<1s", "1-5s", "5-15s", "15-60s", "1-5m", "5-30m", "30m-2h", ">2h"]
        for b in order:
            if b in duration_buckets:
                print(f"  {b:<10} {duration_buckets[b]} session(s)")

    # Top 5 single edge events by magnitude
    print()
    print("Top 10 single edge observations:")
    top = sorted(rows, key=lambda r: r[3], reverse=True)[:10]
    print(f"  {'pair':<30} {'direction':<22} {'net':>8} {'gross':>8} {'poly':>8} {'kalshi':>8}")
    for r in top:
        print(f"  {r[0][:30]:<30} {r[1]:<22} {r[3]:>+8.4f} {r[4]:>+8.4f} {r[5]:>8.4f} {r[6]:>8.4f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-edge", type=float, default=0.0,
                    help="threshold for 'profitable' edge (default 0 = breakeven)")
    ap.add_argument("--pair", help="substring filter on pair name")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"no database at {args.db}; run `python -m scripts.run_feeds` first")
        return
    analyze(args.db, args.min_edge, args.pair)


if __name__ == "__main__":
    main()
