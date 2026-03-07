# Credit Card Statement Analyzer (ccsa)

Local-first CLI to fetch password-protected credit card statement PDFs from Gmail, normalize them into a common format, and export a master table (CSV/XLSX) plus summaries.

---

## Quick start

**Development (no package install):** use a venv, install only dependencies, run from source:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run init
./run imap fetch
```

The `run` script sets `PYTHONPATH=src` and uses `.venv` if present—no `pip install -e .` needed.

**Optional:** to get a global `ccsa` command: `pip install -e .` then `ccsa init` / `ccsa imap fetch`.

**Logging:** Set `CCSA_LOG=DEBUG` for verbose logs (e.g. `CCSA_LOG=DEBUG ./run imap fetch`).

---

## IMAP setup

ccsa fetches statement PDFs via **IMAP** (search by sender + optional subject) and saves PDF attachments under `data/raw-pdfs/<bank>/<card>/`. Each rule uses **5 parallel workers** to fetch messages. State is stored in `.local/state/imap_fetched.json` and is **reconciled with the data directory** on every run: if a PDF was deleted from disk, that message is re-fetched next time.

### Gmail: use an App Password

If you use Gmail, the password in config should be an **App Password** (not your normal password). In config you only set **`email`** and **`passwords`**; see [docs/IMAP_SETUP.md](docs/IMAP_SETUP.md) for how to create an app password.

1. Enable **2‑Step Verification** on your Google account.
2. Generate an **App Password** for “Mail”.
3. Put credentials into `.local/config/secrets.json` under **`inboxes`**:
   - **`email`**: your address (e.g. `your.email@gmail.com`)
   - **`passwords`**: list with the app password (e.g. `["xxxx xxxx xxxx xxxx"]`). You can add multiple passwords per inbox; they are tried in order if login fails.

Docs:
- Google help: `https://support.google.com/accounts/answer/185833`

---

## Configuring banks and cards

Use **`.local/config/card_rules.json`** — one object per bank/card with:

- **`bank`**, **`card`** — identifiers (folder: `data/raw-pdfs/<bank>/<card>/`)
- **`from_emails`** — list of sender addresses to search (IMAP)
- **`passwords`** — list of PDF passwords (first one used for statement PDF decryption)
- Optional: **`to_emails`** (inbox/recipient filter), **`subject_contains`**, **`file_suffix`**

Optional **`app.json`** can hold an `imap` block to override server/folder.  
See **[docs/CONFIG.md](docs/CONFIG.md)** for details and examples.  
See **[docs/IMAP_SETUP.md](docs/IMAP_SETUP.md)** for Gmail IMAP (App Password) setup.

---

## Commands (planned / current)

| Command | Description |
|--------|-------------|
| `ccsa init` | Create `.local/` and `data/` and write config templates. |
| `ccsa imap fetch` | Fetch new statement PDFs into `data/raw-pdfs/` (per `card_rules.json`). |
| `ccsa pdf normalize` | Decrypt and normalize PDFs to JSON. |
| `ccsa export master --format csv\|xlsx` | Export master transaction table. |
| `ccsa analyze monthly` | Monthly spend per card/bank. |

---

## Security

- **`.local/`** and **`data/`** are in `.gitignore`. Do not commit:
  - **`card_rules.json`**, **`secrets.json`** (IMAP credentials, passwords, bank mappings)
  - Downloaded PDFs and exports

---

## Project layout

See [PLAN.md](PLAN.md) for architecture, data model, and milestones.
