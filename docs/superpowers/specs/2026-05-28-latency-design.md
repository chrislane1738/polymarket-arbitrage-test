# Latency Reduction — Design Spec

**Date:** 2026-05-28
**Status:** Approved, ready for implementation planning
**Scope:** Event-driven arb detector + full per-pair latency telemetry. Paper-safe (no real-money execution).

---

## Problem

The arb detector (`analytics/arb.py:run()`) currently polls on a fixed 2-second timer. A book change can sit unnoticed for up to ~2s (avg ~1s) before the detector re-evaluates and a candidate is persisted / a paper trade fires. On prediction-market books that reprice in well under a second, this means the system is systematically late — both for signal capture and for the realism of the paper fills it records.

**Goal:** reduce signal latency from ~1000ms-avg to ~10-60ms, and measure it end-to-end per pair so the improvement is visible and slow pairs are findable.

**Non-goals (deferred):** pure-event-driven (zero debounce); real-money execution path (pre-staged maker orders, WS order ops, co-location). These are noted in "Future work" and are out of scope here.

---

## Architecture

Three cooperating pieces, plus a rollout gate.

### 1. `DirtySignal` (new module `analytics/signal.py`)

A single shared object owned by `run_feeds` and passed to both the WS feeds (producers) and the arb loop (consumer).

State:
- `_dirty: dict[str, int]` — maps `pair_name → first_dirty_ts_ns` (the timestamp of the *first* mark since the last drain). Using a dict (not a set) lets us compute latency from the first mark, not the most recent.
- `_event: asyncio.Event` — signals "at least one pair is dirty."

Interface:
- `mark(pair_name: str) -> None` — if `pair_name` not already in `_dirty`, record `time.time_ns()` as its first-dirty ts; always `_event.set()`. Idempotent within an eval cycle (repeated marks of the same pair keep the earliest ts).
- `drain() -> dict[str, int]` — return a copy of `_dirty`, then clear `_dirty` and `_event.clear()`. Returns the `{pair_name: first_dirty_ts_ns}` map so the consumer can compute latency.
- `mark_all(pair_names) -> None` — convenience for the heartbeat path; marks every pair (used by the fallback sweep and any global refresh).

Concurrency: single asyncio event loop, so no locks needed. `drain()` must snapshot-then-clear without an `await` in between (atomic w.r.t. the loop).

### 2. Asset → pair reverse index

WS handlers know an `asset_id` (Polymarket token) or `market_ticker` (Kalshi), not a `pair_name`. We build two dicts once at startup from the pairs config:
- `poly_token_to_pairs: dict[str, list[str]]`
- `kalshi_ticker_to_pairs: dict[str, list[str]]`

(Lists because, in principle, a token could appear in more than one configured pair.) Built in `scripts/run_feeds.py` alongside the existing book-state wiring and passed into each feed's `run()`.

WS handler change (both `polymarket_ws` and `kalshi_ws`): after a successful `book_state.apply(ev)` that returns a changed asset/ticker, look up the pair(s) and call `dirty.mark(pair)` for each. If the asset isn't in the index (untracked), do nothing.

### 3. Event-driven arb loop

`analytics/arb.py:run()` gains an optional `dirty: DirtySignal | None` and `latency_store` hook. When `dirty` is provided AND the `EVENT_DRIVEN` gate is on, the loop becomes:

```
while True:
    try:
        await asyncio.wait_for(dirty.event.wait(), timeout=HEARTBEAT_S)   # 30.0
    except TimeoutError:
        dirty_map = {p: None for p in all_pair_names}   # full safety-net sweep
    else:
        await asyncio.sleep(DEBOUNCE_S)                  # 0.05 — coalesce burst
        dirty_map = dirty.drain()
    pair_quotes = build_pair_quotes()                    # existing snapshot fn
    affected = [pq for pq in pair_quotes if pq.pair_name in dirty_map]
    paper.mark_all(affected); paper.maybe_exit(affected)
    for pq in affected:
        for cand in evaluate(pq, now_ns):
            ... persist + paper.maybe_enter (unchanged logic) ...
            record latency for pq.pair_name if dirty_map[pq.pair_name] is not None
```

When the gate is off, the loop keeps the existing 2-second polling behavior verbatim (both code paths preserved during rollout).

Key properties:
- **Event-driven**: wakes ~immediately on a book change.
- **Debounced**: a 50ms coalescing window caps eval rate under churn; a burst of N deltas on a pair collapses into one eval.
- **Heartbeat fallback (30s)**: if a dirty signal is ever missed, or a pair changes via a path that doesn't mark dirty (e.g., resolver-driven state), every pair is still evaluated at least every 30s. Worst case degrades to roughly today's cadence, never worse.

### 4. Telemetry

- **Measurement point:** `latency_ms = persist_time_ns − first_dirty_ts_ns` for each evaluated pair where `dirty_map[pair]` is not None (heartbeat-swept pairs have no meaningful "dirty" origin, so they're skipped to avoid polluting the metric).
- **Storage:** new table `pair_latency (id, ts_ns, pair_name, latency_ms)`, created via the existing idempotent-migration pattern in `store/sqlite.py`. Written through a new `Store.record_pair_latency(pair_name, latency_ms)`.
- **Pruning:** a dedicated lightweight async task in `run_feeds` deletes `pair_latency` rows older than 7 days, running once at startup and then hourly. Kept separate from the resolver so the two concerns stay independent. Bounded so the 2.x GB DB problem doesn't recur on this table.
- **Surfacing:**
  - `server.py`: new endpoint returning per-pair P50/P95 over a recent window (e.g., last 1h), computed in SQL.
  - `static/index.html`: a dashboard tile rendering the per-pair P50/P95 table.
  - A periodic log line (e.g., every heartbeat) summarizing P50/P95 across pairs.

### 5. Rollout gate

`EVENT_DRIVEN` env var (default `0` = off), same pattern as `FILL_DEPTH_ENABLED`. Deploy with it off (no behavior change), verify the dirty-signal wiring is firing (telemetry shows marks), then flip on. The 30s heartbeat is the safety net either way.

---

## Components & boundaries

| Unit | Responsibility | Depends on |
|------|----------------|------------|
| `analytics/signal.py` | `DirtySignal`: dirty-set + event + first-dirty timestamps | stdlib only (asyncio, time) |
| reverse index (in `run_feeds`) | token/ticker → pair_name(s) | pairs config |
| `feeds/*_ws.py` | mark dirty after book update | `DirtySignal`, reverse index |
| `analytics/arb.py:run()` | event-driven loop + latency recording | `DirtySignal`, `Store` |
| `store/sqlite.py` | `pair_latency` table + record + prune + P50/P95 query | sqlite |
| `server.py` + `index.html` | surface latency telemetry | `Store` |

Each is independently testable; the `DirtySignal` and reverse index are pure/near-pure and get direct unit tests.

---

## Testing

- **`DirtySignal`**: `mark` sets the event and records first-dirty ts; repeated marks keep earliest ts; `drain` returns the map and clears state; `mark_all` marks every pair.
- **Reverse index**: token→pairs and ticker→pairs lookups, including multi-pair and missing-key cases.
- **Latency computation**: given a known first-dirty ts and persist ts, `latency_ms` is correct; heartbeat-swept pairs (ts None) are skipped.
- **Store**: `pair_latency` table created (fresh + idempotent on existing DB); `record_pair_latency` persists; P50/P95 query returns expected percentiles on seeded data; prune deletes >7-day rows.
- **Loop logic**: the drain→filter→evaluate step tested via a single-iteration helper with a fake `DirtySignal` and in-memory store (no live sockets). Backward-compat: with the gate off, the loop still polls and produces identical candidates.

---

## Live-safety

- Core loop change is gated by `EVENT_DRIVEN` (default off) → zero behavior change until explicitly flipped, mirroring the depth-walking rollout.
- 30s heartbeat guarantees every pair evaluates regularly even if signal wiring has a gap.
- New `pair_latency` table is additive (idempotent migration); existing queries unaffected.
- The running process loads code in memory; file edits don't affect it until a restart (and restarts are gated/owner-approved).

---

## Future work (explicitly out of scope)

1. **Pure event-driven** (DEBOUNCE_S=0): lowest latency, risks CPU spikes under churn. Easy follow-on once telemetry shows real eval rates.
2. **Real-money execution path**: pre-staged maker orders, WS order operations, us-east-1 co-location. Separate spec; requires capital + infra and a different risk review.
