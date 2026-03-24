"""Load master.csv into SQLite (transactions.sqlite)."""

from __future__ import annotations

import csv
import logging
import re
import sqlite3
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _null_if_empty(s: str | None) -> str | None:
    if s is None:
        return None
    t = s.strip()
    return t if t else None


def _parse_date_ymd(s: str) -> str | None:
    """CSV / ISO calendar day → ``YYYY-MM-DD`` or None."""
    t = (s or "").strip()
    if not _ISO_DATE.match(t):
        return None
    try:
        return date.fromisoformat(t).isoformat()
    except ValueError:
        return None


def import_master_csv_to_sqlite(csv_path: Path, db_path: Path) -> int:
    """
    Replace ``transactions`` from master CSV. All columns optional except ``id``.
    ``date`` is ``YYYY-MM-DD`` (same shape as the CSV). Indexed: ``date``, ``amount``.
    """
    csv_path = Path(csv_path)
    db_path = Path(db_path)
    if not csv_path.is_file():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TABLE IF EXISTS transactions")
        conn.execute(
            """
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                bank TEXT,
                card TEXT,
                description TEXT,
                amount REAL,
                currency TEXT,
                category TEXT,
                transaction_type TEXT,
                tags TEXT
            )
            """
        )
        conn.execute("CREATE INDEX idx_transactions_date ON transactions(date)")
        conn.execute("CREATE INDEX idx_transactions_amount ON transactions(amount)")

        rows: list[tuple] = []
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                conn.commit()
                return 0
            for row in reader:
                d = _parse_date_ymd(row.get("date", ""))
                amt_raw = row.get("amount", "")
                try:
                    amount_val = float(amt_raw) if amt_raw not in ("", None) else None
                except ValueError:
                    amount_val = None
                rows.append(
                    (
                        d,
                        _null_if_empty(row.get("bank")),
                        _null_if_empty(row.get("card")),
                        _null_if_empty(row.get("description")),
                        amount_val,
                        _null_if_empty(row.get("currency")),
                        _null_if_empty(row.get("category")),
                        _null_if_empty(row.get("transaction_type")),
                        _null_if_empty(row.get("tags")),
                    )
                )
        conn.executemany(
            """
            INSERT INTO transactions (
                date, bank, card, description, amount, currency,
                category, transaction_type, tags
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def sync_master_csv_to_sqlite(csv_path: Path) -> None:
    """Rebuild ``transactions.sqlite`` beside ``csv_path`` from master CSV."""
    csv_path = Path(csv_path).resolve()
    db_path = csv_path.parent / "transactions.sqlite"
    try:
        n = import_master_csv_to_sqlite(csv_path, db_path)
        logger.info("SQLite: %s rows → %s", n, db_path)
    except FileNotFoundError as e:
        logger.warning("%s", e)
    except OSError as e:
        logger.warning("SQLite export failed: %s", e)
