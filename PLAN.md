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
- **`.local/`**: credentials + configs + state
  - `.local/config/app.json`: email rules + password rules + basic settings
  - `.local/config/secrets.json`: variables used to resolve password templates (e.g. DOB)
  - `.local/credentials/`: Gmail OAuth client JSON and cached token
  - `.local/state/`: incremental sync cursor (e.g. last seen Gmail message IDs)
- **`data/`**:
  - `data/raw-pdfs/`: downloaded PDFs, organized by bank/card/month
  - `data/normalized/`: normalized JSON (one JSON per statement, plus optional extracted transactions)
  - `data/exports/`: CSV/XLSX outputs

### Modules
- **`ccsa.config`**: load/validate config, template password resolution.
- **`ccsa.gmail`**:
  - Gmail OAuth flow
  - query builder per bank/card rule
  - download attachments (PDFs) + dedupe
- **`ccsa.pdf`**:
  - decrypt PDFs using resolved password
  - extract text/tables
- **`ccsa.parsers`** (plugin system):
  - one parser per bank/card statement format
  - outputs a common schema (below)
- **`ccsa.export`**:
  - build a master table (pandas)
  - export CSV/XLSX
- **`ccsa.analyze`**:
  - monthly spend per card/bank
  - category/merchant rollups (as available)

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

### `app.json` (local, not committed)
- **Email rules**: `bank`, optional `card`, `from_email`, optional `subject_contains`, optional extra Gmail query terms.
- **Password rules**: `bank`, optional `card`, `password_template`.
  - Templates use `{variable}` placeholders resolved from `secrets.json`.
  - Example: `{dob_ddmmyyyy}` or `HDFC{pan_last4}`

### `secrets.json` (local, not committed)
- `variables`: key/value used for password templates.
- No passwords should be hardcoded into committed code.

## CLI design (user workflows)

### Initialize
- `ccsa init`
  - create `.local/` and `data/` structure
  - create config templates with examples

### Fetch statements from Gmail
- `ccsa gmail auth` (one-time): perform OAuth and store token under `.local/credentials/`
- `ccsa gmail fetch`
  - for each email rule:
    - run Gmail query (`from:... subject:... newer_than:...` etc.)
    - download PDF attachments
    - store into `data/raw-pdfs/<bank>/<card>/<YYYY-MM>/...pdf`
  - dedupe using Gmail message id + attachment id stored in `.local/state/`

### Normalize PDFs
- `ccsa pdf normalize`
  - for each raw PDF:
    - find matching password rule (bank/card)
    - resolve template using `secrets.json`
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
- `ccsa init` writes template `app.json` and `secrets.json`

### Milestone 2: Gmail integration
- OAuth client read from `.local/credentials/gmail_oauth_client.json`
- token caching in `.local/credentials/`
- download PDFs + maintain sync state

### Milestone 3: PDF handling + plugin parser interface
- decrypt PDFs robustly
- define parser base class + registry
- implement at least one example parser (even if heuristic)

### Milestone 4: Export + summaries
- master table CSV/XLSX export
- monthly spend per card/bank

## Extensibility checklist (adding a new bank/card)
- add an `email_rule` in `app.json`
- add a `password_rule` in `app.json`
- implement a new parser module under `ccsa.parsers`
- run fetch → normalize → export