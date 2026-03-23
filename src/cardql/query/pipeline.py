"""Natural-language Q&A over ``transactions.sqlite`` via LangChain + Ollama."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from typing import Any, Callable, NamedTuple

from .json_extract import extract_sql_from_llm_response
from .schema import (
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_OLLAMA_MODEL,
    QueryResult,
    QueryStep,
    TRANSACTIONS_DDL,
    _MAX_ROWS_JSON_PER_STEP,
)
from .sql_safety import fix_or_precedence, validate_select_sql

log = logging.getLogger("cardql.llm_query")


def _short_status_line(text: str, max_len: int = 110) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= max_len else t[: max_len - 1] + "…"


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
        sql = extract_sql_from_llm_response(raw)
        if sql is None:
            _prog(f"[{iteration}/{cap}] No SQL found in response")
            steps.append(QueryStep(
                iteration=iteration,
                sql="(none extracted)",
                error="Could not extract a SELECT from model output",
            ))
            continue

        sql = fix_or_precedence(sql)
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


