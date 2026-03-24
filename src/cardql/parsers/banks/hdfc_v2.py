"""
HDFC Bank statement parser (v2). Newer domestic-transactions table format.

Format: "Domestic Transactions" table with header
  DATE & TIME TRANSACTION DESCRIPTION REWARDS AMOUNT PI
Lines: 15/07/2025| 00:00 DESC  C 19.80 l   or  04/08/2025| 08:37 DESC +  C 16,099.00 l
Amount has "C " prefix; optional "+ " before C for credits.
Statement date: "Statement Date" then "15 Aug, 2025". Billing: "16 Jul, 2025 - 15 Aug, 2025".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Union

from ..schema import Statement, Transaction

_MONTH_NAMES = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def _ddmmyyyy_to_iso(d: str) -> str:
    parts = d.split("/")
    if len(parts) != 3:
        return d
    try:
        day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
        return f"{year:04d}-{month:02d}-{day:02d}"
    except (ValueError, IndexError):
        return d


def _dd_mon_yyyy_to_iso(s: str) -> Optional[str]:
    """Parse '15 Aug, 2025' -> '2025-08-15'."""
    m = re.match(r"(\d{1,2})\s+(\w{3}),\s*(\d{4})", s.strip())
    if not m:
        return None
    try:
        day = int(m.group(1))
        month_str = m.group(2)
        year = int(m.group(3))
        if month_str not in _MONTH_NAMES:
            return None
        month = _MONTH_NAMES.index(month_str) + 1
        return f"{year:04d}-{month:02d}-{day:02d}"
    except (ValueError, IndexError):
        return None


# Line shape: 15/07/2025| 00:00 DESCRIPTION ... C 19.80 l  (debit)
# Credits: ... +  C 16,099.00 l  (payment / refund — must match + C before amount)
# Do not use a single greedy ".+ ... ?C" — "CREDIT CARD ... +  C amount l" would
# otherwise match the last " C amount" only and drop the "+", mis-classifying as debit.
_V2_LINE_PREFIX = re.compile(r"^(\d{2}/\d{2}/\d{4})\|\s*\d{1,2}:\d{2}\s+(.+)$")
_V2_CREDIT_TAIL = re.compile(r"^(.+)\s+\+\s+C\s+([\d,]+\.?\d*)\s+l\s*$")
_V2_DEBIT_TAIL = re.compile(r"^(.+)\s+C\s+([\d,]+\.?\d*)\s+l\s*$")


def _parse_v2_line(
    line: str,
    bank: str = "HDFC",
    card: str = "Card A",
) -> Optional[Transaction]:
    line = line.strip()
    m0 = _V2_LINE_PREFIX.match(line)
    if not m0:
        return None
    date_str, rest = m0.group(1), m0.group(2)
    m = _V2_CREDIT_TAIL.match(rest)
    is_credit = True
    if not m:
        m = _V2_DEBIT_TAIL.match(rest)
        is_credit = False
    if not m:
        return None
    description, amount_str = m.group(1).strip(), m.group(2)
    if len(description) > 500:
        return None
    try:
        amount_val = float(amount_str.replace(",", ""))
    except ValueError:
        return None
    if is_credit:
        amount_val = -amount_val
    return Transaction(
        date=_ddmmyyyy_to_iso(date_str),
        bank=bank,
        card=card,
        description=description,
        amount=amount_val,
        currency="INR",
        category=None,
        transaction_type="refund" if is_credit else "purchase",
        raw={"hdfc_v2_line": line},
    )


def parse(
    text: str,
    source_pdf_path: Optional[Union[str, Path]] = None,
    bank: str = "HDFC",
    card: str = "Card A",
) -> Statement:
    start, end = None, None
    # Billing period line: "16 Jul, 2025 - 15 Aug, 2025" or "16 Aug, 2025 - 15 Sep, 2025"
    bp = re.search(
        r"(\d{1,2}\s+\w{3},\s*\d{4})\s*-\s*(\d{1,2}\s+\w{3},\s*\d{4})",
        text,
    )
    if bp:
        start = _dd_mon_yyyy_to_iso(bp.group(1).strip())
        end = _dd_mon_yyyy_to_iso(bp.group(2).strip())
    if not end:
        # Fallback: first "DD Mon, YYYY" in text
        for line in text.splitlines():
            d = _dd_mon_yyyy_to_iso(line)
            if d:
                end = d
                break

    transactions: list[Transaction] = []
    in_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if "Domestic Transactions" in line:
            in_section = True
            continue
        if "Reward Points" in line and "Summary" in line:
            in_section = False
            continue
        if "International Transactions" in line or "GST Summary" in line:
            in_section = False
            continue
        if "Page " in line and " of " in line:
            continue
        if not in_section or not line:
            continue
        txn = _parse_v2_line(line, bank=bank, card=card)
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
