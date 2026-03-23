"""Ingest: IMAP fetch, PDF unlock, and text extraction."""

from . import pdf
from .imap import FetchResult, connect, fetch_pdfs, unlock_pdf

__all__ = ["FetchResult", "connect", "fetch_pdfs", "pdf", "unlock_pdf"]
