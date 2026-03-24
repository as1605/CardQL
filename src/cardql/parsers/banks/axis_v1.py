"""
Axis Bank statement parser (v1). Columnar DD/MM/YYYY lines with Dr|Cr amounts.

Parses PDF text:
  DATE TRANSACTION DETAILS MERCHANT CATEGORY AMOUNT (Rs.)
  14/02/2025 ZOMATO RESTAURANTS 547.59 Dr
  17/02/2025 BBPS PAYMENT RECEIVED - ... 13.00 Cr
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Union

from ..schema import Statement, Transaction

_TWO_WORD_CATEGORIES = {
    ("DEPT", "STORES"),
    ("AUTO", "SERVICES"),
    ("HOME", "FURNISHING"),
    ("FOOD", "PRODUCTS"),
}


def _ddmmyyyy_to_iso(d: str) -> str:
    parts = d.split("/")
    if len(parts) != 3:
        return d
    try:
        day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
        return f"{year:04d}-{month:02d}-{day:02d}"
    except (ValueError, IndexError):
        return d


def _parse_transaction_line(
    line: str,
    bank: str = "Axis",
    card: str = "Card B",
) -> Optional[Transaction]:
    line = line.strip()
    date_match = re.match(r"^(\d{2}/\d{2}/\d{4})\s+(.+)$", line)
    if not date_match:
        return None
    date_str, rest = date_match.group(1), date_match.group(2)
    tokens = rest.split()
    if len(tokens) < 3 or tokens[-1] not in ("Dr", "Cr"):
        return None
    sign = tokens[-1]
    amount_str = tokens[-2].replace(",", "")
    try:
        amount_val = float(amount_str)
    except ValueError:
        return None
    if sign == "Cr":
        amount_val = -amount_val
    if len(tokens) >= 4 and (tokens[-4], tokens[-3]) in _TWO_WORD_CATEGORIES:
        category = f"{tokens[-4]} {tokens[-3]}"
        desc_tokens = tokens[:-4]
    else:
        category = tokens[-3]
        desc_tokens = tokens[:-3]
    description = " ".join(desc_tokens) if desc_tokens else ""
    return Transaction(
        date=_ddmmyyyy_to_iso(date_str),
        bank=bank,
        card=card,
        description=description,
        amount=amount_val,
        currency="INR",
        category=category,
        transaction_type="refund" if sign == "Cr" else "purchase",
        raw={"axis_line": line},
    )


def _extract_statement_period(text: str) -> tuple[Optional[str], Optional[str]]:
    m = re.search(r"(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})", text)
    if m:
        return _ddmmyyyy_to_iso(m.group(1)), _ddmmyyyy_to_iso(m.group(2))
    return None, None


def parse(
    text: str,
    source_pdf_path: Optional[Union[str, Path]] = None,
    bank: str = "Axis",
    card: str = "Card B",
) -> Statement:
    start, end = _extract_statement_period(text)
    transactions: list[Transaction] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or "End of Statement" in line or line.startswith("DATE TRANSACTION DETAILS"):
            continue
        if re.match(r"^\d{2}/\d{2}/\d{4}\s*-\s*\d{2}/\d{2}/\d{4}", line):
            continue
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
