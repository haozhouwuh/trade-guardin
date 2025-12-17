# src/trade_guardian/infra/tickers.py
from __future__ import annotations

from typing import List
import csv
import os
import re

_VALID = re.compile(r"^[A-Z0-9\.\-\_]+$")  # allow BRK.B, etc.

def load_tickers_csv(path: str) -> List[str]:
    """
    Load tickers from a csv where each line contains a symbol (or first column is symbol).
    Skips:
      - blank lines
      - comment lines starting with # or //
      - header-like tokens: SYMBOL, TICKER, SYM
      - invalid symbols
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"tickers.csv not found: {path}")

    out: List[str] = []
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            sym = (row[0] or "").strip().upper()
            if not sym:
                continue
            if sym.startswith("#") or sym.startswith("//"):
                continue
            if sym in {"SYMBOL", "TICKER", "SYM"}:
                continue
            if not _VALID.match(sym):
                continue
            out.append(sym)

    # de-dup while keeping order
    seen = set()
    uniq: List[str] = []
    for s in out:
        if s not in seen:
            uniq.append(s)
            seen.add(s)
    return uniq
