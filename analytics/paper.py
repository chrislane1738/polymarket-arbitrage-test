"""Client-side paper trader.

When the arb detector fires a candidate with net_edge >= PAPER_MIN_ENTRY_EDGE
and no open paper position for that (pair, direction), we "fill" both legs at
their current best ask. The position is then marked-to-market on each detector
tick using the current best *bid* on each leg (i.e., the liquidation value if
you had to close both legs into the market right now).

PnL is reported two ways:
  - mark_pnl           : (poly_bid + kalshi_bid - entry_cost - fees) * size
                         What you'd net if you closed both legs RIGHT NOW.
  - held_to_expiry_pnl : (1.0 - entry_cost - fees) * size
                         What you net if you hold to resolution (one side
                         pays $1 by definition for a true binary).

There is intentionally no auto-exit. Paper positions are held until you
close them manually via `python -m scripts.paper close <id>` or never. This
makes the simulator honest about whether the original entry signal was good,
rather than getting credit for clever exit timing the model never had.
"""
from __future__ import annotations

import logging
import os
import time

# Entry-edge floor. History of this knob:
#   0.005 (original) — too loose: too many marginal entries got eaten by fees
#   0.012 (tried)    — too tight: 0 entries in 2 hours of live arbs because
#                       current market edges cluster at 0.005-0.011
#   0.008 (current)  — middle ground: filters obvious non-arbs without
#                       starving entries entirely. Tunable via env.
PAPER_MIN_ENTRY_EDGE = float(os.environ.get("PAPER_MIN_ENTRY_EDGE", "0.008"))
PAPER_SIZE_CONTRACTS = int(os.environ.get("PAPER_SIZE_CONTRACTS", "100"))
PAPER_TRADING_ENABLED = os.environ.get("PAPER_TRADING", "1") not in ("0", "false", "False")

# TTE (time-to-expiry) entry filter. Skip entries whose earliest close_time
# is more than this many days away. Avoids tying up capital in 7-month BTC
# year-end markets when the goal is to actually realize PnL via resolution.
PAPER_MAX_TTE_DAYS = float(os.environ.get("PAPER_MAX_TTE_DAYS", "30"))

# Auto-exit: close positions whose MAE per contract drops below this threshold,
# but only after PAPER_AUTO_EXIT_MIN_AGE_S so we don't panic-close on the
# normal entry spread. Set the threshold to a positive number to disable.
#
# Empirical note (2026-05-25): the previous default of -0.02 fired on normal
# book drift, locking in losses on positions that would have realized profit
# at expiry. 12/12 closed positions in the first multi-hour run were exited
# at -$0.44 to -$4.85 each, despite the underlying arb being intact.
# Loosened to -0.10 so the rule fires only on genuinely catastrophic moves —
# the actual "resolution risk" failure mode it was designed for. The right
# long-term signal is mid-price *divergence* between venues, not spread
# expansion; that's a TODO.
PAPER_AUTO_EXIT_MAE_PER_CONTRACT = float(os.environ.get("PAPER_AUTO_EXIT_MAE", "-0.10"))
PAPER_AUTO_EXIT_MIN_AGE_S = float(os.environ.get("PAPER_AUTO_EXIT_MIN_AGE_S", "60"))

# After an auto-exit, refuse to re-enter the same (pair, direction) for this
# many seconds. Prevents a thrash loop when the arb persists but our exit rule
# keeps closing it. Defaults to the same window as min-age so the system gives
# the market real time to either reprice away or stabilize.
PAPER_REENTRY_COOLDOWN_S = float(os.environ.get("PAPER_REENTRY_COOLDOWN_S", "60"))

# MFE exit policies. Two tiers:
#
# Tier 1 — fixed-threshold realization: close when mark/c crosses this level.
#   Conservative profit-taking on positions that don't have outsized peaks.
#   Only fires when the position's historical MFE is below the trailing
#   activation threshold (so we don't pin small profits when the peak is
#   actually still climbing).
PAPER_MFE_EXIT_PER_CONTRACT = float(os.environ.get("PAPER_MFE_EXIT", "0.03"))
PAPER_MFE_EXIT_MIN_AGE_S = float(os.environ.get("PAPER_MFE_EXIT_MIN_AGE_S", "30"))
#
# Tier 2 — trailing-MFE realization: once MFE crosses the activation threshold,
#   switch to trailing logic — close when current mark falls below
#   PAPER_TRAILING_MFE_GIVEBACK × MFE peak. Lets large peaks run before banking.
#   Vegas id=20 example: MFE peaked at 12.68¢/c. With giveback=0.5, the
#   close trigger is 6.34¢/c — we capture roughly $6 instead of the $3 the
#   fixed rule would have grabbed at the first crossing of 3¢.
PAPER_TRAILING_MFE_ACTIVATION = float(os.environ.get("PAPER_TRAILING_MFE_ACTIVATION", "0.06"))
PAPER_TRAILING_MFE_GIVEBACK = float(os.environ.get("PAPER_TRAILING_MFE_GIVEBACK", "0.5"))

# Per-pair entry penalty: pairs with cumulative realized PnL below this
# threshold across PAPER_BLACKLIST_MIN_CLOSES closes require an entry edge
# multiplier of PAPER_BLACKLIST_EDGE_MULT to re-enter. Stops the system from
# repeatedly funding losing pairs (e.g., NBA Spurs at -$10 / 4 closes).
PAPER_BLACKLIST_LOSS_USD = float(os.environ.get("PAPER_BLACKLIST_LOSS_USD", "-5.0"))
PAPER_BLACKLIST_MIN_CLOSES = int(os.environ.get("PAPER_BLACKLIST_MIN_CLOSES", "3"))
PAPER_BLACKLIST_EDGE_MULT = float(os.environ.get("PAPER_BLACKLIST_EDGE_MULT", "2.0"))

# When FILL_REQUIRE_FULL=1, refuse to open if either leg's depth-walked
# fill quantity is below the requested size. Use this in real-money mode
# to avoid naked single-leg exposure on illiquid books.
# Read at import time as a default; maybe_enter() re-reads per-call for tests.
PAPER_FILL_REQUIRE_FULL = os.environ.get("FILL_REQUIRE_FULL", "0") not in ("0", "false", "False", "")

log = logging.getLogger("paper")


class PaperTrader:
    def __init__(self, store, enabled: bool | None = None) -> None:
        self.store = store
        self.enabled = PAPER_TRADING_ENABLED if enabled is None else enabled
        self._open_keys: set[tuple[str, str]] = self._reload_open_keys()
        # (pair, direction) -> ts_ns of last auto-exit, for cooldown
        self._last_exit_ts: dict[tuple[str, str], int] = {}
        if self.enabled:
            log.info(
                "paper trader ON  | min_edge=%.4f size=%d contracts | %d open positions",
                PAPER_MIN_ENTRY_EDGE, PAPER_SIZE_CONTRACTS, len(self._open_keys),
            )
        else:
            log.info("paper trader OFF (set PAPER_TRADING=1 to enable)")

    def _reload_open_keys(self) -> set[tuple[str, str]]:
        return {
            (r["pair_name"], r["direction"])
            for r in self.store.list_open_paper_positions()
        }

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
        require_full = os.environ.get("FILL_REQUIRE_FULL", "0") not in ("0", "false", "False", "")
        if require_full and cand.get("partial_fill"):
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
        max_tte_days = float(os.environ.get("PAPER_MAX_TTE_DAYS", str(PAPER_MAX_TTE_DAYS)))
        if max_tte_days > 0:
            meta = self.store.get_pair_resolution(pq.pair_name)
            if meta:
                closes = [t for t in (meta.get("poly_close_ts_ns"), meta.get("kalshi_close_ts_ns")) if t]
                if closes:
                    tte_s = (min(closes) - time.time_ns()) / 1e9
                    if tte_s > max_tte_days * 86400:
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

    def _compute_exit_legs(self, pq, direction: str, size: float):
        """Return (poly_exit_price, kalshi_exit_price) for closing this position.

        When FILL_DEPTH_ENABLED and the relevant bid-side depth dict is on pq,
        return depth-walked VWAP for `size` contracts. Otherwise top-of-book bid.
        Returns (None, None) if either side has no quote.
        """
        from analytics.depth import walk_levels
        gate_on = os.environ.get("FILL_DEPTH_ENABLED", "0") not in ("0", "false", "False", "")

        def _resolve(top_bid, depth):
            if gate_on and depth:
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

    def _exit_leg_prices(self, pq, direction: str):
        """Return (poly_close, kalshi_close) bid prices to use for closing.

        Walks bid-side depth when available; falls back to top-of-book.
        """
        return self._compute_exit_legs(pq, direction, PAPER_SIZE_CONTRACTS)

    def _execute_exit(self, pos, poly_close, kalshi_close, reason: str, detail: str) -> None:
        result = self.store.close_paper_position(
            pos["id"], poly_close, kalshi_close, reason=reason
        )
        if not result:
            return
        d = pos["direction"]
        self._open_keys.discard((pos["pair_name"], d))
        self._last_exit_ts[(pos["pair_name"], d)] = time.time_ns()
        log.info(
            "PAPER EXIT  id=%d  %s [%s]  reason=%s  %s  closed pnl=%+.3f  (cooldown %ds)",
            pos["id"], pos["pair_name"], d, reason, detail, result["pnl"],
            int(PAPER_REENTRY_COOLDOWN_S),
        )

    def maybe_exit(self, pair_quotes) -> None:
        """Apply auto-exit policies to all open positions.

        Policies (first match wins, all share post-exit cooldown):
          1. MAE catastrophe stop: close when MAE/c < threshold.
          2. Trailing MFE: once MFE/c has crossed PAPER_TRAILING_MFE_ACTIVATION,
             close when mark/c falls below giveback × MFE/c. Catches outsized
             peaks that retrace.
          3. Fixed MFE: close when mark/c >= PAPER_MFE_EXIT_PER_CONTRACT, but
             only if trailing hasn't activated (so we don't pin small profits
             when the peak is still climbing).
        """
        if not self.enabled:
            return
        now_ns = time.time_ns()
        by_pair = {p.pair_name: p for p in pair_quotes}

        mae_threshold = PAPER_AUTO_EXIT_MAE_PER_CONTRACT
        mae_min_age_ns = int(PAPER_AUTO_EXIT_MIN_AGE_S * 1e9)
        mfe_threshold = PAPER_MFE_EXIT_PER_CONTRACT
        mfe_min_age_ns = int(PAPER_MFE_EXIT_MIN_AGE_S * 1e9)
        trailing_activation = PAPER_TRAILING_MFE_ACTIVATION
        trailing_giveback = PAPER_TRAILING_MFE_GIVEBACK

        for pos in self.store.list_open_paper_positions():
            pq = by_pair.get(pos["pair_name"])
            if not pq:
                continue
            poly_close, kalshi_close = self._exit_leg_prices(pq, pos["direction"])
            if poly_close is None or kalshi_close is None:
                continue
            age_ns = now_ns - pos["opened_ts_ns"]
            size = pos["size"] or 1
            mark = pos.get("mark_pnl")
            mae = pos.get("max_adverse_pnl")
            mfe = pos.get("max_favorable_pnl")

            # Policy 1: MAE catastrophe stop
            if (
                mae_threshold < 0
                and age_ns >= mae_min_age_ns
                and mae is not None
                and mae / size < mae_threshold
            ):
                self._execute_exit(
                    pos, poly_close, kalshi_close,
                    reason="auto_exit_mae",
                    detail=f"MAE/c={mae/size:+.4f}",
                )
                continue

            if mark is None or age_ns < mfe_min_age_ns:
                continue

            mark_per_c = mark / size
            mfe_per_c = (mfe / size) if mfe is not None else None
            trailing_active = (
                trailing_activation > 0
                and mfe_per_c is not None
                and mfe_per_c >= trailing_activation
            )

            # Policy 2: Trailing MFE.
            # CRITICAL GUARD: require mark > 0. Without this, the rule will
            # close a position at a loss when MFE was reached far in the past
            # and the mark has since reverted to negative — that locks in a
            # spread loss that hold-to-expiry would have avoided (the binary
            # still pays $1 at resolution). Learned the hard way on Vegas id=20:
            # MFE +12.68¢/c hours before this rule existed → mark −1.12¢/c at
            # rule deployment → rule fired and locked −$1.12 vs held-to-exp +$0.98.
            if trailing_active and mark_per_c > 0 and mark_per_c < trailing_giveback * mfe_per_c:
                self._execute_exit(
                    pos, poly_close, kalshi_close,
                    reason="mfe_trailing",
                    detail=f"mark/c={mark_per_c:+.4f} < {trailing_giveback:.2f}×MFE({mfe_per_c:+.4f})",
                )
                continue

            # Policy 3: Fixed MFE (only when trailing hasn't activated)
            if (
                not trailing_active
                and mfe_threshold > 0
                and mark_per_c >= mfe_threshold
            ):
                self._execute_exit(
                    pos, poly_close, kalshi_close,
                    reason="mfe_target_hit",
                    detail=f"mark/c={mark_per_c:+.4f}  (target≥{mfe_threshold:+.4f})",
                )
                continue

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
