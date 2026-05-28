import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ns INTEGER NOT NULL,
    venue TEXT NOT NULL,
    event_type TEXT NOT NULL,
    market_id TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_venue_ts ON events(venue, ts_ns);
CREATE INDEX IF NOT EXISTS idx_events_market ON events(market_id, ts_ns);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ns INTEGER NOT NULL,
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    side TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_market_ts ON trades(market_id, ts_ns);

CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ns INTEGER NOT NULL,
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    best_bid REAL,
    best_ask REAL
);
CREATE INDEX IF NOT EXISTS idx_quotes_market_ts ON quotes(venue, market_id, ts_ns);

CREATE TABLE IF NOT EXISTS arb_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ns INTEGER NOT NULL,
    pair_name TEXT NOT NULL,
    direction TEXT NOT NULL,
    gross_edge REAL,
    net_edge REAL,
    cost REAL,
    poly_leg_price REAL,
    kalshi_leg_price REAL,
    fees REAL,
    snapshot TEXT
);
CREATE INDEX IF NOT EXISTS idx_arb_ts ON arb_candidates(ts_ns);
CREATE INDEX IF NOT EXISTS idx_arb_pair_ts ON arb_candidates(pair_name, ts_ns);

CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_ts_ns INTEGER NOT NULL,
    closed_ts_ns INTEGER,
    pair_name TEXT NOT NULL,
    direction TEXT NOT NULL,
    size REAL NOT NULL,
    entry_poly_price REAL NOT NULL,
    entry_kalshi_price REAL NOT NULL,
    entry_net_edge REAL,
    entry_fees REAL,
    close_poly_price REAL,
    close_kalshi_price REAL,
    close_pnl REAL,
    last_mark_ts_ns INTEGER,
    mark_pnl REAL,
    held_to_expiry_pnl REAL
);
CREATE INDEX IF NOT EXISTS idx_paper_open ON paper_positions(closed_ts_ns);

CREATE TABLE IF NOT EXISTS pair_resolution (
    pair_name              TEXT PRIMARY KEY,
    poly_close_ts_ns       INTEGER,
    kalshi_close_ts_ns     INTEGER,
    poly_result            TEXT,     -- 'yes', 'no', 'voided', or NULL
    kalshi_result          TEXT,     -- same
    poly_result_ts_ns      INTEGER,  -- when we observed this result
    kalshi_result_ts_ns    INTEGER,
    last_check_ts_ns       INTEGER,
    last_error             TEXT,
    poly_condition_id      TEXT      -- cached for Polymarket lookups
);
"""


class Store:
    def __init__(self, path: str | Path = "data/feed.db") -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        # Idempotent column additions for forward-compat with existing dbs
        for stmt in (
            "ALTER TABLE paper_positions ADD COLUMN max_adverse_pnl REAL",
            "ALTER TABLE paper_positions ADD COLUMN max_adverse_ts_ns INTEGER",
            "ALTER TABLE paper_positions ADD COLUMN max_favorable_pnl REAL",
            "ALTER TABLE paper_positions ADD COLUMN max_favorable_ts_ns INTEGER",
            "ALTER TABLE paper_positions ADD COLUMN close_reason TEXT",
            "ALTER TABLE paper_positions ADD COLUMN resolution_poly TEXT",
            "ALTER TABLE paper_positions ADD COLUMN resolution_kalshi TEXT",
            "ALTER TABLE arb_candidates ADD COLUMN fill_vwap_poly REAL",
            "ALTER TABLE arb_candidates ADD COLUMN fill_vwap_kalshi REAL",
            "ALTER TABLE arb_candidates ADD COLUMN fill_qty_poly REAL",
            "ALTER TABLE arb_candidates ADD COLUMN fill_qty_kalshi REAL",
            "ALTER TABLE arb_candidates ADD COLUMN levels_consumed_poly INTEGER",
            "ALTER TABLE arb_candidates ADD COLUMN levels_consumed_kalshi INTEGER",
            "ALTER TABLE arb_candidates ADD COLUMN partial_fill INTEGER",
            "ALTER TABLE paper_positions ADD COLUMN entry_fill_vwap_poly REAL",
            "ALTER TABLE paper_positions ADD COLUMN entry_fill_vwap_kalshi REAL",
            "ALTER TABLE paper_positions ADD COLUMN entry_levels_consumed_poly INTEGER",
            "ALTER TABLE paper_positions ADD COLUMN entry_levels_consumed_kalshi INTEGER",
            "ALTER TABLE paper_positions ADD COLUMN entry_partial_fill INTEGER",
        ):
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists

    @staticmethod
    def _now_ns() -> int:
        return time.time_ns()

    # ------------------------------------------------------------------ raw events / trades

    def record_polymarket(self, ev: dict[str, Any]) -> None:
        evt = ev.get("event_type", "unknown")
        market = ev.get("asset_id") or ev.get("market")
        ts = self._now_ns()
        self.conn.execute(
            "INSERT INTO events (ts_ns, venue, event_type, market_id, payload) VALUES (?, ?, ?, ?, ?)",
            (ts, "polymarket", str(evt), market, json.dumps(ev)),
        )
        if evt == "last_trade_price":
            try:
                self.conn.execute(
                    "INSERT INTO trades (ts_ns, venue, market_id, price, size, side) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        ts,
                        "polymarket",
                        market,
                        float(ev["price"]),
                        float(ev["size"]),
                        ev.get("side"),
                    ),
                )
            except (KeyError, ValueError, TypeError):
                pass

    def record_kalshi(self, ev: dict[str, Any]) -> None:
        """WS event (auth-gated path)."""
        msg_type = ev.get("type", "unknown")
        msg = ev.get("msg") or {}
        market = msg.get("market_ticker")
        ts = self._now_ns()
        self.conn.execute(
            "INSERT INTO events (ts_ns, venue, event_type, market_id, payload) VALUES (?, ?, ?, ?, ?)",
            (ts, "kalshi", str(msg_type), market, json.dumps(ev)),
        )
        if msg_type == "trade":
            try:
                price = float(msg["yes_price"]) / 100.0
                self.conn.execute(
                    "INSERT INTO trades (ts_ns, venue, market_id, price, size, side) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        ts,
                        "kalshi",
                        market,
                        price,
                        float(msg["count"]),
                        msg.get("taker_side"),
                    ),
                )
            except (KeyError, ValueError, TypeError):
                pass

    # ------------------------------------------------------------------ REST-poller writes

    def record_kalshi_orderbook_rest(self, ticker: str, ob_payload: dict) -> None:
        """Persist the orderbook snapshot and extract a quote row.

        Kalshi REST returns an object like:
          {"orderbook_fp": {"yes_dollars": [[price_str, size_str], ...],
                             "no_dollars":  [[price_str, size_str], ...]}}

        Both arrays are bids: people willing to BUY YES at price (yes_dollars),
        and people willing to BUY NO at price (no_dollars). The best YES ASK is
        therefore (1 - best NO BID), since selling YES @ p == buying NO @ (1-p).
        """
        ts = self._now_ns()
        self.conn.execute(
            "INSERT INTO events (ts_ns, venue, event_type, market_id, payload) VALUES (?, ?, ?, ?, ?)",
            (ts, "kalshi", "orderbook_rest", ticker, json.dumps(ob_payload)),
        )
        ob = ob_payload.get("orderbook_fp") or ob_payload.get("orderbook") or {}
        yes_levels = ob.get("yes") or ob.get("yes_dollars") or []
        no_levels = ob.get("no") or ob.get("no_dollars") or []

        def best(levels):
            best_p = None
            for entry in levels:
                try:
                    p = float(entry[0])
                except (ValueError, TypeError, IndexError):
                    continue
                # If integers (cents), normalize to dollars
                if p > 1.0:
                    p = p / 100.0
                if best_p is None or p > best_p:
                    best_p = p
            return best_p

        yes_bid = best(yes_levels)
        no_bid = best(no_levels)
        yes_ask = (1.0 - no_bid) if no_bid is not None else None
        self.conn.execute(
            "INSERT INTO quotes (ts_ns, venue, market_id, best_bid, best_ask) VALUES (?, ?, ?, ?, ?)",
            (ts, "kalshi", ticker, yes_bid, yes_ask),
        )

    def record_kalshi_trade_rest(self, ticker: str, trade: dict) -> None:
        ts = self._now_ns()
        self.conn.execute(
            "INSERT INTO events (ts_ns, venue, event_type, market_id, payload) VALUES (?, ?, ?, ?, ?)",
            (ts, "kalshi", "trade_rest", ticker, json.dumps(trade)),
        )
        try:
            price_raw = trade.get("yes_price_dollars") or trade.get("yes_price")
            price = float(price_raw)
            if price > 1.0:
                price = price / 100.0
            size = float(trade.get("count_fp") or trade.get("count") or 0)
            self.conn.execute(
                "INSERT INTO trades (ts_ns, venue, market_id, price, size, side) VALUES (?, ?, ?, ?, ?, ?)",
                (ts, "kalshi", ticker, price, size, trade.get("taker_side")),
            )
        except (KeyError, ValueError, TypeError):
            pass

    # ------------------------------------------------------------------ quotes (generic)

    def record_quote(
        self,
        venue: str,
        market_id: str,
        best_bid: float | None,
        best_ask: float | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO quotes (ts_ns, venue, market_id, best_bid, best_ask) VALUES (?, ?, ?, ?, ?)",
            (self._now_ns(), venue, market_id, best_bid, best_ask),
        )

    def latest_quote(self, venue: str, market_id: str) -> tuple[float | None, float | None, int | None]:
        row = self.conn.execute(
            "SELECT best_bid, best_ask, ts_ns FROM quotes "
            "WHERE venue = ? AND market_id = ? ORDER BY ts_ns DESC LIMIT 1",
            (venue, market_id),
        ).fetchone()
        if not row:
            return (None, None, None)
        return (row[0], row[1], row[2])

    # ------------------------------------------------------------------ arb candidates

    def record_arb_candidate(self, pair_quote, cand: dict) -> None:
        snapshot = {
            "poly_yes_bid": pair_quote.poly_yes_bid,
            "poly_yes_ask": pair_quote.poly_yes_ask,
            "poly_no_bid": pair_quote.poly_no_bid,
            "poly_no_ask": pair_quote.poly_no_ask,
            "kalshi_yes_bid": pair_quote.kalshi_yes_bid,
            "kalshi_yes_ask": pair_quote.kalshi_yes_ask,
            "kalshi_ticker": pair_quote.kalshi_ticker,
        }
        self.conn.execute(
            "INSERT INTO arb_candidates "
            "(ts_ns, pair_name, direction, gross_edge, net_edge, cost, "
            " poly_leg_price, kalshi_leg_price, fees, snapshot, "
            " fill_vwap_poly, fill_vwap_kalshi, fill_qty_poly, fill_qty_kalshi, "
            " levels_consumed_poly, levels_consumed_kalshi, partial_fill) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._now_ns(),
                pair_quote.pair_name,
                cand["direction"],
                cand["gross_edge"],
                cand["net_edge"],
                cand["cost"],
                cand["poly_leg_price"],
                cand["kalshi_leg_price"],
                cand["fees"],
                json.dumps(snapshot),
                cand.get("fill_vwap_poly"),
                cand.get("fill_vwap_kalshi"),
                cand.get("fill_qty_poly"),
                cand.get("fill_qty_kalshi"),
                cand.get("levels_consumed_poly"),
                cand.get("levels_consumed_kalshi"),
                1 if cand.get("partial_fill") else 0,
            ),
        )

    # ------------------------------------------------------------------ paper trading

    def open_paper_position(
        self,
        pair_name: str,
        direction: str,
        size: float,
        entry_poly_price: float,
        entry_kalshi_price: float,
        entry_net_edge: float,
        entry_fees: float,
        entry_fill_vwap_poly: float | None = None,
        entry_fill_vwap_kalshi: float | None = None,
        entry_levels_consumed_poly: int | None = None,
        entry_levels_consumed_kalshi: int | None = None,
        entry_partial_fill: bool | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO paper_positions "
            "(opened_ts_ns, pair_name, direction, size, entry_poly_price, "
            " entry_kalshi_price, entry_net_edge, entry_fees, "
            " entry_fill_vwap_poly, entry_fill_vwap_kalshi, "
            " entry_levels_consumed_poly, entry_levels_consumed_kalshi, "
            " entry_partial_fill) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._now_ns(),
                pair_name,
                direction,
                size,
                entry_poly_price,
                entry_kalshi_price,
                entry_net_edge,
                entry_fees,
                entry_fill_vwap_poly,
                entry_fill_vwap_kalshi,
                entry_levels_consumed_poly,
                entry_levels_consumed_kalshi,
                None if entry_partial_fill is None else (1 if entry_partial_fill else 0),
            ),
        )
        return cur.lastrowid

    def list_paper_positions(self, open_only: bool = False) -> list[dict]:
        q = (
            "SELECT id, opened_ts_ns, closed_ts_ns, pair_name, direction, size, "
            "       entry_poly_price, entry_kalshi_price, entry_net_edge, entry_fees, "
            "       close_poly_price, close_kalshi_price, close_pnl, close_reason, "
            "       last_mark_ts_ns, mark_pnl, held_to_expiry_pnl, "
            "       max_adverse_pnl, max_adverse_ts_ns, "
            "       max_favorable_pnl, max_favorable_ts_ns "
            "FROM paper_positions"
        )
        if open_only:
            q += " WHERE closed_ts_ns IS NULL"
        q += " ORDER BY id"
        cols = [
            "id", "opened_ts_ns", "closed_ts_ns", "pair_name", "direction", "size",
            "entry_poly_price", "entry_kalshi_price", "entry_net_edge", "entry_fees",
            "close_poly_price", "close_kalshi_price", "close_pnl", "close_reason",
            "last_mark_ts_ns", "mark_pnl", "held_to_expiry_pnl",
            "max_adverse_pnl", "max_adverse_ts_ns",
            "max_favorable_pnl", "max_favorable_ts_ns",
        ]
        return [dict(zip(cols, row)) for row in self.conn.execute(q)]

    def list_open_paper_positions(self) -> list[dict]:
        return self.list_paper_positions(open_only=True)

    def get_pair_realized(self, pair_name: str) -> tuple[float, int]:
        """Return (cumulative realized PnL, close count) for a pair."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(close_pnl), 0.0), COUNT(*) "
            "FROM paper_positions WHERE pair_name = ? AND closed_ts_ns IS NOT NULL",
            (pair_name,),
        ).fetchone()
        return (float(row[0] or 0.0), int(row[1] or 0))

    def update_paper_mark(self, pos_id: int, mark_pnl: float, held_pnl: float) -> None:
        """Update MTM and roll MAE/MFE if a new extreme was reached."""
        ts = self._now_ns()
        # Read current extremes
        row = self.conn.execute(
            "SELECT max_adverse_pnl, max_favorable_pnl FROM paper_positions WHERE id = ?",
            (pos_id,),
        ).fetchone()
        cur_mae, cur_mfe = (row or (None, None))
        new_mae = mark_pnl if cur_mae is None or mark_pnl < cur_mae else cur_mae
        new_mfe = mark_pnl if cur_mfe is None or mark_pnl > cur_mfe else cur_mfe
        mae_changed = cur_mae is None or mark_pnl < cur_mae
        mfe_changed = cur_mfe is None or mark_pnl > cur_mfe
        self.conn.execute(
            "UPDATE paper_positions SET "
            "  last_mark_ts_ns = ?, mark_pnl = ?, held_to_expiry_pnl = ?, "
            "  max_adverse_pnl = ?, max_favorable_pnl = ?, "
            "  max_adverse_ts_ns = COALESCE(?, max_adverse_ts_ns), "
            "  max_favorable_ts_ns = COALESCE(?, max_favorable_ts_ns) "
            "WHERE id = ?",
            (
                ts, mark_pnl, held_pnl,
                new_mae, new_mfe,
                ts if mae_changed else None,
                ts if mfe_changed else None,
                pos_id,
            ),
        )

    def close_paper_position(
        self,
        pos_id: int,
        close_poly_price: float,
        close_kalshi_price: float,
        reason: str = "manual",
    ) -> dict | None:
        row = self.conn.execute(
            "SELECT entry_poly_price, entry_kalshi_price, entry_fees, size "
            "FROM paper_positions WHERE id = ? AND closed_ts_ns IS NULL",
            (pos_id,),
        ).fetchone()
        if not row:
            return None
        e_poly, e_kalshi, fees, size = row
        liquidation = close_poly_price + close_kalshi_price
        entry_cost = e_poly + e_kalshi
        pnl_per = liquidation - entry_cost - (fees or 0.0)
        pnl = pnl_per * size
        self.conn.execute(
            "UPDATE paper_positions SET closed_ts_ns = ?, close_poly_price = ?, "
            "close_kalshi_price = ?, close_pnl = ?, close_reason = ? WHERE id = ?",
            (self._now_ns(), close_poly_price, close_kalshi_price, pnl, reason, pos_id),
        )
        return {"id": pos_id, "pnl": pnl, "pnl_per_contract": pnl_per, "reason": reason}

    # ------------------------------------------------------------------ pair resolution

    def upsert_pair_resolution_meta(
        self,
        pair_name: str,
        poly_close_ts_ns: int | None = None,
        kalshi_close_ts_ns: int | None = None,
        poly_condition_id: str | None = None,
    ) -> None:
        """Insert or update the resolution metadata row for a pair.

        Uses COALESCE so callers can update a subset of fields without
        clobbering previously-cached values.
        """
        self.conn.execute(
            "INSERT INTO pair_resolution "
            "(pair_name, poly_close_ts_ns, kalshi_close_ts_ns, poly_condition_id) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(pair_name) DO UPDATE SET "
            "  poly_close_ts_ns   = COALESCE(excluded.poly_close_ts_ns,   poly_close_ts_ns), "
            "  kalshi_close_ts_ns = COALESCE(excluded.kalshi_close_ts_ns, kalshi_close_ts_ns), "
            "  poly_condition_id  = COALESCE(excluded.poly_condition_id,  poly_condition_id)",
            (pair_name, poly_close_ts_ns, kalshi_close_ts_ns, poly_condition_id),
        )

    def record_pair_result(
        self,
        pair_name: str,
        venue: str,
        result: str | None,
        observed_ts_ns: int | None = None,
        last_error: str | None = None,
    ) -> None:
        """Record an observed result ('yes'/'no'/'voided') for one venue.

        venue must be 'poly' or 'kalshi'. Result of None just bumps last_check_ts_ns
        and (optionally) last_error.
        """
        if venue not in ("poly", "kalshi"):
            raise ValueError(f"venue must be 'poly' or 'kalshi', got {venue}")
        col = f"{venue}_result"
        ts_col = f"{venue}_result_ts_ns"
        observed = observed_ts_ns if (observed_ts_ns is not None and result is not None) else None
        # Ensure row exists
        self.conn.execute(
            "INSERT OR IGNORE INTO pair_resolution (pair_name) VALUES (?)",
            (pair_name,),
        )
        if result is not None:
            self.conn.execute(
                f"UPDATE pair_resolution SET {col} = ?, {ts_col} = ?, "
                f"last_check_ts_ns = ?, last_error = ? WHERE pair_name = ?",
                (result, observed, self._now_ns(), last_error, pair_name),
            )
        else:
            self.conn.execute(
                "UPDATE pair_resolution SET last_check_ts_ns = ?, last_error = ? WHERE pair_name = ?",
                (self._now_ns(), last_error, pair_name),
            )

    def get_pair_resolution(self, pair_name: str) -> dict | None:
        row = self.conn.execute(
            "SELECT pair_name, poly_close_ts_ns, kalshi_close_ts_ns, "
            "       poly_result, kalshi_result, poly_result_ts_ns, kalshi_result_ts_ns, "
            "       last_check_ts_ns, last_error, poly_condition_id "
            "FROM pair_resolution WHERE pair_name = ?",
            (pair_name,),
        ).fetchone()
        if not row:
            return None
        keys = ["pair_name", "poly_close_ts_ns", "kalshi_close_ts_ns",
                "poly_result", "kalshi_result", "poly_result_ts_ns", "kalshi_result_ts_ns",
                "last_check_ts_ns", "last_error", "poly_condition_id"]
        return dict(zip(keys, row))

    def list_pair_resolutions(self) -> list[dict]:
        keys = ["pair_name", "poly_close_ts_ns", "kalshi_close_ts_ns",
                "poly_result", "kalshi_result", "poly_result_ts_ns", "kalshi_result_ts_ns",
                "last_check_ts_ns", "last_error", "poly_condition_id"]
        out = []
        for row in self.conn.execute(
            "SELECT pair_name, poly_close_ts_ns, kalshi_close_ts_ns, "
            "       poly_result, kalshi_result, poly_result_ts_ns, kalshi_result_ts_ns, "
            "       last_check_ts_ns, last_error, poly_condition_id "
            "FROM pair_resolution"
        ):
            out.append(dict(zip(keys, row)))
        return out

    def resolve_paper_position(
        self,
        pos_id: int,
        poly_result: str,
        kalshi_result: str,
    ) -> dict | None:
        """Close a paper position based on per-venue binary resolutions.

        For Direction A (poly_yes_kalshi_no): position holds Poly YES + Kalshi NO.
          poly leg pays $1 iff poly_result == 'yes'
          kalshi leg pays $1 iff kalshi_result == 'no'

        For Direction B (kalshi_yes_poly_no): position holds Kalshi YES + Poly NO.
          kalshi leg pays $1 iff kalshi_result == 'yes'
          poly leg pays $1 iff poly_result == 'no'

        Voided/null results: that leg refunds the price paid (entry price for the
        leg), modeling a voided market.

        Returns None if the position is already closed.
        """
        row = self.conn.execute(
            "SELECT direction, entry_poly_price, entry_kalshi_price, entry_fees, size "
            "FROM paper_positions WHERE id = ? AND closed_ts_ns IS NULL",
            (pos_id,),
        ).fetchone()
        if not row:
            return None
        direction, entry_poly, entry_kalshi, fees, size = row
        fees = fees or 0.0

        def leg_payout(side: str, venue_result: str, entry_price: float) -> float:
            # side is 'yes' or 'no' (which side of the binary this leg holds)
            if venue_result == "voided" or venue_result is None:
                return float(entry_price)  # refund leg cost
            if venue_result == side:
                return 1.0
            return 0.0

        if direction == "poly_yes_kalshi_no":
            poly_leg_pay = leg_payout("yes", poly_result, entry_poly)
            kalshi_leg_pay = leg_payout("no", kalshi_result, entry_kalshi)
        elif direction == "kalshi_yes_poly_no":
            poly_leg_pay = leg_payout("no", poly_result, entry_poly)
            kalshi_leg_pay = leg_payout("yes", kalshi_result, entry_kalshi)
        else:
            return None

        total_payout = poly_leg_pay + kalshi_leg_pay
        entry_cost = entry_poly + entry_kalshi
        pnl_per = total_payout - entry_cost - fees
        pnl = pnl_per * size

        self.conn.execute(
            "UPDATE paper_positions SET "
            "  closed_ts_ns = ?, "
            "  close_poly_price = ?, close_kalshi_price = ?, "
            "  close_pnl = ?, close_reason = ?, "
            "  resolution_poly = ?, resolution_kalshi = ? "
            "WHERE id = ?",
            (
                self._now_ns(),
                poly_leg_pay, kalshi_leg_pay,
                pnl, "resolved",
                poly_result, kalshi_result,
                pos_id,
            ),
        )
        return {
            "id": pos_id,
            "pnl": pnl,
            "pnl_per_contract": pnl_per,
            "poly_leg_pay": poly_leg_pay,
            "kalshi_leg_pay": kalshi_leg_pay,
        }

    def close(self) -> None:
        self.conn.close()
