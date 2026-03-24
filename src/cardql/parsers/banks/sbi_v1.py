"""
SBI Card statement parser (v1). D/C/M suffix amounts (card label from path when set).

Parses PDF text:
  Transaction Details Date Amount ( ` )
  18 May 25 NETFLIX MUMBAI MAH 199.00 D
  02 Jun 25 PAYMENT RECEIVED ... 5,744.34 C
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Union

from ..schema import Statement, Transaction

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _ddmonyy_to_iso(d: str) -> str:
    m = re.match(r"^(\d{1,2})\s+(\w{3})\s+(\d{2})$", d.strip(), re.IGNORECASE)
    if not m:
        return d
    try:
        day = int(m.group(1))
        mon = _MONTHS.get(m.group(2).lower())
        year = int(m.group(3))
        year = year + 2000 if year < 50 else year + 1900
        if mon is None:
            return d
        return f"{year:04d}-{mon:02d}-{day:02d}"
    except (ValueError, IndexError):
        return d


def _extract_statement_period(text: str) -> tuple[Optional[str], Optional[str]]:
    m = re.search(
        r"Statement Period\s*:\s*(\d{1,2})\s+(\w{3})\s+(\d{2})\s+to\s+(\d{1,2})\s+(\w{3})\s+(\d{2})",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None, None
    try:
        d1, mon1, y1 = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        d2, mon2, y2 = int(m.group(4)), m.group(5).lower(), int(m.group(6))
        y1 = y1 + 2000 if y1 < 50 else y1 + 1900
        y2 = y2 + 2000 if y2 < 50 else y2 + 1900
        m1, m2 = _MONTHS.get(mon1, 1), _MONTHS.get(mon2, 1)
        return f"{y1}-{m1:02d}-{d1:02d}", f"{y2}-{m2:02d}-{d2:02d}"
    except (ValueError, IndexError):
        return None, None


def _parse_transaction_line(
    line: str,
    bank: str = "SBI",
    card: str = "Card F",
) -> Optional[Transaction]:
    line = line.strip()
    m = re.match(r"^(\d{1,2}\s+\w{3}\s+\d{2})\s+(.+)$", line, re.IGNORECASE)
    if not m or len(m.group(2).split()) < 2:
        return None
    date_str, rest = m.group(1), m.group(2)
    tokens = rest.split()
    if tokens[-1] not in ("D", "C", "M"):
        return None
    sign = tokens[-1]
    amount_str = tokens[-2].replace(",", "")
    try:
        amount_val = float(amount_str)
    except ValueError:
        return None
    if sign == "C":
        amount_val = -amount_val
    description = " ".join(tokens[:-2]) if len(tokens) > 2 else ""
    return Transaction(
        date=_ddmonyy_to_iso(date_str),
        bank=bank,
        card=card,
        description=description,
        amount=amount_val,
        currency="INR",
        category=None,
        transaction_type="refund" if sign == "C" else "purchase",
        raw={"sbi_line": line},
    )


# SBI PDF text order is not always: header then rows. Payment credits often appear
# *above* "TRANSACTIONS FOR ...", and some debits appear *below* "SHOP & SMILE SUMMARY".
_TXN_LINE_RE = re.compile(
    r"^\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{2}\s+"
    r".+\s+[\d,]+\.\d+\s+[DCM]$",
    re.I,
)


def parse(
    text: str,
    source_pdf_path: Optional[Union[str, Path]] = None,
    bank: str = "SBI",
    card: str = "Card F",
) -> Statement:
    start, end = _extract_statement_period(text)
    transactions: list[Transaction] = []
    seen_lines: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or not _TXN_LINE_RE.match(line):
            continue
        if line in seen_lines:
            continue
        seen_lines.add(line)
        txn = _parse_transaction_line(line, bank=bank, card=card)
        if txn is not None:
            transactions.append(txn)
    transactions.sort(key=lambda t: (t.date, t.description))
    return Statement(
        statement_period_start=start,
        statement_period_end=end,
        source_pdf_path=str(source_pdf_path) if source_pdf_path else None,
        bank=bank,
        card=card,
        transactions=transactions,
    )
