"""
Parser registry: parsers named by bank_v1, bank_v2, ...

For each PDF we try all parser variants for that bank. The result with the most
transactions is used. A successful parse with 0 transactions is valid (e.g. card not used).
We only log a warning and return None when every parser raises (no variant could parse the format).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional

from .schema import Statement

from .banks import (
    parse_axis_v1,
    parse_hdfc_v1,
    parse_hdfc_v2,
    parse_hsbc_v1,
    parse_icici_v1,
    parse_indusind_v1,
    parse_sbi_v1,
)

logger = logging.getLogger(__name__)

ParserFunc = Callable[..., Statement]

# Bank slug -> list of (parser_name, parser_func). All variants are tried; best result (most txns) wins.
_BANK_PARSERS: dict[str, list[tuple[str, ParserFunc]]] = {
    "axis": [("axis_v1", parse_axis_v1)],
    "hdfc": [("hdfc_v1", parse_hdfc_v1), ("hdfc_v2", parse_hdfc_v2)],
    "hsbc": [("hsbc_v1", parse_hsbc_v1)],
    "icici": [("icici_v1", parse_icici_v1)],
    "indusind": [("indusind_v1", parse_indusind_v1)],
    "sbi": [("sbi_v1", parse_sbi_v1)],
}


def get_parsers_for_bank(bank_slug: str) -> List[tuple[str, ParserFunc]]:
    """Return list of (parser_name, parser_func) for this bank."""
    b = (bank_slug or "").strip().lower()
    return list(_BANK_PARSERS.get(b, []))


def try_parse_with_bank(
    bank_slug: str,
    text: str,
    source_pdf_path: Any = None,
    bank_display: str = "",
    card_display: str = "",
) -> Optional[Statement]:
    """
    Try every parser variant for this bank; return the Statement with the most transactions.
    If a parser raises, it is skipped. Successful parse with 0 transactions is valid (card not used).
    Only when every parser raises do we log a warning and return None.
    """
    pairs = get_parsers_for_bank(bank_slug)
    if not pairs:
        return None

    best_statement: Optional[Statement] = None
    best_count = -1
    results: list[tuple[str, int, Optional[Exception]]] = []  # (name, txn_count or -1 on error, error)

    for parser_name, parser in pairs:
        try:
            st = parser(
                text,
                source_pdf_path=source_pdf_path,
                bank=bank_display or bank_slug.title(),
                card=card_display or "",
            )
            n = len(st.transactions)
            results.append((parser_name, n, None))
            if n > best_count:
                best_count = n
                best_statement = st
        except Exception as e:
            results.append((parser_name, -1, e))
            continue

    if best_statement is not None:
        return best_statement

    source = getattr(source_pdf_path, "name", None) or source_pdf_path or "PDF"
    summary = ", ".join(f"{n}={c} txns" if c >= 0 else f"{n}=error" for n, c, _ in results)
    logger.warning(
        "No parser could parse %s (bank=%s). All variants failed. Tried: %s",
        source,
        bank_slug,
        summary,
    )
    return None


def get_parser(bank_slug: str, card_slug: str) -> Optional[ParserFunc]:
    """Return first parser func for this bank (for backward compat). Callers should prefer try_parse_with_bank."""
    pairs = get_parsers_for_bank(bank_slug)
    return pairs[0][1] if pairs else None


def list_parsers() -> list[tuple[str, str]]:
    """List (bank_slug, parser_name) e.g. ('hdfc', 'hdfc_v1'), ('hdfc', 'hdfc_v2')."""
    out = []
    for bank, name_func_list in _BANK_PARSERS.items():
        for name, _ in name_func_list:
            out.append((bank, name))
    return out
