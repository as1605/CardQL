"""Normalize raw PDFs under ``data/raw-pdfs`` to statement JSON under ``data/normalized``."""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import LoadedConfig, resolve_password
from ..parsers import get_parsers_for_bank, try_parse_with_bank
from ..paths import Paths
from . import pdf as pdf_module
from .imap import unlock_pdf

logger = logging.getLogger(__name__)

SKIP_PDF_SUBSTRINGS = ("terms", "conditions", "most-important", "tariff", "mitc")


def normalize_pdfs(
    paths: Paths,
    loaded: LoadedConfig,
    *,
    force_normalize: bool = False,
    single_pdf: Path | None = None,
) -> None:
    """Extract text, parse with bank parsers, write one JSON per PDF."""
    if single_pdf is not None:
        p = Path(single_pdf).resolve()
        if not p.exists():
            logger.error("File not found: %s", p)
            return
        try:
            rel = p.relative_to(paths.raw_pdfs_dir)
            parts = rel.parts
            if len(parts) >= 2:
                bank_slug, card_slug = parts[0], parts[1]
            else:
                bank_slug, card_slug = "", ""
        except ValueError:
            bank_slug, card_slug = "", ""
        if not bank_slug or not get_parsers_for_bank(bank_slug):
            logger.error("PDF must be under data/raw-pdfs/<bank>/<card>/")
            return
        out_file = paths.normalized_dir / bank_slug / card_slug / f"{p.stem}.json"
        if out_file.exists() and not force_normalize:
            logger.info("Already normalized: %s", out_file)
            return
        bank_name_str = bank_slug.title()
        card_name_str = card_slug.title()
        password = resolve_password(loaded, bank_name_str, card_name_str)
        raw = p.read_bytes()
        data, _ = unlock_pdf(raw, password)
        text = pdf_module.extract_text_from_pdf(data)
        statement = try_parse_with_bank(
            bank_slug,
            text,
            source_pdf_path=p,
            bank_display=bank_name_str,
            card_display=card_name_str,
        )
        if statement is None:
            logger.error("No parser succeeded for %s", p)
            return
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(statement.model_dump_json(indent=2), encoding="utf-8")
        logger.info("%s txns → %s", len(statement.transactions), out_file)
        return

    pdf_files = [
        p
        for p in paths.raw_pdfs_dir.rglob("*.pdf")
        if not any(s in p.stem.lower() for s in SKIP_PDF_SUBSTRINGS)
    ]
    to_process = []
    for p in pdf_files:
        try:
            rel = p.relative_to(paths.raw_pdfs_dir)
            if len(rel.parts) < 2:
                continue
            bank_slug, card_slug = rel.parts[0], rel.parts[1]
            if not get_parsers_for_bank(bank_slug):
                continue
            out_file = paths.normalized_dir / bank_slug / card_slug / f"{p.stem}.json"
            if out_file.exists() and not force_normalize:
                continue
            to_process.append((p, bank_slug, card_slug, out_file))
        except ValueError:
            continue

    if not to_process:
        logger.info("Normalize: nothing new to parse")
        return

    for p, bank_slug, card_slug, out_file in to_process:
        try:
            bank_name_str = bank_slug.title()
            card_name_str = card_slug.title()
            password = resolve_password(loaded, bank_name_str, card_name_str)
            raw = p.read_bytes()
            data, _ = unlock_pdf(raw, password)
            text = pdf_module.extract_text_from_pdf(data)
            statement = try_parse_with_bank(
                bank_slug,
                text,
                source_pdf_path=p,
                bank_display=bank_name_str,
                card_display=card_name_str,
            )
            if statement is None:
                logger.warning("Skip (no parser): %s", p.relative_to(paths.repo_root))
                continue
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(statement.model_dump_json(indent=2), encoding="utf-8")
            logger.info(
                "%s txns → %s",
                len(statement.transactions),
                out_file.relative_to(paths.repo_root),
            )
        except Exception as e:
            logger.warning("Normalize failed for %s: %s", p.name, e)
