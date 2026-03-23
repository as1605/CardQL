from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field

DEFAULT_OLLAMA_MODEL = os.environ.get("CARDQL_OLLAMA_MODEL", "qwen3.5:0.8b-q8_0")
DEFAULT_OLLAMA_BASE_URL = os.environ.get("CARDQL_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
DEFAULT_MAX_ITERATIONS = int(os.environ.get("CARDQL_QUERY_MAX_ITERATIONS", "5"))
_MAX_ROWS_JSON_PER_STEP = 60_000

TRANSACTIONS_DDL = """\
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
);
CREATE INDEX idx_transactions_date ON transactions(date);
CREATE INDEX idx_transactions_amount ON transactions(amount);"""


class QueryStep(BaseModel):
    """A single executed SQL step (for trace / CLI)."""

    iteration: int
    sql: str
    row_count: int = 0
    error: str | None = None
    rows_json_truncated: str | None = None


class QueryResult(BaseModel):
    """Final outcome for the CLI."""

    answer: str = ""
    sql_executed: str | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[QueryStep] = Field(default_factory=list)
    clarification: str | None = None
    planner_raw: str | None = None
    error: str | None = None
    stopped_reason: str | None = None
