"""Replay logged events from the SQLite store.

Usage:
    python -m scripts.replay                 # tail 100 most recent events (any venue)
    python -m scripts.replay --venue kalshi  # filter to one venue
    python -m scripts.replay --trades        # just the trades table
    python -m scripts.replay --follow        # keep tailing as new events arrive
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = "data/feed.db"


def _row_str(row: tuple, payload_chars: int = 200) -> str:
    ts, venue, evt, mkt, payload = row
    return f"{ts}\t{venue}\t{evt}\t{mkt}\t{payload[:payload_chars]}"


def replay_events(venue: str | None, limit: int, follow: bool) -> None:
    conn = sqlite3.connect(DB_PATH)
    last_id = 0
    base = "SELECT id, ts_ns, venue, event_type, market_id, payload FROM events"

    if not follow:
        q = base + (" WHERE venue = ?" if venue else "") + " ORDER BY ts_ns DESC LIMIT ?"
        args: tuple = (venue, limit) if venue else (limit,)
        rows = list(conn.execute(q, args))
        for r in reversed(rows):
            print(_row_str(r[1:]))
        return

    # follow mode: print backlog, then poll
    init_q = base + (" WHERE venue = ?" if venue else "") + " ORDER BY id DESC LIMIT ?"
    init_args: tuple = (venue, limit) if venue else (limit,)
    for r in reversed(list(conn.execute(init_q, init_args))):
        last_id = max(last_id, r[0])
        print(_row_str(r[1:]))

    try:
        while True:
            q = base + " WHERE id > ?" + (" AND venue = ?" if venue else "") + " ORDER BY id"
            args = (last_id, venue) if venue else (last_id,)
            for r in conn.execute(q, args):
                last_id = r[0]
                print(_row_str(r[1:]))
            sys.stdout.flush()
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


def replay_trades(venue: str | None, limit: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    q = "SELECT ts_ns, venue, market_id, price, size, side FROM trades"
    args: tuple = ()
    if venue:
        q += " WHERE venue = ?"
        args = (venue,)
    q += " ORDER BY ts_ns DESC LIMIT ?"
    args = args + (limit,)
    rows = list(conn.execute(q, args))
    for ts, venue, mkt, price, size, side in reversed(rows):
        print(f"{ts}\t{venue}\t{mkt}\t{price:.4f}\t{size}\t{side or ''}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--venue", choices=["polymarket", "kalshi"], default=None)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--trades", action="store_true", help="show trades table instead of events")
    ap.add_argument("--follow", "-f", action="store_true", help="tail new events as they arrive")
    args = ap.parse_args()

    if not Path(DB_PATH).exists():
        print(f"no database at {DB_PATH}; run `python -m scripts.run_feeds` first")
        return

    if args.trades:
        replay_trades(args.venue, args.limit)
    else:
        replay_events(args.venue, args.limit, args.follow)


if __name__ == "__main__":
    main()
