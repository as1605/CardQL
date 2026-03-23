# Contributing to CardQL

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

This installs the **`cardql`** console script into your venv.

Alternatively, without editable install:

```bash
export PYTHONPATH=src
python -m cardql --help
```

Or use **`./run`** from the repo root (it sets `PYTHONPATH=src` and prefers `.venv/bin/python`).

## Layout

- **`src/cardql/cli/`** — Typer CLI: `main.py` (commands), `pipeline.py` (full stack + data build), `helpers.py`.
- **`src/cardql/ingest/`** — IMAP fetch (`imap.py`), PDF text extraction (`pdf.py`).
- **`src/cardql/export/`** — CSV → SQLite (`sqlite.py`).
- **`src/cardql/ui/`** — Streamlit app (`streamlit_app.py`).
- **`src/cardql/query/`** — LangChain + Ollama NL→SQL (`pipeline.py`, `schema.py`, `sql_safety.py`, `json_extract.py`, `legacy.py`, `ollama_setup.py`).
- **`src/cardql/parsers/banks/`** — Bank-specific PDF parsers; see [docs/PDF_PARSING.md](docs/PDF_PARSING.md) to add a bank.

## Config safety

**`cardql init`** and **`write_config_templates`** only create files that are missing; they do **not** overwrite existing `.local/config/*` files.
