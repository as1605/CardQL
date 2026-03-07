## Plan

Build a local-first system that fetches password-protected credit card statement PDFs from Gmail, stores them safely (gitignored), converts them into a **common normalized transaction schema**, and exports a master table + summaries for analysis.

### Goals (from `TASK.md`)
- **Secure mapping**:
  - map **bank/card → sender email + search pattern**
  - map **bank/card → PDF password pattern**
  - store these in **local config JSON** that is **not committed**
  - initialize via a **CLI tool**
- **Gmail fetcher**: search emails and download new statement PDFs.
- **Storage**: download PDFs neatly into a **gitignored** folder.
- **Normalization**: convert each PDF into **common JSON**.
- **Interface**: query/export a master table (date, bank, card, merchant, amount, etc.) + monthly summaries.
- **Modular/extensible** architecture so adding banks/cards is easy.

### Non-goals (v0)
- Perfect parsing for every bank statement (we’ll start with a plugin interface + a sample parser).
- Advanced interactive UI (CLI-first; later we can add a small web UI).

## Architecture

### Directories (local-first, gitignored)
- **`.local/`**: config and state
  - `.local/config/card_rules.json`: one entry per bank/card with `from_emails[]` and `passwords[]` (optional fallback: `email_rules.json` + `password_rules.json`)
  - `.local/config/secrets.json`: **`inboxes`** (each: `email` + `passwords` for IMAP)
  - `.local/config/app.json`: optional; only for `imap` overrides (host, folder, max_messages_per_rule)
  - `.local/state/imap_fetched.json`: per-UID sync state (reconciled with disk on each run)
- **`data/`**:
  - `data/raw-pdfs/<bank>/<card>/`: downloaded PDFs (e.g. `2025-01_statement.pdf`)
  - `data/normalized/`: normalized JSON (future)
  - `data/exports/`: CSV/XLSX outputs (future)

### Modules
- **`ccsa.config`**: load/validate config, password template resolution, IMAP settings.
- **`ccsa.imap`**:
  - IMAP connect, folder selection (All Mail preferred)
  - per-rule search (FROM / SUBJECT), UID-based skip (state + disk reconciliation)
  - parallel fetch (5 workers per rule), decrypt on save, state persist
- **`ccsa.pdf`** (future): decrypt, extract text/tables.
- **`ccsa.parsers`** (future): plugin per bank/card, common schema.
- **`ccsa.export`** (future): master table, CSV/XLSX.
- **`ccsa.analyze`** (future): monthly spend, rollups.

## Data model (normalized schema)

### `Transaction` (row-level)
- **required**:
  - `date`: ISO date (`YYYY-MM-DD`)
  - `bank`: string
  - `card`: string (or identifier)
  - `description`: merchant / narration
  - `amount`: number (positive for spend; refunds negative or separate `type`)
  - `currency`: string (default `INR`)
- **optional**:
  - `category`: inferred or provided by statement
  - `transaction_type`: `purchase` / `refund` / `fee` / `interest`
  - `reference`: ARN / RRN / txn id if available
  - `raw`: dict for parser-specific fields (kept for traceability)

### `Statement` (document-level)
- `statement_period_start`, `statement_period_end`
- `statement_date`
- `source_pdf_path`
- `transactions: Transaction[]`

## Configuration & security

### card_rules.json (local, not committed)
- One object per bank/card: **`bank`**, **`card`**, **`from_emails`** (list), **`passwords`** (list; first used for PDF decryption).
- Optional: `subject_contains`, `file_suffix`.
- Fallback: if `card_rules.json` is missing, ccsa can load `email_rules.json` + `password_rules.json`.

### secrets.json (local, not committed)
- **`inboxes`**: list of `{ email, passwords }` for IMAP (e.g. Gmail app password — see docs). PDF passwords are set per card in **card_rules.json**.

## CLI design (user workflows)

### Initialize
- `ccsa init`
  - create `.local/` and `data/` structure
  - create config templates with examples

### Fetch statements (IMAP)
- **`ccsa imap fetch`**
  - Reads **`card_rules.json`** (or fallback `email_rules.json` + `password_rules.json`); each card rule expands to one IMAP search per `from_email`
  - State in `.local/state/imap_fetched.json` keyed by **message UID**
  - On each run: reconcile state with disk (drop UIDs whose file is missing), then skip UIDs already in state
  - Fetch new messages with **5 parallel workers** per rule; decrypt and save PDFs under `data/raw-pdfs/<bank>/<card>/`
  - Safe to rerun; interrupted runs can be retried

### Normalize PDFs
- `ccsa pdf normalize`
  - for each raw PDF:
    - find matching password rule (bank/card)
    - decrypt if needed
    - select parser plugin by (bank/card + heuristics)
    - write normalized statement JSON to `data/normalized/`

### Export & analyze
- `ccsa export master --format csv|xlsx`
  - merges all normalized transactions into a single table
- `ccsa analyze monthly`
  - outputs monthly totals per bank/card

## Milestones (implementation order)

### Milestone 0: Project skeleton
- Python package + CLI entrypoint
- `.gitignore` includes `.local/` and `data/`

### Milestone 1: Local config initialization
- `ccsa init` creates `.local/` and `data/`; writes template **`card_rules.json`** and **`secrets.json`** when missing

### Milestone 2: IMAP fetch (done)
- IMAP connect (Gmail App Password or other provider)
- Search by FROM/SUBJECT per rule, UID-based skip
- State reconciled with data directory; parallel fetch (5 workers per rule)
- Decrypt on save; reunlock previously locked PDFs from state

### Milestone 3: PDF handling + plugin parser interface
- decrypt PDFs robustly
- define parser base class + registry
- implement at least one example parser (even if heuristic)

### Milestone 4: Export + summaries
- master table CSV/XLSX export
- monthly spend per card/bank

## Extensibility checklist (adding a new bank/card)
- Add an entry in **`card_rules.json`**: `bank`, `card`, `from_emails`, `passwords` (and optional `subject_contains`, `file_suffix`).
- Run **`ccsa imap fetch`**; PDFs appear under `data/raw-pdfs/<bank>/<card>/`.
- (Future) Implement a parser under `ccsa.parsers` for normalize/export.