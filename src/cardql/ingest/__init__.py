"""Ingest: IMAP fetch, PDF unlock, and text extraction."""

from . import pdf
from .imap import FetchResult, connect, fetch_pdfs, unlock_pdf
from .normalize import SKIP_PDF_SUBSTRINGS, normalize_pdfs

__all__ = [
    "FetchResult",
    "SKIP_PDF_SUBSTRINGS",
    "connect",
    "fetch_pdfs",
    "normalize_pdfs",
    "pdf",
    "unlock_pdf",
]
