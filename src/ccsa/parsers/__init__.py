"""Parsers per bank: named bank_v1, bank_v2, ... Try all variants for a bank until one succeeds."""

from .schema import Statement, Transaction
from .registry import get_parser, get_parsers_for_bank, list_parsers, try_parse_with_bank
from .banks import (
    parse_axis_v1,
    parse_hdfc_v1,
    parse_hdfc_v2,
    parse_hsbc_v1,
    parse_icici_v1,
    parse_indusind_v1,
    parse_sbi_v1,
)

__all__ = [
    "Statement",
    "Transaction",
    "get_parser",
    "get_parsers_for_bank",
    "list_parsers",
    "try_parse_with_bank",
    "parse_axis_v1",
    "parse_hdfc_v1",
    "parse_hsbc_v1",
    "parse_icici_v1",
    "parse_indusind_v1",
    "parse_sbi_v1",
]
