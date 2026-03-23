"""
HDFC Bank statement parser (v1). Diners Privilege / similar formats.

Parses PDF text:
  Date Transaction Description ... Amount (in Rs.)
  15/02/2025 12:02:24 PAYTM UTILITY NOIDA 20.00
  25/02/2025 TATA 1MG HEALTHCARE SOLNEW DELHI 12.73 Cr
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


def _parse_transaction_line(
    line: str,
    bank: str = "HDFC",
    card: str = "Diners Privilege",
) -> Optional[Transaction]:
    line = line.strip()
    m = re.match(r"^(\d{2}/\d{2}/\d{4})(?:\s+\d{1,2}:\d{2}:\d{2})?\s+(.+)$", line)
    if not m:
        return None
    date_str, rest = m.group(1), m.group(2)
    rest = rest.rstrip()
    is_credit = rest.endswith(" Cr") or rest.endswith("Cr")
    if is_credit:
        rest = rest[:-3].rstrip() if rest.endswith(" Cr") else rest[:-2].rstrip()
    amount_m = re.search(r"[\d,]+\.?\d*\s*$", rest)
    if not amount_m:
        return None
    amount_str = amount_m.group().replace(",", "").strip()
    try:
        amount_val = float(amount_str)
    except ValueError:
        return None
    if is_credit:
        amount_val = -amount_val
    description = rest[: amount_m.start()].strip()
    if not description or len(description) > 500:
        return None
    return Transaction(
        date=_ddmmyyyy_to_iso(date_str),
        bank=bank,
        card=card,
        description=description,
        amount=amount_val,
        currency="INR",
        category=None,
        transaction_type="refund" if is_credit else "purchase",
        raw={"hdfc_line": line},
    )


def parse(
    text: str,
    source_pdf_path: Optional[Union[str, Path]] = None,
    bank: str = "HDFC",
    card: str = "Diners Privilege",
) -> Statement:
    start, end = None, None
    m = re.search(r"Statement Date\s*:\s*(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
    if m:
        end = _ddmmyyyy_to_iso(m.group(1))
    transactions: list[Transaction] = []
    in_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if "Domestic Transactions" in line:
            in_section = True
            continue
        if "Reward Points Summary" in line or "International Transactions" in line:
            in_section = False
            continue
        if "Page " in line and " of " in line:
            in_section = True
            continue
        if not in_section or not line:
            continue
        if re.match(r"^\d{2}/\d{2}/\d{4}\s", line):
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
