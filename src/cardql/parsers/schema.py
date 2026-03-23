"""Normalized schema for statements and transactions (see docs/ARCHITECTURE.md)."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class Transaction(BaseModel):
    """One transaction row (normalized)."""

    date: str  # YYYY-MM-DD
    bank: str
    card: str
    description: str
    amount: float  # positive = spend, negative = credit/refund
    currency: str = "INR"
    category: Optional[str] = None
    transaction_type: Optional[str] = None  # purchase | refund | fee | interest
    reference: Optional[str] = None
    raw: Optional[dict] = None


class Statement(BaseModel):
    """One statement document with metadata and transactions."""

    statement_period_start: Optional[str] = None  # YYYY-MM-DD
    statement_period_end: Optional[str] = None
    statement_date: Optional[str] = None
    source_pdf_path: Optional[str] = None
    bank: str = ""
    card: str = ""
    transactions: list[Transaction] = Field(default_factory=list)
