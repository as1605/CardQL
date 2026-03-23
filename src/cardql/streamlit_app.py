from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import streamlit as st

from cardql.config import ensure_local_dirs
from cardql.llm_query import DEFAULT_OLLAMA_BASE_URL, DEFAULT_OLLAMA_MODEL, run_natural_language_query
from cardql.ollama_setup import (
    ensure_ollama_api_and_tags,
    model_in_tags_payload,
    normalize_base_url,
    pull_ollama_model_if_needed,
)
from cardql.paths import get_paths

APP_NAME = "CardQL"
APP_TAGLINE = "Chat with your credit card statements."

# Display label → Ollama library tag (see https://ollama.com/library )
OLLAMA_MODEL_CHOICES: list[tuple[str, str]] = [
    ("Qwen3.5-0.8B", "qwen3.5:0.8b-q8_0"),
    ("Qwen3.5-4B", "qwen3.5:4b-q8_0"),
    ("Qwen3-Coder-30B", "qwen3-coder:30b"),
]
OLLAMA_MODEL_LABELS = [c[0] for c in OLLAMA_MODEL_CHOICES]
OLLAMA_TAG_BY_LABEL = dict(OLLAMA_MODEL_CHOICES)

_LIVE_ATTEMPT_RE = re.compile(r"^\[(\d+)/(\d+)\]\s*(.*)$")
_ROW_COUNT_RE = re.compile(r"[→\->]\s*(\d+)\s*row", re.I)


def _init_state() -> None:
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "progress" not in st.session_state:
        st.session_state.progress = ""


def _default_db_path() -> str:
    paths = ensure_local_dirs(get_paths())
    return str((paths.exports_dir / "transactions.sqlite").resolve())


def _default_ollama_label() -> str:
    env = (os.environ.get("CARDQL_OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL or "").strip()
    tags = [t for _, t in OLLAMA_MODEL_CHOICES]
    if env in tags:
        return OLLAMA_MODEL_LABELS[tags.index(env)]
    for (label, tag) in OLLAMA_MODEL_CHOICES:
        root = tag.split(":", 1)[0]
        if env == tag or env.startswith(root + ":"):
            return label
    return OLLAMA_MODEL_LABELS[0]


def _render_reasoning_trace(
    *,
    steps: list[dict[str, Any]],
    sql: str | None,
    rows: list[dict[str, Any]],
    stopped: str | None,
    had_error: bool,
) -> None:
    """Step-style trace (collapsed by default), like reasoning in chat UIs."""
    n = 1
    if steps or sql or rows:
        st.markdown(
            f"{n}. **Context** — The model was given your table schema and sample rows from the database."
        )
        n += 1

    for s in steps:
        it = s.get("iteration", n)
        err = s.get("error")
        sq = (s.get("sql") or "").strip()
        if err:
            st.markdown(f"{n}. **Attempt {it}** — This query did not succeed.")
            st.caption(err)
            if sq and sq not in ("(none extracted)", "(none)"):
                with st.expander("SQL from this attempt", expanded=False):
                    st.code(sq, language="sql")
        else:
            rc = s.get("row_count", 0)
            st.markdown(f"{n}. **Attempt {it}** — Query ran successfully ({rc} row(s) returned).")
            if len(steps) > 1 and sq:
                with st.expander("SQL for this attempt", expanded=False):
                    st.code(sq, language="sql")
        n += 1

    if steps and had_error and not any(not s.get("error") for s in steps):
        st.markdown(f"{n}. **Outcome** — No successful query; see the error above.")
    elif stopped == "answer" and not had_error:
        st.markdown(f"{n}. **Summary** — The model turned the query result into the reply above.")
    elif stopped == "sql_only":
        st.markdown(f"{n}. **Stop** — SQL-only mode (no summary step).")

    if sql or rows:
        st.divider()

    if sql:
        with st.expander("Executed SQL", expanded=True):
            st.code(sql, language="sql")

    if rows:
        st.markdown("**What the database returned**")
        if len(rows) > 40:
            st.dataframe(rows, use_container_width=True, height=420)
        else:
            st.dataframe(rows, use_container_width=True)


def _render_message(msg: dict[str, Any]) -> None:
    role = msg.get("role", "assistant")
    with st.chat_message(role):
        if role == "user":
            st.markdown(msg.get("text", ""))
            return

        text = (msg.get("text") or "").strip()
        error = msg.get("error")
        steps = msg.get("steps") or []
        stopped = msg.get("stopped_reason")
        sql = msg.get("sql")
        rows = msg.get("rows") or []

        if error:
            st.error(error)

        has_work = bool(steps or sql or rows)
        skip_redundant = bool(error and text == "Query failed.")
        if text and not skip_redundant:
            with st.container(border=True):
                st.markdown("### Answer")
                st.markdown(text)
        elif has_work and not error:
            with st.container(border=True):
                st.markdown("### Answer")
                st.caption("No narrative reply was generated; open **Reasoning** for SQL and data.")

        if has_work:
            with st.expander("Reasoning", expanded=False):
                _render_reasoning_trace(
                    steps=steps,
                    sql=sql,
                    rows=rows,
                    stopped=stopped,
                    had_error=bool(error),
                )


def _sync_live_query_ui(
    line: str,
    *,
    ph_attempt: Any,
    ph_phase: Any,
    ph_rows: Any,
) -> None:
    line = line.strip()
    m = _LIVE_ATTEMPT_RE.match(line)
    rest = (m.group(3) or "").strip() if m else ""

    if m:
        ph_attempt.metric("Attempt", f"{m.group(1)} of {m.group(2)}")

    if "Loading schema" in line:
        ph_phase.markdown("**Context** · loading schema and sample rows")
    elif "Context ready" in line:
        ph_phase.markdown("**Context** · ready for SQL generation")
    elif m and rest.startswith("Generating SQL"):
        ph_phase.markdown("**SQL** · model is drafting a query")
    elif m and "LLM error" in rest:
        ph_phase.markdown("**SQL** · model request failed")
    elif m and rest.startswith("LLM →"):
        ph_phase.markdown("**SQL** · parsing model output")
    elif m and rest.startswith("No SQL found"):
        ph_phase.markdown("**SQL** · no SELECT found — will retry")
    elif m and rest.startswith("SQL:"):
        ph_phase.markdown("**SQL** · validating against safety rules")
    elif m and rest.startswith("Invalid:"):
        ph_phase.markdown("**SQL** · rejected — asking model to correct")
    elif m and rest.startswith("Executing SQL"):
        ph_phase.markdown("**Run** · executing on SQLite")
    elif "SQL error:" in line:
        ph_phase.markdown("**Run** · database reported an error")
    elif _ROW_COUNT_RE.search(line):
        ph_phase.markdown("**Run** · rows fetched")
        rm = _ROW_COUNT_RE.search(line)
        if rm:
            ph_rows.metric("Rows (latest)", int(rm.group(1)))
    elif m and rest and any(k in rest for k in ("SUM", "COUNT", "AVG", "MIN", "MAX", "ROUND(")):
        ph_phase.markdown("**Aggregate** · computing rollups")
    elif "Generating answer from SQL" in line:
        ph_phase.markdown("**Answer** · summarizing from the result set")
    elif line.startswith("Done:"):
        ph_phase.markdown("**Answer** · finished")
    elif "Answer error" in line:
        ph_phase.markdown("**Answer** · generation failed")
    elif m and rest:
        ph_phase.markdown("**In progress** · refining query")
    else:
        ph_phase.markdown("**In progress** · working")


def _ensure_ollama(ensure_server: bool, model_tag: str) -> None:
    if not ensure_server:
        return
    paths = ensure_local_dirs(get_paths())
    base = normalize_base_url(DEFAULT_OLLAMA_BASE_URL)
    tags, msgs, _ = ensure_ollama_api_and_tags(base, paths=paths, start_background=True)
    if not model_in_tags_payload(tags, model_tag):
        bar = st.progress(0.0)
        cap = st.empty()
        cap.caption(f"Downloading `{model_tag}`…")

        def _on_pull_progress(p: float) -> None:
            frac = min(1.0, max(0.0, float(p)))
            bar.progress(frac)
            cap.caption(f"Downloading `{model_tag}` — {min(100, int(round(frac * 100)))}%")

        msgs.extend(
            pull_ollama_model_if_needed(
                tags,
                model_tag,
                pull_if_missing=True,
                announce_pull=False,
                base_url=base,
                progress_callback=_on_pull_progress,
            )
        )
    boring = frozenset({"Ollama API is reachable."})
    display = [m for m in msgs if m not in boring]
    if not display:
        return
    with st.container(border=True):
        st.markdown("##### Ollama")
        for m in display:
            st.markdown(f"- {m}")


def main() -> None:
    st.set_page_config(page_title=APP_NAME, page_icon="💳", layout="wide")
    _init_state()

    st.title(APP_NAME)
    st.caption(APP_TAGLINE)

    ollama_base = normalize_base_url(DEFAULT_OLLAMA_BASE_URL)

    with st.sidebar:
        st.subheader("Settings")
        db_path = st.text_input("SQLite DB path", value=_default_db_path())
        sample_rows = st.slider("Sample rows in prompt", 1, 200, 20)
        max_iterations = st.slider("Max SQL attempts", 1, 20, 5)
        max_result_rows = st.slider("Max rows returned", 20, 1000, 200, step=20)
        ensure_server = st.checkbox("Ensure Ollama server/model", value=True)
        if "ollama_pick" not in st.session_state:
            st.session_state.ollama_pick = _default_ollama_label()
        st.selectbox(
            "Ollama model",
            OLLAMA_MODEL_LABELS,
            key="ollama_pick",
            help="If the tag is not installed locally, Ollama will download it and use it for the next reply.",
        )
        _tag = OLLAMA_TAG_BY_LABEL[st.session_state.ollama_pick]
        st.caption(f"Active tag: `{_tag}`")
        st.caption(f"URL: `{ollama_base}`")

    model_tag = OLLAMA_TAG_BY_LABEL[st.session_state.ollama_pick]
    if ensure_server:
        _ensure_ollama(True, model_tag)

    for m in st.session_state.messages:
        _render_message(m)

    prompt = st.chat_input("Ask about your transactions...")
    if not prompt:
        return

    st.session_state.messages.append({"role": "user", "text": prompt})
    _render_message(st.session_state.messages[-1])

    with st.chat_message("assistant"):
        with st.status("Thinking…", expanded=True) as status:
            live = st.container()
            with live:
                c1, c2, c3 = st.columns(3)
                ph_attempt = c1.empty()
                ph_phase = c2.empty()
                ph_rows = c3.empty()
                ph_attempt.metric("Attempt", "—")
                ph_phase.markdown("**Context** · getting ready…")
                ph_rows.metric("Rows (latest)", "—")
                est_steps = max(10, 4 + max_iterations * 8)
                bar = st.progress(0.0)
                tick = {"n": 0}

            def _on_progress(line: str) -> None:
                st.session_state.progress = line
                tick["n"] += 1
                bar.progress(min(0.98, tick["n"] / est_steps))
                _sync_live_query_ui(line, ph_attempt=ph_attempt, ph_phase=ph_phase, ph_rows=ph_rows)

            try:
                db = Path(db_path).expanduser().resolve()
                if not db.is_file():
                    raise RuntimeError(f"Database not found: {db}")

                result = run_natural_language_query(
                    prompt,
                    str(db),
                    sample_rows=sample_rows,
                    max_result_rows=max_result_rows,
                    max_iterations=max_iterations,
                    progress_callback=_on_progress,
                    ollama_model=model_tag,
                    ollama_base_url=ollama_base,
                )

                bar.progress(1.0)
                text = result.answer or result.clarification or ""
                if not text and result.error:
                    text = "Query failed."

                payload: dict[str, Any] = {
                    "role": "assistant",
                    "text": text,
                    "sql": result.sql_executed,
                    "rows": result.rows,
                    "error": result.error,
                    "steps": [s.model_dump() for s in result.steps],
                    "stopped_reason": result.stopped_reason,
                }
                st.session_state.messages.append(payload)
                if result.error:
                    status.update(label="Reasoning stopped (error)", state="error")
                else:
                    status.update(label="Reasoning complete", state="complete")
            except Exception as e:
                bar.progress(1.0)
                payload = {"role": "assistant", "text": "Request failed.", "error": str(e)}
                st.session_state.messages.append(payload)
                status.update(label="Request failed", state="error")

        _render_message(st.session_state.messages[-1])


if __name__ == "__main__":
    main()
