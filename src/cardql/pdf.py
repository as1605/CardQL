"""
PDF load, decrypt, and text extraction.

Uses pikepdf for decryption (same as IMAP flow) and pypdf for text extraction.
"""

from __future__ import annotations

import io
from pathlib import Path

from pypdf import PdfReader


def extract_text_from_pdf(data: bytes) -> str:
    """
    Extract raw text from PDF bytes (assumes PDF is already decrypted).
    """
    reader = PdfReader(io.BytesIO(data))
    parts = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            parts.append(text)
    return "\n".join(parts)


def extract_text_from_path(pdf_path: Path, data: bytes | None = None) -> str:
    """
    Extract text from a PDF file. If `data` is provided, use it (e.g. after
    decryption); otherwise read from `pdf_path`.
    """
    if data is not None:
        return extract_text_from_pdf(data)
    raw = pdf_path.read_bytes()
    return extract_text_from_pdf(raw)
