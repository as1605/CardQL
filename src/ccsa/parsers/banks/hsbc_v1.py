"""
HSBC Bank statement parser (v1). TravelOne / similar formats.

Parses PDF text with DDMMM dates (e.g. 13FEB) and amounts; handles concatenated lines.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Union

from ..schema import Statement, Transaction

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _ddmmm_to_iso(date_str: str, year: int) -> str:
    m = re.match(r"^(\d{2})([A-Z]{3})$", date_str.upper())
    if not m:
        return date_str
    day, mon = m.group(1), m.group(2)
    month = _MONTHS.get(mon)
    if month is None:
        return date_str
    return f"{year:04d}-{month:02d}-{int(day):02d}"


def _extract_year_from_period(text: str) -> Optional[int]:
    m = re.search(r"\b(20\d{2})\s+To\s+\d{1,2}\s+[A-Z]{3}\s+(20\d{2})", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(20\d{2})\b", text)
    return int(m.group(1)) if m else None


def parse(
    text: str,
    source_pdf_path: Optional[Union[str, Path]] = None,
    bank: str = "HSBC",
    card: str = "TravelOne",
) -> Statement:
    year = _extract_year_from_period(text) or 2026
    start, end = None, None
    m = re.search(r"(\d{1,2})\s+([A-Z]{3})\s+(20\d{2})\s+To\s+(\d{1,2})\s+([A-Z]{3})\s+(20\d{2})", text, re.IGNORECASE)
    if m:
        d1, mon1, y1, d2, mon2, y2 = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)
        if mon1.upper() in _MONTHS and mon2.upper() in _MONTHS:
            start = f"{y1}-{_MONTHS[mon1.upper()]:02d}-{int(d1):02d}"
            end = f"{y2}-{_MONTHS[mon2.upper()]:02d}-{int(d2):02d}"

    transactions: list[Transaction] = []
    month_pattern = "|".join(_MONTHS.keys())
    pattern = re.compile(r"(\d{2})(" + month_pattern + r")", re.IGNORECASE)
    stop_markers = [
        "TOTAL PURCHASE OUTSTANDING",
        "TOTAL CASH OUTSTANDING",
        "NET OUTSTANDING BALANCE",
        "PURCHASES & INSTALLMENTS",
        "Interest Rate applicable",
    ]
    pos = 0
    start_marker = re.search(r"(?:OPENING BALANCE|PURCHASES\s*&\s*INSTALLMENTS)", text, re.IGNORECASE)
    if start_marker:
        pos = start_marker.end()
    it = pattern.finditer(text, pos)
    for match in it:
        date_str = (match.group(1) + match.group(2).upper())[:5]
        if len(date_str) != 5 or date_str[2:5] not in _MONTHS:
            continue
        segment_start = match.start()
        segment_end = match.end()
        next_match = pattern.search(text, segment_end + 1)
        segment_end = next_match.start() if next_match else len(text)
        for stop in stop_markers:
            idx = text.find(stop, segment_start)
            if 0 <= idx < segment_end:
                segment_end = idx
        rest = text[segment_start:segment_end]
        after_date = rest[5:].strip()
        amount_matches = list(re.finditer(r"(?=(\d{1,2},\d{3}\.\d{2}))", after_date))
        if amount_matches:
            amount_matches = [re.search(re.escape(m.group(1)), after_date) for m in amount_matches]
            amount_matches = [m for m in amount_matches if m]
        if not amount_matches:
            amount_matches = list(re.finditer(r"[\d,]+\.\d{2}\b", after_date))
        if not amount_matches:
            continue

        def amount_value(m):
            try:
                return float(m.group().replace(",", ""))
            except (ValueError, AttributeError):
                return 0.0

        values = [(amount_value(m), m) for m in amount_matches if 1 <= amount_value(m) <= 500_000]
        if not values:
            continue
        _, best_match = min(values, key=lambda x: x[0])
        amount_str = best_match.group().replace(",", "")
        try:
            amount_val = float(amount_str)
        except ValueError:
            continue
        if amount_val <= 0:
            continue
        desc = after_date[: best_match.start()].strip()
        if not desc or len(desc) > 500:
            continue
        transactions.append(
            Transaction(
                date=_ddmmm_to_iso(date_str, year),
                bank=bank,
                card=card,
                description=desc,
                amount=amount_val,
                currency="INR",
                category=None,
                transaction_type="purchase",
                raw={"hsbc_segment": rest[:80]},
            )
        )
    return Statement(
        statement_period_start=start,
        statement_period_end=end,
        source_pdf_path=str(source_pdf_path) if source_pdf_path else None,
        bank=bank,
        card=card,
        transactions=transactions,
    )
