"""
IndusInd Bank statement parser (v1). Standard statement layout / similar formats.

Parses PDF text:
  Date Transaction Details Merchant Category Reward Points Amount (in )
  17/01/2026 DINING OUTLET GURGAON IN RESTAURANTS 21 1,043.00 DR
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Union

from ..schema import Statement, Transaction


def _ddmmyyyy_to_iso(d: str) -> str:
    parts = d.split("/")
    if len(parts) != 3:
        return d
    try:
        day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
        return f"{year:04d}-{month:02d}-{day:02d}"
    except (ValueError, IndexError):
        return d


def _extract_statement_period(text: str) -> tuple[Optional[str], Optional[str]]:
    m = re.search(r"(\d{2}/\d{2}/\d{4})\s+To\s+(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
    if m:
        return _ddmmyyyy_to_iso(m.group(1)), _ddmmyyyy_to_iso(m.group(2))
    return None, None


def _parse_transaction_line(
    line: str,
    bank: str = "IndusInd",
    card: str = "Card C",
) -> Optional[Transaction]:
    line = line.strip()
    m = re.match(r"^(\d{2}/\d{2}/\d{4})\s+(.+)$", line)
    if not m or len(m.group(2).split()) < 3:
        return None
    date_str, rest = m.group(1), m.group(2)
    tokens = rest.split()
    if tokens[-1].upper() not in ("DR", "CR"):
        return None
    sign = tokens[-1].upper()
    amount_str = tokens[-2].replace(",", "")
    try:
        amount_val = float(amount_str)
    except ValueError:
        return None
    if sign == "CR":
        amount_val = -amount_val
    category = tokens[-3] if len(tokens) >= 3 else None
    description = " ".join(tokens[:-3]) if tokens[:-3] else ""
    return Transaction(
        date=_ddmmyyyy_to_iso(date_str),
        bank=bank,
        card=card,
        description=description,
        amount=amount_val,
        currency="INR",
        category=category,
        transaction_type="refund" if sign == "CR" else "purchase",
        raw={"indusind_line": line},
    )


def parse(
    text: str,
    source_pdf_path: Optional[Union[str, Path]] = None,
    bank: str = "IndusInd",
    card: str = "Card C",
) -> Statement:
    start, end = _extract_statement_period(text)
    transactions: list[Transaction] = []
    in_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if "Purchases & Cash Transactions" in line or "Date Transaction Details" in line:
            in_section = True
            continue
        if line.startswith("Total ") and re.search(r"\d+\s+[\d,]+\.\d+", line):
            in_section = False
            continue
        if not in_section or not line:
            continue
        if re.match(r"^\d{2}/\d{2}/\d{4}\s+.+\s+DR$", line) or re.match(r"^\d{2}/\d{2}/\d{4}\s+.+\s+CR$", line):
            txn = _parse_transaction_line(line, bank=bank, card=card)
            if txn is not None:
                transactions.append(txn)
    return Statement(
        statement_period_start=start,
        statement_period_end=end,
        source_pdf_path=str(source_pdf_path) if source_pdf_path else None,
        bank=bank,
        card=card,
        transactions=transactions,
    )
