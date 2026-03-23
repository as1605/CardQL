"""
Natural-language Q&A over ``transactions.sqlite`` via LangChain + a local Ollama model.

**Two-phase pipeline (optimised for small 0.5-3 B models):**

1. *SQL generation* — ask the model for a single ``SELECT``, extract it
   robustly, auto-fix common mistakes (OR-precedence), validate, execute.
   Retry with error feedback up to ``max_iterations``.
2. *Answer synthesis* — feed the question + SQL evidence + SQL-computed
   aggregates to the LLM for a concise natural-language answer.

The LLM is never trusted with arithmetic — all totals come from SQLite.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from typing import Any, Callable, NamedTuple

from pydantic import BaseModel, Field

log = logging.getLogger("cardql.llm_query")


def _short_status_line(text: str, max_len: int = 110) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= max_len else t[: max_len - 1] + "…"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# SQL safety
# ---------------------------------------------------------------------------

_FORBIDDEN = (
    " ATTACH ", " PRAGMA ", " INSERT ", " UPDATE ", " DELETE ",
    " DROP ", " CREATE ", " ALTER ", " REPLACE ", " VACUUM ", " DETACH ",
)


def validate_select_sql(raw: str) -> tuple[bool, str]:
    """Allow a single SQLite ``SELECT`` only.  Returns ``(ok, sql_or_error)``."""
    s = (raw or "").strip()
    if not s:
        return False, "Empty SQL"
    if s.endswith(";"):
        s = s[:-1].strip()
    if ";" in s:
        return False, "Only one SQL statement allowed (no ; in the middle)"
    if not re.match(r"(?is)\s*select\b", s):
        return False, "Only SELECT queries are allowed"
    padded = f" {s.upper()} "
    for bad in _FORBIDDEN:
        if bad in padded:
            return False, f"Forbidden keyword in query: {bad.strip()}"
    return True, s


# ---------------------------------------------------------------------------
# SQL post-processing: fix OR-precedence
# ---------------------------------------------------------------------------


def _fix_or_precedence(sql: str) -> str:
    """Wrap ``… LIKE … OR … LIKE …`` in parentheses when followed by ``AND``.

    Small models generate::

        WHERE LOWER(tags) LIKE '%x%' OR LOWER(description) LIKE '%x%' AND date >= '...'

    which applies the date filter only to the second branch (AND binds tighter).
    This function rewrites it to::

        WHERE (LOWER(tags) LIKE '%x%' OR LOWER(description) LIKE '%x%') AND date >= '...'

    Already-parenthesised groups are left untouched.
    """
    upper = sql.upper()
    where_pos = upper.find(" WHERE ")
    if where_pos < 0:
        return sql

    after_where = where_pos + 7
    # End of the WHERE clause
    where_end = len(sql)
    for kw in (" GROUP ", " ORDER ", " LIMIT ", " HAVING "):
        idx = upper.find(kw, after_where)
        if 0 <= idx < where_end:
            where_end = idx

    where_clause = sql[after_where:where_end]
    wc_upper = where_clause.upper()

    if " OR " not in wc_upper or " AND " not in wc_upper:
        return sql

    or_idx = wc_upper.find(" OR ")

    # Check paren depth at the OR — if > 0, it's already wrapped
    depth = 0
    for i in range(or_idx):
        if where_clause[i] == "(":
            depth += 1
        elif where_clause[i] == ")":
            depth -= 1
    if depth > 0:
        return sql

    # Find the first top-level AND after the OR
    search_from = or_idx + 4
    and_idx = -1
    d = 0
    while search_from < len(wc_upper) - 4:
        ch = where_clause[search_from]
        if ch == "(":
            d += 1
        elif ch == ")":
            d -= 1
        elif d == 0 and wc_upper[search_from : search_from + 5] == " AND ":
            and_idx = search_from
            break
        search_from += 1

    if and_idx < 0:
        return sql

    fixed = "(" + where_clause[:and_idx] + ")" + where_clause[and_idx:]
    return sql[:after_where] + fixed + sql[where_end:]


# ---------------------------------------------------------------------------
# DB context + execution
# ---------------------------------------------------------------------------


def _connect_readonly(db_path: str) -> sqlite3.Connection:
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class _BundleResult(NamedTuple):
    text: str
    today: str
    last_month_start: str
    last_month_end: str
    days_ago_365: str
    this_year_start: str
    last_year_start: str
    last_year_end: str


def collect_schema_bundle(db_path: str, sample_limit: int = 20) -> _BundleResult:
    """Stats + tag frequencies + calendar anchors + random sample rows."""
    conn = _connect_readonly(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        row = conn.execute(
            "SELECT MIN(date), MAX(date), COUNT(DISTINCT bank), COUNT(DISTINCT card) "
            "FROM transactions"
        ).fetchone()
        min_d, max_d, banks, cards = row[0], row[1], row[2], row[3]
        lim = max(1, min(int(sample_limit), 500))
        sample = conn.execute(
            "SELECT * FROM transactions ORDER BY RANDOM() LIMIT ?", (lim,)
        ).fetchall()
        cal = conn.execute(
            "SELECT date('now'),"                                       # 0 today
            "  date('now','start of month','-1 month'),"               # 1 lm_start
            "  date('now','start of month','-1 day'),"                 # 2 lm_end
            "  date('now','-365 days'),"                               # 3 365 days ago
            "  date('now','start of year'),"                           # 4 this year
            "  date('now','start of year','-1 year'),"                 # 5 last year start
            "  date('now','start of year','-1 day')"                   # 6 last year end
        ).fetchone()
        today = cal[0]
        lm_start, lm_end = cal[1], cal[2]
        d365 = cal[3]
        ty_start = cal[4]
        ly_start, ly_end = cal[5], cal[6]

        tag_rows = conn.execute(
            "SELECT tags, COUNT(*) AS cnt FROM transactions "
            "WHERE tags IS NOT NULL AND tags != '' "
            "GROUP BY tags ORDER BY cnt DESC LIMIT 40"
        ).fetchall()
        tag_lines = ", ".join(f"{r[0]}({r[1]})" for r in tag_rows)

        lines = [
            f"Rows: {n}  date range: {min_d} .. {max_d}  banks: {banks}  cards: {cards}",
            (
                f"Calendar: today={today}  "
                f"last_month={lm_start}..{lm_end}  "
                f"365_days_ago={d365}  "
                f"this_year_start={ty_start}  "
                f"last_year={ly_start}..{ly_end}"
            ),
        ]
        if tag_lines:
            lines.append(f"Tags (with count): {tag_lines}")
        lines += ["", "Sample rows:"]
        for r in sample:
            lines.append(json.dumps({k: r[k] for k in r.keys()}, ensure_ascii=False))

        return _BundleResult(
            text="\n".join(lines),
            today=today,
            last_month_start=lm_start,
            last_month_end=lm_end,
            days_ago_365=d365,
            this_year_start=ty_start,
            last_year_start=ly_start,
            last_year_end=ly_end,
        )
    finally:
        conn.close()


def execute_select(
    db_path: str, sql: str, max_rows: int = 500
) -> tuple[list[dict[str, Any]], str | None]:
    """Run validated SELECT; cap rows.  Returns ``(rows, error)``."""
    ok, sql_norm = validate_select_sql(sql)
    if not ok:
        return [], sql_norm
    conn = _connect_readonly(db_path)
    try:
        cur = conn.execute(sql_norm)
        rows = cur.fetchmany(max_rows + 1)
        if len(rows) > max_rows:
            rows = rows[:max_rows]
        return [{k: r[k] for k in r.keys()} for r in rows], None
    except sqlite3.Error as e:
        return [], str(e)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SQL-level auto-aggregate
# ---------------------------------------------------------------------------

_AGG_RE = re.compile(r"\b(SUM|COUNT|AVG|MIN|MAX)\s*\(", re.IGNORECASE)


def _has_aggregate(sql: str) -> bool:
    return bool(_AGG_RE.search(sql))


def _run_auto_aggregate(db_path: str, base_sql: str) -> str | None:
    """Run ``SUM / COUNT / MIN / MAX / AVG`` over *base_sql* via pure SQL.

    Skipped when *base_sql* already contains aggregate functions (wrapping
    ``SUM(amount)`` in another ``SUM`` would reference a wrong column name).
    """
    if _has_aggregate(base_sql):
        return None

    agg_sql = (
        "SELECT SUM(amount) AS total_amount, "
        "COUNT(*) AS transaction_count, "
        "MIN(amount) AS min_amount, "
        "MAX(amount) AS max_amount, "
        f"ROUND(AVG(amount), 2) AS avg_amount FROM ({base_sql})"
    )
    ok, agg_norm = validate_select_sql(agg_sql)
    if not ok:
        return None
    conn = _connect_readonly(db_path)
    try:
        row = conn.execute(agg_norm).fetchone()
        if row is None:
            return None
        total, cnt, mn, mx, avg = row[0], row[1], row[2], row[3], row[4]
        return (
            f"SQL-computed aggregates: "
            f"SUM(amount)={total}  COUNT={cnt}  "
            f"MIN(amount)={mn}  MAX(amount)={mx}  AVG(amount)={avg}"
        )
    except sqlite3.Error:
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Robust SQL extraction from LLM output
# ---------------------------------------------------------------------------

_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_CODE_FENCE = re.compile(r"```\w*\s*([\s\S]*?)```", re.IGNORECASE)
_THINK_WRAPPER = re.compile(
    r"(?:```\s*think\s*[\s\S]*?```|`think[\s\S]*?`)",
    re.IGNORECASE | re.DOTALL,
)


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract the first ``{…}`` JSON object from *text* (tolerates wrappers)."""
    t = (text or "").strip()
    t = _THINK_WRAPPER.sub("", t).strip()
    m = _JSON_FENCE.search(t)
    if m:
        t = m.group(1).strip()
    start = t.find("{")
    if start < 0:
        raise ValueError("No JSON object found in model output")
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                return _loads_json_lenient(t[start : i + 1])
    raise ValueError("Unbalanced JSON in model output")


def _loads_json_lenient(raw: str) -> dict[str, Any]:
    """``json.loads`` with repairs for trailing ``;`` / ``,`` and duplicate ``sql`` keys."""

    def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in pairs:
            if k == "sql" and v in (None, "") and "sql" in out:
                continue  # keep the non-empty one
            out[k] = v
        return out

    for candidate in (raw, re.sub(r";\s*}", "}", raw), re.sub(r",\s*}", "}", raw)):
        try:
            return json.loads(candidate, object_pairs_hook=_object_pairs)
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("Could not parse JSON even after repairs", raw, 0)


def _extract_sql_from_llm_response(text: str) -> str | None:
    """Robustly pull a ``SELECT`` out of whatever the model returned.

    Strategies (tried in order):
    1. Parse as JSON, read ``sql`` key.
    2. Find SQL inside a markdown code fence.
    3. Find bare ``SELECT …`` in the text.
    """
    t = (text or "").strip()
    if not t:
        return None
    t = _THINK_WRAPPER.sub("", t).strip()
    if not t:
        return None

    # Strategy 1: JSON with a "sql" key
    try:
        data = extract_json_object(t)
        for key in ("sql", "query", "SQL"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip().rstrip(";").strip() or None
    except (ValueError, json.JSONDecodeError):
        pass

    # Strategy 2: code fence
    m = _CODE_FENCE.search(t)
    if m:
        inner = m.group(1).strip().rstrip(";").strip()
        if re.match(r"(?i)\s*SELECT\b", inner):
            return inner

    # Strategy 3: bare SELECT
    match = re.search(r"(SELECT\b[\s\S]+?)(?:;|\Z)", t, re.IGNORECASE)
    if match:
        sql = match.group(1).strip()
        return sql or None

    return None


# ---------------------------------------------------------------------------
# LLM construction
# ---------------------------------------------------------------------------

_THINK_ENV = "CARDQL_OLLAMA_THINK"


def _ollama_reasoning_param() -> bool | None:
    v = os.environ.get(_THINK_ENV, "0").lower()
    return True if v in ("1", "true", "yes", "on") else False


def _message_text(msg: Any) -> str:
    """Normalise an ``AIMessage`` (or similar) to a plain string."""
    try:
        from langchain_core.messages import AIMessage
    except ImportError:
        return str(getattr(msg, "content", "") or "")

    if isinstance(msg, AIMessage):
        c = msg.content
        if isinstance(c, str) and c.strip():
            return c
        if isinstance(c, list):
            parts = [
                (b if isinstance(b, str) else b.get("text", ""))
                for b in c
                if isinstance(b, (str, dict))
            ]
            joined = "".join(parts)
            if joined.strip():
                return joined
        for key in ("reasoning_content", "thinking"):
            alt = (getattr(msg, "additional_kwargs", None) or {}).get(key)
            if isinstance(alt, str) and alt.strip():
                return alt
    return str(getattr(msg, "content", "") or "")


def _make_llm_sql(
    *,
    model: str | None = None,
    base_url: str | None = None,
):
    """LLM for SQL generation (JSON mode, low temperature)."""
    from langchain_ollama import ChatOllama

    use_json = os.environ.get("CARDQL_PLANNER_JSON_FORMAT", "1").lower() not in ("0", "false", "no")
    kw: dict[str, Any] = {
        "model": model or DEFAULT_OLLAMA_MODEL,
        "base_url": base_url or DEFAULT_OLLAMA_BASE_URL,
        "temperature": 0.1,
        "num_predict": 1024,
        "reasoning": _ollama_reasoning_param(),
    }
    if use_json:
        kw["format"] = "json"
    try:
        return ChatOllama(**kw)
    except TypeError:
        kw.pop("reasoning", None)
        return ChatOllama(**kw)


def _make_llm_answer(
    *,
    model: str | None = None,
    base_url: str | None = None,
):
    """LLM for natural-language answer (no JSON constraint)."""
    from langchain_ollama import ChatOllama

    kw: dict[str, Any] = {
        "model": model or DEFAULT_OLLAMA_MODEL,
        "base_url": base_url or DEFAULT_OLLAMA_BASE_URL,
        "temperature": 0.2,
        "num_predict": 2048,
        "reasoning": _ollama_reasoning_param(),
    }
    try:
        return ChatOllama(**kw)
    except TypeError:
        kw.pop("reasoning", None)
        return ChatOllama(**kw)


# ---------------------------------------------------------------------------
# Evidence formatting + answer synthesis
# ---------------------------------------------------------------------------


def _format_evidence(
    steps: list[QueryStep],
    aggregate_line: str | None = None,
) -> str:
    if not steps:
        return "(none)"
    parts: list[str] = []
    for s in steps:
        parts.append(f"--- Step {s.iteration} ---")
        parts.append(f"SQL: {s.sql}")
        if s.error:
            parts.append(f"Error: {s.error}")
        else:
            parts.append(f"Rows returned: {s.row_count}")
            if s.rows_json_truncated:
                parts.append(s.rows_json_truncated)
    if aggregate_line:
        parts.append("")
        parts.append(aggregate_line)
    return "\n".join(parts)


def _synthesize_answer(
    question: str,
    steps: list[QueryStep],
    bundle_text: str,
    aggregate_line: str | None = None,
    *,
    ollama_model: str | None = None,
    ollama_base_url: str | None = None,
) -> str:
    """Single LLM call: answer the question from SQL evidence."""
    from langchain_core.messages import SystemMessage
    from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate, PromptTemplate

    ev = _format_evidence(steps, aggregate_line=aggregate_line)
    system_msg = SystemMessage(
        content=(
            "Answer the user's question using ONLY the SQL results and "
            "SQL-computed aggregates below. Use the SUM/COUNT/MIN/MAX/AVG "
            "numbers exactly as given — do NOT recalculate or guess totals. "
            "Be concise."
        )
    )
    _human = PromptTemplate.from_template(
        "Database context:\n{bundle}\n\n"
        "Question: {question}\n\n"
        "SQL evidence:\n{ev}\n\n"
        "Answer:"
    )
    prompt = ChatPromptTemplate.from_messages(
        [system_msg, HumanMessagePromptTemplate(prompt=_human)]
    )
    chain = prompt | _make_llm_answer(model=ollama_model, base_url=ollama_base_url)
    return _message_text(
        chain.invoke({"bundle": bundle_text, "question": question, "ev": ev})
    ).strip()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _build_few_shot_examples(ctx: _BundleResult) -> str:
    """Build few-shot SQL examples using **actual** calendar dates.

    Small models copy examples literally — hardcoded dates would be used
    regardless of what the real calendar says.  By injecting the real dates
    from the DB's ``date('now', ...)`` the model just copies the right ones.
    """
    return (
        "Examples (use the dates from Calendar above, copy the WHERE pattern exactly):\n"
        "\n"
        "Q: How much on Zomato last month?\n"
        "SQL: SELECT SUM(amount) AS total FROM transactions "
        f"WHERE (LOWER(tags) LIKE '%zomato%' OR LOWER(description) LIKE '%zomato%') "
        f"AND date >= '{ctx.last_month_start}' AND date <= '{ctx.last_month_end}'\n"
        "\n"
        "Q: Show each Amazon transaction last month\n"
        "SQL: SELECT date, description, amount FROM transactions "
        f"WHERE (LOWER(tags) LIKE '%amazon%' OR LOWER(description) LIKE '%amazon%') "
        f"AND date >= '{ctx.last_month_start}' AND date <= '{ctx.last_month_end}' ORDER BY date\n"
        "\n"
        "Q: How much on Swiggy in the last 365 days?\n"
        "SQL: SELECT SUM(amount) AS total FROM transactions "
        f"WHERE (LOWER(tags) LIKE '%swiggy%' OR LOWER(description) LIKE '%swiggy%') "
        f"AND date >= '{ctx.days_ago_365}'\n"
        "\n"
        "Q: Total spend by category this year\n"
        "SQL: SELECT category, SUM(amount) AS total FROM transactions "
        f"WHERE date >= '{ctx.this_year_start}' "
        "GROUP BY category ORDER BY total DESC\n"
        "\n"
        "Rules:\n"
        "- Totals → SUM(amount), NEVER COUNT(*) or SELECT *\n"
        "- The OR for merchant MUST be inside (...) parentheses\n"
        "- Use the Calendar dates from the context for relative periods\n"
        "- Dates: ISO YYYY-MM-DD"
    )


def run_natural_language_query(
    question: str,
    db_path: str,
    *,
    sample_rows: int = 20,
    max_result_rows: int = 200,
    max_iterations: int | None = None,
    sql_only: bool = False,
    progress_callback: Callable[[str], None] | None = None,
    ollama_model: str | None = None,
    ollama_base_url: str | None = None,
) -> QueryResult:
    """
    Two-phase pipeline (optimised for small ≤3 B models):

    **Phase 1** — SQL generation + execution (up to *max_iterations* attempts).
    **Phase 2** — Answer synthesis from SQL evidence + auto-aggregates.

    *ollama_model* / *ollama_base_url* override :data:`DEFAULT_OLLAMA_MODEL` /
    :data:`DEFAULT_OLLAMA_BASE_URL` for both LLM calls.
    """
    from langchain_core.messages import SystemMessage
    from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate, PromptTemplate

    db_path = os.path.abspath(db_path)
    if not os.path.isfile(db_path):
        return QueryResult(error=f"Database not found: {db_path}")

    cap = max_iterations if max_iterations is not None else DEFAULT_MAX_ITERATIONS
    cap = max(1, min(int(cap), 20))

    ollama_m = ollama_model or DEFAULT_OLLAMA_MODEL
    ollama_u = ollama_base_url or DEFAULT_OLLAMA_BASE_URL

    def _prog(msg: str) -> None:
        line = _short_status_line(msg)
        log.info("%s", line)
        if progress_callback:
            progress_callback(line)

    # ── Context ──────────────────────────────────────────────────────────
    _prog("Loading schema + samples…")
    ctx = collect_schema_bundle(db_path, sample_limit=sample_rows)
    _prog(f"Context ready ({sample_rows} samples) · up to {cap} SQL attempt(s)")

    # ── Phase 1: SQL generation ──────────────────────────────────────────
    examples = _build_few_shot_examples(ctx)

    sql_system = (
        "You write SQLite SELECT queries. "
        'Respond with ONLY a JSON object: {"sql": "SELECT …"}\n\n'
        f"Table:\n{TRANSACTIONS_DDL}\n\n"
        f"{examples}"
    )

    _human_sql = PromptTemplate.from_template(
        "{bundle}\n\n"
        "Question: {question}\n"
        "{retry_context}\n"
        'Respond ONLY: {{"sql": "SELECT ..."}}'
    )

    sql_prompt = ChatPromptTemplate.from_messages([
        SystemMessage(content=sql_system),
        HumanMessagePromptTemplate(prompt=_human_sql),
    ])
    sql_chain = sql_prompt | _make_llm_sql(model=ollama_m, base_url=ollama_u)

    steps: list[QueryStep] = []
    last_rows: list[dict[str, Any]] = []
    last_sql: str | None = None
    last_raw: str | None = None
    agg_line: str | None = None

    for iteration in range(1, cap + 1):
        retry_context = ""
        if steps and steps[-1].error:
            s = steps[-1]
            retry_context = (
                f"Previous SQL failed:\n  SQL: {s.sql}\n  Error: {s.error}\n"
                "Write a corrected query."
            )

        _prog(f"[{iteration}/{cap}] Generating SQL…")
        try:
            msg = sql_chain.invoke({
                "bundle": ctx.text,
                "question": question,
                "retry_context": retry_context,
            })
            raw = _message_text(msg)
        except Exception as e:
            log.exception("SQL generation failed")
            _prog(f"[{iteration}/{cap}] LLM error")
            return QueryResult(error=f"LLM error: {e}", steps=steps)

        last_raw = raw
        _prog(f"[{iteration}/{cap}] LLM → {_short_status_line(raw, 88)}")

        # Extract → fix OR-precedence → validate
        sql = _extract_sql_from_llm_response(raw)
        if sql is None:
            _prog(f"[{iteration}/{cap}] No SQL found in response")
            steps.append(QueryStep(
                iteration=iteration,
                sql="(none extracted)",
                error="Could not extract a SELECT from model output",
            ))
            continue

        sql = _fix_or_precedence(sql)
        _prog(f"[{iteration}/{cap}] SQL: {_short_status_line(sql, 96)}")

        ok, sql_norm = validate_select_sql(sql)
        if not ok:
            _prog(f"[{iteration}/{cap}] Invalid: {sql_norm}")
            steps.append(QueryStep(iteration=iteration, sql=sql, error=f"Validation: {sql_norm}"))
            if sql_only:
                return QueryResult(error=f"Invalid SQL: {sql_norm}", planner_raw=raw, steps=steps)
            continue

        if sql_only:
            return QueryResult(
                sql_executed=sql_norm, planner_raw=raw, steps=steps, stopped_reason="sql_only"
            )

        _prog(f"[{iteration}/{cap}] Executing SQL…")
        rows, err = execute_select(db_path, sql_norm, max_rows=max_result_rows)
        last_sql = sql_norm
        last_rows = rows

        rows_json = json.dumps(rows, ensure_ascii=False, indent=2)
        if len(rows_json) > _MAX_ROWS_JSON_PER_STEP:
            rows_json = rows_json[:_MAX_ROWS_JSON_PER_STEP] + "\n... [truncated]"

        steps.append(QueryStep(
            iteration=iteration,
            sql=sql_norm,
            row_count=len(rows),
            error=err,
            rows_json_truncated=None if err else rows_json,
        ))

        if err:
            _prog(f"[{iteration}/{cap}] SQL error: {_short_status_line(err, 88)}")
            continue

        _prog(f"[{iteration}/{cap}] → {len(rows)} row(s)")

        # Auto-aggregate (only when model didn't already use SUM/COUNT/etc.)
        agg_line = _run_auto_aggregate(db_path, sql_norm)
        if agg_line:
            _prog(f"[{iteration}/{cap}] {agg_line}")

        break  # success → answer phase

    # ── Phase 2: Answer synthesis ────────────────────────────────────────
    if not any(s.error is None for s in steps):
        last_err = steps[-1].error if steps else "no SQL generated"
        return QueryResult(
            error=f"All {len(steps)} SQL attempt(s) failed. Last: {last_err}",
            planner_raw=last_raw,
            steps=steps,
        )

    try:
        _prog("Generating answer from SQL results…")
        answer = _synthesize_answer(
            question,
            steps,
            ctx.text,
            aggregate_line=agg_line,
            ollama_model=ollama_m,
            ollama_base_url=ollama_u,
        )
        _prog(f"Done: {_short_status_line(answer, 96)}")
    except Exception as e:
        log.exception("Answer generation failed")
        _prog(f"Answer error: {_short_status_line(str(e), 88)}")
        return QueryResult(
            error=f"Answer generation error: {e}",
            sql_executed=last_sql,
            rows=last_rows,
            steps=steps,
        )

    return QueryResult(
        answer=answer,
        sql_executed=last_sql,
        rows=last_rows,
        steps=steps,
        stopped_reason="answer",
    )


# ---------------------------------------------------------------------------
# Backward-compat aliases (used by tests / old code)
# ---------------------------------------------------------------------------

# Old planner types — kept so existing tests still import
from typing import Literal  # noqa: E402
from pydantic import ValidationError, model_validator  # noqa: E402, F811


class LoopTurnOutput(BaseModel):
    action: Literal["sql", "clarify", "answer"]
    sql: str | None = None
    clarification: str | None = None
    answer: str | None = None
    rationale: str | None = None

    @model_validator(mode="after")
    def clear_non_sql_fields_on_sql_action(self) -> LoopTurnOutput:
        if self.action == "sql":
            return self.model_copy(update={"answer": None, "clarification": None})
        return self


def parse_loop_turn(text: str) -> LoopTurnOutput:
    data = extract_json_object(text)
    return LoopTurnOutput.model_validate(data)


def parse_planner_output(text: str) -> LoopTurnOutput:
    return parse_loop_turn(text)
