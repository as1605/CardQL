# CardQL

**Chat with your credit card statements.**

Local-first CLI to fetch password-protected credit card statement PDFs from Gmail, normalize them into a common format, and export a master CSV. Re-runnable and self-healing: run once or on a schedule.

---

## Quick start

**Install** (from the repo root — no editable install needed):

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install .               # installs the `cardql` command
```

**One command** — setup, fetch, normalize, and export (uses existing config; skips work already done):

```bash
# Edit .local/config/secrets.json and .local/config/card_rules.json (see below)
cardql
```

Or explicitly: `cardql run` (same as above). Optional: `cardql run --force` to re-normalize all PDFs; `cardql run -o path/to/master.csv` to set the output path. After a successful export, **`--open` / `-O`** opens **`master.csv`** with your OS default app (e.g. Excel/Numbers) and then starts an interactive **`sqlite3`** session on **`transactions.sqlite`** in the **same** terminal (it prints the exact **SQL** and **`.headers` / `.mode`** it will run, then **`sqlite3`** executes that preview and stays interactive) (macOS: `open`; Linux: `xdg-open`; Windows: default handler). Works on **`cardql`**, **`cardql run`**, **`cardql export master`**, and **`cardql export sqlite`**.

**Without `pip install .`:** the `run` script sets `PYTHONPATH=src` and uses `.venv` if present (only `requirements.txt` needed):

```bash
./run init
./run              # or: ./run run
./run imap fetch
./run pdf normalize
./run export master
./run export sqlite   # master.csv → data/exports/transactions.sqlite
```

**NL chat / query** (`cardql query`, `cardql ui`): same `requirements.txt` already includes LangChain + Streamlit. You still need **Ollama** and a model (e.g. `qwen3.5:0.8b-q8_0`). Run **`cardql ollama setup`** once to pull weights if needed.

**Logging:** Set `CARDQL_LOG=DEBUG` for verbose logs (e.g. `CARDQL_LOG=DEBUG cardql run`). Output uses **Rich**: colored levels, compact timestamps, and styled IMAP fetch lines.

**Environment variables** for Ollama and query tuning use the **`CARDQL_`** prefix (for example `CARDQL_OLLAMA_MODEL`, `CARDQL_OLLAMA_BASE_URL`). Names from before the CardQL rename are no longer read.

---

## Commands

| Command | Description |
|---------|-------------|
| `cardql` or `cardql run` | **Full pipeline:** setup dirs/config, fetch PDFs, normalize to JSON, export master CSV. Re-runnable and self-healing. |
| `cardql init` | Create `.local/` and `data/` and write config templates (no overwrite). |
| `cardql imap fetch` | Fetch new statement PDFs into `data/raw-pdfs/` per `card_rules.json`. |
| `cardql pdf parse <path>` | Parse one PDF to JSON (stdout or `-o file.json`). |
| `cardql pdf normalize` | Parse all PDFs in `data/raw-pdfs/` to `data/normalized/` (skips existing; use `-f` to re-parse). |
| `cardql check gaps` | Warn if any month is missing for a card between its first and last statement (`-s raw-pdfs` or `normalized`). |
| `cardql export master` | Merge normalized JSONs to a single CSV (default: `data/exports/master.csv`). |
| `cardql export sqlite` | Load `master.csv` into SQLite `transactions.sqlite` (default: same folder as CSV). Also runs after `cardql run` / `export master`. Use **`--open` / `-O`** to open the CSV and drop into `sqlite3` in this shell. |
| `cardql query "…"` | NL Q&A over **`transactions.sqlite`** (**Ollama** + LangChain). **`--ensure-server`** (default): start `ollama serve` in background + `ollama pull` if needed. [docs/LLM_QUERY.md](docs/LLM_QUERY.md). |
| `cardql ui` | Launch **Streamlit chat UI** for NL querying over `transactions.sqlite` (uses same backend as `cardql query`). |
| `cardql ollama setup` | **E2E:** ensure Ollama API is up (background **`ollama serve`**) and download the default chat model (`ollama pull`). |

---

## IMAP setup

cardql fetches statement PDFs via **IMAP** (search by sender + optional subject) and saves attachments under `data/raw-pdfs/<bank>/<card>/`. State is stored in `.local/state/imap_fetched.json`; if a PDF was deleted from disk, that message is re-fetched on the next run.

**Gmail:** use an **App Password** (not your normal password). See [docs/IMAP_SETUP.md](docs/IMAP_SETUP.md).

1. Enable **2-Step Verification** on your Google account.
2. Generate an **App Password** for “Mail”.
3. Put credentials in `.local/config/secrets.json` under **`inboxes`**:
   - **`email`**: your address
   - **`passwords`**: list with the app password (e.g. `["xxxx xxxx xxxx xxxx"]`)

---

## Configuring banks and cards

Use **`.local/config/card_rules.json`** — one object per bank/card:

- **`bank`**, **`card`** — identifiers (folder: `data/raw-pdfs/<bank>/<card>/`)
- **`from_emails`** — list of sender addresses to search (IMAP)
- **`passwords`** — list of PDF passwords (first used for statement decryption)
- Optional: **`to_emails`**, **`subject_contains`**, **`file_suffix`**

Optional **`app.json`** can override IMAP server/folder. See [docs/CONFIG.md](docs/CONFIG.md) and [docs/IMAP_SETUP.md](docs/IMAP_SETUP.md).

**NL queries:** `cardql query` uses a **local-only** **Qwen3.5-0.8GB** (via Ollama; real transaction data in prompts is OK by design) — [docs/LLM_QUERY.md](docs/LLM_QUERY.md), [PLAN.md](PLAN.md) (Milestone 5).

---

## Parsers and supported banks

Parsers live under `src/cardql/parsers/banks/` (e.g. `axis_v1`, `hdfc_v1`, `hdfc_v2`). For each PDF, all variants for that bank are tried; the result with the **most transactions** is used. Supported banks: **Axis**, **HDFC**, **HSBC**, **ICICI**, **IndusInd**, **SBI**. See [docs/PDF_PARSING.md](docs/PDF_PARSING.md) for format details and adding new banks/variants.

---

## Security

- **`.local/`** and **`data/`** are in `.gitignore`. Do not commit:
  - **`card_rules.json`**, **`secrets.json`** (IMAP credentials, PDF passwords, bank mappings)
  - Downloaded PDFs and exports

---

## Project layout

- **`data/raw-pdfs/<bank>/<card>/`** — statement PDFs (from IMAP or manual)
- **`data/normalized/<bank>/<card>/`** — parsed JSON per statement
- **`data/exports/master.csv`** — merged transaction table
- **`data/exports/transactions.sqlite`** — same data as SQLite (rebuilt when exporting)
- **`.local/config/`** — `secrets.json`, `card_rules.json`, optional `app.json`
- **`src/cardql/`** — CLI, IMAP fetch, PDF extraction, parsers (see [PLAN.md](PLAN.md))
