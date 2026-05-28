# Depth-Walking Fills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace top-of-book paper fills with depth-walked VWAP fills, so paper PnL realistically reflects slippage through 2-3 book levels — the single biggest paper→real leakage source.

**Architecture:** The in-memory book state in `analytics/book.py` already maintains full L2 (price→size dicts per side). Currently the arb detector only reads top-of-book from sqlite via `Store.latest_quote()`. We plumb the live book-state instances through `scripts/run_feeds.py` to the arb evaluator, add a pure `walk_levels()` primitive, and route VWAP fills into both `arb.evaluate()` and `analytics/paper.py` for entry, mark, and exit. Schema gets additive columns (`fill_vwap_*`, `levels_consumed_*`) so we can analyze slippage retrospectively. Everything is gated by `FILL_DEPTH_ENABLED=1` env var so the live system can revert instantly.

**Tech Stack:** Python 3.14, asyncio, sqlite3 (stdlib), websockets, pytest (added by this plan).

**CRITICAL — Live system caveat:** A paper trader with 8 open positions is currently running (PID 2800). The mark/exit changes in Tasks 8-9 will cause a one-time MTM revaluation on existing positions when the new code is loaded, likely making marks 1-3¢/c worse. The `FILL_DEPTH_ENABLED` gate lets the user run new code in "entry-only" mode first if desired. Do not restart the live process during implementation — that's Task 10 with explicit user approval.

**Tradeoffs accepted:**
- Single in-memory book state shared between writer (WS task) and reader (arb task). Same event loop, no real race, but readers snapshot to list before walking to be defensive against future threading changes.
- `kalshi_rest.py` does NOT populate book state in this plan (REST only ships top-5 depth and is rarely used since WS auth works). Documented in handoff at end.
- No backward-compat for old `entry_poly_price` semantics — when depth mode is on, that field now stores the fill VWAP, not top-of-book ask. Old rows already in DB are not migrated; analytics scripts can join on `opened_ts_ns < DEPTH_CUTOVER_TS` if needed.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `requirements-dev.txt` | Create | pytest dev dep |
| `tests/__init__.py` | Create | Test package marker |
| `tests/conftest.py` | Create | pytest config + sys.path injection |
| `tests/test_depth.py` | Create | Unit tests for depth-walking primitive |
| `tests/test_book.py` | Create | Unit tests for book-state level accessors |
| `tests/test_arb_depth.py` | Create | Integration tests: arb.evaluate() with depth dicts |
| `tests/test_paper_depth.py` | Create | Integration tests: paper trader fills via depth |
| `analytics/depth.py` | Create | Pure `walk_levels(levels, qty, side)` primitive |
| `analytics/book.py` | Modify | Add `get_levels(asset, side)` snapshot accessors to both book classes |
| `analytics/arb.py` | Modify | `PairQuote` gains depth dict fields; `evaluate()` reads `FILL_SIZE_CONTRACTS` env and computes fill-VWAP cost when depth available; passes through `fill_vwap_*` and `levels_consumed_*` |
| `analytics/paper.py` | Modify | Entry consumes `fill_vwap_*` from cand; mark/exit use new helper that depth-walks bid side; `FILL_DEPTH_ENABLED` gate |
| `feeds/polymarket_ws.py` | Modify | Accept optional `book_state` arg (instantiates if None) so caller can share instance |
| `feeds/kalshi_ws.py` | Modify | Same — accept optional `book_state` |
| `scripts/run_feeds.py` | Modify | Instantiate single `PolymarketBookState` + `KalshiBookState`; pass to WS feeds AND to `build_pair_quote_factory` (which now populates depth dicts on `PairQuote`) |
| `store/sqlite.py` | Modify | Idempotent ALTER TABLE for new columns on `arb_candidates` + `paper_positions`; update `record_arb_candidate()` and `open_paper_position()` signatures |

---

## Task 1: Test infrastructure

**Files:**
- Create: `requirements-dev.txt`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_smoke.py`

- [ ] **Step 1: Create requirements-dev.txt**

```
pytest>=8.0
```

- [ ] **Step 2: Install dev deps**

Run: `.venv/bin/pip install -r requirements-dev.txt`
Expected: `Successfully installed pytest-...`

- [ ] **Step 3: Create tests/__init__.py**

Empty file.

- [ ] **Step 4: Create tests/conftest.py**

```python
"""Pytest configuration. Ensures project root is on sys.path so tests can
import top-level packages (analytics, feeds, store)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

- [ ] **Step 5: Create tests/test_smoke.py**

```python
def test_smoke():
    assert 1 + 1 == 2


def test_can_import_analytics():
    from analytics import arb, book, paper  # noqa: F401
```

- [ ] **Step 6: Run smoke tests**

Run: `.venv/bin/python -m pytest tests/test_smoke.py -v`
Expected: 2 passed.

- [ ] **Step 7: Commit**

```bash
git add requirements-dev.txt tests/__init__.py tests/conftest.py tests/test_smoke.py
git commit -m "test: bootstrap pytest infrastructure"
```

---

## Task 2: Depth-walking primitive

**Files:**
- Create: `analytics/depth.py`
- Create: `tests/test_depth.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_depth.py`:

```python
import pytest

from analytics.depth import walk_levels


def test_walk_ask_empty_book_returns_no_fill():
    vwap, filled, levels_used = walk_levels({}, qty=10.0, side="ask")
    assert vwap is None
    assert filled == 0.0
    assert levels_used == 0


def test_walk_ask_single_level_partial_fill():
    levels = {0.50: 100.0}
    vwap, filled, levels_used = walk_levels(levels, qty=50.0, side="ask")
    assert vwap == pytest.approx(0.50)
    assert filled == pytest.approx(50.0)
    assert levels_used == 1


def test_walk_ask_walks_ascending_through_multiple_levels():
    # ask side: walk from cheapest price up
    levels = {0.52: 100.0, 0.50: 30.0, 0.51: 50.0}
    vwap, filled, levels_used = walk_levels(levels, qty=100.0, side="ask")
    # fills: 30 @ 0.50 + 50 @ 0.51 + 20 @ 0.52 = 15.0 + 25.5 + 10.4 = 50.9
    assert filled == pytest.approx(100.0)
    assert vwap == pytest.approx(50.9 / 100.0)
    assert levels_used == 3


def test_walk_ask_runs_out_of_liquidity_returns_partial():
    levels = {0.50: 20.0}
    vwap, filled, levels_used = walk_levels(levels, qty=100.0, side="ask")
    assert filled == pytest.approx(20.0)
    assert vwap == pytest.approx(0.50)
    assert levels_used == 1


def test_walk_bid_walks_descending_from_top():
    # bid side: walk from highest price down
    levels = {0.53: 100.0, 0.55: 30.0, 0.54: 50.0}
    vwap, filled, levels_used = walk_levels(levels, qty=100.0, side="bid")
    # fills: 30 @ 0.55 + 50 @ 0.54 + 20 @ 0.53 = 16.5 + 27.0 + 10.6 = 54.1
    assert filled == pytest.approx(100.0)
    assert vwap == pytest.approx(54.1 / 100.0)
    assert levels_used == 3


def test_walk_invalid_side_raises():
    with pytest.raises(ValueError):
        walk_levels({0.50: 10.0}, qty=5.0, side="middle")


def test_walk_zero_qty_returns_no_fill():
    levels = {0.50: 100.0}
    vwap, filled, levels_used = walk_levels(levels, qty=0.0, side="ask")
    assert vwap is None
    assert filled == 0.0
    assert levels_used == 0
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `.venv/bin/python -m pytest tests/test_depth.py -v`
Expected: `ModuleNotFoundError: No module named 'analytics.depth'` (collection error).

- [ ] **Step 3: Create analytics/depth.py**

```python
"""Depth-walking primitive.

Given a {price: size} dict for one side of a book and a target quantity,
compute the fill VWAP, filled quantity, and number of levels consumed.

Used by:
  - analytics.arb to compute realistic-fill edge instead of top-of-book
  - analytics.paper to compute liquidation VWAP for mark/exit

Pure function. No I/O. No side effects.
"""
from __future__ import annotations


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
    sorted_levels = sorted(levels.items(), key=lambda kv: kv[0], reverse=(side == "bid"))

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
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `.venv/bin/python -m pytest tests/test_depth.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add analytics/depth.py tests/test_depth.py
git commit -m "feat(analytics): add walk_levels depth-walking primitive"
```

---

## Task 3: Book-state level accessors

**Files:**
- Modify: `analytics/book.py`
- Create: `tests/test_book.py`

- [ ] **Step 1: Write failing tests**

`tests/test_book.py`:

```python
from analytics.book import KalshiBookState, PolymarketBookState


def test_polymarket_get_levels_returns_snapshot_copy():
    bs = PolymarketBookState()
    bs.apply({
        "asset_id": "A1",
        "event_type": "book",
        "bids": [{"price": "0.50", "size": "100"}, {"price": "0.49", "size": "50"}],
        "asks": [{"price": "0.52", "size": "30"}, {"price": "0.53", "size": "80"}],
    })
    asks = bs.get_levels("A1", "asks")
    bids = bs.get_levels("A1", "bids")
    assert asks == {0.52: 30.0, 0.53: 80.0}
    assert bids == {0.50: 100.0, 0.49: 50.0}
    # Mutate the returned snapshot — must not affect internal state
    asks[0.52] = 9999.0
    assert bs.get_levels("A1", "asks")[0.52] == 30.0


def test_polymarket_get_levels_unknown_asset_returns_empty():
    bs = PolymarketBookState()
    assert bs.get_levels("nope", "asks") == {}
    assert bs.get_levels("nope", "bids") == {}


def test_kalshi_get_levels_yes_and_no_sides():
    bs = KalshiBookState()
    bs.apply({
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": "T1",
            "yes": [[50, 100], [49, 50]],
            "no":  [[48, 200], [47, 75]],
        },
    })
    yes_levels = bs.get_levels("T1", "yes")
    no_levels = bs.get_levels("T1", "no")
    assert yes_levels == {0.50: 100.0, 0.49: 50.0}
    assert no_levels == {0.48: 200.0, 0.47: 75.0}


def test_kalshi_get_levels_unknown_ticker_returns_empty():
    bs = KalshiBookState()
    assert bs.get_levels("nope", "yes") == {}
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `.venv/bin/python -m pytest tests/test_book.py -v`
Expected: `AttributeError: 'PolymarketBookState' object has no attribute 'get_levels'`.

- [ ] **Step 3: Add get_levels to PolymarketBookState**

Edit `analytics/book.py`. After the `apply` method of `PolymarketBookState` (before the class ends, around line 64), add:

```python
    def get_levels(self, asset_id: str, side: str) -> dict[float, float]:
        """Return a shallow copy of {price: size} for the requested side.

        side: 'bids' or 'asks'. Returns {} for unknown asset or empty side.
        Copy is defensive: callers can mutate without affecting internal state.
        """
        if side not in ("bids", "asks"):
            raise ValueError(f"side must be 'bids' or 'asks', got {side!r}")
        book = self.books.get(asset_id)
        if not book:
            return {}
        return dict(book.get(side, {}))
```

- [ ] **Step 4: Add get_levels to KalshiBookState**

Edit `analytics/book.py`. After the `apply` method of `KalshiBookState` (end of file, after line 151), add:

```python
    def get_levels(self, ticker: str, side: str) -> dict[float, float]:
        """Return a shallow copy of {price_dollars: size} for the requested side.

        side: 'yes' or 'no' — both store BID-side liquidity for that outcome.
        """
        if side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no', got {side!r}")
        book = self.books.get(ticker)
        if not book:
            return {}
        return dict(book.get(side, {}))
```

- [ ] **Step 5: Run tests to confirm pass**

Run: `.venv/bin/python -m pytest tests/test_book.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add analytics/book.py tests/test_book.py
git commit -m "feat(book): expose get_levels snapshot accessors"
```

---

## Task 4: Add depth fields to PairQuote

**Files:**
- Modify: `analytics/arb.py:61-93` (the `PairQuote` dataclass)

- [ ] **Step 1: Write a smoke test in tests/test_arb_depth.py**

Create `tests/test_arb_depth.py`:

```python
from analytics.arb import PairQuote


def test_pairquote_accepts_depth_fields_default_none():
    pq = PairQuote(pair_name="x")
    assert pq.poly_yes_asks is None
    assert pq.poly_no_asks is None
    assert pq.kalshi_yes_bids is None
    assert pq.kalshi_no_bids is None


def test_pairquote_populates_depth_fields():
    pq = PairQuote(
        pair_name="x",
        poly_yes_asks={0.50: 100.0},
        poly_no_asks={0.48: 200.0},
        kalshi_yes_bids={0.49: 50.0},
        kalshi_no_bids={0.51: 75.0},
    )
    assert pq.poly_yes_asks == {0.50: 100.0}
    assert pq.kalshi_no_bids == {0.51: 75.0}
```

- [ ] **Step 2: Run test to confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_arb_depth.py -v`
Expected: `TypeError: PairQuote.__init__() got an unexpected keyword argument 'poly_yes_asks'`.

- [ ] **Step 3: Extend PairQuote with depth fields**

Edit `analytics/arb.py`. Replace the existing `PairQuote` dataclass (lines 61-93) with:

```python
@dataclass
class PairQuote:
    pair_name: str
    poly_yes_bid: float | None = None
    poly_yes_ask: float | None = None
    poly_yes_ts_ns: int | None = None
    poly_no_bid: float | None = None
    poly_no_ask: float | None = None
    poly_no_ts_ns: int | None = None
    kalshi_yes_bid: float | None = None
    kalshi_yes_ask: float | None = None
    kalshi_ts_ns: int | None = None
    kalshi_ticker: str | None = None

    # Depth dicts: {price: size}. None means caller didn't supply L2 (use
    # top-of-book fallback). Polymarket exposes bids+asks directly per asset.
    # Kalshi books store BID-side liquidity on both yes and no — the YES ask
    # depth is reconstructed from the NO bid depth (price flipped to 1 - p).
    poly_yes_bids: dict[float, float] | None = None
    poly_yes_asks: dict[float, float] | None = None
    poly_no_bids: dict[float, float] | None = None
    poly_no_asks: dict[float, float] | None = None
    kalshi_yes_bids: dict[float, float] | None = None
    kalshi_no_bids: dict[float, float] | None = None

    def is_stale(self, now_ns: int, max_age_s: float = MAX_QUOTE_AGE_S) -> dict[str, bool]:
        cutoff = now_ns - int(max_age_s * 1e9)
        return {
            "poly_yes": self.poly_yes_ts_ns is None or self.poly_yes_ts_ns < cutoff,
            "poly_no":  self.poly_no_ts_ns  is None or self.poly_no_ts_ns  < cutoff,
            "kalshi":   self.kalshi_ts_ns   is None or self.kalshi_ts_ns   < cutoff,
        }

    @property
    def kalshi_no_bid(self) -> float | None:
        if self.kalshi_yes_ask is None:
            return None
        return 1.0 - self.kalshi_yes_ask

    @property
    def kalshi_no_ask(self) -> float | None:
        if self.kalshi_yes_bid is None:
            return None
        return 1.0 - self.kalshi_yes_bid

    @property
    def kalshi_yes_asks(self) -> dict[float, float] | None:
        """Reconstructed YES-ask depth: each NO bid at price p becomes
        a YES ask at price (1 - p) with the same size."""
        if self.kalshi_no_bids is None:
            return None
        return {1.0 - p: s for p, s in self.kalshi_no_bids.items()}

    @property
    def kalshi_no_asks(self) -> dict[float, float] | None:
        if self.kalshi_yes_bids is None:
            return None
        return {1.0 - p: s for p, s in self.kalshi_yes_bids.items()}
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `.venv/bin/python -m pytest tests/test_arb_depth.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add analytics/arb.py tests/test_arb_depth.py
git commit -m "feat(arb): add depth dict fields to PairQuote"
```

---

## Task 5: evaluate() consumes depth and computes fill-VWAP edges

**Files:**
- Modify: `analytics/arb.py` (the `evaluate` function around line 96-161, plus add `FILL_SIZE_CONTRACTS` env constant near other constants)

- [ ] **Step 1: Append tests to tests/test_arb_depth.py**

Add to `tests/test_arb_depth.py`:

```python
import pytest

from analytics.arb import PairQuote, evaluate


def test_evaluate_without_depth_falls_back_to_top_of_book(monkeypatch):
    """Backward compat: when depth dicts are None, behave like before —
    use top-of-book ask, no fill_vwap / levels_consumed fields."""
    monkeypatch.setenv("FILL_DEPTH_ENABLED", "1")  # gate is on but no depth → fallback
    monkeypatch.setenv("FILL_SIZE_CONTRACTS", "100")
    pq = PairQuote(
        pair_name="x",
        poly_yes_ask=0.50, poly_yes_ts_ns=1_000_000_000,
        kalshi_yes_bid=0.55, kalshi_yes_ask=0.56, kalshi_ts_ns=1_000_000_000,
    )
    cands = evaluate(pq, now_ns=2_000_000_000)
    a = next(c for c in cands if c["direction"] == "poly_yes_kalshi_no")
    # kalshi_no_ask = 1 - kalshi_yes_bid = 0.45; cost = 0.50 + 0.45 = 0.95
    assert a["cost"] == pytest.approx(0.95)
    assert a["fill_vwap_poly"] == pytest.approx(0.50)
    assert a["fill_vwap_kalshi"] == pytest.approx(0.45)
    assert a["levels_consumed_poly"] == 0
    assert a["levels_consumed_kalshi"] == 0


def test_evaluate_with_depth_walks_book_and_changes_cost(monkeypatch):
    monkeypatch.setenv("FILL_DEPTH_ENABLED", "1")
    monkeypatch.setenv("FILL_SIZE_CONTRACTS", "100")
    pq = PairQuote(
        pair_name="x",
        poly_yes_ask=0.50, poly_yes_ts_ns=1_000_000_000,
        poly_yes_asks={0.50: 30.0, 0.51: 50.0, 0.52: 100.0},  # walked VWAP for 100 = 0.509
        kalshi_yes_bid=0.55, kalshi_yes_ask=0.56, kalshi_ts_ns=1_000_000_000,
        kalshi_yes_bids={0.55: 40.0, 0.54: 100.0},  # NO ask = 1 - YES bid; walk YES bids to get NO ask VWAP
    )
    cands = evaluate(pq, now_ns=2_000_000_000)
    a = next(c for c in cands if c["direction"] == "poly_yes_kalshi_no")
    # Poly YES ask VWAP @ 100 = (30*0.50 + 50*0.51 + 20*0.52) / 100 = 50.9 / 100 = 0.509
    assert a["fill_vwap_poly"] == pytest.approx(0.509)
    assert a["levels_consumed_poly"] == 3
    # Kalshi NO ask = 1 - kalshi_yes_bid; ask VWAP @ 100 from YES-bid depth
    # YES bids: 0.55:40, 0.54:100 (walk descending — already top-of-book is 0.55)
    # NO asks: 0.45:40, 0.46:100 (walk ascending) — VWAP @ 100 = (40*0.45 + 60*0.46) / 100 = 0.456
    assert a["fill_vwap_kalshi"] == pytest.approx(0.456)
    assert a["levels_consumed_kalshi"] == 2
    # Cost is depth-walked sum
    assert a["cost"] == pytest.approx(0.509 + 0.456)


def test_evaluate_depth_disabled_ignores_depth_dicts(monkeypatch):
    monkeypatch.setenv("FILL_DEPTH_ENABLED", "0")
    pq = PairQuote(
        pair_name="x",
        poly_yes_ask=0.50, poly_yes_ts_ns=1_000_000_000,
        poly_yes_asks={0.50: 5.0, 0.99: 1000.0},  # depth would push cost way up
        kalshi_yes_bid=0.55, kalshi_yes_ask=0.56, kalshi_ts_ns=1_000_000_000,
    )
    cands = evaluate(pq, now_ns=2_000_000_000)
    a = next(c for c in cands if c["direction"] == "poly_yes_kalshi_no")
    # With gate off, should match old behavior: top-of-book ask
    assert a["fill_vwap_poly"] == pytest.approx(0.50)
    assert a["levels_consumed_poly"] == 0


def test_evaluate_insufficient_depth_returns_partial_marker(monkeypatch):
    """When walked-fill quantity < requested size, mark candidate as partial.
    We still emit so the user can see it, but add a flag so paper trader can
    refuse if FILL_REQUIRE_FULL=1."""
    monkeypatch.setenv("FILL_DEPTH_ENABLED", "1")
    monkeypatch.setenv("FILL_SIZE_CONTRACTS", "100")
    pq = PairQuote(
        pair_name="x",
        poly_yes_ask=0.50, poly_yes_ts_ns=1_000_000_000,
        poly_yes_asks={0.50: 10.0},  # only 10 contracts available, want 100
        kalshi_yes_bid=0.55, kalshi_yes_ask=0.56, kalshi_ts_ns=1_000_000_000,
        kalshi_yes_bids={0.55: 200.0},
    )
    cands = evaluate(pq, now_ns=2_000_000_000)
    a = next(c for c in cands if c["direction"] == "poly_yes_kalshi_no")
    assert a["fill_qty_poly"] == pytest.approx(10.0)
    assert a["fill_qty_kalshi"] == pytest.approx(100.0)
    assert a["partial_fill"] is True
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `.venv/bin/python -m pytest tests/test_arb_depth.py -v`
Expected: 4 failures (KeyErrors on new dict fields).

- [ ] **Step 3: Add FILL_* env constants and update evaluate()**

Edit `analytics/arb.py`. Add near the top with other constants (after line 45):

```python
import os as _os

# Depth-walked fill behavior. When enabled, evaluate() walks the supplied
# depth dicts on each leg up to FILL_SIZE_CONTRACTS contracts and uses the
# resulting VWAP as the fill price (instead of top-of-book ask). When
# depth dicts are absent or the gate is off, falls back to top-of-book.
FILL_DEPTH_ENABLED = _os.environ.get("FILL_DEPTH_ENABLED", "0") not in ("0", "false", "False", "")
FILL_SIZE_CONTRACTS = float(_os.environ.get("FILL_SIZE_CONTRACTS", "100"))
```

Replace the `evaluate` function (lines 96-161) with:

```python
def evaluate(pq: PairQuote, now_ns: int | None = None) -> list[dict]:
    """Return list of candidate trades with edge breakdown.

    Skips any direction whose required quote leg is stale (older than
    MAX_QUOTE_AGE_S). When FILL_DEPTH_ENABLED and depth dicts are present
    on pq, fills are computed by walking up to FILL_SIZE_CONTRACTS contracts
    of the relevant side and using the VWAP as the leg price. Otherwise
    falls back to top-of-book ask.

    Each candidate dict now also contains:
      fill_vwap_poly, fill_vwap_kalshi : float — actual per-contract fill price
      fill_qty_poly, fill_qty_kalshi   : float — contracts that would be filled
      levels_consumed_poly, levels_consumed_kalshi : int — book levels walked
                                                          (0 = top-of-book fallback)
      partial_fill : bool — True if either leg's filled qty < requested
    """
    from analytics.depth import walk_levels

    import time as _time
    if now_ns is None:
        now_ns = _time.time_ns()
    stale = pq.is_stale(now_ns)
    results: list[dict] = []

    def _resolve_leg(top_of_book: float | None, depth: dict | None, side: str):
        """Return (price_used, fill_qty, levels_consumed) for one leg."""
        if FILL_DEPTH_ENABLED and depth:
            vwap, filled, levels_used = walk_levels(depth, FILL_SIZE_CONTRACTS, side)
            if vwap is not None:
                return (vwap, filled, levels_used)
        # Fallback: top-of-book, full requested qty assumed
        if top_of_book is None:
            return (None, 0.0, 0)
        return (top_of_book, FILL_SIZE_CONTRACTS, 0)

    # Direction A: buy YES on Poly, buy NO on Kalshi
    if (
        pq.poly_yes_ask is not None
        and pq.kalshi_no_ask is not None
        and not stale["poly_yes"]
        and not stale["kalshi"]
    ):
        poly_vwap, poly_qty, poly_levels = _resolve_leg(pq.poly_yes_ask, pq.poly_yes_asks, "ask")
        kalshi_vwap, kalshi_qty, kalshi_levels = _resolve_leg(pq.kalshi_no_ask, pq.kalshi_no_asks, "ask")
        if poly_vwap is not None and kalshi_vwap is not None:
            cost = poly_vwap + kalshi_vwap
            gross = 1.0 - cost
            fees = poly_fee(poly_vwap) + kalshi_fee(kalshi_vwap)
            net = gross - fees
            partial = (poly_qty < FILL_SIZE_CONTRACTS) or (kalshi_qty < FILL_SIZE_CONTRACTS)
            results.append({
                "direction": "poly_yes_kalshi_no",
                "gross_edge": gross,
                "net_edge": net,
                "cost": cost,
                "poly_leg_price": poly_vwap,
                "kalshi_leg_price": kalshi_vwap,
                "fees": fees,
                "fill_vwap_poly": poly_vwap,
                "fill_vwap_kalshi": kalshi_vwap,
                "fill_qty_poly": poly_qty,
                "fill_qty_kalshi": kalshi_qty,
                "levels_consumed_poly": poly_levels,
                "levels_consumed_kalshi": kalshi_levels,
                "partial_fill": partial,
            })

    # Direction B: buy YES on Kalshi, buy NO on Poly
    if (
        pq.kalshi_yes_ask is not None
        and pq.poly_no_ask is not None
        and not stale["poly_no"]
        and not stale["kalshi"]
    ):
        poly_vwap, poly_qty, poly_levels = _resolve_leg(pq.poly_no_ask, pq.poly_no_asks, "ask")
        kalshi_vwap, kalshi_qty, kalshi_levels = _resolve_leg(pq.kalshi_yes_ask, pq.kalshi_yes_asks, "ask")
        if poly_vwap is not None and kalshi_vwap is not None:
            cost = poly_vwap + kalshi_vwap
            gross = 1.0 - cost
            fees = poly_fee(poly_vwap) + kalshi_fee(kalshi_vwap)
            net = gross - fees
            partial = (poly_qty < FILL_SIZE_CONTRACTS) or (kalshi_qty < FILL_SIZE_CONTRACTS)
            results.append({
                "direction": "kalshi_yes_poly_no",
                "gross_edge": gross,
                "net_edge": net,
                "cost": cost,
                "poly_leg_price": poly_vwap,
                "kalshi_leg_price": kalshi_vwap,
                "fees": fees,
                "fill_vwap_poly": poly_vwap,
                "fill_vwap_kalshi": kalshi_vwap,
                "fill_qty_poly": poly_qty,
                "fill_qty_kalshi": kalshi_qty,
                "levels_consumed_poly": poly_levels,
                "levels_consumed_kalshi": kalshi_levels,
                "partial_fill": partial,
            })

    return results
```

- [ ] **Step 4: Run all tests**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: all tests pass (smoke + depth + book + arb_depth).

- [ ] **Step 5: Commit**

```bash
git add analytics/arb.py tests/test_arb_depth.py
git commit -m "feat(arb): evaluate() walks book depth for fill VWAP"
```

---

## Task 6: Plumb book state through run_feeds

**Files:**
- Modify: `feeds/polymarket_ws.py:15-52` (signature of `run`)
- Modify: `feeds/kalshi_ws.py:53-120` (signature of `run`)
- Modify: `scripts/run_feeds.py:33-117` (factory + main wiring)

- [ ] **Step 1: Modify feeds/polymarket_ws.py to accept shared book_state**

Edit `feeds/polymarket_ws.py`. Change the `run` function signature and body:

```python
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
```

- [ ] **Step 2: Modify feeds/kalshi_ws.py to accept shared book_state**

Edit `feeds/kalshi_ws.py`. Change the `run` function signature (line 53) and remove the internal `book_state = KalshiBookState()` instantiation (line 69), replacing with a guard:

```python
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
```

(Leave everything below that intact — the existing `while True` reconnect loop already uses the local `book_state` variable.)

- [ ] **Step 3: Modify scripts/run_feeds.py to share book state and populate depth in PairQuote**

Edit `scripts/run_feeds.py`. Replace the `build_pair_quote_factory` function (lines 33-52) and the relevant block of `main` (lines 77-104) with:

```python
from analytics.book import KalshiBookState, PolymarketBookState  # noqa: E402


def build_pair_quote_factory(
    pairs: list[MarketPair],
    store: Store,
    poly_book: PolymarketBookState,
    kalshi_book: KalshiBookState,
):
    """Returns a function that snapshots current quotes + depth into PairQuote objects."""

    def _factory():
        out = []
        for p in pairs:
            pq = arb.PairQuote(pair_name=p.name, kalshi_ticker=p.kalshi_yes_ticker)
            if p.polymarket_yes_token:
                bid, ask, ts = store.latest_quote("polymarket", p.polymarket_yes_token)
                pq.poly_yes_bid, pq.poly_yes_ask, pq.poly_yes_ts_ns = bid, ask, ts
                pq.poly_yes_bids = poly_book.get_levels(p.polymarket_yes_token, "bids") or None
                pq.poly_yes_asks = poly_book.get_levels(p.polymarket_yes_token, "asks") or None
            if p.polymarket_no_token:
                bid, ask, ts = store.latest_quote("polymarket", p.polymarket_no_token)
                pq.poly_no_bid, pq.poly_no_ask, pq.poly_no_ts_ns = bid, ask, ts
                pq.poly_no_bids = poly_book.get_levels(p.polymarket_no_token, "bids") or None
                pq.poly_no_asks = poly_book.get_levels(p.polymarket_no_token, "asks") or None
            if p.kalshi_yes_ticker:
                bid, ask, ts = store.latest_quote("kalshi", p.kalshi_yes_ticker)
                pq.kalshi_yes_bid, pq.kalshi_yes_ask, pq.kalshi_ts_ns = bid, ask, ts
                pq.kalshi_yes_bids = kalshi_book.get_levels(p.kalshi_yes_ticker, "yes") or None
                pq.kalshi_no_bids = kalshi_book.get_levels(p.kalshi_yes_ticker, "no") or None
            out.append(pq)
        return out

    return _factory
```

Then in `main()`, replace the task-construction block (around lines 82-104) with:

```python
    paper = PaperTrader(store)
    poly_book = PolymarketBookState()
    kalshi_book = KalshiBookState()
    tasks = [
        asyncio.create_task(
            polymarket_ws.run(poly_tokens, store, book_state=poly_book),
            name="polymarket_ws",
        ),
        asyncio.create_task(
            arb.run(store, build_pair_quote_factory(pairs, store, poly_book, kalshi_book), paper=paper),
            name="arb",
        ),
        asyncio.create_task(resolver.run(pairs, store), name="resolver"),
    ]
    if use_kalshi_ws:
        log.info("Kalshi feed: WebSocket (auth detected)")
        tasks.append(asyncio.create_task(
            kalshi_ws.run(kalshi_tickers, store, book_state=kalshi_book),
            name="kalshi_ws",
        ))
    else:
        # Be explicit about why so people debugging KALSHI_API_KEY_ID = empty issues see it
        kid = os.environ.get("KALSHI_API_KEY_ID")
        pth = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        reason = []
        if not kid:
            reason.append("KALSHI_API_KEY_ID is empty or unset")
        if not pth:
            reason.append("KALSHI_PRIVATE_KEY_PATH is empty or unset")
        log.warning("Kalshi feed: REST polling (no WS — %s; depth-walking disabled for kalshi)",
                    ", ".join(reason) or "auth unset")
        tasks.append(asyncio.create_task(kalshi_rest.run(kalshi_tickers, store), name="kalshi_rest"))
```

- [ ] **Step 4: Smoke-test that imports still work**

Run: `.venv/bin/python -c "from scripts.run_feeds import build_pair_quote_factory; print('ok')"`
Expected: `ok`.

- [ ] **Step 5: Run all tests**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: all pass (no regressions).

- [ ] **Step 6: Commit**

```bash
git add feeds/polymarket_ws.py feeds/kalshi_ws.py scripts/run_feeds.py
git commit -m "feat(run_feeds): share book state between WS feeds and arb evaluator"
```

---

## Task 7: Schema migration for fill-VWAP columns

**Files:**
- Modify: `store/sqlite.py` (idempotent ALTERs around line 99-111, and `record_arb_candidate` ~line 259, and `open_paper_position` ~line 290)

- [ ] **Step 1: Write a failing test in tests/test_store_schema.py**

Create `tests/test_store_schema.py`:

```python
import tempfile
from pathlib import Path

from store.sqlite import Store


def test_schema_includes_new_fill_columns():
    with tempfile.TemporaryDirectory() as td:
        s = Store(path=Path(td) / "t.db")
        cur = s.conn.execute("PRAGMA table_info(arb_candidates)")
        cols = {r[1] for r in cur.fetchall()}
        for c in (
            "fill_vwap_poly", "fill_vwap_kalshi",
            "fill_qty_poly", "fill_qty_kalshi",
            "levels_consumed_poly", "levels_consumed_kalshi",
            "partial_fill",
        ):
            assert c in cols, f"arb_candidates missing column {c}"

        cur = s.conn.execute("PRAGMA table_info(paper_positions)")
        cols = {r[1] for r in cur.fetchall()}
        for c in (
            "entry_fill_vwap_poly", "entry_fill_vwap_kalshi",
            "entry_levels_consumed_poly", "entry_levels_consumed_kalshi",
            "entry_partial_fill",
        ):
            assert c in cols, f"paper_positions missing column {c}"
        s.close()


def test_record_arb_candidate_persists_fill_fields():
    with tempfile.TemporaryDirectory() as td:
        s = Store(path=Path(td) / "t.db")

        class FakePQ:
            pair_name = "x"
            poly_yes_bid = poly_yes_ask = poly_no_bid = poly_no_ask = None
            kalshi_yes_bid = kalshi_yes_ask = None
            kalshi_ticker = "T1"

        cand = {
            "direction": "poly_yes_kalshi_no",
            "gross_edge": 0.05, "net_edge": 0.04, "cost": 0.95,
            "poly_leg_price": 0.50, "kalshi_leg_price": 0.45, "fees": 0.01,
            "fill_vwap_poly": 0.509, "fill_vwap_kalshi": 0.456,
            "fill_qty_poly": 100.0, "fill_qty_kalshi": 100.0,
            "levels_consumed_poly": 3, "levels_consumed_kalshi": 2,
            "partial_fill": False,
        }
        s.record_arb_candidate(FakePQ(), cand)
        row = s.conn.execute(
            "SELECT fill_vwap_poly, fill_vwap_kalshi, levels_consumed_poly, partial_fill "
            "FROM arb_candidates ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == 0.509
        assert row[1] == 0.456
        assert row[2] == 3
        assert row[3] == 0  # sqlite stores bool as int
        s.close()
```

- [ ] **Step 2: Run test to confirm fail**

Run: `.venv/bin/python -m pytest tests/test_store_schema.py -v`
Expected: `AssertionError: arb_candidates missing column fill_vwap_poly` (or similar).

- [ ] **Step 3: Add idempotent ALTERs**

Edit `store/sqlite.py`. Extend the idempotent ALTER block at lines 99-111 (after the existing seven entries, before the closing `):`) by adding these statements to the same tuple:

```python
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
```

- [ ] **Step 4: Update record_arb_candidate to persist new fields**

Edit `store/sqlite.py`. Replace the `record_arb_candidate` method (~line 259-286) with:

```python
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
```

- [ ] **Step 5: Update open_paper_position to accept and persist new fields**

Edit `store/sqlite.py`. Replace the `open_paper_position` method (~line 290-316) with:

```python
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
```

- [ ] **Step 6: Run tests to confirm pass**

Run: `.venv/bin/python -m pytest tests/test_store_schema.py -v`
Expected: 2 passed.

- [ ] **Step 7: Verify idempotent migration on existing live DB (NON-DESTRUCTIVE)**

Run: `.venv/bin/python -c "from store.sqlite import Store; s = Store(); print('ok'); s.close()"`
Expected: `ok` — no exceptions. The live `data/feed.db` now has the new columns (NULL on all old rows).

Verify with: `.venv/bin/python -c "import sqlite3; c=sqlite3.connect('data/feed.db'); print([r[1] for r in c.execute('PRAGMA table_info(paper_positions)').fetchall()])"`
Expected: list of column names includes `entry_fill_vwap_poly`, etc.

- [ ] **Step 8: Commit**

```bash
git add store/sqlite.py tests/test_store_schema.py
git commit -m "feat(store): add fill_vwap and levels_consumed columns"
```

---

## Task 8: Paper trader entry uses fill VWAP

**Files:**
- Modify: `analytics/paper.py:114-162` (the `maybe_enter` method, plus add `FILL_DEPTH_ENABLED` import/gate)

- [ ] **Step 1: Add failing test in tests/test_paper_depth.py**

Create `tests/test_paper_depth.py`:

```python
import os
import tempfile
from pathlib import Path

import pytest

from analytics import paper as paper_mod
from analytics.arb import PairQuote
from analytics.paper import PaperTrader
from store.sqlite import Store


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as td:
        s = Store(path=Path(td) / "t.db")
        yield s
        s.close()


def test_maybe_enter_with_depth_persists_fill_vwap(store, monkeypatch):
    monkeypatch.setenv("FILL_DEPTH_ENABLED", "1")
    monkeypatch.setenv("FILL_SIZE_CONTRACTS", "100")
    monkeypatch.setenv("PAPER_MAX_TTE_DAYS", "0")  # disable TTE filter
    pt = PaperTrader(store, enabled=True)
    pq = PairQuote(pair_name="x")
    cand = {
        "direction": "poly_yes_kalshi_no",
        "net_edge": 0.05,
        "cost": 0.95,
        "poly_leg_price": 0.509,
        "kalshi_leg_price": 0.456,
        "fees": 0.01,
        "fill_vwap_poly": 0.509,
        "fill_vwap_kalshi": 0.456,
        "fill_qty_poly": 100.0,
        "fill_qty_kalshi": 100.0,
        "levels_consumed_poly": 3,
        "levels_consumed_kalshi": 2,
        "partial_fill": False,
    }
    pid = pt.maybe_enter(pq, cand)
    assert pid is not None
    row = store.conn.execute(
        "SELECT entry_fill_vwap_poly, entry_fill_vwap_kalshi, "
        "       entry_levels_consumed_poly, entry_partial_fill, entry_poly_price "
        "FROM paper_positions WHERE id = ?",
        (pid,),
    ).fetchone()
    assert row[0] == pytest.approx(0.509)
    assert row[1] == pytest.approx(0.456)
    assert row[2] == 3
    assert row[3] == 0
    # entry_poly_price also stores the VWAP (legacy field, same semantics now)
    assert row[4] == pytest.approx(0.509)


def test_maybe_enter_rejects_partial_when_required(store, monkeypatch):
    monkeypatch.setenv("FILL_DEPTH_ENABLED", "1")
    monkeypatch.setenv("FILL_REQUIRE_FULL", "1")
    monkeypatch.setenv("PAPER_MAX_TTE_DAYS", "0")
    pt = PaperTrader(store, enabled=True)
    pq = PairQuote(pair_name="x")
    cand = {
        "direction": "poly_yes_kalshi_no",
        "net_edge": 0.05, "cost": 0.95,
        "poly_leg_price": 0.509, "kalshi_leg_price": 0.456, "fees": 0.01,
        "fill_vwap_poly": 0.509, "fill_vwap_kalshi": 0.456,
        "fill_qty_poly": 10.0, "fill_qty_kalshi": 100.0,  # partial on poly
        "levels_consumed_poly": 1, "levels_consumed_kalshi": 2,
        "partial_fill": True,
    }
    pid = pt.maybe_enter(pq, cand)
    assert pid is None
```

- [ ] **Step 2: Run test to confirm fail**

Run: `.venv/bin/python -m pytest tests/test_paper_depth.py::test_maybe_enter_with_depth_persists_fill_vwap -v`
Expected: failure — fill_vwap columns are NULL because maybe_enter doesn't pass them.

- [ ] **Step 3: Update maybe_enter and add FILL_REQUIRE_FULL gate**

Edit `analytics/paper.py`. Add near the other env constants (after line 88):

```python
# When FILL_REQUIRE_FULL=1, refuse to open if either leg's depth-walked
# fill quantity is below the requested size. Use this in real-money mode
# to avoid naked single-leg exposure on illiquid books.
PAPER_FILL_REQUIRE_FULL = os.environ.get("FILL_REQUIRE_FULL", "0") not in ("0", "false", "False", "")
```

Replace the `maybe_enter` method (lines 114-162) with:

```python
    def maybe_enter(self, pq, cand: dict) -> int | None:
        """Open a paper position if criteria met. Returns position id or None."""
        if not self.enabled:
            return None
        # Per-pair loss penalty: pairs with bad track record need a bigger edge
        required_edge = PAPER_MIN_ENTRY_EDGE
        if PAPER_BLACKLIST_LOSS_USD < 0 and PAPER_BLACKLIST_MIN_CLOSES > 0:
            realized, n_closes = self.store.get_pair_realized(pq.pair_name)
            if n_closes >= PAPER_BLACKLIST_MIN_CLOSES and realized < PAPER_BLACKLIST_LOSS_USD:
                required_edge = PAPER_MIN_ENTRY_EDGE * PAPER_BLACKLIST_EDGE_MULT
        if cand["net_edge"] < required_edge:
            return None
        # Reject partial fills when configured (single-leg risk too high)
        if PAPER_FILL_REQUIRE_FULL and cand.get("partial_fill"):
            log.info(
                "PAPER SKIP  %s [%s]  partial_fill (poly_qty=%.1f kalshi_qty=%.1f)",
                pq.pair_name, cand["direction"],
                cand.get("fill_qty_poly", 0.0), cand.get("fill_qty_kalshi", 0.0),
            )
            return None
        key = (pq.pair_name, cand["direction"])
        if key in self._open_keys:
            return None
        # Respect post-exit cooldown
        last_exit = self._last_exit_ts.get(key)
        if last_exit is not None:
            age_s = (time.time_ns() - last_exit) / 1e9
            if age_s < PAPER_REENTRY_COOLDOWN_S:
                return None
        # TTE filter: only enter if resolution is within PAPER_MAX_TTE_DAYS
        if PAPER_MAX_TTE_DAYS > 0:
            meta = self.store.get_pair_resolution(pq.pair_name)
            if meta:
                closes = [t for t in (meta.get("poly_close_ts_ns"), meta.get("kalshi_close_ts_ns")) if t]
                if closes:
                    tte_s = (min(closes) - time.time_ns()) / 1e9
                    if tte_s > PAPER_MAX_TTE_DAYS * 86400:
                        return None
                    if tte_s < 0:
                        return None
        pid = self.store.open_paper_position(
            pair_name=pq.pair_name,
            direction=cand["direction"],
            size=PAPER_SIZE_CONTRACTS,
            entry_poly_price=cand["poly_leg_price"],
            entry_kalshi_price=cand["kalshi_leg_price"],
            entry_net_edge=cand["net_edge"],
            entry_fees=cand["fees"],
            entry_fill_vwap_poly=cand.get("fill_vwap_poly"),
            entry_fill_vwap_kalshi=cand.get("fill_vwap_kalshi"),
            entry_levels_consumed_poly=cand.get("levels_consumed_poly"),
            entry_levels_consumed_kalshi=cand.get("levels_consumed_kalshi"),
            entry_partial_fill=cand.get("partial_fill"),
        )
        self._open_keys.add(key)
        log.info(
            "PAPER OPEN  id=%d  %s [%s]  size=%d  cost/c=%.4f  edge/c=%+.4f  "
            "vwap_poly=%.4f(L%d)  vwap_kalshi=%.4f(L%d)%s",
            pid, pq.pair_name, cand["direction"], PAPER_SIZE_CONTRACTS,
            cand["poly_leg_price"] + cand["kalshi_leg_price"], cand["net_edge"],
            cand.get("fill_vwap_poly") or cand["poly_leg_price"],
            cand.get("levels_consumed_poly") or 0,
            cand.get("fill_vwap_kalshi") or cand["kalshi_leg_price"],
            cand.get("levels_consumed_kalshi") or 0,
            "  PARTIAL" if cand.get("partial_fill") else "",
        )
        return pid
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `.venv/bin/python -m pytest tests/test_paper_depth.py -v`
Expected: 2 passed.

- [ ] **Step 5: Run full suite, no regressions**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add analytics/paper.py tests/test_paper_depth.py
git commit -m "feat(paper): entry uses depth-walked fill VWAP"
```

---

## Task 9: Paper mark/exit use depth-walked bid VWAP

**Files:**
- Modify: `analytics/paper.py:164-309` (mark_all and exit helpers)

- [ ] **Step 1: Add failing test**

Append to `tests/test_paper_depth.py`:

```python
def test_mark_all_uses_depth_walked_bid_when_available(store, monkeypatch):
    monkeypatch.setenv("FILL_DEPTH_ENABLED", "1")
    monkeypatch.setenv("FILL_SIZE_CONTRACTS", "100")
    monkeypatch.setenv("PAPER_MAX_TTE_DAYS", "0")
    pt = PaperTrader(store, enabled=True)
    # Open a position
    pq_entry = PairQuote(pair_name="x")
    cand = {
        "direction": "poly_yes_kalshi_no",
        "net_edge": 0.05, "cost": 0.95,
        "poly_leg_price": 0.50, "kalshi_leg_price": 0.45, "fees": 0.0,
        "fill_vwap_poly": 0.50, "fill_vwap_kalshi": 0.45,
        "fill_qty_poly": 100.0, "fill_qty_kalshi": 100.0,
        "levels_consumed_poly": 1, "levels_consumed_kalshi": 1, "partial_fill": False,
    }
    pid = pt.maybe_enter(pq_entry, cand)
    # Now mark with a different pq that has bid depth — closing leg = bid side
    pq_mark = PairQuote(
        pair_name="x",
        poly_yes_bid=0.51, poly_yes_ask=0.52,
        poly_yes_bids={0.51: 30.0, 0.50: 100.0},  # walk 100 from top: (30*0.51 + 70*0.50)/100 = 0.503
        kalshi_yes_bid=0.49, kalshi_yes_ask=0.50,
        # For direction A we close Kalshi NO; NO bid depth comes from kalshi.kalshi_no_bids
        kalshi_no_bids={0.48: 200.0},  # walk 100: 0.48
    )
    pt.mark_all([pq_mark])
    row = store.conn.execute(
        "SELECT mark_pnl FROM paper_positions WHERE id = ?", (pid,)
    ).fetchone()
    # Liquidation value @ 100 = 0.503 (poly_yes_bid VWAP) + 0.48 (kalshi_no_bid) = 0.983
    # Entry cost = 0.95, fees = 0 → mark/c = 0.033, mark_pnl = 3.30
    assert row[0] == pytest.approx(3.30, abs=0.01)
```

- [ ] **Step 2: Run to confirm fail**

Run: `.venv/bin/python -m pytest tests/test_paper_depth.py::test_mark_all_uses_depth_walked_bid_when_available -v`
Expected: mark_pnl uses top-of-book (0.51 + 0.48 = 0.99 → 4.0), not depth-walked.

- [ ] **Step 3: Add depth-aware exit helper and update mark_all + maybe_exit**

Edit `analytics/paper.py`. Add this helper method to the `PaperTrader` class (place it just before `_exit_leg_prices` around line 164):

```python
    def _compute_exit_legs(self, pq, direction: str, size: float):
        """Return (poly_exit_price, kalshi_exit_price) for closing this position.

        When FILL_DEPTH_ENABLED and the relevant bid-side depth dict is on pq,
        return depth-walked VWAP for `size` contracts. Otherwise top-of-book bid.
        Returns (None, None) if either side has no quote.
        """
        from analytics.arb import FILL_DEPTH_ENABLED
        from analytics.depth import walk_levels

        def _resolve(top_bid: float | None, depth: dict | None) -> float | None:
            if FILL_DEPTH_ENABLED and depth:
                vwap, filled, _ = walk_levels(depth, size, "bid")
                if vwap is not None:
                    return vwap
            return top_bid

        if direction == "poly_yes_kalshi_no":
            poly = _resolve(pq.poly_yes_bid, pq.poly_yes_bids)
            kalshi = _resolve(pq.kalshi_no_bid, pq.kalshi_no_bids)
        elif direction == "kalshi_yes_poly_no":
            poly = _resolve(pq.poly_no_bid, pq.poly_no_bids)
            kalshi = _resolve(pq.kalshi_yes_bid, pq.kalshi_yes_bids)
        else:
            return (None, None)
        return (poly, kalshi)
```

Replace the existing `_exit_leg_prices` method (lines 164-173) with a thin wrapper that uses `_compute_exit_legs` with the configured paper size:

```python
    def _exit_leg_prices(self, pq, direction: str):
        """Return (poly_close, kalshi_close) bid prices to use for closing.

        Walks bid-side depth when available; falls back to top-of-book.
        """
        return self._compute_exit_legs(pq, direction, PAPER_SIZE_CONTRACTS)
```

Replace the `mark_all` method (lines 281-309) with:

```python
    def mark_all(self, pair_quotes) -> None:
        """Update MTM for every open position from current pair quotes.

        Uses depth-walked bid VWAP for liquidation value when available.
        """
        if not self.enabled:
            return
        by_pair = {p.pair_name: p for p in pair_quotes}
        for pos in self.store.list_open_paper_positions():
            pq = by_pair.get(pos["pair_name"])
            if pq is None:
                continue
            poly_exit, kalshi_exit = self._compute_exit_legs(pq, pos["direction"], pos["size"])
            if poly_exit is None or kalshi_exit is None:
                continue
            entry_cost = pos["entry_poly_price"] + pos["entry_kalshi_price"]
            liquidation_value = poly_exit + kalshi_exit
            fees = pos["entry_fees"] or 0.0
            mark_per_c = liquidation_value - entry_cost - fees
            mark_pnl = mark_per_c * pos["size"]
            held_per_c = 1.0 - entry_cost - fees
            held_pnl = held_per_c * pos["size"]
            self.store.update_paper_mark(pos["id"], mark_pnl, held_pnl)
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `.venv/bin/python -m pytest tests/test_paper_depth.py -v`
Expected: 3 passed.

- [ ] **Step 5: Full regression**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add analytics/paper.py tests/test_paper_depth.py
git commit -m "feat(paper): mark and exit use depth-walked bid VWAP"
```

---

## Task 10: Live deployment + verification (REQUIRES USER GO)

**Files:** None modified — operational task.

⚠️ **STOP — this task requires explicit user approval before proceeding.** It restarts the live feeds process and changes behavior on 8 open positions.

- [ ] **Step 1: Show user the plan**

Tell the user:
> Tasks 1-9 are done and tested. Ready to deploy live. This will:
> 1. Kill PID 2800 (current run_feeds)
> 2. Restart with `FILL_DEPTH_ENABLED=1 FILL_SIZE_CONTRACTS=100` in env
> 3. On first arb-detector tick, all 8 open positions get re-marked using depth-walked bid VWAP — likely 1-3¢/c worse than current marks
> 4. Could trigger MAE catastrophe stops or skew MFE exit triggers
>
> Recommend: deploy with `FILL_DEPTH_ENABLED=0` first, observe that no regressions occur, then flip the gate next tick to compare.
>
> Approve restart?

- [ ] **Step 2: If user approves, restart**

```bash
kill 2800 2>/dev/null
sleep 2
FILL_DEPTH_ENABLED=1 FILL_SIZE_CONTRACTS=100 .venv/bin/python -m scripts.run_feeds > logs/run_feeds.log 2>&1 &
disown
sleep 5
pgrep -f "scripts.run_feeds"  # confirm new PID
```

- [ ] **Step 3: Verify depth-walk is firing in logs**

Run: `tail -50 logs/run_feeds.log | grep -E "PAPER OPEN|vwap_"`
Expected: any new entries show `vwap_poly=...(L<n>) vwap_kalshi=...(L<n>)` with `L>0` when depth was used.

- [ ] **Step 4: Compare marks against pre-deploy baseline**

Run: `.venv/bin/python -m scripts.paper pnl`
Expected: Realized unchanged (no new closes from restart alone). Open MTM may have shifted; document the delta.

- [ ] **Step 5: Document the deploy in HANDOFF.md**

Append to `HANDOFF.md` under "Pending TODOs & known issues" (or wherever appropriate), one short paragraph noting the depth-walking deploy date, the env flags, and the observed MTM delta.

- [ ] **Step 6: Commit only the HANDOFF.md change**

```bash
git add HANDOFF.md
git commit -m "docs: log depth-walking deploy in handoff"
```

---

## Self-Review Notes (author)

- Spec coverage: every paper-realism item #1, #5 from the design conversation is covered (depth-walking on both entry and exit). Items #2-#4, #6-#9 are explicitly deferred to subsequent plans.
- No placeholders: every code step has full code; no `TODO`, no `similar to above`.
- Type consistency: `entry_fill_vwap_poly` named consistently across `arb.py` (`fill_vwap_poly`) → `paper.py` (passed as `entry_fill_vwap_poly`) → `sqlite.py` (column `entry_fill_vwap_poly`).
- Function names: `walk_levels`, `get_levels`, `_compute_exit_legs` consistent.

---

## Handoff: Latency Improvements (next plan)

Once depth-walking is shipped, the next leverage point is **latency** — both the speed at which signals are generated and the speed at which fills happen. Open this as a separate plan; do not bundle with depth-walking (different testing surface, different risk profile).

### Scope for the next plan

1. **Event-driven arb detector** (`analytics/arb.py:164-211`)
   Currently the arb loop sleeps `interval=2.0` seconds between evaluations. That means up to 2 seconds of latency between an actionable quote arriving and the system noticing.
   - Refactor: instead of a wall-clock timer, the arb detector subscribes to a per-pair "dirty" signal that the WS feeds raise whenever they update a relevant asset's book.
   - Implementation sketch: add an `asyncio.Event` per `pair_name` in a shared dict; WS handlers `set()` the event after a book mutation that touches a tracked asset; the arb loop `await`s "any event set", then evaluates only the dirty pairs.
   - Expected impact: median signal latency 1000ms → 10ms.

2. **Per-pair latency telemetry**
   Add wall-clock measurement: timestamp at book-state mutation → timestamp at arb-candidate persist. Persist median/P95 per pair in a new `pair_latency` table. Surface in `server.py` dashboard.

3. **Pre-staged maker orders** (REAL MONEY ONLY — not for paper)
   Sit a resting limit on the cheap side just below the arb trigger. When the cross happens, you're queued ahead of takers. Requires a new module to manage live order state. Defer until paper + depth-walking has been verified to produce a clean edge signal.

4. **WebSocket order operations** (REAL MONEY ONLY)
   Both Polymarket and Kalshi have WS order endpoints. Move from REST to WS for the order path. ~50-200ms HTTP → ~5ms WS per leg.

5. **Co-location in us-east-1** (REAL MONEY ONLY — INFRA TASK, NOT CODE)
   Spin up a VPS in AWS us-east-1. Rsync code, configure systemd unit, mirror env vars. Reduces home-Bay-Area RTT (~70ms) to colocated (~1-3ms) — 140ms saved per arb round-trip. Pure infrastructure change; no code modifications beyond a `deploy/` directory.

### Recommended sequencing

```
Plan 2 (latency): #1 event-driven detector + #2 telemetry  ← code, paper-safe
Plan 3 (real money): #3 + #4 + #5                          ← requires capital + infra
```

### Live system safety checklist for plan 2

- Event-driven detector changes the polling cadence — make sure the resolver task is still on its own schedule (it polls REST every N minutes, separate from arb).
- Don't remove the wall-clock fallback entirely; keep a `interval=30.0` heartbeat so if the event dispatch breaks, the loop still ticks occasionally.
- Telemetry table should auto-prune (e.g., keep last 7 days) or it will balloon the sqlite file.

### Files most relevant to next plan

- `analytics/arb.py` — the loop being refactored
- `feeds/polymarket_ws.py`, `feeds/kalshi_ws.py` — emit dirty signals on book mutation
- `scripts/run_feeds.py` — pass the shared event dict
- `store/sqlite.py` — new `pair_latency` table
- `server.py` — surface telemetry on dashboard
