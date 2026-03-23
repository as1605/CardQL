# CardQL: natural language queries on `transactions` (local LLM)

Ask questions in plain English over **`data/exports/transactions.sqlite`** using a **small local model**. The project assumes **Qwen3.5-0.8GB** via **Ollama** on **localhost** by default — follow the upstream model card for the exact tag (e.g. `qwen3.5:0.8b-q8_0` on [ollama.com](https://ollama.com)).

## Implemented in code

- **Interfaces:** `cardql query "your question"` (CLI) and `cardql ui` (Streamlit chat UI), both using the same backend. Install: `pip install -r requirements.txt` then `pip install .` (all Python deps are in `requirements.txt`; no optional extras).
- **Stack:** `langchain-core` (prompts + `Runnable`) + `langchain-ollama` (`ChatOllama`).
- **Two-phase pipeline** (optimised for small 0.5–3 B models):
  1. **SQL generation** — ask the model for `{"sql": "SELECT …"}`. A robust extractor (`_extract_sql_from_llm_response`) handles clean JSON, JSON with trailing semicolons, markdown code fences, bare `SELECT` in prose, and junk extra keys. On failure the error is fed back and the model retries (up to `--max-iterations`, default 5).
  2. **Answer synthesis** — feed question + SQL results to the LLM for a concise natural-language answer.
- **Safety:** **validate `SELECT` in Python**; read-only SQLite (`file:…?mode=ro`).
- **Flags:** `--db`, `--sql-only` (first SQL attempt, no execute/answer), `--sample-rows`, `--max-iterations` / `-n`, `--verbose` / `-v` (print each SQL step), **`--ensure-server` / `--no-ensure-server`** (below).
- **Prompt strategy:** System prompt is ~150 tokens. DDL, column tips (OR parenthesisation, SUM for totals, LIKE for merchants), and a calendar hint (`today`, `last_month` range) go in the context bundle.
- **Progress:** `run_natural_language_query(..., progress_callback=fn)` emits one-line stages. The CLI updates a Rich spinner; same lines go to **INFO** on logger `cardql.llm_query`.

### End-to-end: Ollama server + model download

1. Install **[Ollama](https://ollama.com/download)** (desktop app or CLI on PATH).
2. Python deps: `pip install -r requirements.txt` (includes LangChain + Streamlit).
3. **One-shot setup** (starts `ollama serve` in the background if nothing is listening, then `ollama pull` the model if missing):

   ```bash
   cardql ollama setup
   # optional: cardql ollama setup --model qwen3.5:0.8b-q8_0
   ```

   - Logs: **`.local/state/ollama_serve.log`**
   - PID: **`.local/state/ollama_serve_cardql.pid`**

4. **`cardql query`** and **`cardql ui`** run the same backend. `cardql query` has `--ensure-server`; `cardql ui` exposes the same behavior as a checkbox in the sidebar.

cardql does **not** install the Ollama binary; it only runs `ollama serve` / `ollama pull` when the CLI is available.

### Qwen3: empty output / reasoning mode

Some **Qwen3** builds in Ollama use **extended reasoning** that routes output away from `AIMessage.content`.

- **Default:** `ChatOllama(reasoning=False)` (Ollama `think: false`) — model emits normal text / JSON.
- **Optional:** `CARDQL_OLLAMA_THINK=1` turns reasoning on (requires recent `langchain-ollama`).

### Architecture: why two phases, not an agentic loop

A 0.8B model cannot reliably follow a multi-action JSON schema (`action: sql | clarify | answer` with 5 keys). It produces:
- Duplicate JSON keys (`"sql": "...", "sql": null`)
- Trailing semicolons in JSON (`"sql": "SELECT ...";`)
- Markdown tables / prose instead of JSON
- Missing `action` keys with hallucinated row data

The two-phase approach keeps each LLM call focused on **one task**:
1. "Write a SELECT query" → extract SQL robustly from whatever the model returns
2. "Answer from these rows" → plain text

### Neater / more efficient setups

| Approach | When to use | Notes |
|----------|-------------|--------|
| **Ollama Desktop** (macOS / Windows) | Default for laptops | Menu-bar app starts API automatically. Run `cardql ollama setup` once for weights. |
| **`--no-ensure-server`** | Desktop always running | Fastest invocations: no health/pull check. |
| **Docker** (`ollama/ollama`) | Reproducible / CI / Linux | One container exposes `:11434`; point `CARDQL_OLLAMA_BASE_URL` at it. |

## CLI

```bash
pip install -r requirements.txt
pip install .
ollama pull qwen3.5:0.8b-q8_0

cardql query "How much did I spend on Zomato last month?"
cardql query -n 8 -v "Compare spend by bank across quarters"
cardql query --sql-only "total by bank last month"
cardql ui
```

- `--db` defaults to `data/exports/transactions.sqlite`
- `--sample-rows` controls how many random rows feed the context (default 20)
- `CARDQL_QUERY_MAX_ITERATIONS` env var sets the default max SQL attempts

## Dependencies

- **`requirements.txt`** — full stack: core + `langchain-core`, `langchain-ollama`, `streamlit` (mirrors `pyproject.toml` `dependencies`)
- **Ollama** running locally with your model

## Safety checklist

- DB opened read-only (`?mode=ro`).
- Validator rejects non-`SELECT`, chained statements, `ATTACH`, `PRAGMA`, etc.
- All inference is **local** — full transaction data in prompts is fine by design.
