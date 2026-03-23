# CardQL architecture

## Pipeline (high level)

```mermaid
flowchart LR
  subgraph ingest [Ingest]
    imap[IMAP fetch]
    pdfUnlock[PDF unlock]
  end
  subgraph parse [Parse]
    extract[Text extract]
    banks[Bank parsers]
  end
  subgraph export_layer [Export]
    csv[Master CSV]
    sqlite[SQLite]
  end
  subgraph query_layer [Query]
    ollama[Ollama]
    llm[NL to SQL plus answer]
    ui[Streamlit]
  end
  imap --> pdfUnlock
  pdfUnlock --> extract
  extract --> banks
  banks --> csv
  csv --> sqlite
  sqlite --> ollama
  ollama --> llm
  llm --> ui
```

## Bare `cardql` (no subcommand)

1. **Init** — `ensure_local_dirs`, `write_config_templates` (create-if-missing only).
2. **Fetch** — IMAP when `card_rules.json` has email rules (unless `--no-fetch`).
3. **Parse / export** — Normalize PDFs to JSON under `data/normalized/`, merge to **`data/exports/master.csv`**, build **`transactions.sqlite`**, optionally open CSV (unless `--no-open`).
4. **Ollama** — Ensure API + pull default model (unless `--skip-ollama`).
5. **UI** — Launch Streamlit (unless `--no-ui`).

## Data locations

- **`.local/config/`** — `secrets.json`, `card_rules.json`, optional `tags.json`, `app.json` (gitignored).
- **`data/raw-pdfs/<bank>/<card>/`** — PDFs.
- **`data/normalized/`** — Per-statement JSON.
- **`data/exports/master.csv`**, **`transactions.sqlite`** — Query surfaces for NL and `cardql sql`.

## LLM and privacy

Natural-language Q&A runs **only against local SQLite** via **Ollama**; prompts include schema and sample rows. Arithmetic and aggregates are enforced in **SQL**, not trusted from free-form model output. No third-party cloud API is required for parsing or querying.

## Package layout (source)

| Area | Path | Role |
|------|------|------|
| Ingest | `cardql/ingest/` | IMAP fetch, PDF unlock, text extraction |
| Parse | `cardql/parsers/` | Bank parsers + normalized schema |
| Export | `cardql/export/` | `master.csv` → SQLite |
| Query | `cardql/query/` | NL→SQL pipeline, Ollama helpers (`ollama_setup.py`) |
| UI | `cardql/ui/` | Streamlit |
| CLI | `cardql/cli/` | Typer entrypoint |

Top-level **`paths.py`** and **`config.py`** wire repo layout and settings.
