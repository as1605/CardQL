"""Merge normalized statement JSON into master.csv."""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from ..config import CompiledTag, compute_tags
from ..parsers.schema import Statement, Transaction
from ..paths import Paths

logger = logging.getLogger(__name__)

MASTER_CSV_COLUMNS = [
    "date",
    "bank",
    "card",
    "description",
    "amount",
    "currency",
    "category",
    "transaction_type",
    "tags",
]


def merge_normalized_to_master_csv(paths: Paths, tags: list[CompiledTag]) -> Path | None:
    """
    Read all ``*.json`` under ``paths.normalized_dir``, merge transactions, write
    ``paths.exports_dir / master.csv`` with tag columns computed from ``tags``.
    """
    all_txns: list[Transaction] = []
    for jf in paths.normalized_dir.rglob("*.json"):
        try:
            st = Statement.model_validate_json(jf.read_text(encoding="utf-8"))
            for t in st.transactions:
                all_txns.append(t)
        except Exception as e:
            logger.warning("Skip JSON %s: %s", jf.name, e)

    if not all_txns:
        logger.warning("No transactions to export. Add PDFs or check card_rules.json.")
        return None

    all_txns.sort(key=lambda t: (t.date, t.bank, t.card))
    out_path = (paths.exports_dir / "master.csv").resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(MASTER_CSV_COLUMNS)
        for t in all_txns:
            tag_str = compute_tags(t.description, tags)
            writer.writerow(
                [
                    t.date,
                    t.bank,
                    t.card,
                    t.description,
                    t.amount,
                    t.currency,
                    t.category or "",
                    t.transaction_type or "",
                    tag_str,
                ]
            )
    logger.info("All %s transactions exported to %s", len(all_txns), out_path)
    return out_path
