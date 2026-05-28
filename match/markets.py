from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class MarketPair:
    name: str
    polymarket_yes_token: str | None = None
    polymarket_no_token: str | None = None
    kalshi_yes_ticker: str | None = None  # the YES outcome ticker on Kalshi
    # Convenience flat list for subscribe calls
    polymarket_token_ids: list[str] = field(default_factory=list)
    kalshi_tickers: list[str] = field(default_factory=list)


def _clean(v) -> str | None:
    if not v:
        return None
    s = str(v).strip()
    if not s or "REPLACE" in s.upper():
        return None
    return s


def load_pairs(path: str | Path = "config/markets.yaml") -> list[MarketPair]:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    pairs: list[MarketPair] = []
    for entry in raw.get("pairs", []) or []:
        poly = entry.get("polymarket") or {}
        kalshi = entry.get("kalshi") or {}

        yes_tok = _clean(poly.get("yes_token_id"))
        no_tok = _clean(poly.get("no_token_id"))
        kalshi_yes = _clean(kalshi.get("ticker") or kalshi.get("yes_ticker"))

        token_ids: list[str] = [t for t in (yes_tok, no_tok) if t]
        for t in poly.get("token_ids", []) or []:
            c = _clean(t)
            if c and c not in token_ids:
                token_ids.append(c)

        tickers: list[str] = []
        if kalshi_yes:
            tickers.append(kalshi_yes)
        for t in kalshi.get("tickers", []) or []:
            c = _clean(t)
            if c and c not in tickers:
                tickers.append(c)

        pairs.append(
            MarketPair(
                name=entry.get("name", "unnamed"),
                polymarket_yes_token=yes_tok,
                polymarket_no_token=no_tok,
                kalshi_yes_ticker=kalshi_yes,
                polymarket_token_ids=token_ids,
                kalshi_tickers=tickers,
            )
        )
    return pairs
