"""
ICICI Bank statement parser (v1). Serial-numbered transaction lines / similar formats.

Parses PDF text:
  Date SerNo. Transaction Details ... Amount (in`)
  15/01/2026 12702848045 REPOVIVE, INC. COLLEGE PARK US 18 940.19
  29/01/2026 12776056934 BBPS Payment received 0 1,160.54 CR
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
    m = re.search(
        r"Statement period\s*:\s*(\w+)\s+(\d{1,2}),\s*(\d{4})\s+to\s+(\w+)\s+(\d{1,2}),\s*(\d{4})",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None, None
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    }
    try:
        m1 = months.get(m.group(1).lower(), 1)
        d1, y1 = int(m.group(2)), int(m.group(3))
        m2 = months.get(m.group(4).lower(), 1)
        d2, y2 = int(m.group(5)), int(m.group(6))
        return f"{y1}-{m1:02d}-{d1:02d}", f"{y2}-{m2:02d}-{d2:02d}"
    except (ValueError, IndexError):
        return None, None


def _parse_transaction_line(
    line: str,
    bank: str = "ICICI",
    card: str = "Card D",
) -> Optional[Transaction]:
    line = line.strip()
    if not re.match(r"^\d{2}/\d{2}/\d{4}\s+\d+", line) or len(line.split()) < 4:
        return None
    tokens = line.split()
    date_str = tokens[0]
    if tokens[-1] == "CR":
        amount_str = tokens[-2].replace(",", "")
        desc_tokens = tokens[2:-2]
    else:
        amount_str = tokens[-1].replace(",", "")
        desc_tokens = tokens[2:-1]
    try:
        amount_val = float(amount_str)
    except ValueError:
        return None
    if tokens[-1] == "CR":
        amount_val = -amount_val
    return Transaction(
        date=_ddmmyyyy_to_iso(date_str),
        bank=bank,
        card=card,
        description=" ".join(desc_tokens) if desc_tokens else "",
        amount=amount_val,
        currency="INR",
        category=None,
        transaction_type="refund" if tokens[-1] == "CR" else "purchase",
        raw={"icici_line": line},
    )


def parse(
    text: str,
    source_pdf_path: Optional[Union[str, Path]] = None,
    bank: str = "ICICI",
    card: str = "Card D",
) -> Statement:
    start, end = _extract_statement_period(text)
    transactions: list[Transaction] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or "Statement period" in line or "Date SerNo" in line or "Amount (in" in line:
            continue
        if line.startswith("3747") or line.startswith("Credit Limit"):
            continue
        if re.match(r"^\d{2}/\d{2}/\d{4}\s+\d{10,}", line):
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
