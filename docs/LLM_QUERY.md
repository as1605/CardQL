# CardQL: natural language queries on `transactions` (local LLM)

Ask questions in plain English over **`data/exports/transactions.sqlite`** using a **small local model**. The project assumes **Qwen3.5-0.8GB** via **Ollama** on **localhost** by default — follow the upstream model card for the exact tag (e.g. `qwen3.5:0.8b-q8_0` on [ollama.com](https://ollama.com)).

## Implemented in code

- **Interface:** **`cardql ui`** (Streamlit chat UI) using the NL→SQL pipeline in **`cardql.query`**. Install: `pip install -r requirements.txt` then `pip install .`.
- **Stack:** `langchain-core` (prompts + `Runnable`) + `langchain-ollama` (`ChatOllama`).
- **Two-phase pipeline** (optimised for small 0.5–3 B models):
  1. **SQL generation** — ask the model for `{"sql": "SELECT …"}`. A robust extractor handles clean JSON, markdown code fences, bare `SELECT` in prose. On failure the error is fed back and the model retries (up to `CARDQL_QUERY_MAX_ITERATIONS`, default 5).
  2. **Answer synthesis** — feed question + SQL results to the LLM for a concise natural-language answer.
- **Safety:** **validate `SELECT` in Python**; read-only SQLite (`file:…?mode=ro`).
- **Prompt strategy:** DDL, column tips, and calendar hints go in the context bundle.
- **Progress:** `run_natural_language_query(..., progress_callback=fn)` emits one-line stages.

### End-to-end: Ollama server + model download

1. Install **[Ollama](https://ollama.com/download)**.
2. Python deps: `pip install -r requirements.txt` (includes LangChain + Streamlit).
3. **One-shot setup:**

   ```bash
   cardql ollama
   # optional: cardql ollama --model qwen3.5:0.8b-q8_0
   ```

   - Logs: **`.local/state/ollama_serve.log`**
   - PID: **`.local/state/ollama_serve_cardql.pid`**

4. **`cardql ui`** runs the chat UI; the sidebar can ensure Ollama and pull models.

cardql does **not** install the Ollama binary; it only runs `ollama serve` / `ollama pull` when the CLI is available.

### Qwen3: empty output / reasoning mode

Some **Qwen3** builds use **extended reasoning** that routes output away from `AIMessage.content`.

- **Default:** `ChatOllama(reasoning=False)` where supported.
- **Optional:** `CARDQL_OLLAMA_THINK=1` turns reasoning on (requires recent `langchain-ollama`).

### Architecture: why two phases, not an agentic loop

A 0.8B model cannot reliably follow a multi-action JSON schema with many keys. The two-phase approach keeps each LLM call focused on **one task**: write SQL, then answer from rows.

## CLI / UI

```bash
pip install -r requirements.txt
pip install .
ollama pull qwen3.5:0.8b-q8_0

cardql ui
```

- **`cardql sql`** opens **`sqlite3`** on **`transactions.sqlite`** for raw SQL.
- `CARDQL_QUERY_MAX_ITERATIONS` env var sets the default max SQL attempts.

## Dependencies

- **`requirements.txt`** — full stack (mirrors `pyproject.toml` `dependencies`)
- **Ollama** running locally with your model

## Safety checklist

- DB opened read-only (`?mode=ro`).
- Validator rejects non-`SELECT`, chained statements, `ATTACH`, `PRAGMA`, etc.
- All inference is **local** — full transaction data in prompts is fine by design.
