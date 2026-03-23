"""Export sinks (CSV → SQLite, etc.)."""

from .sqlite import import_master_csv_to_sqlite

__all__ = ["import_master_csv_to_sqlite"]
