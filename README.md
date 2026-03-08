# Credit Card Statement Analyzer (ccsa)

Local-first CLI to fetch password-protected credit card statement PDFs from Gmail, normalize them into a common format, and export a master CSV. Re-runnable and self-healing: run once or on a schedule.

---

## Quick start

**One command** — setup, fetch, normalize, and export (uses existing config; skips work already done):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# Edit .local/config/secrets.json and .local/config/card_rules.json (see below)
.venv/bin/python -m ccsa
```

Or explicitly: `ccsa run` (same as above). Optional: `ccsa run --force` to re-normalize all PDFs; `ccsa run -o path/to/master.csv` to set the output path.

**Development (no package install):** the `run` script sets `PYTHONPATH=src` and uses `.venv` if present:

```bash
./run init
./run              # or: ./run run
./run imap fetch
./run pdf normalize
./run export master
```

**Global install:** `pip install -e .` then `ccsa`, `ccsa run`, `ccsa init`, etc.

**Logging:** Set `CCSA_LOG=DEBUG` for verbose logs (e.g. `CCSA_LOG=DEBUG ccsa run`).

---

## Commands

| Command | Description |
|---------|-------------|
| `ccsa` or `ccsa run` | **Full pipeline:** setup dirs/config, fetch PDFs, normalize to JSON, export master CSV. Re-runnable and self-healing. |
| `ccsa init` | Create `.local/` and `data/` and write config templates (no overwrite). |
| `ccsa imap fetch` | Fetch new statement PDFs into `data/raw-pdfs/` per `card_rules.json`. |
| `ccsa pdf parse <path>` | Parse one PDF to JSON (stdout or `-o file.json`). |
| `ccsa pdf normalize` | Parse all PDFs in `data/raw-pdfs/` to `data/normalized/` (skips existing; use `-f` to re-parse). |
| `ccsa check gaps` | Warn if any month is missing for a card between its first and last statement (`-s raw-pdfs` or `normalized`). |
| `ccsa export master` | Merge normalized JSONs to a single CSV (default: `data/exports/master.csv`). |

---

## IMAP setup

ccsa fetches statement PDFs via **IMAP** (search by sender + optional subject) and saves attachments under `data/raw-pdfs/<bank>/<card>/`. State is stored in `.local/state/imap_fetched.json`; if a PDF was deleted from disk, that message is re-fetched on the next run.

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

---

## Parsers and supported banks

Parsers live under `src/ccsa/parsers/banks/` (e.g. `axis_v1`, `hdfc_v1`, `hdfc_v2`). For each PDF, all variants for that bank are tried; the result with the **most transactions** is used. Supported banks: **Axis**, **HDFC**, **HSBC**, **ICICI**, **IndusInd**, **SBI**. See [docs/PDF_PARSING.md](docs/PDF_PARSING.md) for format details and adding new banks/variants.

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
- **`.local/config/`** — `secrets.json`, `card_rules.json`, optional `app.json`
- **`src/ccsa/`** — CLI, IMAP fetch, PDF extraction, parsers (see [PLAN.md](PLAN.md))
